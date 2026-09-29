"""
Aruba Device Tracker — Home Assistant Integration.

https://github.com/Jam3s97/aruba_device_tracker
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aruba_client import (
    ArubaAuthError,
    ArubaCertificateError,
    ArubaClientData,
    ArubaConnectionError,
    ArubaIAPClient,
)
from .const import (
    CLEANUP_INTERVAL_HOURS,
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    LEGACY_STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
    Platform.NUMBER,
]

type ArubaConfigEntry = ConfigEntry[ArubaIAPCoordinator]


def _option(entry: ConfigEntry, key: str, default: object) -> object:
    """
    Read a setting from options, falling back to data, then to the default.

    Options are runtime-changeable; data is set once at config-flow time.
    """
    return entry.options.get(key, entry.data.get(key, default))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
) -> bool:
    """Set up Aruba Device Tracker using UI config entry."""
    client = ArubaIAPClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        verify_ssl=bool(_option(entry, CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)),
    )

    try:
        await hass.async_add_executor_job(client.login)
    except ArubaAuthError as err:
        # Surfacing this as ConfigEntryAuthFailed starts a reauth flow so the
        # user is prompted for new credentials instead of the integration
        # failing silently forever.
        await hass.async_add_executor_job(client.close)
        raise ConfigEntryAuthFailed(str(err)) from err
    except ArubaCertificateError as err:
        # ConfigEntryError, not ConfigEntryNotReady: the AP is up and answering,
        # the certificate simply isn't trusted. Retrying with backoff would
        # never succeed, so the entry is failed outright with an actionable
        # message instead of retrying forever behind a "Retrying setup" banner.
        await hass.async_add_executor_job(client.close)
        raise ConfigEntryError(str(err)) from err
    except ArubaConnectionError as err:
        # ConfigEntryNotReady makes HA retry with backoff — important when the
        # AP is simply slower to come up than Home Assistant is.
        await hass.async_add_executor_job(client.close)
        raise ConfigEntryNotReady(str(err)) from err

    scan_interval = int(_option(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

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
    entry: ArubaConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    # Unload platforms first: if that fails the entry stays loaded, and it
    # must not be left holding a logged-out session.
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        await coordinator.async_flush_last_seen()
        await hass.async_add_executor_job(coordinator.client.logout)
        await hass.async_add_executor_job(coordinator.client.close)
    return unloaded


class ArubaIAPCoordinator(DataUpdateCoordinator[dict[str, ArubaClientData]]):
    """Coordinator that polls the Aruba IAP for connected client data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ArubaIAPClient,
        entry: ArubaConfigEntry,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialise the coordinator with a client and polling interval."""
        super().__init__(
            hass=hass,
            logger=LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        # Storage is scoped per config entry: a shared key would make two APs
        # overwrite each other's last-seen map on every poll.
        self._store: Store[dict[str, str]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.last_seen"
        )
        # MAC -> ISO-format UTC timestamp string
        self.last_seen: dict[str, str] = {}
        self._last_cleanup: datetime | None = None
        self._removal_listeners: list[Callable[[set[str]], None]] = []

    # ------------------------------------------------------------------
    # Persistent last-seen storage
    # ------------------------------------------------------------------

    async def async_load_last_seen(self) -> None:
        """Load last-seen timestamps from persistent storage."""
        stored = await self._store.async_load()
        if stored is None:
            stored = await self._async_migrate_legacy_store()
        if isinstance(stored, dict):
            self.last_seen = stored
            LOGGER.debug(
                "Aruba Device Tracker: loaded last-seen for %d device(s)",
                len(self.last_seen),
            )

    async def _async_migrate_legacy_store(self) -> dict[str, str] | None:
        """
        Adopt the pre-2.0 shared storage file, if one exists.

        The legacy file is left in place rather than deleted, so downgrading
        is non-destructive.
        """
        legacy: Store[dict[str, str]] = Store(
            self.hass, STORAGE_VERSION, LEGACY_STORAGE_KEY
        )
        stored = await legacy.async_load()
        if not isinstance(stored, dict) or not stored:
            return None

        LOGGER.info(
            "Aruba Device Tracker: migrating %d last-seen record(s) from the "
            "shared legacy store to per-entry storage",
            len(stored),
        )
        await self._store.async_save(stored)
        return stored

    @callback
    def _async_schedule_save(self) -> None:
        """
        Queue a delayed write of the last-seen map.

        Writing on every poll would rewrite the whole file every 30 seconds;
        a delayed save coalesces those into one write per minute, and HA
        flushes any pending write on shutdown.
        """
        self._store.async_delay_save(lambda: self.last_seen, STORAGE_SAVE_DELAY)

    async def async_flush_last_seen(self) -> None:
        """Write any pending last-seen data immediately (used on unload)."""
        await self._store.async_save(self.last_seen)

    # ------------------------------------------------------------------
    # Stale device removal notifications
    # ------------------------------------------------------------------

    @callback
    def async_add_removal_listener(
        self, listener: Callable[[set[str]], None]
    ) -> Callable[[], None]:
        """
        Register a callback invoked with the MACs removed by cleanup.

        The device_tracker platform uses this to forget cleaned-up MACs, so a
        device that reconnects later gets a fresh entity instead of being
        silently skipped.
        """
        self._removal_listeners.append(listener)

        @callback
        def _unsubscribe() -> None:
            self._removal_listeners.remove(listener)

        return _unsubscribe

    # ------------------------------------------------------------------
    # Data update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, ArubaClientData]:
        """Fetch latest client data from the IAP."""
        try:
            result = await self.hass.async_add_executor_job(self.client.get_clients)
        except ArubaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ArubaCertificateError as err:
            # Reachable if the AP's certificate changes or expires after setup.
            # Caught explicitly so the actionable message survives rather than
            # falling through to the generic "unexpected error" branch below.
            raise UpdateFailed(str(err)) from err
        except ArubaConnectionError as err:
            # str(err) is built by the client and carries no session token.
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            # Anything else is a bug rather than an AP problem. The detail goes
            # to the log; the user-facing message stays generic because raw
            # exception text can embed the request URL, and the URL carries the
            # session token as a query parameter.
            LOGGER.exception("Unexpected error polling the Aruba IAP")
            msg = "Unexpected error communicating with the Aruba IAP"
            raise UpdateFailed(msg) from err

        if result is None:
            LOGGER.warning(
                "Aruba IAP get_clients returned None — keeping last known data"
            )
            return self.data or {}

        # Update last-seen timestamps for every device currently online. MACs
        # are already format_mac-normalised by the client.
        if result:
            now_iso = datetime.now(tz=UTC).isoformat()
            for mac in result:
                self.last_seen[mac] = now_iso
            self._async_schedule_save()

        # Run stale-device cleanup if enabled and due.
        await self._async_cleanup_stale_devices()

        return result

    # ------------------------------------------------------------------
    # Stale device cleanup
    # ------------------------------------------------------------------

    @property
    def cleanup_enabled(self) -> bool:
        """Return whether automatic stale-device cleanup is active."""
        return bool(
            _option(self.config_entry, CONF_CLEANUP_ENABLED, DEFAULT_CLEANUP_ENABLED)
        )

    @property
    def cleanup_days(self) -> int:
        """Return the number of days before a device is considered stale."""
        return int(_option(self.config_entry, CONF_CLEANUP_DAYS, DEFAULT_CLEANUP_DAYS))

    async def _async_cleanup_stale_devices(self, *, force: bool = False) -> None:
        """
        Remove entity + registry entries for devices not seen for cleanup_days.

        The threshold is measured in days, so this runs at most hourly rather
        than on every poll — a full registry scan every 30 seconds buys nothing.
        """
        if not self.cleanup_enabled:
            return

        now = datetime.now(tz=UTC)
        if (
            not force
            and self._last_cleanup is not None
            and now - self._last_cleanup < timedelta(hours=CLEANUP_INTERVAL_HOURS)
        ):
            return
        self._last_cleanup = now

        threshold = now - timedelta(days=self.cleanup_days)
        registry = er.async_get(self.hass)
        removed: set[str] = set()

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
            entity_id = registry.async_get_entity_id("device_tracker", DOMAIN, mac)
            if entity_id is not None:
                reg_entry = registry.async_get(entity_id)
                # async_get_entity_id is not scoped to a config entry, so with
                # two APs configured it can return the *other* entry's entity.
                if reg_entry is None or (
                    reg_entry.config_entry_id != self.config_entry.entry_id
                ):
                    continue
                registry.async_remove(entity_id)
                LOGGER.info(
                    "Aruba Device Tracker: removed stale device %s (%s)"
                    " — not seen since %s",
                    mac,
                    entity_id,
                    last_seen_iso,
                )

            del self.last_seen[mac]
            removed.add(mac)

        if removed:
            self._async_schedule_save()
            for listener in list(self._removal_listeners):
                listener(removed)
            LOGGER.info(
                "Aruba Device Tracker: stale device cleanup removed %d device(s)",
                len(removed),
            )
        else:
            LOGGER.debug(
                "Aruba Device Tracker: stale device cleanup found no stale devices"
            )
