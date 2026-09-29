"""Aruba Device Tracker platform."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_ACCESS_POINT,
    ATTR_CHANNEL,
    ATTR_DAYS_UNTIL_CLEANUP,
    ATTR_ESSID,
    ATTR_IP_ADDRESS,
    ATTR_LAST_SEEN,
    ATTR_OS,
    CONF_TRACK_NEW,
    DEFAULT_TRACK_NEW,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ArubaConfigEntry, ArubaIAPCoordinator

LOGGER = logging.getLogger(__name__)

SECONDS_PER_DAY = 86400


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    Set up device tracker entities.

    Entities are created for every MAC currently online (from coordinator data)
    plus every MAC previously seen (from coordinator.last_seen storage).  This
    ensures offline devices get an entity immediately on startup rather than
    waiting for the next poll, which is what prevents the 'no longer provided'
    banner.

    Unique IDs are the bare MAC address — the same convention as HA's own
    nmap_tracker — so the entity platform can correctly match registry entries
    to entity objects across restarts.
    """
    coordinator = entry.runtime_data
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

    @callback
    def _new_entities_for(macs: Iterable[str]) -> list[ArubaClientEntity]:
        """Build entities for any of `macs` not already tracked."""
        entities: list[ArubaClientEntity] = []
        for mac in macs:
            if mac in tracked:
                continue
            tracked.add(mac)
            client_data = (coordinator.data or {}).get(mac) or {}
            # Prefer: live IAP name -> registry stored name -> bare MAC
            initial_name = client_data.get("name") or registry_names.get(mac) or mac
            entities.append(
                ArubaClientEntity(
                    coordinator=coordinator,
                    mac=mac,
                    initial_name=initial_name,
                    new_device_defaults_tracked=track_new,
                )
            )
        return entities

    # ------------------------------------------------------------------
    # Seed from coordinator.last_seen — every MAC ever seen by this
    # integration, including currently offline devices.  This is populated
    # from persistent storage before first_refresh runs, so it's always
    # available here.  Online MACs not yet in last_seen (brand new devices on
    # this very first poll) are picked up by the second loop.
    # ------------------------------------------------------------------
    startup_entities = _new_entities_for(coordinator.last_seen)
    startup_entities.extend(_new_entities_for(coordinator.data or {}))

    if startup_entities:
        async_add_entities(startup_entities)

    # ------------------------------------------------------------------
    # Discover new devices on subsequent coordinator polls.
    # ------------------------------------------------------------------
    @callback
    def _add_new_entities() -> None:
        if not coordinator.data:
            return
        new_entities = _new_entities_for(coordinator.data)
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_add_new_entities))

    @callback
    def _forget_removed(macs: set[str]) -> None:
        """
        Drop cleaned-up MACs from the tracked set.

        Without this, a device removed by stale-device cleanup would stay in
        `tracked` forever, so if it ever reconnected no entity would be created
        for it until Home Assistant restarted.
        """
        tracked.difference_update(macs)

    entry.async_on_unload(coordinator.async_add_removal_listener(_forget_removed))


class ArubaClientEntity(ScannerEntity):
    """Represents a single Wi-Fi client tracked via Aruba IAP."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: ArubaIAPCoordinator,
        mac: str,
        initial_name: str,
        new_device_defaults_tracked: bool,  # noqa: FBT001
    ) -> None:
        """Initialise the tracker entity."""
        super().__init__()
        self._coordinator = coordinator
        self._mac = mac
        self._attr_name = initial_name
        # No _attr_unique_id here: ScannerEntity overrides `unique_id` as a
        # property returning `mac_address`, and never consults _attr_unique_id.
        # The MAC arrives already format_mac-normalised (lowercase, colon
        # separated) from ArubaIAPClient.get_clients.
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
    def _client_data(self) -> dict[str, Any] | None:
        """Return this device's live IAP data, or None when it is away."""
        return (self._coordinator.data or {}).get(self._mac)

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
        data = self._client_data
        return data.get("name") if data else None

    @property
    def ip_address(self) -> str | None:
        """
        Return the current IP address reported by the IAP.

        ScannerEntity surfaces this as the standard `ip` state attribute and
        uses it to register the device with HA's MAC/IP discovery helpers.
        """
        data = self._client_data
        return data.get("ip") if data else None

    @property
    def available(self) -> bool:
        """Always available — offline devices show as away, not unavailable."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """
        Return additional state attributes from the IAP.

        Every attribute here is chosen to be *stable* for as long as the
        device's situation doesn't change. Home Assistant writes a recorder
        row whenever the state or any attribute differs from the previous
        one, so an attribute that ticks on every poll costs one database row
        per device per poll — ~2,880/day/device at the default 30s interval.
        Two consequences:

        - `signal` and `speed` are not published at all. The IAP reports
          them and the client still parses them, but they change almost
          continuously, so they were pure recorder churn.
        - `last_seen` / `days_until_cleanup` are published only while the
          device is **away**. For a connected device "last seen" is by
          definition ~now, so it carried no information the `home` state
          didn't already convey, while changing on every single poll.
        """
        attrs: dict[str, Any] = {}

        if self._connected:
            # Live IAP session details. ATTR_IP_ADDRESS duplicates
            # ScannerEntity's standard `ip` attribute; it is kept for
            # backwards compatibility with existing automations and the
            # documented attribute list.
            data = self._client_data
            if data:
                attrs.update(
                    {
                        ATTR_ACCESS_POINT: data.get("access_point"),
                        ATTR_ESSID: data.get("essid"),
                        ATTR_IP_ADDRESS: data.get("ip"),
                        ATTR_OS: data.get("os"),
                        ATTR_CHANNEL: data.get("channel"),
                    }
                )
            return attrs

        # Away: the last-seen timestamp is frozen, so it is both meaningful
        # and stable. Sourced from persistent storage, so it survives
        # restarts unlike the state's last_changed.
        last_seen_iso = self._coordinator.last_seen.get(self._mac)
        if last_seen_iso is None:
            return attrs

        attrs[ATTR_LAST_SEEN] = last_seen_iso
        if self._coordinator.cleanup_enabled:
            try:
                last_seen_dt = datetime.fromisoformat(last_seen_iso)
            except ValueError:
                return attrs
            cleanup_at = last_seen_dt + timedelta(days=self._coordinator.cleanup_days)
            remaining = cleanup_at - datetime.now(tz=UTC)
            attrs[ATTR_DAYS_UNTIL_CLEANUP] = max(
                0, round(remaining.total_seconds() / SECONDS_PER_DAY)
            )

        return attrs

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return whether this entity is enabled when first created."""
        return self._new_device_defaults_tracked
