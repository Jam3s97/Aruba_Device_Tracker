"""Tests for ArubaClientEntity.extra_state_attributes (last_seen / cleanup)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from custom_components.aruba_device_tracker.const import (
    ATTR_ACCESS_POINT,
    ATTR_DAYS_UNTIL_CLEANUP,
    ATTR_LAST_SEEN,
)
from custom_components.aruba_device_tracker.device_tracker import ArubaClientEntity

TEST_MAC = "aa:bb:cc:dd:ee:ff"


@dataclass
class FakeCoordinator:
    """Minimal stand-in exposing only what extra_state_attributes reads."""

    data: dict[str, dict] | None = None
    last_seen: dict[str, str] = field(default_factory=dict)
    cleanup_enabled: bool = True
    cleanup_days: int = 30


def make_entity(coordinator: FakeCoordinator, mac: str = TEST_MAC) -> ArubaClientEntity:
    """Build an ArubaClientEntity against a fake coordinator, bypassing hass."""
    return ArubaClientEntity(
        coordinator=coordinator,
        mac=mac,
        initial_name="Test Device",
        new_device_defaults_tracked=True,
    )


def iso_ago(days: float) -> str:
    """Return an ISO timestamp `days` days before now (UTC)."""
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


def test_no_last_seen_entry_yields_no_attrs():
    """A MAC with no last_seen record and no live data should have no attrs."""
    coordinator = FakeCoordinator(data=None, last_seen={})
    entity = make_entity(coordinator)

    assert entity.extra_state_attributes == {}


def test_last_seen_present_cleanup_disabled():
    """days_until_cleanup should be omitted entirely when cleanup is off."""
    coordinator = FakeCoordinator(
        data=None,
        last_seen={TEST_MAC: iso_ago(5)},
        cleanup_enabled=False,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert ATTR_LAST_SEEN in attrs
    assert ATTR_DAYS_UNTIL_CLEANUP not in attrs


def test_days_until_cleanup_calculated():
    """days_until_cleanup should reflect cleanup_days minus elapsed days."""
    coordinator = FakeCoordinator(
        data=None,
        last_seen={TEST_MAC: iso_ago(5)},
        cleanup_enabled=True,
        cleanup_days=30,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert attrs[ATTR_DAYS_UNTIL_CLEANUP] == 25


def test_days_until_cleanup_clamped_at_zero():
    """A device already past its cleanup threshold should clamp to 0, not negative."""
    coordinator = FakeCoordinator(
        data=None,
        last_seen={TEST_MAC: iso_ago(40)},
        cleanup_enabled=True,
        cleanup_days=30,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert attrs[ATTR_DAYS_UNTIL_CLEANUP] == 0


def test_invalid_last_seen_timestamp_skips_cleanup_calc():
    """A malformed stored timestamp shouldn't raise; last_seen still surfaces."""
    coordinator = FakeCoordinator(
        data=None,
        last_seen={TEST_MAC: "not-a-real-timestamp"},
        cleanup_enabled=True,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert attrs[ATTR_LAST_SEEN] == "not-a-real-timestamp"
    assert ATTR_DAYS_UNTIL_CLEANUP not in attrs


def test_online_device_shows_iap_attrs_but_not_last_seen():
    """
    A connected device shows session details only.

    last_seen is omitted while connected: it would equal ~now and change on
    every poll, costing a recorder row each time without conveying anything
    the `home` state doesn't already.
    """
    coordinator = FakeCoordinator(
        data={TEST_MAC: {"access_point": "ap-lounge", "essid": "HomeWiFi"}},
        last_seen={TEST_MAC: iso_ago(0)},
        cleanup_enabled=True,
        cleanup_days=30,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert attrs[ATTR_ACCESS_POINT] == "ap-lounge"
    assert ATTR_LAST_SEEN not in attrs
    assert ATTR_DAYS_UNTIL_CLEANUP not in attrs


class TestRecorderChurn:
    """
    Attributes must be stable while a device's situation is unchanged.

    HA writes a recorder row whenever the state or any attribute differs from
    the previous one, so a per-poll attribute costs one row per device per
    poll. These tests pin that down.
    """

    def test_signal_and_speed_are_never_published(self):
        coordinator = FakeCoordinator(
            data={TEST_MAC: {"signal": "62", "speed": "130M", "essid": "HomeWiFi"}},
            last_seen={TEST_MAC: iso_ago(0)},
        )
        attrs = make_entity(coordinator).extra_state_attributes

        assert "signal" not in attrs
        assert "speed" not in attrs

    def test_connected_attrs_stable_when_signal_and_speed_change(self):
        """The whole point: a fluctuating radio reading must not move attrs."""
        coordinator = FakeCoordinator(
            data={
                TEST_MAC: {
                    "access_point": "ap-lounge",
                    "essid": "HomeWiFi",
                    "ip": "192.168.1.50",
                    "os": "iOS",
                    "channel": "36",
                    "signal": "62",
                    "speed": "130M",
                }
            },
            last_seen={TEST_MAC: iso_ago(0)},
        )
        entity = make_entity(coordinator)
        before = entity.extra_state_attributes

        # Next poll: radio readings moved, last_seen advanced, nothing else.
        coordinator.data[TEST_MAC]["signal"] = "48"
        coordinator.data[TEST_MAC]["speed"] = "86M"
        coordinator.last_seen[TEST_MAC] = iso_ago(0)

        assert entity.extra_state_attributes == before

    def test_away_device_attrs_stable_across_polls(self):
        coordinator = FakeCoordinator(data={}, last_seen={TEST_MAC: iso_ago(3)})
        entity = make_entity(coordinator)
        before = entity.extra_state_attributes

        assert entity.extra_state_attributes == before
        assert before[ATTR_LAST_SEEN] == coordinator.last_seen[TEST_MAC]

    def test_away_device_exposes_frozen_last_seen(self):
        frozen = iso_ago(3)
        coordinator = FakeCoordinator(
            data={}, last_seen={TEST_MAC: frozen}, cleanup_days=30
        )
        attrs = make_entity(coordinator).extra_state_attributes

        assert attrs[ATTR_LAST_SEEN] == frozen
        assert attrs[ATTR_DAYS_UNTIL_CLEANUP] == 27
        # No live session details for a device that isn't there.
        assert ATTR_ACCESS_POINT not in attrs


class TestScannerEntityContract:
    """ScannerEntity surfaces ip_address/hostname as standard state attributes."""

    def test_ip_address_populated_while_connected(self):
        coordinator = FakeCoordinator(
            data={TEST_MAC: {"name": "TV", "ip": "192.168.1.50"}},
            last_seen={TEST_MAC: iso_ago(0)},
        )
        entity = make_entity(coordinator)

        assert entity.ip_address == "192.168.1.50"
        assert entity.hostname == "TV"

    def test_ip_address_none_while_away(self):
        coordinator = FakeCoordinator(data={}, last_seen={TEST_MAC: iso_ago(3)})
        entity = make_entity(coordinator)

        assert entity.ip_address is None
        assert entity.hostname is None

    def test_ip_address_none_when_coordinator_has_no_data(self):
        coordinator = FakeCoordinator(data=None, last_seen={})
        entity = make_entity(coordinator)

        assert entity.ip_address is None
        assert entity.hostname is None

    def test_unique_id_is_the_mac_not_attr_unique_id(self):
        """ScannerEntity derives unique_id from mac_address, not _attr_unique_id."""
        coordinator = FakeCoordinator(data=None, last_seen={})
        entity = make_entity(coordinator)

        assert entity.unique_id == TEST_MAC
        assert entity.mac_address == TEST_MAC
