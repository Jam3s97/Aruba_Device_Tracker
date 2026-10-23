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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aruba_client import (
    ArubaAP,
    ArubaAuthError,
    ArubaCertificateError,
    ArubaClientData,
    ArubaConnectionError,
    ArubaError,
    ArubaIAPClient,
)
from .const import (
    CLEANUP_INTERVAL_HOURS,
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    CONF_ENABLE_AP_DEVICES,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_ENABLE_AP_DEVICES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    LEGACY_STORAGE_KEY,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from .utils import is_ap_device, tracker_unique_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

BASE_PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
    Platform.NUMBER,
]

# Forwarded only while the AP-devices option is on. Every entity on these
# platforms belongs to that feature, which is what lets turning it off remove
# them by domain.
AP_PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

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
    ap_devices = bool(_option(entry, CONF_ENABLE_AP_DEVICES, DEFAULT_ENABLE_AP_DEVICES))

    coordinator = ArubaIAPCoordinator(
        hass=hass,
        client=client,
        entry=entry,
        scan_interval=scan_interval,
        ap_devices=ap_devices,
    )

    # Load persisted last-seen data before the first refresh so cleanup
    # logic has accurate timestamps from the moment HA starts.
    await coordinator.async_load_last_seen()

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Setup is about to fail, so async_unload_entry will never run. Release
        # the AP session here, or every ConfigEntryNotReady retry leaks one.
        await hass.async_add_executor_job(client.logout)
        await hass.async_add_executor_job(client.close)
        raise

    entry.runtime_data = coordinator

    if not ap_devices:
        # Run on every setup with the option off, not only when it is turned
        # off: that keeps it idempotent and also tidies up after a downgrade
        # or a reload that failed halfway.
        _async_remove_ap_devices(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)

    # NOTE: No reloading update_listener / async_reload_entry registered here.
    #
    # All runtime-changeable settings (poll interval, track_new, cleanup
    # on/off, cleanup days) are applied live without a reload: the poll
    # interval is pushed to the coordinator, and the rest are read from
    # entry.options at the moment they are used. The switch and number
    # entities do register update listeners (entity.ArubaOptionsEntity), but
    # those only redraw the entity, never reload.
    #
    # A reload IS required when the host, credentials or certificate
    # verification change, but those live in the reconfigure flow
    # (config_flow.py), which reloads via async_update_reload_and_abort —
    # not via update_listener. The options flow applies a new poll
    # interval to the coordinator directly.
    #
    # Registering an update_listener that reloads on every options write
    # causes the banner "entity no longer provided" because the switch and
    # number entities write to options on state changes, which fires the
    # listener and tears down the platform while tracker entities are live.
    #
    # The one option that does need a reload, enable_ap_devices (it decides
    # which platforms are forwarded), is reloaded explicitly by the options
    # flow, and only when its value actually changes.

    return True


@callback
def _async_remove_ap_devices(hass: HomeAssistant, entry: ArubaConfigEntry) -> None:
    """
    Remove this entry's AP entities and AP devices.

    Tracker entities and the IAP control device are never touched.
    """
    ap_domains = {str(platform) for platform in AP_PLATFORMS}
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain in ap_domains:
            ent_reg.async_remove(reg_entry.entity_id)

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not is_ap_device(device, entry.entry_id):
            continue
        if device.config_entries == {entry.entry_id}:
            dev_reg.async_remove_device(device.id)
        else:
            # Before HA 2026.9 a device could belong to several config
            # entries, so two entries for the same cluster shared AP devices:
            # detach this one only. HA 2026.9+ gives each entry its own
            # device (and deprecates remove_config_entry_id), so this branch
            # never runs there.
            dev_reg.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ArubaConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """
    Allow deleting an AP device from the UI once the AP has gone.

    AP devices are kept when an AP drops out of 'show aps' (a rebooting AP
    shouldn't lose its name or area), so this is how a retired AP is removed.
    An AP that is still reporting would come straight back, so it is refused,
    as are the IAP control device and tracker devices.
    """
    if not is_ap_device(device_entry, entry.entry_id):
        return False
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return True
    return not any(
        (DOMAIN, serial) in device_entry.identifiers for serial in coordinator.aps
    )


async def async_migrate_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
) -> bool:
    """Migrate an older config entry to the current version."""
    if entry.version > 1:
        # Written by a newer major version; nothing here can read it.
        return False

    if entry.minor_version < 2:  # noqa: PLR2004
        # Tracker unique IDs were the bare MAC. Rewrite them in place so
        # entity IDs, names, history and customisations all carry over.
        @callback
        def _scope_tracker_unique_id(
            reg_entry: er.RegistryEntry,
        ) -> dict[str, str] | None:
            if reg_entry.domain != "device_tracker":
                return None
            return {
                "new_unique_id": tracker_unique_id(entry.entry_id, reg_entry.unique_id)
            }

        await er.async_migrate_entries(hass, entry.entry_id, _scope_tracker_unique_id)
        hass.config_entries.async_update_entry(entry, minor_version=2)
        LOGGER.debug("Migrated config entry %s to version 1.2", entry.entry_id)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ArubaConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    # Unload platforms first: if that fails the entry stays loaded, and it
    # must not be left holding a logged-out session.
    # Unload exactly what setup forwarded. Reading the option here instead
    # would break the reload that follows toggling it.
    coordinator = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, coordinator.platforms
    )
    if unloaded:
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
        *,
        ap_devices: bool = False,
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
        self._failed_polls = 0
        self._removal_listeners: list[Callable[[set[str]], None]] = []
        # AP devices: fixed for the life of this setup, because it decides
        # which platforms were forwarded. Toggling it reloads the entry.
        self.ap_devices = ap_devices
        self.platforms = BASE_PLATFORMS + (AP_PLATFORMS if ap_devices else [])
        # serial -> AP, last known. Kept on a failed 'show aps' so device
        # names survive; aps_ok says whether it is current.
        self.aps: dict[str, ArubaAP] = {}
        self.aps_ok = False
        # serial -> connected clients, counted from the client list so the
        # sensors always agree with the trackers.
        self.ap_client_counts: dict[str, int] = {}
        self._duplicate_ap_names: set[str] = set()

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
            if self.data is None:
                # First refresh, so there is no last-known data to keep.
                # Returning {} would flip every restored device to away for a
                # poll, firing "left home" automations on an AP hiccup. Fail
                # instead, so setup retries via ConfigEntryNotReady.
                msg = "The Aruba IAP did not return a client list"
                raise UpdateFailed(msg)
            # A blip keeps last-known data. A sustained failure raises, so the
            # coordinator logs the outage once (and the recovery once) and the
            # trackers go unavailable rather than showing stale state forever.
            self._failed_polls += 1
            if self._failed_polls >= UNAVAILABLE_AFTER_FAILED_POLLS:
                msg = (
                    "The Aruba IAP has not returned a client list for "
                    f"{UNAVAILABLE_AFTER_FAILED_POLLS} consecutive polls"
                )
                raise UpdateFailed(msg)
            LOGGER.debug(
                "Aruba IAP get_clients returned None (%d/%d) — keeping last known data",
                self._failed_polls,
                UNAVAILABLE_AFTER_FAILED_POLLS,
            )
            return self.data

        self._failed_polls = 0

        # Update last-seen timestamps for every device currently online. MACs
        # are already format_mac-normalised by the client.
        if result:
            now_iso = datetime.now(tz=UTC).isoformat()
            for mac in result:
                self.last_seen[mac] = now_iso
            self._async_schedule_save()

        if self.ap_devices:
            await self._async_update_aps(result)

        # Run stale-device cleanup if enabled and due.
        await self._async_cleanup_stale_devices()

        return result

    # ------------------------------------------------------------------
    # Access points
    # ------------------------------------------------------------------

    async def _async_update_aps(self, clients: dict[str, ArubaClientData]) -> None:
        """
        Refresh the AP list, isolated from client tracking.

        Runs after 'show clients' in the same poll, never in parallel: the two
        share one HTTP session and login, and load on the AP's management
        plane is the usual cause of failed polls. A failure here only marks
        the AP entities unavailable. The one exception is a rejected login,
        which would fail the next 'show clients' too, so it starts reauth.
        """
        try:
            aps = await self.hass.async_add_executor_job(self.client.get_aps)
        except ArubaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ArubaError as err:
            # str(err) is built by the client and carries no session token.
            LOGGER.debug("Aruba IAP access point list unavailable: %s", err)
            aps = None
        except Exception:
            LOGGER.exception("Unexpected error reading the Aruba IAP access points")
            aps = None

        self.aps_ok = aps is not None
        if aps is not None:
            self._async_rename_ap_devices(aps)
            self.aps = aps
        self.ap_client_counts = self._count_clients_per_ap(clients)

    @callback
    def _async_rename_ap_devices(self, aps: dict[str, ArubaAP]) -> None:
        """
        Follow an AP rename on the controller.

        DeviceInfo is only applied when an entity is added, so a rename while
        running needs an explicit update. A name the user set in HA is stored
        separately (name_by_user) and still wins.
        """
        dev_reg = dr.async_get(self.hass)
        for serial, ap in aps.items():
            previous = self.aps.get(serial)
            if previous is None or previous["name"] == ap["name"]:
                continue
            device = dev_reg.async_get_device(identifiers={(DOMAIN, serial)})
            if device is not None:
                dev_reg.async_update_device(device.id, name=ap["name"])

    def _count_clients_per_ap(
        self, clients: dict[str, ArubaClientData]
    ) -> dict[str, int]:
        """
        Count connected clients per AP serial.

        'show clients' names a client's AP but has no AP serial, so clients
        are matched to APs by name.
        """
        serial_by_name: dict[str, str] = {}
        for serial, ap in self.aps.items():
            name = ap["name"]
            if name in serial_by_name and name not in self._duplicate_ap_names:
                self._duplicate_ap_names.add(name)
                LOGGER.warning(
                    "Two Aruba access points are both named '%s', so their "
                    "client counts can't be told apart. Give each AP a "
                    "unique name on the controller",
                    name,
                )
            serial_by_name[name] = serial

        counts = dict.fromkeys(self.aps, 0)
        for client in clients.values():
            serial = serial_by_name.get(client["access_point"] or "")
            if serial is not None:
                counts[serial] += 1

        for serial, ap in self.aps.items():
            if ap["clients"] is not None and ap["clients"] != counts[serial]:
                # Expected now and then: the two commands are moments apart.
                LOGGER.debug(
                    "Aruba IAP %s reports %d client(s); the client list has %d",
                    ap["name"],
                    ap["clients"],
                    counts[serial],
                )
        return counts

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

            # Device is stale — find and remove its entity registry entry. The
            # unique ID is entry-scoped, so another AP's entity for the same
            # MAC can never match here.
            entity_id = registry.async_get_entity_id(
                "device_tracker",
                DOMAIN,
                tracker_unique_id(self.config_entry.entry_id, mac),
            )
            if entity_id is not None:
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
