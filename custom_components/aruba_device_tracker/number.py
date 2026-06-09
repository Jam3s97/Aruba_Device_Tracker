"""Number platform — Aruba Device Tracker configurable values."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_CLEANUP_DAYS,
    CONF_SCAN_INTERVAL,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CLEANUP_DAYS,
    MAX_SCAN_INTERVAL,
    MIN_CLEANUP_DAYS,
    MIN_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ArubaIAPCoordinator

LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Shared DeviceInfo for the IAP control device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Aruba IAP ({entry.data.get('host', '')})",
        manufacturer="Aruba Networks (HPE)",
        model="Instant AP",
    )


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities for the IAP control device."""
    async_add_entities(
        [
            ArubaPollIntervalNumber(entry),
            ArubaCleanupDaysNumber(entry),
        ]
    )


# ---------------------------------------------------------------------------
# Poll interval (existing)
# ---------------------------------------------------------------------------


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
        """Initialise the poll interval entity."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_poll_interval"
        self._attr_name = "Poll Interval"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the current poll interval in seconds."""
        return self._entry.options.get(
            CONF_SCAN_INTERVAL,
            self._entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

    async def async_set_native_value(self, value: float) -> None:
        """Update the poll interval and apply it to the coordinator immediately."""
        new_options = {**self._entry.options, CONF_SCAN_INTERVAL: int(value)}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)

        coordinator: ArubaIAPCoordinator = self._entry.runtime_data
        coordinator.update_interval = timedelta(seconds=int(value))
        self.async_write_ha_state()

        LOGGER.debug("Aruba Device Tracker poll interval updated to %ds", int(value))


# ---------------------------------------------------------------------------
# Cleanup days (new)
# ---------------------------------------------------------------------------


class ArubaCleanupDaysNumber(NumberEntity):
    """
    Number entity to configure how many days before a device is considered stale.

    Only has an effect when the 'Auto-Remove Stale Devices' switch is on.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-remove-outline"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = MIN_CLEANUP_DAYS
    _attr_native_max_value = MAX_CLEANUP_DAYS
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "d"

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialise the cleanup days entity."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_cleanup_days"
        self._attr_name = "Auto-Remove Stale Devices After"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> float:
        """Return the current cleanup threshold in days."""
        return self._entry.options.get(
            CONF_CLEANUP_DAYS,
            self._entry.data.get(CONF_CLEANUP_DAYS, DEFAULT_CLEANUP_DAYS),
        )

    async def async_set_native_value(self, value: float) -> None:
        """Persist the new cleanup threshold."""
        new_options = {**self._entry.options, CONF_CLEANUP_DAYS: int(value)}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()

        LOGGER.debug(
            "Aruba Device Tracker cleanup threshold updated to %d day(s)", int(value)
        )
