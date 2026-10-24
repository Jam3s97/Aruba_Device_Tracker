"""
Tests for the access-points-as-devices feature.

Same approach as test_coordinator_cleanup.py: the real coordinator, setup and
entity code runs against small fakes rather than a live Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed

import custom_components.aruba_device_tracker as integration
from custom_components.aruba_device_tracker import config_flow
from custom_components.aruba_device_tracker.aruba_client import (
    ArubaAP,
    ArubaAuthError,
    ArubaCertificateError,
)
from custom_components.aruba_device_tracker.binary_sensor import (
    ArubaAPConnectivity,
)
from custom_components.aruba_device_tracker.binary_sensor import (
    async_setup_entry as binary_sensor_setup_entry,
)
from custom_components.aruba_device_tracker.const import DOMAIN
from custom_components.aruba_device_tracker.sensor import (
    ArubaAPClientsSensor,
    ArubaTotalClientsSensor,
)
from custom_components.aruba_device_tracker.sensor import (
    async_setup_entry as sensor_setup_entry,
)
from tests.test_coordinator_cleanup import ENTRY_ID, make_coordinator

OTHER_ENTRY_ID = "entry-b"
SUBENTRY_ID = "sub-cluster"


def make_ap_coordinator(**kwargs):
    """Build a real coordinator with stale-device cleanup off (not under test)."""
    return make_coordinator(cleanup_enabled=False, **kwargs)


def ap(serial: str, name: str, clients: int | None = None) -> ArubaAP:
    """Build a parsed AP."""
    return ArubaAP(
        serial=serial,
        name=name,
        ip="203.0.113.20",
        is_conductor=False,
        model="AP-505",
        clients=clients,
    )


def client(mac: str, access_point: str | None, essid: str | None = None) -> dict:
    """Build the fields of a parsed client that the AP and network features read."""
    return {"mac": mac, "name": mac, "access_point": access_point, "essid": essid}


LOUNGE = ap("TESTSN0001", "LoungeAP")
KITCHEN = ap("TESTSN0002", "KitchenAP")
GARDEN = ap("TESTSN0003", "GardenAP")


def device(
    device_id: str,
    identifier: str | None,
    *entries: str,
    subentry: str | None = None,
    legacy: bool = False,
):
    """
    Build a device entry with one identifier of ours, or none.

    By default it has HA 2026.9+'s single-entry fields only, and none of the
    deprecated multi-entry attributes, so any use of those fails the test.
    ``legacy`` (implied by several entries) builds the 2026.3 shape instead.
    """
    identifiers = {(DOMAIN, identifier)} if identifier else set()
    entries = entries or (ENTRY_ID,)
    if legacy or len(entries) > 1:
        return SimpleNamespace(
            id=device_id,
            identifiers=identifiers,
            config_entries=set(entries),
            config_entries_subentries={e: {subentry} for e in entries},
        )
    return SimpleNamespace(
        id=device_id,
        identifiers=identifiers,
        config_entry_id=entries[0],
        config_subentry_id=subentry,
    )


def poll(coordinator):
    """Run one coordinator update and store its result as HA would."""
    coordinator.data = asyncio.run(coordinator._async_update_data())
    return coordinator.data


# ---------------------------------------------------------------------------
# Coordinator: when and how 'show aps' runs
# ---------------------------------------------------------------------------


class TestPolling:
    def test_option_off_never_requests_the_ap_list(self, dev_reg):  # noqa: ARG002
        calls: list[str] = []
        coordinator = make_ap_coordinator(
            get_clients=lambda: calls.append("clients") or {},
            get_aps=lambda: calls.append("aps") or {},
            ap_devices=False,
        )

        poll(coordinator)

        assert calls == ["clients"]

    def test_option_on_requests_the_ap_list_once_after_the_clients(self, dev_reg):  # noqa: ARG002
        calls: list[str] = []
        coordinator = make_ap_coordinator(
            get_clients=lambda: calls.append("clients") or {},
            get_aps=lambda: calls.append("aps") or {LOUNGE["serial"]: LOUNGE},
            ap_devices=True,
        )

        poll(coordinator)

        assert calls == ["clients", "aps"]
        assert coordinator.aps_ok is True
        assert list(coordinator.aps) == ["TESTSN0001"]

    def test_failed_client_poll_skips_the_ap_list(self, dev_reg):  # noqa: ARG002
        calls: list[str] = []
        coordinator = make_ap_coordinator(
            get_clients=lambda: calls.append("clients"),  # None: a soft failure
            get_aps=lambda: calls.append("aps") or {},
            ap_devices=True,
        )
        coordinator.data = {}  # not the first refresh

        poll(coordinator)

        assert calls == ["clients"]

    @pytest.mark.parametrize(
        "failure",
        [
            lambda: None,  # soft failure: timeout, bad JSON, refused command
            ArubaCertificateError("certificate changed"),
            RuntimeError("bug"),
        ],
        ids=["none", "certificate", "unexpected"],
    )
    def test_ap_failure_leaves_client_tracking_untouched(self, dev_reg, failure):  # noqa: ARG002
        def _get_aps():
            if isinstance(failure, Exception):
                raise failure
            return failure()

        clients = {"aa:bb:cc:dd:ee:01": client("aa:bb:cc:dd:ee:01", "LoungeAP")}
        coordinator = make_ap_coordinator(
            get_clients=lambda: clients, get_aps=_get_aps, ap_devices=True
        )
        coordinator.aps = {LOUNGE["serial"]: LOUNGE}  # last known

        assert poll(coordinator) == clients  # no UpdateFailed, data updated
        assert coordinator.aps_ok is False
        assert list(coordinator.aps) == ["TESTSN0001"]  # kept for device names
        assert coordinator.last_seen  # trackers' last-seen still updated

    def test_rejected_login_during_the_ap_request_starts_reauth(self, dev_reg):  # noqa: ARG002
        def _get_aps():
            raise ArubaAuthError("rejected")

        coordinator = make_ap_coordinator(
            get_clients=dict, get_aps=_get_aps, ap_devices=True
        )

        with pytest.raises(ConfigEntryAuthFailed):
            poll(coordinator)

    def test_recovery_after_a_failure(self, dev_reg):  # noqa: ARG002
        results = iter([None, {LOUNGE["serial"]: LOUNGE}])
        coordinator = make_ap_coordinator(
            get_clients=dict, get_aps=lambda: next(results), ap_devices=True
        )

        poll(coordinator)
        assert coordinator.aps_ok is False
        poll(coordinator)
        assert coordinator.aps_ok is True


# ---------------------------------------------------------------------------
# Coordinator: client counts
# ---------------------------------------------------------------------------


class TestClientCounts:
    def make(self, aps, clients_per_poll):
        polls = iter(clients_per_poll)
        return make_ap_coordinator(
            get_clients=lambda: next(polls),
            get_aps=lambda: {a["serial"]: a for a in aps},
            ap_devices=True,
        )

    def test_clients_are_counted_per_ap_by_name(self, dev_reg):  # noqa: ARG002
        clients = {
            "m1": client("m1", "LoungeAP"),
            "m2": client("m2", "LoungeAP"),
            "m3": client("m3", "KitchenAP"),
        }
        coordinator = self.make([LOUNGE, KITCHEN, GARDEN], [clients])

        poll(coordinator)

        assert coordinator.ap_client_counts == {
            "TESTSN0001": 2,
            "TESTSN0002": 1,
            "TESTSN0003": 0,  # an AP with nobody on it still reports 0
        }

    def test_client_on_an_unknown_or_blank_ap_is_not_counted(self, dev_reg):  # noqa: ARG002
        clients = {"m1": client("m1", "NotAnAP"), "m2": client("m2", None)}
        coordinator = self.make([LOUNGE], [clients])

        poll(coordinator)

        assert coordinator.ap_client_counts == {"TESTSN0001": 0}

    def test_roaming_client_moves_between_counts(self, dev_reg):  # noqa: ARG002
        coordinator = self.make(
            [LOUNGE, KITCHEN],
            [{"m1": client("m1", "LoungeAP")}, {"m1": client("m1", "KitchenAP")}],
        )

        poll(coordinator)
        assert coordinator.ap_client_counts == {"TESTSN0001": 1, "TESTSN0002": 0}
        poll(coordinator)
        assert coordinator.ap_client_counts == {"TESTSN0001": 0, "TESTSN0002": 1}

    def test_duplicate_ap_names_warn_once(self, dev_reg, caplog):  # noqa: ARG002
        twin = ap("TESTSN0009", "LoungeAP")
        coordinator = self.make([LOUNGE, twin], [{}, {}, {}])

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                poll(coordinator)

        assert caplog.text.count("both named 'LoungeAP'") == 1

    def test_mismatch_with_the_ap_reported_count_is_debug_only(self, dev_reg, caplog):  # noqa: ARG002
        coordinator = self.make([ap("TESTSN0001", "LoungeAP", clients=5)], [{}])

        with caplog.at_level(logging.DEBUG):
            poll(coordinator)

        mismatch = [r for r in caplog.records if "reports 5 client" in r.message]
        assert [r.levelno for r in mismatch] == [logging.DEBUG]


class TestDeviceRename:
    def test_controller_rename_updates_the_device_name(self, dev_reg):
        dev_reg.devices.append(device("dev-lounge", "TESTSN0001"))
        names = iter(["LoungeAP", "FamilyRoomAP"])
        coordinator = make_ap_coordinator(
            get_clients=dict,
            get_aps=lambda: {"TESTSN0001": ap("TESTSN0001", next(names))},
            ap_devices=True,
        )

        poll(coordinator)
        assert dev_reg.updates == []  # first sight: DeviceInfo names it
        poll(coordinator)
        assert dev_reg.updates == [("dev-lounge", {"name": "FamilyRoomAP"})]


# ---------------------------------------------------------------------------
# Setup, toggling off, and device removal
# ---------------------------------------------------------------------------


class TestToggleOff:
    def test_removes_ap_entities_and_ap_devices_only(self, dev_reg, ent_reg):
        ent_reg.entries += [
            SimpleNamespace(
                entity_id="binary_sensor.lounge_connectivity",
                unique_id=f"{ENTRY_ID}_TESTSN0001_connectivity",
                domain="binary_sensor",
                config_entry_id=ENTRY_ID,
            ),
            SimpleNamespace(
                entity_id="sensor.total_clients",
                unique_id=f"{ENTRY_ID}_total_clients",
                domain="sensor",
                config_entry_id=ENTRY_ID,
            ),
            SimpleNamespace(
                entity_id="sensor.phone_area",
                unique_id=f"{ENTRY_ID}_area_aa:bb:cc:dd:ee:01",
                domain="sensor",
                config_entry_id=ENTRY_ID,
            ),
            SimpleNamespace(
                entity_id="device_tracker.phone",
                unique_id=f"{ENTRY_ID}_aa:bb:cc:dd:ee:01",
                domain="device_tracker",
                config_entry_id=ENTRY_ID,
            ),
            SimpleNamespace(
                entity_id="switch.track_new",
                unique_id=f"{ENTRY_ID}_track_new_devices",
                domain="switch",
                config_entry_id=ENTRY_ID,
            ),
            SimpleNamespace(
                entity_id="sensor.other_entry",
                unique_id=f"{OTHER_ENTRY_ID}_area_aa:bb:cc:dd:ee:01",
                domain="sensor",
                config_entry_id=OTHER_ENTRY_ID,
            ),
        ]
        dev_reg.devices += [
            device("dev-ap", "TESTSN0001"),
            device("dev-control", ENTRY_ID),  # the IAP control device
            device("dev-phone", None),  # a tracker's MAC-connection device
        ]
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        integration._async_remove_ap_devices(SimpleNamespace(), entry)

        # Total clients stays; the per-AP Clients sensors go with their
        # devices, which HA does when a device is removed.
        # Area sensors have no device, so they are removed by unique ID.
        assert sorted(ent_reg.removed) == [
            "binary_sensor.lounge_connectivity",
            "sensor.phone_area",
        ]
        # Removed outright: on HA 2026.9+ a device has a single config entry,
        # and remove_config_entry_id is deprecated.
        assert dev_reg.updates == [("dev-ap", "removed")]

    def test_device_shared_with_another_entry_is_only_detached(self, dev_reg, ent_reg):  # noqa: ARG002
        """Only possible before HA 2026.9, where devices could be shared."""
        dev_reg.devices.append(device("dev-ap", "TESTSN0001", ENTRY_ID, OTHER_ENTRY_ID))

        integration._async_remove_ap_devices(
            SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID)
        )

        assert dev_reg.updates == [("dev-ap", {"remove_config_entry_id": ENTRY_ID})]


class TestMoveIntoSubentry:
    def test_on_ha_2026_3_too(self, dev_reg):
        """The floor's devices have the multi-entry fields instead."""
        dev_reg.devices += [
            device("dev-ap-old", "TESTSN0001", legacy=True),
            device("dev-ap-new", "TESTSN0002", subentry=SUBENTRY_ID, legacy=True),
        ]

        integration._async_remove_devices_outside_subentry(
            SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID), SUBENTRY_ID
        )

        assert dev_reg.updates == [("dev-ap-old", "removed")]

    def test_our_devices_outside_the_subentry_are_removed(self, dev_reg):
        dev_reg.devices += [
            device("dev-control-old", ENTRY_ID),  # IAP device, pre-subentry
            device("dev-ap-old", "TESTSN0001"),  # beta.1 AP device
            device("dev-ap-new", "TESTSN0002", subentry=SUBENTRY_ID),
            device("dev-control-new", ENTRY_ID, subentry=SUBENTRY_ID),
            device("dev-camera", None),  # linked to a tracker by MAC
        ]

        integration._async_remove_devices_outside_subentry(
            SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID), SUBENTRY_ID
        )

        assert dev_reg.updates == [
            ("dev-control-old", "removed"),
            ("dev-ap-old", "removed"),
        ]


class TestSubentryDeletedInTheUI:
    def run(self, subentries, *, subentry_id=SUBENTRY_ID):
        events: list = []
        entry = SimpleNamespace(
            entry_id=ENTRY_ID,
            options={"scan_interval": 30},
            subentries=subentries,
            runtime_data=SimpleNamespace(ap_subentry_id=subentry_id),
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda _entry, options: events.append(
                    ("save", options)
                ),
                async_schedule_reload=lambda entry_id: events.append(
                    ("reload", entry_id)
                ),
            )
        )
        asyncio.run(integration._async_cluster_subentry_listener(hass, entry))
        return events, entry

    def test_deleting_the_group_turns_the_ap_option_off_and_reloads(self):
        events, entry = self.run({})

        assert events == [
            ("save", {"scan_interval": 30, "enable_ap_devices": False}),
            ("reload", ENTRY_ID),
        ]
        # Cleared, so the AP platforms stop adding to the deleted subentry.
        assert entry.runtime_data.ap_subentry_id is None

    def test_other_entry_updates_are_ignored(self):
        """It also fires on every options write, e.g. from the switches."""
        events, _ = self.run({SUBENTRY_ID: object()})

        assert events == []

    def test_runs_once(self):
        """The options write above fires the listener again; it must not loop."""
        events, _ = self.run({}, subentry_id=None)

        assert events == []


class TestSetupAndUnload:
    @pytest.fixture
    def setup_fakes(self, monkeypatch):
        calls: dict[str, object] = {"removed": 0, "outside": [], "added": []}

        class FakeClient:
            def __init__(self, **kwargs) -> None:
                pass

            def login(self) -> None:
                pass

        class FakeCoordinator:
            def __init__(self, *, ap_devices, area_sensors, **kwargs) -> None:  # noqa: ARG002
                self.platforms = ["base"] + (["ap"] if ap_devices else [])
                self.ap_subentry_id = None
                calls["area_sensors"] = area_sensors

            async def async_load_last_seen(self) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                pass

        async def _forward(entry, platforms) -> None:  # noqa: ARG001
            calls["forwarded"] = platforms

        def _remove(hass, entry) -> None:  # noqa: ARG001
            calls["removed"] += 1

        def _remove_area(hass, entry) -> None:  # noqa: ARG001
            calls["area_removed"] = calls.get("area_removed", 0) + 1

        def _outside(hass, entry, subentry_id) -> None:  # noqa: ARG001
            calls["outside"].append(subentry_id)

        def _add_subentry(entry, subentry) -> bool:
            calls["added"].append(subentry)
            entry.subentries[subentry.subentry_id] = subentry
            return True

        monkeypatch.setattr(integration, "ArubaIAPClient", FakeClient)
        monkeypatch.setattr(integration, "ArubaIAPCoordinator", FakeCoordinator)
        monkeypatch.setattr(integration, "_async_remove_ap_devices", _remove)
        monkeypatch.setattr(integration, "_async_remove_area_sensors", _remove_area)
        monkeypatch.setattr(
            integration, "_async_remove_devices_outside_subentry", _outside
        )

        async def _executor(func, *args):
            return func(*args)

        hass = SimpleNamespace(
            async_add_executor_job=_executor,
            config_entries=SimpleNamespace(
                async_forward_entry_setups=_forward,
                async_add_subentry=_add_subentry,
            ),
        )
        return hass, calls

    def entry(self, options, subentries=None):
        return SimpleNamespace(
            entry_id=ENTRY_ID,
            data={"host": "192.0.2.1", "username": "admin", "password": "pw"},
            options=options,
            subentries=dict(subentries or {}),
            add_update_listener=lambda listener: listener,
            async_on_unload=lambda _unsub: None,
        )

    def test_upgraded_entry_without_the_option_gets_the_default_on(self, setup_fakes):
        hass, calls = setup_fakes

        asyncio.run(integration.async_setup_entry(hass, self.entry({})))

        assert calls["forwarded"] == ["base", "ap"]
        assert calls["removed"] == 0

    def test_the_cluster_subentry_is_created_and_devices_moved_into_it(
        self, setup_fakes
    ):
        hass, calls = setup_fakes
        entry = self.entry({})

        asyncio.run(integration.async_setup_entry(hass, entry))

        [subentry] = calls["added"]
        assert subentry.subentry_type == "access_points"
        assert subentry.title == "Controller and access points"
        assert entry.runtime_data.ap_subentry_id == subentry.subentry_id
        # Devices from before the subentry existed are recreated inside it.
        assert calls["outside"] == [subentry.subentry_id]

    def test_an_existing_cluster_subentry_is_reused(self, setup_fakes):
        hass, calls = setup_fakes
        existing = SimpleNamespace(
            subentry_id=SUBENTRY_ID,
            subentry_type="access_points",
            title="Controller and access points",
        )
        entry = self.entry({}, {SUBENTRY_ID: existing})

        asyncio.run(integration.async_setup_entry(hass, entry))

        assert calls["added"] == []
        assert entry.runtime_data.ap_subentry_id == SUBENTRY_ID

    def test_an_existing_group_gets_the_current_title(self, setup_fakes):
        """The prototype called it "Access points"; renames must reach it."""
        hass, calls = setup_fakes
        renamed: list = []
        hass.config_entries.async_update_subentry = lambda _entry, subentry, title: (
            renamed.append((subentry.subentry_id, title))
        )
        existing = SimpleNamespace(
            subentry_id=SUBENTRY_ID,
            subentry_type="access_points",
            title="Access points",
        )

        asyncio.run(
            integration.async_setup_entry(hass, self.entry({}, {SUBENTRY_ID: existing}))
        )

        assert renamed == [(SUBENTRY_ID, "Controller and access points")]
        assert calls["added"] == []

    def test_the_subentry_exists_even_with_the_option_off(self, setup_fakes):
        """It holds the IAP device, so it isn't tied to the AP option."""
        hass, calls = setup_fakes
        entry = self.entry({"enable_ap_devices": False})

        asyncio.run(integration.async_setup_entry(hass, entry))

        assert len(calls["added"]) == 1
        assert entry.runtime_data.ap_subentry_id is not None

    def test_option_off_forwards_base_platforms_and_cleans_up(self, setup_fakes):
        hass, calls = setup_fakes

        asyncio.run(
            integration.async_setup_entry(
                hass, self.entry({"enable_ap_devices": False})
            )
        )

        assert calls["forwarded"] == ["base"]
        assert calls["removed"] == 1

    def test_area_sensors_are_off_by_default(self, setup_fakes):
        """Opt-in; an upgraded entry without the option gets none."""
        hass, calls = setup_fakes

        asyncio.run(integration.async_setup_entry(hass, self.entry({})))

        assert calls["area_sensors"] is False
        assert calls["area_removed"] == 1  # tidies up any left from earlier

    def test_area_sensors_turned_on(self, setup_fakes):
        hass, calls = setup_fakes

        asyncio.run(
            integration.async_setup_entry(
                hass, self.entry({"enable_area_sensors": True})
            )
        )

        assert calls["area_sensors"] is True
        assert "area_removed" not in calls

    def test_area_sensors_off_removes_them(self, setup_fakes):
        hass, calls = setup_fakes

        asyncio.run(
            integration.async_setup_entry(
                hass, self.entry({"enable_area_sensors": False})
            )
        )

        assert calls["area_sensors"] is False
        assert calls["area_removed"] == 1
        assert calls["removed"] == 0  # AP devices stay

    def test_area_sensors_need_ap_devices(self, setup_fakes):
        """The option on, AP devices off: no Area sensors, as nothing has an area."""
        hass, calls = setup_fakes

        asyncio.run(
            integration.async_setup_entry(
                hass,
                self.entry({"enable_ap_devices": False, "enable_area_sensors": True}),
            )
        )

        assert calls["area_sensors"] is False
        # AP-devices cleanup removes the Area sensors too.
        assert calls["removed"] == 1

    def test_platform_lists(self):
        assert integration.BASE_PLATFORMS == [
            Platform.DEVICE_TRACKER,
            Platform.SWITCH,
            Platform.NUMBER,
            Platform.SENSOR,  # Total clients needs no 'show aps'
        ]
        assert integration.AP_PLATFORMS == [Platform.BINARY_SENSOR]

    def test_unload_uses_the_platforms_setup_forwarded(self):
        """Toggling the option must not change what the reload's unload expects."""
        unloaded: list = []

        async def _unload(entry, platforms) -> bool:  # noqa: ARG001
            unloaded.extend(platforms)
            return True

        async def _flush() -> None:
            pass

        async def _executor(func, *args):
            return func(*args)

        coordinator = SimpleNamespace(
            platforms=["base", "ap"],
            async_flush_last_seen=_flush,
            client=SimpleNamespace(logout=lambda: None, close=lambda: None),
        )
        # The option already reads off, as it does mid-reload.
        entry = SimpleNamespace(
            runtime_data=coordinator, options={"enable_ap_devices": False}, data={}
        )
        hass = SimpleNamespace(
            async_add_executor_job=_executor,
            config_entries=SimpleNamespace(async_unload_platforms=_unload),
        )

        assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
        assert unloaded == ["base", "ap"]


class TestRemoveDeviceFromUI:
    def remove(self, dev, aps=None, *, loaded=True):
        entry = SimpleNamespace(entry_id=ENTRY_ID)
        if loaded:
            entry.runtime_data = SimpleNamespace(aps=aps or {})
        return asyncio.run(
            integration.async_remove_config_entry_device(SimpleNamespace(), entry, dev)
        )

    def test_ap_that_has_gone_can_be_removed(self):
        assert self.remove(device("d", "TESTSN0001"), {"TESTSN0002": KITCHEN}) is True

    def test_ap_still_reporting_is_refused(self):
        assert self.remove(device("d", "TESTSN0001"), {"TESTSN0001": LOUNGE}) is False

    def test_ap_device_can_be_removed_while_the_entry_is_not_loaded(self):
        assert self.remove(device("d", "TESTSN0001"), loaded=False) is True

    def test_control_and_tracker_devices_are_refused(self):
        assert self.remove(device("d", ENTRY_ID)) is False
        assert self.remove(device("d", None)) is False


# ---------------------------------------------------------------------------
# Options flow: reload only when the AP option changes
# ---------------------------------------------------------------------------


BASE_OPTIONS = {
    "track_new_devices": False,
    "scan_interval": 30,
    "cleanup_enabled": True,
    "cleanup_days": 30,
    "enable_area_sensors": False,
}


class TestSetupFlowAsksAboutAPs:
    def run(self, user_input=None):
        class Flow(config_flow.ArubaIAPConfigFlow):
            pass

        flow = Flow()
        flow.hass = SimpleNamespace()
        flow._connection_data = {"host": "192.0.2.1", "username": "u", "password": "p"}
        return asyncio.run(flow.async_step_tracking(user_input))

    def test_both_options_are_on_the_form_with_their_defaults(self):
        schema = self.run()["data_schema"].schema
        defaults = {str(key): key.default() for key in schema}

        assert defaults["enable_ap_devices"] is True
        assert defaults["enable_area_sensors"] is False

    def test_the_answers_are_saved_with_the_entry(self):
        result = self.run(
            {
                **BASE_OPTIONS,
                "enable_ap_devices": False,
                "enable_area_sensors": True,
            }
        )

        assert result["data"]["enable_ap_devices"] is False
        assert result["data"]["enable_area_sensors"] is True


class TestOptionsFlowReload:
    def run(self, current_options, user_input):
        events: list[tuple] = []
        entry = SimpleNamespace(
            entry_id=ENTRY_ID,
            data={},
            options=current_options,
            state=None,  # not LOADED, so the poll interval isn't pushed
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_update_entry=lambda _entry, options: events.append(
                    ("save", options)
                ),
                async_schedule_reload=lambda entry_id: events.append(
                    ("reload", entry_id)
                ),
            )
        )

        class Flow(config_flow.ArubaIAPOptionsFlow):
            config_entry = entry

        flow = Flow()
        flow.hass = hass
        result = asyncio.run(flow.async_step_init(user_input))
        return events, result

    def test_turning_it_off_saves_then_reloads(self):
        user_input = {**BASE_OPTIONS, "enable_ap_devices": False}

        events, result = self.run({}, user_input)

        # Saved before the reload is scheduled, because HA starts the reload
        # task eagerly and it must see the new value.
        assert events == [("save", user_input), ("reload", ENTRY_ID)]
        assert result["data"] == user_input

    def test_turning_area_sensors_on_saves_then_reloads(self):
        user_input = {
            **BASE_OPTIONS,
            "enable_ap_devices": True,
            "enable_area_sensors": True,
        }

        events, _ = self.run({}, user_input)

        assert events == [("save", user_input), ("reload", ENTRY_ID)]

    def test_other_changes_do_not_reload(self):
        events, _ = self.run(
            {"enable_ap_devices": True},
            {**BASE_OPTIONS, "scan_interval": 60, "enable_ap_devices": True},
        )

        assert events == []


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


def fake_coordinator(**overrides):
    """Build a coordinator stand-in carrying what the sensors read."""
    coordinator = SimpleNamespace(
        config_entry=SimpleNamespace(
            entry_id=ENTRY_ID, data={"host": "192.0.2.5"}, options={}
        ),
        aps={LOUNGE["serial"]: LOUNGE},
        aps_ok=True,
        ap_client_counts={LOUNGE["serial"]: 3},
        ap_subentry_id=SUBENTRY_ID,
        ap_devices=True,
        area_sensors=True,
        networks={},
        networks_ok=True,
        network_client_counts={},
        last_seen={},
        ap_serial_by_name={"LoungeAP": LOUNGE["serial"]},
        removal_listeners=[],
        data={"m1": {}, "m2": {}},
        last_update_success=True,
        listeners=[],
    )
    coordinator.async_add_listener = lambda cb: (
        coordinator.listeners.append(cb) or (lambda: None)
    )
    coordinator.async_add_removal_listener = lambda cb: (
        coordinator.removal_listeners.append(cb) or (lambda: None)
    )
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


# Stand-in for the platform's "forget this serial" callback.
FORGET = set().discard


class TestClientsSensor:
    def test_value_ids_and_device(self):
        sensor = ArubaAPClientsSensor(fake_coordinator(), "TESTSN0001", FORGET)

        assert sensor.native_value == 3
        assert sensor.unique_id == f"{ENTRY_ID}_TESTSN0001_clients"
        info = sensor.device_info
        assert info["identifiers"] == {(DOMAIN, "TESTSN0001")}
        assert info["serial_number"] == "TESTSN0001"
        assert info["name"] == "LoungeAP"
        assert info["model"] == "AP-505"
        assert info["manufacturer"] == "Aruba Networks (HPE)"
        # via_device is deprecated on HA 2026.9; via_device_id needs 2026.9+.
        assert "via_device" not in info
        assert "via_device_id" not in info
        assert "connections" not in info  # no MAC: never merged with others

    def test_no_attributes_that_could_churn_the_recorder(self):
        sensor = ArubaAPClientsSensor(fake_coordinator(), "TESTSN0001", FORGET)

        assert not sensor.extra_state_attributes

    @pytest.mark.parametrize(
        "overrides",
        [
            {"last_update_success": False},  # the cluster can't be read
            {"aps_ok": False},  # the last 'show aps' failed
            {"aps": {}},  # this AP has dropped out of the list
        ],
        ids=["cluster-down", "ap-list-failed", "ap-gone"],
    )
    def test_unavailable(self, overrides):
        coordinator = fake_coordinator()
        sensor = ArubaAPClientsSensor(coordinator, "TESTSN0001", FORGET)
        for key, value in overrides.items():
            setattr(coordinator, key, value)

        assert sensor.available is False


class TestTotalClientsSensor:
    def test_counts_every_client_on_the_control_device(self):
        sensor = ArubaTotalClientsSensor(fake_coordinator())

        assert sensor.native_value == 2
        assert sensor.unique_id == f"{ENTRY_ID}_total_clients"
        assert sensor.device_info["identifiers"] == {(DOMAIN, ENTRY_ID)}

    def test_no_clients(self):
        assert ArubaTotalClientsSensor(fake_coordinator(data=None)).native_value == 0


class TestConnectivitySensor:
    def test_on_while_the_ap_is_listed(self):
        sensor = ArubaAPConnectivity(fake_coordinator(), "TESTSN0001", FORGET)

        assert sensor.available is True
        assert sensor.is_on is True
        assert sensor.unique_id == f"{ENTRY_ID}_TESTSN0001_connectivity"
        assert sensor.device_info["identifiers"] == {(DOMAIN, "TESTSN0001")}

    def test_off_but_available_when_the_ap_drops_out(self):
        """The offline case: 'show aps' worked, the AP's row is gone."""
        coordinator = fake_coordinator()
        sensor = ArubaAPConnectivity(coordinator, "TESTSN0001", FORGET)
        coordinator.aps = {}

        assert sensor.available is True
        assert sensor.is_on is False

    @pytest.mark.parametrize(
        "overrides",
        [{"last_update_success": False}, {"aps_ok": False}],
        ids=["cluster-down", "ap-list-failed"],
    )
    def test_unavailable_only_when_the_ap_list_cant_be_read(self, overrides):
        coordinator = fake_coordinator()
        sensor = ArubaAPConnectivity(coordinator, "TESTSN0001", FORGET)
        for key, value in overrides.items():
            setattr(coordinator, key, value)

        assert sensor.available is False

    def test_no_attributes_that_could_churn_the_recorder(self):
        sensor = ArubaAPConnectivity(fake_coordinator(), "TESTSN0001", FORGET)

        assert not sensor.extra_state_attributes


def poll_listeners(coordinator) -> None:
    """Fire every coordinator listener, as a completed poll does."""
    for listener in list(coordinator.listeners):
        listener()


def setup_platform(setup_entry, coordinator, *, subentry_ids=None, subentries=None):
    """Run a platform's async_setup_entry and return what it added."""
    added: list = []
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        runtime_data=coordinator,
        async_on_unload=lambda f: f,
        subentries={SUBENTRY_ID: object()} if subentries is None else subentries,
        options=coordinator.config_entry.options,
        data=coordinator.config_entry.data,
    )

    def _add(entities, config_subentry_id=None) -> None:
        added.extend(entities)
        if subentry_ids is not None:
            subentry_ids.extend(
                (type(e).__name__, config_subentry_id) for e in entities
            )

    hass = SimpleNamespace(
        bus=SimpleNamespace(async_listen=lambda *_args, **_kwargs: lambda: None)
    )
    asyncio.run(setup_entry(hass, entry, _add))
    return added


class TestPlatformSetup:
    def test_sensors_for_new_aps_are_added_on_later_polls(self, dev_reg, ent_reg):  # noqa: ARG002
        coordinator = fake_coordinator()

        added = setup_platform(sensor_setup_entry, coordinator)
        ap_sensors = [e for e in added if isinstance(e, ArubaAPClientsSensor)]
        assert [e.unique_id for e in ap_sensors] == [f"{ENTRY_ID}_TESTSN0001_clients"]

        coordinator.aps = {**coordinator.aps, KITCHEN["serial"]: KITCHEN}
        poll_listeners(coordinator)
        poll_listeners(coordinator)  # a second poll adds nothing more

        ap_sensors = [e for e in added if isinstance(e, ArubaAPClientsSensor)]
        assert [e.unique_id for e in ap_sensors[1:]] == [
            f"{ENTRY_ID}_TESTSN0002_clients"
        ]

    def test_ap_that_is_off_at_startup_still_gets_entities(self, dev_reg):
        """Seeded from the registry, so it reads Disconnected, not missing."""
        dev_reg.devices += [
            device("dev-lounge", "TESTSN0001"),
            device("dev-office", "TESTSN0006"),  # off: not in 'show aps'
            device("dev-control", ENTRY_ID),
            device("dev-other", "TESTSN0007", OTHER_ENTRY_ID),
        ]
        coordinator = fake_coordinator()  # only LoungeAP is listed

        added = setup_platform(binary_sensor_setup_entry, coordinator)

        by_serial = {e._serial: e for e in added}
        assert sorted(by_serial) == ["TESTSN0001", "TESTSN0006"]
        assert by_serial["TESTSN0006"].available is True
        assert by_serial["TESTSN0006"].is_on is False
        # Its device exists already, so name and model are left to the
        # registry rather than overwritten with nothing.
        info = by_serial["TESTSN0006"].device_info
        assert "name" not in info
        assert "model" not in info

    def test_entities_go_in_the_cluster_subentry_except_area_sensors(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,  # noqa: ARG002
    ):
        """
        Total clients too, as it is on the IAP device, which is in the group.

        Area sensors are the exception: they have no device, and a client
        entity in the group would pull tracker-linked devices in with it.
        """
        placed: list = []
        coordinator = fake_coordinator()
        # So the clients' (not yet registered) trackers count as enabled.
        coordinator.config_entry.options = {"track_new_devices": True}

        setup_platform(sensor_setup_entry, coordinator, subentry_ids=placed)
        setup_platform(
            binary_sensor_setup_entry, fake_coordinator(), subentry_ids=placed
        )

        assert dict(placed) == {
            "ArubaTotalClientsSensor": SUBENTRY_ID,
            "ArubaAPClientsSensor": SUBENTRY_ID,
            "ArubaAPConnectivity": SUBENTRY_ID,
            "ArubaClientAreaSensor": None,
        }

    def test_total_clients_without_the_ap_option(self, dev_reg, ent_reg):  # noqa: ARG002
        added = setup_platform(sensor_setup_entry, fake_coordinator(ap_devices=False))

        assert [type(e).__name__ for e in added] == ["ArubaTotalClientsSensor"]

    def test_nothing_is_added_once_the_group_was_deleted(self, dev_reg):  # noqa: ARG002
        """Between the deletion and the reload, polls must not add AP entities."""
        added = setup_platform(
            binary_sensor_setup_entry, fake_coordinator(), subentries={}
        )

        assert added == []

    def test_ap_comes_back_after_its_device_was_deleted(self, dev_reg):  # noqa: ARG002
        """A deleted retired AP that reappears must get entities again."""
        coordinator = fake_coordinator()
        added = setup_platform(binary_sensor_setup_entry, coordinator)
        known_forget = added[0]._forget

        known_forget("TESTSN0001")  # what removing the entity triggers
        poll_listeners(coordinator)

        assert [e._serial for e in added] == ["TESTSN0001", "TESTSN0001"]
