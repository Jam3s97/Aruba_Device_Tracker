"""Shared utilities for Aruba Device Tracker integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
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
