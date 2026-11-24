"""
Aruba Device Tracker — Home Assistant Integration.

https://github.com/Jam3s97/aruba_device_tracker
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .aruba_client import (
    ArubaAuthError,
    ArubaCertificateError,
    ArubaConnectionError,
    ArubaIAPClient,
)
from .const import (
    AP_PLATFORMS,
    CLUSTER_SUBENTRY_TITLE,
    CLUSTER_SUBENTRY_TYPE,
    CONF_ENABLE_AP_DEVICES,
    CONF_ENABLE_AREA_SENSORS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_ENABLE_AP_DEVICES,
    DEFAULT_ENABLE_AREA_SENSORS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)
from .coordinator import ArubaIAPCoordinator, _last_seen_store, _option
from .utils import (
    device_in_subentry,
    device_only_in_entry,
    is_ap_device,
    is_area_unique_id,
    tracker_unique_id,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

LOGGER = logging.getLogger(__name__)

type ArubaConfigEntry = ConfigEntry[ArubaIAPCoordinator]


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
    # Area sensors need AP devices: an AP's device is what holds its area.
    area_sensors = ap_devices and bool(
        _option(entry, CONF_ENABLE_AREA_SENSORS, DEFAULT_ENABLE_AREA_SENSORS)
    )

    coordinator = ArubaIAPCoordinator(
        hass=hass,
        client=client,
        entry=entry,
        scan_interval=scan_interval,
        ap_devices=ap_devices,
        area_sensors=area_sensors,
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

    coordinator.ap_subentry_id = _async_ensure_cluster_subentry(hass, entry)
    # Devices from before the subentry existed (the IAP device on every
    # install, and 2.2.0-beta.1's AP devices) are removed so they are
    # recreated inside it. Entity settings and IDs are restored from the
    # registry's deleted entries, so history carries on.
    _async_remove_devices_outside_subentry(hass, entry, coordinator.ap_subentry_id)
    entry.async_on_unload(entry.add_update_listener(_async_cluster_subentry_listener))

    # Run on every setup with an option off, not only when it is turned off:
    # that keeps it idempotent and also tidies up after a downgrade or a
    # reload that failed halfway.
    if not ap_devices:
        _async_remove_ap_devices(hass, entry)
    elif not area_sensors:
        _async_remove_area_sensors(hass, entry)

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
def _async_ensure_cluster_subentry(hass: HomeAssistant, entry: ArubaConfigEntry) -> str:
    """
    Return the id of the subentry grouping this integration's devices.

    HA lists a config entry's devices grouped by subentry. Putting the IAP
    device and the AP devices in one keeps them apart from the devices HA
    links to trackers by MAC (another integration's vacuum or camera), which
    land in "Devices that don't belong to a sub-entry". It exists whether or
    not the AP option is on, because it holds the IAP device. It is created
    in code, not by a subentry flow, so users can't add one themselves.
    """
    for subentry in entry.subentries.values():
        if subentry.subentry_type == CLUSTER_SUBENTRY_TYPE:
            if subentry.title != CLUSTER_SUBENTRY_TITLE:
                # Keeps a renamed title in step on installs that already
                # have the group.
                hass.config_entries.async_update_subentry(
                    entry, subentry, title=CLUSTER_SUBENTRY_TITLE
                )
            return subentry.subentry_id
    subentry = ConfigSubentry(
        data=MappingProxyType({}),
        subentry_type=CLUSTER_SUBENTRY_TYPE,
        title=CLUSTER_SUBENTRY_TITLE,
        unique_id=CLUSTER_SUBENTRY_TYPE,
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry.subentry_id


async def _async_cluster_subentry_listener(
    hass: HomeAssistant, entry: ArubaConfigEntry
) -> None:
    """
    Handle the cluster group being deleted in the UI.

    Deleting the subentry removes every device in it, the IAP device
    included, while the integration is still running. Treated as turning the
    AP option off: the reload brings the IAP device back in a new group,
    without the APs. Fires on every entry update (options writes included)
    and only acts when the subentry has gone.
    """
    coordinator = entry.runtime_data
    subentry_id = coordinator.ap_subentry_id
    if subentry_id is None or subentry_id in entry.subentries:
        return
    # Cleared first: the update below fires this listener again, and the AP
    # platforms must stop adding entities to the deleted subentry.
    coordinator.ap_subentry_id = None
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_ENABLE_AP_DEVICES: False}
    )
    hass.config_entries.async_schedule_reload(entry.entry_id)


@callback
def _async_remove_devices_outside_subentry(
    hass: HomeAssistant, entry: ArubaConfigEntry, subentry_id: str
) -> None:
    """
    Remove this integration's own devices that aren't in the cluster subentry.

    They are recreated inside it as their entities are added. Only devices
    carrying one of this integration's identifiers (the IAP device and AP
    devices) are considered: the devices HA links to trackers carry only a
    MAC connection and stay where they are.
    """
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not any(domain == DOMAIN for domain, _ in device.identifiers):
            continue
        if device_in_subentry(device, entry.entry_id, subentry_id):
            continue
        _async_remove_device(dev_reg, device, entry)


@callback
def _async_remove_ap_devices(hass: HomeAssistant, entry: ArubaConfigEntry) -> None:
    """
    Remove this entry's AP devices and AP entities.

    Removing a device removes its entities, which covers the per-AP Clients
    sensors. The sensor platform is shared with Total clients and the network
    sensors, so it isn't cleared by domain; Area sensors, which need AP
    devices but have no device of their own, are removed by unique ID.
    Tracker entities, the IAP control device and its entities (Total clients,
    the network sensors) are never touched.
    """
    _async_remove_area_sensors(hass, entry)
    ap_domains = {str(platform) for platform in AP_PLATFORMS}
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.domain in ap_domains:
            ent_reg.async_remove(reg_entry.entity_id)

    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if is_ap_device(device, entry.entry_id):
            _async_remove_device(dev_reg, device, entry)


@callback
def _async_remove_area_sensors(hass: HomeAssistant, entry: ArubaConfigEntry) -> None:
    """
    Remove every Area sensor of this entry.

    By unique ID: they have no device, so no device removal takes them.
    """
    ent_reg = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if is_area_unique_id(entry.entry_id, reg_entry.unique_id):
            ent_reg.async_remove(reg_entry.entity_id)


@callback
def _async_remove_device(
    dev_reg: dr.DeviceRegistry, device: dr.DeviceEntry, entry: ArubaConfigEntry
) -> None:
    """Remove ``device``, or only detach this entry from it if it is shared."""
    if device_only_in_entry(device, entry.entry_id):
        dev_reg.async_remove_device(device.id)
    else:
        # Before HA 2026.9 a device could belong to several config entries,
        # so two entries for the same cluster shared AP devices: detach this
        # one only. HA 2026.9+ gives each entry its own device (and
        # deprecates remove_config_entry_id), so this branch never runs there.
        dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


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


async def async_remove_entry(hass: HomeAssistant, entry: ArubaConfigEntry) -> None:
    """
    Delete the entry's last-seen file when the integration is removed.

    HA removes the entry's devices and entities itself, but not files in
    .storage. Without this, each removed entry left its file behind for good.
    HA unloads the entry first, and unloading flushes with async_save, which
    cancels any pending delayed write, so no late save can recreate the file.
    The legacy shared file is not touched: it predates per-entry storage and
    is kept so a downgrade still finds its data.
    """
    await _last_seen_store(hass, entry.entry_id).async_remove()
