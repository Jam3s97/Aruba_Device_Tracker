"""
The Aruba IAP coordinator: polls the IAP and holds the latest data.

Entities read from it and never fetch data themselves. Setup and teardown
live in __init__.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
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
    ArubaNetwork,
)
from .const import (
    AP_PLATFORMS,
    BASE_PLATFORMS,
    CLEANUP_INTERVAL_HOURS,
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LEGACY_STORAGE_KEY,
    NETWORK_REFRESH_MINUTES,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from .utils import (
    area_unique_id,
    find_ap_device,
    tracker_unique_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from . import ArubaConfigEntry

LOGGER = logging.getLogger(__name__)


def _option(entry: ConfigEntry, key: str, default: object) -> object:
    """
    Read a setting from options, falling back to data, then to the default.

    Options are runtime-changeable; data is set once at config-flow time.
    """
    return entry.options.get(key, entry.data.get(key, default))


def _last_seen_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, str]]:
    """
    Return the entry's last-seen store.

    Scoped per config entry: a shared key would make two APs overwrite each
    other's last-seen map on every poll. Shared by the coordinator and
    async_remove_entry, so the file removed is the one that was written.
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.last_seen")


class ArubaIAPCoordinator(DataUpdateCoordinator[dict[str, ArubaClientData]]):
    """Coordinator that polls the Aruba IAP for connected client data."""

    def __init__(  # noqa: PLR0913 - independent settings, fixed at setup
        self,
        hass: HomeAssistant,
        client: ArubaIAPClient,
        entry: ArubaConfigEntry,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        *,
        ap_devices: bool = False,
        area_sensors: bool = False,
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
        self._store = _last_seen_store(hass, entry.entry_id)
        # MAC -> ISO-format UTC timestamp string
        self.last_seen: dict[str, str] = {}
        self._last_cleanup: datetime | None = None
        self._failed_polls = 0
        self._removal_listeners: list[Callable[[set[str]], None]] = []
        # AP devices: fixed for the life of this setup, because it decides
        # which platforms were forwarded. Toggling it reloads the entry.
        self.ap_devices = ap_devices
        # Fixed for this setup like ap_devices; toggling it reloads the entry.
        self.area_sensors = area_sensors
        self.platforms = BASE_PLATFORMS + (AP_PLATFORMS if ap_devices else [])
        # The subentry AP entities and devices belong to; set by setup.
        self.ap_subentry_id: str | None = None
        # serial -> AP, last known. Kept on a failed 'show aps' so device
        # names survive; aps_ok says whether it is current.
        self.aps: dict[str, ArubaAP] = {}
        self.aps_ok = False
        # serial -> connected clients, counted from the client list so the
        # sensors always agree with the trackers.
        self.ap_client_counts: dict[str, int] = {}
        # AP name -> serial, from the last 'show aps'. 'show clients' names a
        # client's AP but not its serial, so this is how the Clients and Area
        # sensors find a client's AP.
        self.ap_serial_by_name: dict[str, str] = {}
        self._duplicate_ap_names: set[str] = set()
        # profile -> network, from 'show network', fetched every
        # NETWORK_REFRESH_MINUTES. Kept on a failed fetch; networks_ok says
        # whether the last attempt worked.
        self.networks: dict[str, ArubaNetwork] = {}
        self.networks_ok = False
        # profile -> connected clients, recounted every poll.
        self.network_client_counts: dict[str, int] = {}
        self._last_network_refresh: datetime | None = None
        self._duplicate_essids: set[str] = set()

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
        await self._async_update_networks(result)

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
        aps = await self._async_fetch_table(self.client.get_aps, "access points")
        self.aps_ok = aps is not None
        if aps is not None:
            self._async_rename_ap_devices(aps)
            self.aps = aps
        self.ap_client_counts = self._count_clients_per_ap(clients)

    async def _async_fetch_table[T](
        self, fetch: Callable[[], dict[str, T] | None], what: str
    ) -> dict[str, T] | None:
        """
        Run one of the client's table fetches, mapping failures to None.

        Shared by 'show aps' and 'show network'. Any failure returns None, so
        it only affects that table's entities. A rejected login is the
        exception: it raises ConfigEntryAuthFailed, because the next
        'show clients' would be rejected too.
        """
        try:
            return await self.hass.async_add_executor_job(fetch)
        except ArubaAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ArubaError as err:
            # str(err) is built by the client and carries no session token.
            LOGGER.debug("Aruba IAP %s unavailable: %s", what, err)
        except Exception:
            LOGGER.exception("Unexpected error reading the Aruba IAP %s", what)
        return None

    async def _async_update_networks(self, clients: dict[str, ArubaClientData]) -> None:
        """
        Refresh the network list when due, and recount clients per network.

        The list of configured networks rarely changes, so 'show network' is
        fetched at most every NETWORK_REFRESH_MINUTES (after 'show clients'
        and 'show aps', never in parallel). A failed fetch is retried on the
        next poll rather than after the full interval, so a blip doesn't
        leave the sensors unavailable for half an hour. The counts come from
        the client list on every poll, so they agree with the trackers.
        """
        now = datetime.now(tz=UTC)
        due = (
            self._last_network_refresh is None
            or now - self._last_network_refresh
            >= (timedelta(minutes=NETWORK_REFRESH_MINUTES))
        )
        refreshed = False
        if due:
            networks = await self._async_fetch_table(
                self.client.get_networks, "networks"
            )
            self.networks_ok = networks is not None
            if networks is not None:
                self.networks = networks
                self._last_network_refresh = now
                refreshed = True
        self.network_client_counts = self._count_clients_per_network(
            clients, compare=refreshed
        )

    def _count_clients_per_network(
        self, clients: dict[str, ArubaClientData], *, compare: bool
    ) -> dict[str, int]:
        """
        Count connected clients per network profile.

        Clients report the broadcast SSID (ESSID), which can differ from the
        profile name, so they are matched through each network's ESSID. With
        ``compare`` (just after a fetch, while the controller's own counts are
        current) the two are compared at debug level.
        """
        if not self.networks:
            return {}
        profile_by_essid: dict[str, str] = {}
        for profile, network in self.networks.items():
            essid = network["essid"]
            if essid in profile_by_essid and essid not in self._duplicate_essids:
                self._duplicate_essids.add(essid)
                LOGGER.warning(
                    "Two Aruba networks both broadcast the SSID '%s', so their "
                    "client counts can't be told apart",
                    essid,
                )
            profile_by_essid[essid] = profile

        counts = dict.fromkeys(self.networks, 0)
        for client in clients.values():
            profile = profile_by_essid.get(client["essid"] or "")
            if profile is not None:
                counts[profile] += 1

        if compare:
            for profile, network in self.networks.items():
                reported = network["clients"]
                if reported is not None and reported != counts[profile]:
                    LOGGER.debug(
                        "Aruba IAP network %s reports %d client(s); the client "
                        "list has %d",
                        network["essid"],
                        reported,
                        counts[profile],
                    )
        return counts

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
            device = find_ap_device(dev_reg, self.config_entry.entry_id, serial)
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
        self.ap_serial_by_name = serial_by_name

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

            # The client's Area sensor goes with its tracker.
            area_entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, area_unique_id(self.config_entry.entry_id, mac)
            )
            if area_entity_id is not None:
                registry.async_remove(area_entity_id)

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
