"""Number platform — Poll Interval for aruba device tracker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from datetime import timedelta

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
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
    async_add_entities([ArubaPollIntervalNumber(entry)])


class ArubaPollIntervalNumber(NumberEntity):
    """Number entity to configure how often the IAP is polled."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:timer-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_SCAN_INTERVAL
    _attr_native_max_value = MAX_SCAN_INTERVAL
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "s"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poll_interval"
        self._attr_name = "Poll Interval"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Aruba IAP ({entry.data.get('host', '')})",
            manufacturer="Aruba Networks (HPE)",
            model="Instant AP",
        )

    @property
    def native_value(self) -> float:
        """Return the current poll interval in seconds."""
        return self._entry.options.get(
            CONF_SCAN_INTERVAL,
            self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the poll interval and restart the coordinator update interval."""
        new_options = {**self._entry.options, CONF_SCAN_INTERVAL: int(value)}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

        # Update the coordinator's interval live — no reload needed
        coordinator: ArubaIAPCoordinator = self._entry.runtime_data
        coordinator.update_interval = timedelta(seconds=int(value))
        self.async_write_ha_state()

        LOGGER.debug("Aruba IAP poll interval updated to %ds", int(value))
