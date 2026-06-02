"""Switch platform — Track New Devices toggle for Aruba IAP."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_TRACK_NEW, DEFAULT_TRACK_NEW, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([ArubaTrackNewSwitch(entry)])


class ArubaTrackNewSwitch(SwitchEntity):
    """Switch to toggle whether newly discovered devices are tracked by default."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:radar"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_track_new_devices"
        self._attr_name = "Track New Devices"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Aruba IAP ({entry.data.get('host', '')})",
            manufacturer="Aruba Networks (HPE)",
            model="Instant AP",
        )

    @property
    def is_on(self) -> bool:
        return self._entry.options.get(
            CONF_TRACK_NEW,
            self._entry.data.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, value: bool) -> None:
        new_options = {**self._entry.options, CONF_TRACK_NEW: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()
