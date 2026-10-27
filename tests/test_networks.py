"""
Tests for the per-network (SSID) client counts.

Same approach as test_ap_devices.py: the real coordinator and sensor code run
against small fakes rather than a live Home Assistant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.aruba_device_tracker.aruba_client import (
    ArubaAuthError,
    ArubaNetwork,
)
from custom_components.aruba_device_tracker.const import DOMAIN
from custom_components.aruba_device_tracker.sensor import (
    ArubaNetworkClientsSensor,
)
from custom_components.aruba_device_tracker.sensor import (
    async_setup_entry as sensor_setup_entry,
)
from tests.test_ap_devices import (
    ENTRY_ID,
    SUBENTRY_ID,
    client,
    fake_coordinator,
    poll,
    poll_listeners,
    setup_platform,
)
from tests.test_coordinator_cleanup import make_coordinator


def network(
    profile: str, essid: str | None = None, *, enabled: bool = True, clients=None
) -> ArubaNetwork:
    """Build a parsed network; the ESSID defaults to the profile name."""
    return ArubaNetwork(
        profile=profile, essid=essid or profile, enabled=enabled, clients=clients
    )


HOME = network("Home")
VISITORS = network("Visitors", "VisitorNet")
SPARE = network("spare", "Spare", enabled=False)


def make(get_networks, clients_per_poll=None, **kwargs):
    """Build a real coordinator whose polls return the given client lists."""
    polls = iter(clients_per_poll or [{}] * 10)
    return make_coordinator(
        cleanup_enabled=False,
        get_clients=lambda: next(polls),
        get_networks=get_networks,
        **kwargs,
    )


def as_dict(*networks: ArubaNetwork) -> dict[str, ArubaNetwork]:
    """Key networks by profile, as get_networks returns them."""
    return {n["profile"]: n for n in networks}


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class TestFetching:
    def test_fetched_after_the_client_list_and_the_ap_list(self, dev_reg):  # noqa: ARG002
        calls: list[str] = []
        coordinator = make_coordinator(
            cleanup_enabled=False,
            get_clients=lambda: calls.append("clients") or {},
            get_aps=lambda: calls.append("aps") or {},
            get_networks=lambda: calls.append("networks") or {},
            ap_devices=True,
        )

        poll(coordinator)

        assert calls == ["clients", "aps", "networks"]

    def test_fetched_without_ap_devices_too(self):
        """Per-network counts are a controller feature, not an AP one."""
        coordinator = make(lambda: as_dict(HOME))

        poll(coordinator)

        assert coordinator.networks_ok is True
        assert list(coordinator.networks) == ["Home"]

    def test_fetched_at_most_every_30_minutes(self):
        calls: list[str] = []
        coordinator = make(lambda: calls.append("networks") or as_dict(HOME))

        poll(coordinator)
        poll(coordinator)
        assert calls == ["networks"]

        coordinator._last_network_refresh = datetime.now(tz=UTC) - timedelta(minutes=31)
        poll(coordinator)
        assert calls == ["networks", "networks"]

    def test_a_failed_fetch_is_retried_on_the_next_poll(self):
        results = iter([None, as_dict(HOME)])
        coordinator = make(lambda: next(results))

        poll(coordinator)
        assert coordinator.networks_ok is False
        poll(coordinator)
        assert coordinator.networks_ok is True

    def test_a_failed_fetch_keeps_the_last_list(self):
        results = iter([as_dict(HOME), None])
        coordinator = make(lambda: next(results))

        poll(coordinator)
        coordinator._last_network_refresh = None  # make the next poll fetch
        poll(coordinator)

        assert coordinator.networks_ok is False
        assert list(coordinator.networks) == ["Home"]

    def test_skipped_when_the_client_list_failed(self):
        calls: list[str] = []
        coordinator = make_coordinator(
            cleanup_enabled=False,
            get_clients=lambda: None,
            get_networks=lambda: calls.append("networks") or {},
        )
        coordinator.data = {}  # not the first refresh

        poll(coordinator)

        assert calls == []

    def test_rejected_login_starts_reauth(self):
        def _get_networks():
            raise ArubaAuthError("rejected")

        coordinator = make(_get_networks)

        with pytest.raises(ConfigEntryAuthFailed):
            poll(coordinator)


class TestCounts:
    def test_counted_by_broadcast_ssid_including_quiet_networks(self):
        clients = {
            "m1": client("m1", "LoungeAP", "Home"),
            "m2": client("m2", "LoungeAP", "Home"),
            "m3": client("m3", "LoungeAP", "VisitorNet"),  # profile "Visitors"
            "m4": client("m4", "LoungeAP", "SomeoneElses"),  # not configured
            "m5": client("m5", "LoungeAP", None),
        }
        coordinator = make(lambda: as_dict(HOME, VISITORS, SPARE), [clients])

        poll(coordinator)

        assert coordinator.network_client_counts == {
            "Home": 2,
            "Visitors": 1,
            "spare": 0,
        }

    def test_recounted_every_poll_between_fetches(self):
        coordinator = make(
            lambda: as_dict(HOME),
            [{"m1": client("m1", "LoungeAP", "Home")}, {}],
        )

        poll(coordinator)
        assert coordinator.network_client_counts == {"Home": 1}
        poll(coordinator)  # no fetch this time
        assert coordinator.network_client_counts == {"Home": 0}

    def test_duplicate_ssid_warns_once(self, caplog):
        coordinator = make(lambda: as_dict(HOME, network("Home-5G", "Home")))

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                coordinator._last_network_refresh = None
                poll(coordinator)

        assert caplog.text.count("both broadcast the SSID 'Home'") == 1

    def test_controller_count_compared_only_after_a_fetch(self, caplog):
        """Between fetches its figures are up to 30 minutes old."""
        coordinator = make(lambda: as_dict(network("Home", clients=5)))

        with caplog.at_level(logging.DEBUG):
            poll(coordinator)
            poll(coordinator)

        assert caplog.text.count("reports 5 client") == 1


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


def network_coordinator(**overrides):
    """Build a coordinator stand-in carrying networks, for the sensors."""
    coordinator = fake_coordinator(
        networks=as_dict(HOME, VISITORS, SPARE),
        network_client_counts={"Home": 4, "Visitors": 0, "spare": 0},
        ap_devices=False,
    )
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


class TestNetworkSensor:
    def test_value_name_and_ids(self):
        sensor = ArubaNetworkClientsSensor(network_coordinator(), "Home")

        assert sensor.native_value == 4
        assert sensor.available is True
        assert sensor.unique_id == f"{ENTRY_ID}_network_Home"
        assert sensor.device_info["identifiers"] == {(DOMAIN, ENTRY_ID)}
        assert sensor.translation_key == "network_clients"
        assert sensor.translation_placeholders == {"essid": "Home"}

    def test_named_after_the_ssid_not_the_profile(self):
        sensor = ArubaNetworkClientsSensor(network_coordinator(), "Visitors")

        assert sensor.translation_placeholders == {"essid": "VisitorNet"}

    def test_name_follows_an_ssid_rename(self):
        coordinator = network_coordinator()
        sensor = ArubaNetworkClientsSensor(coordinator, "Visitors")

        coordinator.networks = as_dict(network("Visitors", "Guests"))

        assert sensor.translation_placeholders == {"essid": "Guests"}

    def test_quiet_network_reads_zero(self):
        sensor = ArubaNetworkClientsSensor(network_coordinator(), "Visitors")

        assert sensor.available is True
        assert sensor.native_value == 0

    @pytest.mark.parametrize(
        ("profile", "overrides"),
        [
            ("spare", {}),  # disabled on the controller
            ("Home", {"networks_ok": False}),  # the last fetch failed
            ("Home", {"last_update_success": False}),  # cluster can't be read
            ("Gone", {}),  # not listed
        ],
        ids=["disabled", "fetch-failed", "cluster-down", "not-listed"],
    )
    def test_unavailable(self, profile, overrides):
        sensor = ArubaNetworkClientsSensor(network_coordinator(**overrides), profile)

        assert sensor.available is False

    def test_no_attributes_that_could_churn_the_recorder(self):
        sensor = ArubaNetworkClientsSensor(network_coordinator(), "Home")

        assert not sensor.extra_state_attributes


def network_sensors(added: list) -> list:
    """Return the network sensors among what a platform added."""
    return [e for e in added if isinstance(e, ArubaNetworkClientsSensor)]


class TestNetworkSensorLifecycle:
    def test_one_sensor_per_network_in_the_cluster_group(self, dev_reg, ent_reg):  # noqa: ARG002
        subentry_ids: list = []

        added = setup_platform(
            sensor_setup_entry, network_coordinator(), subentry_ids=subentry_ids
        )

        assert sorted(e._profile for e in network_sensors(added)) == [
            "Home",
            "Visitors",
            "spare",
        ]
        assert {subentry for _, subentry in subentry_ids} == {SUBENTRY_ID}

    def test_deleted_network_is_removed_after_a_successful_read(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,
    ):
        coordinator = network_coordinator()
        setup_platform(sensor_setup_entry, coordinator)
        ent_reg.entries.append(
            SimpleNamespace(
                entity_id="sensor.clients_on_spare",
                unique_id=f"{ENTRY_ID}_network_spare",
                domain="sensor",
                config_entry_id=ENTRY_ID,
            )
        )

        coordinator.networks = as_dict(HOME, VISITORS)  # 'spare' deleted
        poll_listeners(coordinator)

        assert ent_reg.removed == ["sensor.clients_on_spare"]

    def test_a_failed_read_never_removes_anything(self, dev_reg, ent_reg):  # noqa: ARG002
        coordinator = network_coordinator()
        setup_platform(sensor_setup_entry, coordinator)

        coordinator.networks_ok = False
        coordinator.networks = {}
        poll_listeners(coordinator)

        assert ent_reg.removed == []

    def test_a_new_network_gets_a_sensor(self, dev_reg, ent_reg):  # noqa: ARG002
        coordinator = network_coordinator()
        added = setup_platform(sensor_setup_entry, coordinator)

        coordinator.networks = {**coordinator.networks, **as_dict(network("Office"))}
        poll_listeners(coordinator)
        poll_listeners(coordinator)  # added once

        assert [e._profile for e in network_sensors(added)].count("Office") == 1

    def test_registered_sensors_stay_when_the_first_fetch_failed(
        self,
        dev_reg,  # noqa: ARG002
        ent_reg,
    ):
        """They read unavailable rather than disappearing until it succeeds."""
        ent_reg.entries.append(
            SimpleNamespace(
                entity_id="sensor.clients_on_home",
                unique_id=f"{ENTRY_ID}_network_Home",
                domain="sensor",
                config_entry_id=ENTRY_ID,
            )
        )
        coordinator = network_coordinator(networks={}, networks_ok=False)

        added = network_sensors(setup_platform(sensor_setup_entry, coordinator))

        assert [e._profile for e in added] == ["Home"]
        assert added[0].available is False
        assert added[0].translation_placeholders == {"essid": "Home"}
