"""Aruba Device Tracker platform."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac

from .const import (
    ATTR_ACCESS_POINT,
    ATTR_CHANNEL,
    ATTR_ESSID,
    ATTR_IP_ADDRESS,
    ATTR_OS,
    ATTR_SIGNAL,
    ATTR_SPEED,
    CONF_TRACK_NEW,
    DEFAULT_TRACK_NEW,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ArubaIAPCoordinator

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up device tracker entities.

    Entities are created for every MAC currently online (from coordinator data)
    plus every MAC previously seen (from coordinator.last_seen storage).  This
    ensures offline devices get an entity immediately on startup rather than
    waiting for the next poll, which is what prevents the 'no longer provided'
    banner.

    Unique IDs use the bare MAC address — the same convention as HA's own
    nmap_tracker — so the entity platform can correctly match registry entries
    to entity objects across restarts.
    """
    coordinator: ArubaIAPCoordinator = entry.runtime_data
    tracked: set[str] = set()

    track_new: bool = entry.options.get(
        CONF_TRACK_NEW,
        entry.data.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
    )

    # Build a mac -> registry name lookup so offline devices keep their
    # friendly name (e.g. "iPad") rather than falling back to the raw MAC.
    # The entity registry stores the original_name set at creation time and
    # any user-customised name, keyed by unique_id (bare MAC for our entities).
    registry = er.async_get(hass)
    registry_names: dict[str, str] = {}
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "device_tracker":
            # unique_id is the bare MAC for our entities
            stored_name = reg_entry.name or reg_entry.original_name
            if stored_name:
                registry_names[reg_entry.unique_id] = stored_name

    # ------------------------------------------------------------------
    # Seed from coordinator.last_seen — every MAC ever seen by this
    # integration, including currently offline devices.  This is populated
    # from persistent storage before first_refresh runs, so it's always
    # available here.
    # ------------------------------------------------------------------
    startup_entities: list[ArubaClientEntity] = []

    for mac in coordinator.last_seen:
        if mac not in tracked:
            tracked.add(mac)
            client_data = (coordinator.data or {}).get(mac, {})
            # Prefer: live IAP name → registry stored name → bare MAC
            initial_name = client_data.get("name") or registry_names.get(mac) or mac
            startup_entities.append(
                ArubaClientEntity(
                    coordinator=coordinator,
                    entry=entry,
                    mac=mac,
                    initial_name=initial_name,
                    new_device_defaults_tracked=track_new,
                )
            )

    # Also catch any online devices not yet in last_seen (brand new devices
    # on this very first poll).
    for mac, client_data in (coordinator.data or {}).items():
        if mac not in tracked:
            tracked.add(mac)
            initial_name = client_data.get("name") or registry_names.get(mac) or mac
            startup_entities.append(
                ArubaClientEntity(
                    coordinator=coordinator,
                    entry=entry,
                    mac=mac,
                    initial_name=initial_name,
                    new_device_defaults_tracked=track_new,
                )
            )

    if startup_entities:
        async_add_entities(startup_entities)

    # ------------------------------------------------------------------
    # Discover new devices on subsequent coordinator polls.
    # ------------------------------------------------------------------
    @callback
    def _add_new_entities() -> None:
        if not coordinator.data:
            return
        new_entities: list[ArubaClientEntity] = []
        for mac, client_data in coordinator.data.items():
            if mac not in tracked:
                tracked.add(mac)
                new_entities.append(
                    ArubaClientEntity(
                        coordinator=coordinator,
                        entry=entry,
                        mac=mac,
                        initial_name=client_data.get("name") or mac,
                        new_device_defaults_tracked=track_new,
                    )
                )
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_add_new_entities)


class ArubaClientEntity(ScannerEntity):
    """Represents a single Wi-Fi client tracked via Aruba IAP."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ArubaIAPCoordinator,
        entry: ConfigEntry,
        mac: str,
        initial_name: str,
        new_device_defaults_tracked: bool,  # noqa: FBT001
    ) -> None:
        """Initialise the tracker entity."""
        super().__init__()
        self._coordinator = coordinator
        self._entry = entry
        self._mac = mac
        self._attr_name = initial_name
        # Unique ID uses format_mac-normalised MAC (lowercase colon-separated)
        # per HA unique ID requirements.
        self._attr_unique_id = format_mac(mac)
        self._new_device_defaults_tracked = new_device_defaults_tracked
        # Set initial connected state synchronously from coordinator data
        # (first_refresh has already completed before async_setup_entry runs).
        self._connected: bool = mac in (coordinator.data or {})

    async def async_added_to_hass(self) -> None:
        """Subscribe to coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update connected state from the latest coordinator data."""
        if self._coordinator.data is not None:
            self._connected = self._mac in self._coordinator.data
        self.async_write_ha_state()

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        """Return True if the device is currently seen by the IAP."""
        return self._connected

    @property
    def mac_address(self) -> str:
        """Return the MAC address of the device."""
        return self._mac

    @property
    def hostname(self) -> str | None:
        """Return the hostname reported by the IAP."""
        if self._coordinator.data is None:
            return None
        data = self._coordinator.data.get(self._mac)
        return data.get("name") if data else None

    @property
    def available(self) -> bool:
        """Always available — offline devices show as away, not unavailable."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes from the IAP."""
        if self._coordinator.data is None:
            return {}
        data = self._coordinator.data.get(self._mac)
        if not data:
            return {}
        return {
            ATTR_ACCESS_POINT: data.get("access_point"),
            ATTR_ESSID: data.get("essid"),
            ATTR_IP_ADDRESS: data.get("ip"),
            ATTR_OS: data.get("os"),
            ATTR_CHANNEL: data.get("channel"),
            ATTR_SIGNAL: data.get("signal"),
            ATTR_SPEED: data.get("speed"),
        }

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether this entity is enabled when first created."""
        return self._new_device_defaults_tracked
