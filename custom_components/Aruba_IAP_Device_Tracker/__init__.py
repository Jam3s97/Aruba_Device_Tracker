"""
Aruba IAP Device Tracker — Home Assistant Integration.
https://github.com/Jam3s97/Aruba_AP_Device_Tracker
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aruba_client import ArubaIAPClient
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.config_entries import ConfigEntry

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up Aruba IAP Device Tracker using UI config entry."""
    client = ArubaIAPClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    connected = await hass.async_add_executor_job(client.login)
    if not connected:
        LOGGER.error("Failed to connect to Aruba IAP at %s", entry.data[CONF_HOST])
        return False

    coordinator = ArubaIAPCoordinator(hass=hass, client=client)

    # Fetch initial data before setting up platforms
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator on the entry itself (modern HA pattern)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    coordinator: ArubaIAPCoordinator = entry.runtime_data
    await hass.async_add_executor_job(coordinator.client.logout)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


class ArubaIAPCoordinator(DataUpdateCoordinator):
    """Coordinator that polls the Aruba IAP for connected client data."""

    def __init__(self, hass: HomeAssistant, client: ArubaIAPClient) -> None:
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.client = client

    async def _async_update_data(self) -> dict:
        """Fetch latest client data from the IAP."""
        try:
            result = await self.hass.async_add_executor_job(self.client.get_clients)
            if result is None:
                LOGGER.warning(
                    "Aruba IAP get_clients returned None — keeping last known data"
                )
                return self.data or {}
            return result
        except Exception as err:
            raise UpdateFailed(f"Error communicating with Aruba IAP: {err}") from err
