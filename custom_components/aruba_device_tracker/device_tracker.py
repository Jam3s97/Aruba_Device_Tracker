"""aruba device tracker platform."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    DOMAIN,
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
    """Set up device tracker entities from a config entry."""
    coordinator: ArubaIAPCoordinator = entry.runtime_data
    tracked: set[str] = set()

    @callback
    def _add_new_entities() -> None:
        track_new = entry.options.get(
            CONF_TRACK_NEW,
            entry.data.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
        )
        new_entities = []
        for mac, client_data in coordinator.data.items():
            if mac not in tracked:
                tracked.add(mac)
                new_entities.append(
                    ArubaClientEntity(coordinator, entry, mac, client_data, track_new)
                )
        if new_entities:
            async_add_entities(new_entities)

    _add_new_entities()
    coordinator.async_add_listener(_add_new_entities)


class ArubaClientEntity(CoordinatorEntity, ScannerEntity):
    """Represents a single Wi-Fi client tracked via Aruba IAP."""

    def __init__(
        self,
        coordinator: ArubaIAPCoordinator,
        entry: ConfigEntry,
        mac: str,
        initial_data: dict[str, Any],
        new_device_defaults_tracked: bool,
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._entry = entry
        self._new_device_defaults_tracked = new_device_defaults_tracked
        self._attr_name = initial_data.get("name", mac)
        self._attr_unique_id = f"{DOMAIN}_{mac}"

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        return self._mac in self.coordinator.data

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def hostname(self) -> str | None:
        data = self.coordinator.data.get(self._mac)
        return data.get("name") if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data.get(self._mac)
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
        return self._new_device_defaults_tracked
