"""Shared utilities for Aruba Device Tracker integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


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
