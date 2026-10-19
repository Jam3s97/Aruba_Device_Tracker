"""Shared utilities for Aruba Device Tracker integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def tracker_unique_id(entry_id: str, mac: str) -> str:
    """
    Return the entity-registry unique ID for a tracked client.

    Scoped to the config entry because HA requires unique IDs to be unique
    across the whole integration: with the bare MAC, a second AP entry seeing
    the same device had its entity dropped as a duplicate.
    """
    return f"{entry_id}_{mac}"


def get_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return shared DeviceInfo for the IAP control device."""
    host = entry.data.get(CONF_HOST, "")
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Aruba IAP ({host})",
        manufacturer="Aruba Networks (HPE)",
        model="Instant AP",
        configuration_url=f"https://{host}" if host else None,
    )
