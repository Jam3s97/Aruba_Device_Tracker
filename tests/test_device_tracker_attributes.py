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
        entry=None,
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


def test_online_device_includes_last_seen_and_iap_attrs():
    """A currently-connected device should show both last_seen and IAP details."""
    coordinator = FakeCoordinator(
        data={TEST_MAC: {"access_point": "ap-lounge", "essid": "HomeWiFi"}},
        last_seen={TEST_MAC: iso_ago(0)},
        cleanup_enabled=True,
        cleanup_days=30,
    )
    entity = make_entity(coordinator)
    attrs = entity.extra_state_attributes

    assert attrs[ATTR_ACCESS_POINT] == "ap-lounge"
    assert ATTR_LAST_SEEN in attrs
    assert attrs[ATTR_DAYS_UNTIL_CLEANUP] == 30
