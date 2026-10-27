"""
Sensor platform — client counts, and the area each tracked client is in.

Total clients and one Clients sensor per configured network sit on the IAP
control device and always exist. The per-AP Clients sensors and the per-client
Area sensors are only added while the AP-devices option is on. Every count
comes from the client list, so it always agrees with the trackers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_TRACK_NEW, DEFAULT_TRACK_NEW, DOMAIN
from .entity import ArubaAPEntity, async_setup_ap_entities
from .utils import (
    area_unique_id,
    find_ap_device,
    get_device_info,
    is_area_unique_id,
    network_unique_id,
    tracker_unique_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Any

    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import ArubaConfigEntry, ArubaIAPCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the client-count sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [ArubaTotalClientsSensor(coordinator)],
        config_subentry_id=coordinator.ap_subentry_id,
    )
    _async_setup_network_sensors(hass, entry, async_add_entities)
    if coordinator.ap_devices:
        async_setup_ap_entities(hass, entry, async_add_entities, ArubaAPClientsSensor)
    if coordinator.area_sensors:
        _async_setup_area_sensors(hass, entry, async_add_entities)


@callback
def _async_setup_area_sensors(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """
    Keep one Area sensor per *enabled* tracker, created disabled.

    Mirrors the trackers: seeded from last-seen storage and the live client
    list, new clients added on later polls, and clients removed by stale
    cleanup forgotten so they get a new sensor if they come back. Only for
    trackers that are enabled: an Area sensor for a tracker the user switched
    off has nothing to show. Enabling or disabling a tracker makes HA reload
    the entry, which re-runs this. Not added to the cluster subentry and
    given no device, so tracker-linked devices stay out of the group and no
    device is created per phone.
    """
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    sensors: dict[str, ArubaClientAreaSensor] = {}
    area_prefix = area_unique_id(entry.entry_id, "")

    def _tracker(mac: str) -> er.RegistryEntry | None:
        tracker_id = ent_reg.async_get_entity_id(
            "device_tracker", DOMAIN, tracker_unique_id(entry.entry_id, mac)
        )
        return ent_reg.async_get(tracker_id) if tracker_id else None

    def _tracker_enabled(mac: str) -> bool:
        tracker = _tracker(mac)
        if tracker is not None:
            return tracker.disabled_by is None
        # Not registered yet: it's about to be created, and is created
        # enabled only while Track new devices is on.
        return bool(
            entry.options.get(
                CONF_TRACK_NEW, entry.data.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW)
            )
        )

    def _name_for(mac: str) -> str:
        # The tracker's name: live from the AP, else the tracker's registry
        # name (including one the user set), else the MAC.
        live = ((coordinator.data or {}).get(mac) or {}).get("name")
        if live:
            return live
        tracker = _tracker(mac)
        return (tracker and (tracker.name or tracker.original_name)) or mac

    @callback
    def _add(macs: Iterable[str]) -> None:
        # Recorded as they're built: a client both remembered and connected
        # appears twice in the seed list, and must still get one sensor. A
        # disabled tracker's client isn't recorded, so it is looked at again
        # once its tracker is enabled (after the reload that triggers).
        new: list[ArubaClientAreaSensor] = []
        for mac in macs:
            if mac not in sensors and _tracker_enabled(mac):
                sensors[mac] = ArubaClientAreaSensor(coordinator, mac, _name_for(mac))
                new.append(sensors[mac])
        if new:
            async_add_entities(new)

    # Area sensors left over from a tracker that has since been disabled
    # would otherwise linger as "no longer provided".
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if is_area_unique_id(entry.entry_id, reg_entry.unique_id) and (
            not _tracker_enabled(reg_entry.unique_id.removeprefix(area_prefix))
        ):
            ent_reg.async_remove(reg_entry.entity_id)

    @callback
    def _forget(macs: set[str]) -> None:
        for mac in macs:
            sensors.pop(mac, None)

    issue_id = f"aps_without_area_{entry.entry_id}"

    @callback
    def _check_ap_areas() -> None:
        # Only worth raising once someone has enabled an Area sensor: an AP
        # without an area makes those sensors read unknown.
        enabled = any(s.hass is not None and s.enabled for s in sensors.values())
        dev_reg = dr.async_get(hass)
        missing = sorted(
            ap["name"]
            for serial, ap in coordinator.aps.items()
            if (device := find_ap_device(dev_reg, entry.entry_id, serial)) is not None
            and device.area_id is None
        )
        if enabled and missing:
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="aps_without_area",
                translation_placeholders={"aps": ", ".join(missing)},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    @callback
    def _on_poll() -> None:
        _add(list(coordinator.data or {}))
        _check_ap_areas()

    @callback
    def _redraw(_event: Event[Mapping[str, Any]]) -> None:
        # An AP moved to another area, or an area was renamed or deleted.
        for sensor in sensors.values():
            if sensor.hass is not None:
                sensor.async_write_ha_state()
        _check_ap_areas()

    _add([*coordinator.last_seen, *(coordinator.data or {})])
    entry.async_on_unload(coordinator.async_add_listener(_on_poll))
    # Cleared on unload too, so turning AP devices off (a reload) clears it.
    entry.async_on_unload(lambda: ir.async_delete_issue(hass, DOMAIN, issue_id))
    entry.async_on_unload(coordinator.async_add_removal_listener(_forget))
    entry.async_on_unload(
        hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            _redraw,
            event_filter=_area_changed,
        )
    )
    entry.async_on_unload(
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _redraw)
    )


@callback
def _area_changed(event_data: Mapping[str, Any]) -> bool:
    """Return True for a device-registry update that changed an area."""
    return event_data.get("action") == "update" and "area_id" in (
        event_data.get("changes") or {}
    )


class ArubaClientAreaSensor(CoordinatorEntity["ArubaIAPCoordinator"], SensorEntity):
    """
    The area of the access point a tracked client is connected to.

    That is the nearest AP's area, usually but not always the room the device
    is in: clients often stay on a farther AP, and one AP can cover several
    rooms. Disabled by default; users enable it for the devices they want.
    The state changes only when the area does, once per roam rather than per
    poll.
    """

    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, coordinator: ArubaIAPCoordinator, mac: str, name: str) -> None:
        """
        Initialise the sensor.

        No device, so no has_entity_name: the name is the tracker's name plus
        " area", fixed when the sensor is first created.
        """
        super().__init__(coordinator)
        self.mac = mac
        self._attr_name = f"{name} area"
        self._attr_unique_id = area_unique_id(coordinator.config_entry.entry_id, mac)

    def _area(self) -> ar.AreaEntry | None:
        """Return the area of the client's AP, or None if not known."""
        client = (self.coordinator.data or {}).get(self.mac)
        if client is None:
            return None  # away
        serial = self.coordinator.ap_serial_by_name.get(client["access_point"] or "")
        if serial is None:
            return None  # its AP isn't in the AP list
        device = find_ap_device(
            dr.async_get(self.hass), self.coordinator.config_entry.entry_id, serial
        )
        if device is None or device.area_id is None:
            return None  # the AP has no area yet
        return ar.async_get(self.hass).async_get_area(device.area_id)

    @property
    def native_value(self) -> str | None:
        """Return the area's name, or None (unknown) if it isn't known."""
        area = self._area()
        return area.name if area else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """
        Return the client's MAC, and the area's id while one is known.

        ``mac`` matches the tracker's own ``mac`` attribute, linking the two:
        the sensor has no device to show that, and two trackers can share a
        name (a phone with a private MAC per SSID). It never changes, so it
        adds no recorder rows. ``area_id`` survives the area being renamed.
        """
        area = self._area()
        attrs = {"mac": self.mac}
        if area:
            attrs["area_id"] = area.id
        return attrs


@callback
def _async_setup_network_sensors(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """
    Keep one Clients sensor per configured network.

    Networks come from 'show network', which lists every configured network,
    quiet and disabled ones included. A network deleted on the controller has
    its sensor removed, but only once a *successful* 'show network' no longer
    lists it, so a failed read never removes anything.
    """
    coordinator = entry.runtime_data
    ent_reg = er.async_get(hass)
    prefix = network_unique_id(entry.entry_id, "")
    known: set[str] = set()

    def _add(profiles: list[str]) -> None:
        subentry_id = coordinator.ap_subentry_id
        if subentry_id is None or subentry_id not in entry.subentries:
            # The cluster group was just deleted; a reload follows.
            return
        new = [p for p in profiles if p not in known]
        known.update(new)
        if new:
            async_add_entities(
                [ArubaNetworkClientsSensor(coordinator, p) for p in new],
                config_subentry_id=subentry_id,
            )

    @callback
    def _sync() -> None:
        if coordinator.networks_ok:
            for profile in sorted(known - set(coordinator.networks)):
                known.discard(profile)
                entity_id = ent_reg.async_get_entity_id(
                    "sensor", DOMAIN, network_unique_id(entry.entry_id, profile)
                )
                if entity_id is not None:
                    ent_reg.async_remove(entity_id)
        _add(list(coordinator.networks))

    if not coordinator.networks_ok:
        # The first fetch failed: keep the sensors already registered, so
        # they read unavailable rather than disappearing until it succeeds.
        _add(
            [
                reg_entry.unique_id.removeprefix(prefix)
                for reg_entry in er.async_entries_for_config_entry(
                    ent_reg, entry.entry_id
                )
                if reg_entry.domain == "sensor"
                and reg_entry.unique_id.startswith(prefix)
            ]
        )
    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


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


class ArubaNetworkClientsSensor(CoordinatorEntity["ArubaIAPCoordinator"], SensorEntity):
    """Number of clients connected to one network (SSID) across the cluster."""

    _attr_has_entity_name = True
    _attr_translation_key = "network_clients"
    _attr_icon = "mdi:wifi"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ArubaIAPCoordinator, profile: str) -> None:
        """Initialise the sensor on the IAP control device."""
        super().__init__(coordinator)
        self._profile = profile
        entry = coordinator.config_entry
        self._attr_unique_id = network_unique_id(entry.entry_id, profile)
        self._attr_device_info = get_device_info(entry)

    @property
    def translation_placeholders(self) -> dict[str, str]:
        """
        Name the sensor after the network's broadcast SSID.

        Read live rather than fixed at creation, so renaming the SSID on the
        controller renames the sensor. Falls back to the profile name while
        the network list is unknown.
        """
        network = self.coordinator.networks.get(self._profile)
        return {"essid": network["essid"] if network else self._profile}

    @property
    def available(self) -> bool:
        """Return whether the network is listed, current and enabled."""
        network = self.coordinator.networks.get(self._profile)
        return (
            super().available
            and self.coordinator.networks_ok
            and network is not None
            and network["enabled"]
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of connected clients."""
        return self.coordinator.network_client_counts.get(self._profile)


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
