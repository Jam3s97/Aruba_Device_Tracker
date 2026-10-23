"""
Binary sensor platform — whether each access point is online.

Only forwarded while the AP-devices option is on (see AP_PLATFORMS in
__init__.py). 'show aps' has no status column: an AP that loses power simply
drops out of the list (within about a minute, in testing), and a booting AP
reappears once it has rejoined the cluster. So presence in the list is the
status.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)

from .entity import ArubaAPEntity, async_setup_ap_entities

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import ArubaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Connectivity sensors."""
    async_setup_ap_entities(hass, entry, async_add_entities, ArubaAPConnectivity)


class ArubaAPConnectivity(ArubaAPEntity, BinarySensorEntity):
    """
    Whether one access point is listed by the conductor.

    Mode is deliberately ignored: a booting AP is briefly listed as 'monitor'
    before 'access', but 'monitor' is also a mode an AP can be set to on
    purpose (an air monitor), so a listed AP counts as connected either way.
    """

    _key = "connectivity"
    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def available(self) -> bool:
        """
        Return whether the AP list is current.

        Unlike the Clients sensor this stays available while the AP is
        missing: reporting that it is offline is this sensor's whole job.
        Only a cluster that can't be read, or a failed 'show aps', makes it
        unavailable.
        """
        return super().available and self.coordinator.aps_ok

    @property
    def is_on(self) -> bool:
        """Return True if the AP is in the latest 'show aps'."""
        return self._serial in self.coordinator.aps
