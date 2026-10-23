"""
Sensor platform — client counts per access point and for the cluster.

Only forwarded while the AP-devices option is on (see AP_PLATFORMS in
__init__.py). Each AP's Clients sensor is counted from the client list, so it
always agrees with the trackers. Total clients sits on the IAP control device.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity import ArubaAPEntity, async_setup_ap_entities
from .utils import get_device_info

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ArubaConfigEntry, ArubaIAPCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the client-count sensors."""
    async_add_entities([ArubaTotalClientsSensor(entry.runtime_data)])
    async_setup_ap_entities(hass, entry, async_add_entities, ArubaAPClientsSensor)


class ArubaAPClientsSensor(ArubaAPEntity, SensorEntity):
    """Number of clients connected to one access point."""

    _key = "clients"
    _attr_translation_key = "clients"
    _attr_icon = "mdi:access-point"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        """
        Return whether this AP's count is current.

        Unavailable when the cluster can't be read at all, when the last
        'show aps' failed, or when this AP is missing from the list (offline:
        the Connectivity sensor reports that).
        """
        return super().available and self.ap_listed

    @property
    def native_value(self) -> int | None:
        """Return the number of connected clients."""
        return self.coordinator.ap_client_counts.get(self._serial)


class ArubaTotalClientsSensor(CoordinatorEntity["ArubaIAPCoordinator"], SensorEntity):
    """Number of clients connected anywhere in the cluster."""

    _attr_has_entity_name = True
    _attr_translation_key = "total_clients"
    _attr_icon = "mdi:account-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ArubaIAPCoordinator) -> None:
        """Initialise the sensor on the IAP control device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_total_clients"
        self._attr_device_info = get_device_info(entry)

    @property
    def native_value(self) -> int:
        """Return the number of connected clients."""
        return len(self.coordinator.data or {})
