"""
Aruba Device Tracker — Home Assistant Integration.

https://github.com/Jam3s97/aruba_device_tracker
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aruba_client import ArubaIAPClient
from .const import (
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    CONF_SCAN_INTERVAL,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
    Platform.NUMBER,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up Aruba Device Tracker using UI config entry."""
    client = ArubaIAPClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    connected = await hass.async_add_executor_job(client.login)
    if not connected:
        LOGGER.error("Failed to connect to Aruba IAP at %s", entry.data[CONF_HOST])
        return False

    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL,
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    coordinator = ArubaIAPCoordinator(
        hass=hass,
        client=client,
        entry=entry,
        scan_interval=scan_interval,
    )

    # Load persisted last-seen data before the first refresh so cleanup
    # logic has accurate timestamps from the moment HA starts.
    await coordinator.async_load_last_seen()

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # NOTE: No update_listener / async_reload_entry registered here.
    #
    # All runtime-changeable settings (poll interval, track_new, cleanup
    # on/off, cleanup days) are applied live by their respective entity
    # handlers without needing a full integration reload.
    #
    # A reload IS required when credentials or host change, but that is
    # handled inside the options flow itself (config_flow.py) which calls
    # async_update_entry on the data dict and then triggers a reload via
    # the normal config-entries reload mechanism — not via update_listener.
    #
    # Registering an update_listener that reloads on every options write
    # causes the banner "entity no longer provided" because the switch and
    # number entities write to options on state changes, which fires the
    # listener and tears down the platform while tracker entities are live.

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    coordinator: ArubaIAPCoordinator = entry.runtime_data
    await hass.async_add_executor_job(coordinator.client.logout)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


class ArubaIAPCoordinator(DataUpdateCoordinator):
    """Coordinator that polls the Aruba IAP for connected client data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ArubaIAPClient,
        entry: ConfigEntry,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialise the coordinator with a client and polling interval."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._entry = entry
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # MAC -> ISO-format UTC timestamp string
        self.last_seen: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Persistent last-seen storage
    # ------------------------------------------------------------------

    async def async_load_last_seen(self) -> None:
        """Load last-seen timestamps from persistent storage."""
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self.last_seen = stored
            LOGGER.debug(
                "Aruba Device Tracker: loaded last-seen for %d device(s)",
                len(self.last_seen),
            )

    async def _async_save_last_seen(self) -> None:
        """Persist last-seen timestamps to storage."""
        await self._store.async_save(self.last_seen)

    # ------------------------------------------------------------------
    # Data update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        """Fetch latest client data from the IAP."""
        try:
            result = await self.hass.async_add_executor_job(self.client.get_clients)
        except Exception as err:
            msg = f"Error communicating with Aruba IAP: {err}"
            raise UpdateFailed(msg) from err

        if result is None:
            LOGGER.warning(
                "Aruba IAP get_clients returned None — keeping last known data"
            )
            return self.data or {}

        # Update last-seen timestamps for every device currently online.
        now_iso = datetime.now(tz=UTC).isoformat()
        for mac in result:
            self.last_seen[format_mac(mac)] = now_iso

        await self._async_save_last_seen()

        # Run stale-device cleanup if enabled.
        await self._async_cleanup_stale_devices()

        return result

    # ------------------------------------------------------------------
    # Stale device cleanup
    # ------------------------------------------------------------------

    @property
    def cleanup_enabled(self) -> bool:
        """Return whether automatic stale-device cleanup is active."""
        return self._entry.options.get(
            CONF_CLEANUP_ENABLED,
            self._entry.data.get(CONF_CLEANUP_ENABLED, DEFAULT_CLEANUP_ENABLED),
        )

    @property
    def cleanup_days(self) -> int:
        """Return the number of days before a device is considered stale."""
        return int(
            self._entry.options.get(
                CONF_CLEANUP_DAYS,
                self._entry.data.get(CONF_CLEANUP_DAYS, DEFAULT_CLEANUP_DAYS),
            )
        )

    async def _async_cleanup_stale_devices(self) -> None:
        """Remove entity + registry entries for devices not seen for cleanup_days."""
        if not self.cleanup_enabled:
            return

        threshold = datetime.now(tz=UTC) - timedelta(days=self.cleanup_days)
        registry = er.async_get(self.hass)
        removed: list[str] = []

        for mac, last_seen_iso in list(self.last_seen.items()):
            try:
                last_seen_dt = datetime.fromisoformat(last_seen_iso)
            except ValueError:
                LOGGER.warning(
                    "Aruba Device Tracker: invalid last_seen timestamp for %s (%s)"
                    " — skipping cleanup for this device",
                    mac,
                    last_seen_iso,
                )
                continue

            if last_seen_dt >= threshold:
                continue

            # Device is stale — find and remove its entity registry entry.
            unique_id = format_mac(mac)
            entity_id = registry.async_get_entity_id(
                "device_tracker", DOMAIN, unique_id
            )
            if entity_id:
                registry.async_remove(entity_id)
                LOGGER.info(
                    "Aruba Device Tracker: removed stale device %s (%s)"
                    " — not seen since %s",
                    mac,
                    entity_id,
                    last_seen_iso,
                )

            del self.last_seen[mac]
            removed.append(mac)

        if removed:
            await self._async_save_last_seen()
