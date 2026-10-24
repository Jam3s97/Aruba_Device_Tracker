"""Tests for the per-client Area sensors."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.aruba_device_tracker import ArubaIAPCoordinator
from custom_components.aruba_device_tracker.const import DOMAIN
from custom_components.aruba_device_tracker.sensor import (
    ArubaClientAreaSensor,
    _area_changed,
)
from custom_components.aruba_device_tracker.sensor import (
    async_setup_entry as sensor_setup_entry,
)
from tests.test_ap_devices import (
    ENTRY_ID,
    LOUNGE,
    client,
    fake_coordinator,
    poll_listeners,
    setup_platform,
)
from tests.test_coordinator_cleanup import FakeRegistry, iso_ago, make_coordinator

PHONE = "aa:bb:cc:dd:ee:01"


def area_coordinator(*, track_new: bool = True, **overrides):
    """
    Build a coordinator stand-in with one connected phone on LoungeAP.

    ``track_new`` decides whether a client with no tracker registered yet
    counts as having an enabled tracker (it is about to be created enabled).
    """
    coordinator = fake_coordinator(
        **{"data": {PHONE: {**client(PHONE, "LoungeAP"), "name": "Phone"}}, **overrides}
    )
    coordinator.config_entry.options = {"track_new_devices": track_new}
    return coordinator


def tracker_entry(mac: str, *, disabled_by=None, name=None):
    """Build a tracker's entity-registry entry."""
    return SimpleNamespace(
        entity_id=f"device_tracker.{mac.replace(':', '_')}",
        unique_id=f"{ENTRY_ID}_{mac}",
        name=name,
        original_name=mac,
        disabled_by=disabled_by,
        config_entry_id=ENTRY_ID,
    )


def lounge_device(area_id: str | None):
    """Build LoungeAP's device entry, with an area or without."""
    return SimpleNamespace(
        id="dev-lounge",
        identifiers={(DOMAIN, LOUNGE["serial"])},
        config_entries={ENTRY_ID},
        config_entries_subentries={ENTRY_ID: {None}},
        area_id=area_id,
    )


def sensor_for(coordinator, mac=PHONE, name="Phone"):
    """Build a sensor bound to a fake hass, as the platform would."""
    sensor = ArubaClientAreaSensor(coordinator, mac, name)
    sensor.hass = SimpleNamespace()
    return sensor


class TestState:
    def test_the_connected_aps_area(self, dev_reg, area_reg):
        area_reg.add("lounge_room", "Lounge room")
        dev_reg.devices.append(lounge_device("lounge_room"))

        sensor = sensor_for(area_coordinator())

        assert sensor.native_value == "Lounge room"
        assert sensor.extra_state_attributes == {
            "mac": PHONE,
            "area_id": "lounge_room",
        }

    def test_follows_a_roam(self, dev_reg, area_reg):
        area_reg.add("lounge_room", "Lounge room")
        area_reg.add("kitchen", "Kitchen")
        dev_reg.devices.append(lounge_device("lounge_room"))
        dev_reg.devices.append(
            SimpleNamespace(
                id="dev-kitchen",
                identifiers={(DOMAIN, "TESTSN0002")},
                config_entries={ENTRY_ID},
                area_id="kitchen",
            )
        )
        coordinator = area_coordinator(
            ap_serial_by_name={"LoungeAP": LOUNGE["serial"], "KitchenAP": "TESTSN0002"}
        )
        sensor = sensor_for(coordinator)

        coordinator.data = {PHONE: client(PHONE, "KitchenAP")}

        assert sensor.native_value == "Kitchen"

    @pytest.mark.parametrize(
        "case",
        ["away", "unknown-ap", "ap-without-area", "no-ap-device"],
    )
    def test_unknown(self, dev_reg, area_reg, case):
        area_reg.add("lounge_room", "Lounge room")
        coordinator = area_coordinator()
        if case == "away":
            coordinator.data = {}
            dev_reg.devices.append(lounge_device("lounge_room"))
        elif case == "unknown-ap":
            coordinator.data = {PHONE: client(PHONE, "NotListedAP")}
            dev_reg.devices.append(lounge_device("lounge_room"))
        elif case == "ap-without-area":
            dev_reg.devices.append(lounge_device(None))

        sensor = sensor_for(coordinator)

        assert sensor.native_value is None
        # The MAC still links it to its tracker; no area_id without an area.
        assert sensor.extra_state_attributes == {"mac": PHONE}

    def test_mac_matches_the_trackers_attribute_and_never_changes(
        self, dev_reg, area_reg
    ):
        """Same key and value as the tracker's mac, and stable across roams."""
        area_reg.add("lounge_room", "Lounge room")
        dev_reg.devices.append(lounge_device("lounge_room"))
        coordinator = area_coordinator()
        sensor = sensor_for(coordinator)
        before = sensor.extra_state_attributes["mac"]

        coordinator.data = {}  # the phone leaves

        assert before == sensor.extra_state_attributes["mac"] == PHONE

    def test_disabled_by_default_and_named_after_the_tracker(self):
        sensor = sensor_for(area_coordinator(), name="Jo's phone")

        assert sensor.entity_registry_enabled_default is False
        assert sensor.name == "Jo's phone area"
        assert sensor.unique_id == f"{ENTRY_ID}_area_{PHONE}"
        assert sensor.device_info is None  # no device per phone


def area_sensors(added: list) -> list:
    """Return the Area sensors among what a platform added."""
    return [e for e in added if isinstance(e, ArubaClientAreaSensor)]


class TestPlatform:
    def test_one_per_client_seen_now_or_before(self, dev_reg, ent_reg):  # noqa: ARG002
        coordinator = area_coordinator(last_seen={"aa:bb:cc:dd:ee:02": "x"})

        added = area_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert sorted(e.mac for e in added) == [PHONE, "aa:bb:cc:dd:ee:02"]

    def test_a_client_remembered_and_connected_gets_one_sensor(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,  # noqa: ARG002
    ):
        """Regression: it appears in both seed lists and got two sensors."""
        coordinator = area_coordinator(last_seen={PHONE: "x"})

        added = area_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert [e.mac for e in added] == [PHONE]

    def test_none_for_a_disabled_tracker(self, dev_reg, ent_reg):  # noqa: ARG002
        """An Area sensor for a tracker the user switched off has nothing to show."""
        ent_reg.entries.append(tracker_entry(PHONE, disabled_by="user"))

        added = area_sensors(setup_platform(sensor_setup_entry, area_coordinator()))

        assert added == []

    def test_one_for_an_enabled_tracker(self, dev_reg, ent_reg):  # noqa: ARG002
        ent_reg.entries.append(tracker_entry(PHONE))
        coordinator = area_coordinator(track_new=False)

        added = area_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert [e.mac for e in added] == [PHONE]

    @pytest.mark.parametrize(("track_new", "expected"), [(True, 1), (False, 0)])
    def test_a_new_client_follows_track_new_devices(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,  # noqa: ARG002
        track_new,
        expected,
    ):
        """Its tracker isn't registered yet; Track new decides if it's enabled."""
        coordinator = area_coordinator(track_new=track_new)

        added = area_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert len(added) == expected

    def test_a_leftover_for_a_disabled_tracker_is_removed(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,
    ):
        """Otherwise it would linger as "no longer provided"."""
        ent_reg.entries += [
            tracker_entry(PHONE, disabled_by="user"),
            SimpleNamespace(
                entity_id="sensor.phone_area",
                unique_id=f"{ENTRY_ID}_area_{PHONE}",
                config_entry_id=ENTRY_ID,
            ),
        ]

        setup_platform(sensor_setup_entry, area_coordinator())

        assert ent_reg.removed == ["sensor.phone_area"]

    def test_none_while_the_option_is_off(self, dev_reg, ent_reg):  # noqa: ARG002
        """Setup sets area_sensors from the option and AP devices."""
        coordinator = area_coordinator(area_sensors=False)

        assert area_sensors(setup_platform(sensor_setup_entry, coordinator)) == []

    def test_named_from_the_trackers_registry_entry_when_offline(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,
    ):
        """An offline client has no live name: use the tracker's, even a custom one."""
        offline = "aa:bb:cc:dd:ee:02"
        ent_reg.entries.append(tracker_entry(offline, name="Kids' tablet"))
        coordinator = area_coordinator(data={}, last_seen={offline: "x"})

        [sensor] = area_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert sensor.name == "Kids' tablet area"

    def test_new_client_gets_a_sensor_once(self, dev_reg, ent_reg):  # noqa: ARG002
        coordinator = area_coordinator()
        added = setup_platform(sensor_setup_entry, coordinator)

        coordinator.data = {**coordinator.data, "aa:bb:cc:dd:ee:03": client("x", None)}
        poll_listeners(coordinator)
        poll_listeners(coordinator)

        assert [e.mac for e in area_sensors(added)].count("aa:bb:cc:dd:ee:03") == 1

    def test_a_cleaned_up_client_that_returns_gets_a_new_sensor(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,  # noqa: ARG002
    ):
        coordinator = area_coordinator()
        added = setup_platform(sensor_setup_entry, coordinator)

        for listener in coordinator.removal_listeners:
            listener({PHONE})  # stale-device cleanup removed it
        poll_listeners(coordinator)

        assert [e.mac for e in area_sensors(added)].count(PHONE) == 2


class TestRedrawFilter:
    def test_an_area_change_redraws(self):
        assert _area_changed({"action": "update", "changes": {"area_id": "old"}})

    @pytest.mark.parametrize(
        "event_data",
        [
            {"action": "update", "changes": {"name": "old"}},
            {"action": "create"},
            {"action": "remove"},
        ],
    )
    def test_other_device_updates_are_ignored(self, event_data):
        assert not _area_changed(event_data)


class TestStaleCleanup:
    def test_the_area_sensor_goes_with_its_tracker(self, monkeypatch):
        monkeypatch.setattr(
            "custom_components.aruba_device_tracker.er.async_get",
            lambda hass: hass.registry,
        )
        registry = FakeRegistry({PHONE: ENTRY_ID})
        registry._unique_ids.add(f"{ENTRY_ID}_area_{PHONE}")
        coordinator = make_coordinator(
            last_seen={PHONE: iso_ago(31)}, registry=registry, cleanup_days=30
        )

        asyncio.run(ArubaIAPCoordinator._async_cleanup_stale_devices(coordinator))

        # The fake registry names every entity "device_tracker.<unique id>".
        assert sorted(registry.removed) == [
            f"device_tracker.{ENTRY_ID}_{PHONE}",
            f"device_tracker.{ENTRY_ID}_area_{PHONE}",
        ]


ISSUE_ID = f"aps_without_area_{ENTRY_ID}"


class TestAPsWithoutAreaNotice:
    def prepare(self, dev_reg, *, enabled: bool, area_id: str | None):
        dev_reg.devices.append(lounge_device(area_id))
        coordinator = area_coordinator()
        added = area_sensors(setup_platform(sensor_setup_entry, coordinator))
        for sensor in added:
            if enabled:
                sensor.hass = SimpleNamespace()  # added to hass, i.e. enabled
        poll_listeners(coordinator)
        return coordinator

    def test_raised_once_an_area_sensor_is_enabled(self, dev_reg, ent_reg, issues):  # noqa: ARG002
        self.prepare(dev_reg, enabled=True, area_id=None)

        assert issues == {ISSUE_ID: {"aps": "LoungeAP"}}

    def test_not_raised_while_every_area_sensor_is_disabled(
        self,
        dev_reg,
        ent_reg,  # noqa: ARG002
        issues,
    ):
        self.prepare(dev_reg, enabled=False, area_id=None)

        assert issues == {}

    def test_cleared_once_every_ap_has_an_area(self, dev_reg, ent_reg, issues):  # noqa: ARG002
        coordinator = self.prepare(dev_reg, enabled=True, area_id=None)
        assert ISSUE_ID in issues

        dev_reg.devices[0].area_id = "lounge_room"
        poll_listeners(coordinator)

        assert issues == {}
