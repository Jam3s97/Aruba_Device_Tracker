"""Shared utilities for Aruba Device Tracker integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MANUFACTURER

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.device_registry import DeviceEntry

    from .aruba_client import ArubaAP


def tracker_unique_id(entry_id: str, mac: str) -> str:
    """
    Return the entity-registry unique ID for a tracked client.

    Scoped to the config entry because HA requires unique IDs to be unique
    across the whole integration: with the bare MAC, a second AP entry seeing
    the same device had its entity dropped as a duplicate.
    """
    return f"{entry_id}_{mac}"


def ap_unique_id(entry_id: str, serial: str, key: str) -> str:
    """
    Return the entity-registry unique ID for one of an AP's entities.

    Entry-scoped for the same reason as tracker_unique_id: two entries
    pointed at the same cluster both see every AP.
    """
    return f"{entry_id}_{serial}_{key}"


def area_unique_id(entry_id: str, mac: str) -> str:
    """
    Return the entity-registry unique ID for a tracked client's Area sensor.

    A prefix rather than a suffix (``_area``), so it can never be confused
    with a network sensor whose profile happens to be named "area".
    """
    return f"{entry_id}_area_{mac}"


def is_area_unique_id(entry_id: str, unique_id: str) -> bool:
    """Return True if ``unique_id`` is one of this entry's Area sensors."""
    return unique_id.startswith(area_unique_id(entry_id, ""))


def network_unique_id(entry_id: str, profile: str) -> str:
    """
    Return the entity-registry unique ID for a network's client-count sensor.

    Keyed by profile name, which stays the same if the broadcast SSID is
    renamed on the controller.
    """
    return f"{entry_id}_network_{profile}"


def find_ap_device(
    dev_reg: dr.DeviceRegistry, entry_id: str, serial: str
) -> DeviceEntry | None:
    """
    Return this entry's device for the AP with ``serial``, if it has one.

    Looks through this entry's own devices rather than calling
    ``async_get_device``: HA 2026.9 deprecates that (identifiers are only
    unique within a config entry now) for ``async_get_device_by_identifier``,
    which the 2026.3 floor doesn't have. An entry has a handful of devices,
    so the scan is cheap.
    """
    identifier = (DOMAIN, serial)
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(dev_reg, entry_id)
            if identifier in device.identifiers
        ),
        None,
    )


def device_in_subentry(device: DeviceEntry, entry_id: str, subentry_id: str) -> bool:
    """
    Return True if ``device`` is in this entry's subentry ``subentry_id``.

    HA 2026.9 gave each device a single config entry and subentry
    (``config_entry_id`` / ``config_subentry_id``), and 2026.10 deprecates
    the old multi-entry attributes (``config_entries``,
    ``config_entries_subentries``), which are plain fields on the 2026.3
    floor. Which one the device has tells which model this HA uses, so the
    deprecated names are never touched on new HA.
    """
    if hasattr(device, "config_subentry_id"):
        return (
            device.config_entry_id == entry_id
            and device.config_subentry_id == subentry_id
        )
    return subentry_id in device.config_entries_subentries.get(entry_id, set())


def device_only_in_entry(device: DeviceEntry, entry_id: str) -> bool:
    """
    Return True if ``device`` belongs to this config entry and no other.

    Always true on HA 2026.9+, where a device has a single config entry. On
    the 2026.3 floor a device can be shared between entries. See
    device_in_subentry for why both models are handled.
    """
    if hasattr(device, "config_entry_id"):
        return device.config_entry_id == entry_id
    return device.config_entries == {entry_id}


def is_ap_device(device: DeviceEntry, entry_id: str) -> bool:
    """
    Return True if ``device`` is one of this integration's AP devices.

    AP devices are identified by serial number. The IAP control device is
    identified by the entry id, and the devices HA creates for trackers carry
    only a MAC connection, so neither matches.
    """
    return any(
        domain == DOMAIN and value != entry_id for domain, value in device.identifiers
    )


def get_ap_device_info(
    entry: ConfigEntry, serial: str, ap: ArubaAP | None
) -> DeviceInfo:
    """
    Return DeviceInfo for one access point, keyed by its serial number.

    ``ap`` is None for an AP known only from the device registry (it was off
    when HA started). Its device already exists, so name and model are left
    for the registry to keep rather than overwritten with nothing.
    """
    host = entry.data.get(CONF_HOST, "")
    info = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer=MANUFACTURER,
        serial_number=serial,
        # The cluster's web UI is served by the Virtual Controller.
        configuration_url=f"https://{host}" if host else None,
        # No via_device link to the control device: HA 2026.9 deprecates
        # via_device for via_device_id, which the 2026.3 floor lacks.
    )
    if ap is not None:
        info["name"] = ap["name"]
        info["model"] = ap["model"]
    return info


def get_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return shared DeviceInfo for the IAP control device."""
    host = entry.data.get(CONF_HOST, "")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Aruba IAP ({host})",
        manufacturer=MANUFACTURER,
        model="Instant AP",
        configuration_url=f"https://{host}" if host else None,
    )
