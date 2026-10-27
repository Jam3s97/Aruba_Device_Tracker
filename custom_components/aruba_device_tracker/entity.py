"""Base entities: options-backed controls, and per-access-point entities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .utils import ap_unique_id, get_ap_device_info, is_ap_device

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import (
        AddConfigEntryEntitiesCallback,
    )

    from . import ArubaConfigEntry, ArubaIAPCoordinator


class ArubaOptionsEntity(Entity):
    """
    An entity whose state is read from the config entry's options.

    Its state is only written when something calls async_write_ha_state, so
    without this a change made under Configure would leave the entity showing
    the old value. The update listener only redraws the entity; it never
    reloads the entry (see the note in async_setup_entry).
    """

    _entry: ArubaConfigEntry
    # Settings, so HA lists them under the device's Configuration section and
    # leaves them out of auto-generated dashboards and of voice commands such
    # as "turn off all switches".
    _attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        """Redraw whenever the entry's options change."""
        await super().async_added_to_hass()
        self.async_on_remove(self._entry.add_update_listener(self._async_entry_updated))

    async def _async_entry_updated(
        self,
        hass: HomeAssistant,  # noqa: ARG002
        entry: ArubaConfigEntry,  # noqa: ARG002
    ) -> None:
        """Write the current option value to the state machine."""
        self.async_write_ha_state()


class ArubaAPEntity(CoordinatorEntity["ArubaIAPCoordinator"]):
    """
    An entity on one access point's device, keyed by the AP's serial number.

    Subclasses set ``_key``, the unique-ID suffix, and decide availability.
    """

    _attr_has_entity_name = True
    _key: str

    def __init__(
        self,
        coordinator: ArubaIAPCoordinator,
        serial: str,
        forget: Callable[[str], None],
    ) -> None:
        """Initialise the entity and describe its AP device."""
        super().__init__(coordinator)
        self._serial = serial
        self._forget = forget
        entry = coordinator.config_entry
        self._attr_unique_id = ap_unique_id(entry.entry_id, serial, self._key)
        self._attr_device_info = get_ap_device_info(
            entry, serial, coordinator.aps.get(serial)
        )

    async def async_added_to_hass(self) -> None:
        """Let the platform recreate this entity if it is ever removed."""
        await super().async_added_to_hass()
        # E.g. the user deleted a retired AP's device: if that AP comes back,
        # it must get new entities rather than be skipped as already known.
        self.async_on_remove(lambda: self._forget(self._serial))

    @property
    def ap_listed(self) -> bool:
        """Return True if this AP is in the latest successful 'show aps'."""
        return self.coordinator.aps_ok and self._serial in self.coordinator.aps


@callback
def async_setup_ap_entities(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    entity_class: type[ArubaAPEntity],
) -> None:
    """
    Add an ``entity_class`` entity for every AP, now and as APs appear.

    Seeded from the device registry as well as the live AP list, so an AP
    that is off when HA starts still gets its entities (Connectivity reads
    off) instead of none. This mirrors how trackers are seeded from
    last-seen storage so offline devices restore as away.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add(serials: Iterable[str]) -> None:
        subentry_id = coordinator.ap_subentry_id
        if subentry_id is None or subentry_id not in entry.subentries:
            # The cluster group was just deleted; a reload follows.
            return
        new: list[ArubaAPEntity] = []
        for serial in serials:
            if serial not in known:
                known.add(serial)
                new.append(entity_class(coordinator, serial, known.discard))
        if new:
            # In the subentry, so the AP devices are grouped apart from the
            # devices HA links to trackers by MAC.
            async_add_entities(new, config_subentry_id=subentry_id)

    dev_reg = dr.async_get(hass)
    registered = sorted(
        value
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
        if is_ap_device(device, entry.entry_id)
        for domain, value in device.identifiers
        if domain == DOMAIN
    )
    _add([*coordinator.aps, *registered])
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: _add(list(coordinator.aps)))
    )
