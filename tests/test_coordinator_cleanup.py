"""
Tests for ArubaIAPCoordinator cleanup, storage and auth-failure behaviour.

These exercise the real coordinator methods against small fakes rather than a
live Home Assistant instance, following the same approach as
test_device_tracker_attributes.py. Full config-flow / Store coverage would
need pytest-homeassistant-custom-component.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.aruba_device_tracker import ArubaIAPCoordinator
from custom_components.aruba_device_tracker.aruba_client import (
    ArubaAuthError,
    ArubaConnectionError,
)

ENTRY_ID = "entry-a"
OTHER_ENTRY_ID = "entry-b"
MAC = "aa:bb:cc:dd:ee:ff"


@dataclass
class FakeStore:
    """Records delayed/immediate saves instead of touching the filesystem."""

    delayed: list[dict[str, str]] = field(default_factory=list)
    saved: list[dict[str, str]] = field(default_factory=list)

    def async_delay_save(self, data_func, delay):  # noqa: ARG002
        self.delayed.append(dict(data_func()))

    async def async_save(self, data):
        self.saved.append(dict(data))


class FakeRegistry:
    """Minimal entity registry covering the three methods cleanup uses."""

    def __init__(self, entities: dict[str, str]) -> None:
        # unique_id -> owning config entry id
        self._entities = dict(entities)
        self.removed: list[str] = []

    def async_get_entity_id(self, domain, platform, unique_id):  # noqa: ARG002
        return f"device_tracker.{unique_id}" if unique_id in self._entities else None

    def async_get(self, entity_id):
        unique_id = entity_id.removeprefix("device_tracker.")
        owner = self._entities.get(unique_id)
        return None if owner is None else SimpleNamespace(config_entry_id=owner)

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)
        self._entities.pop(entity_id.removeprefix("device_tracker."), None)


def make_coordinator(
    last_seen=None,
    registry=None,
    *,
    cleanup_enabled=True,
    cleanup_days=30,
    get_clients=None,
):
    """Build a real coordinator with fakes, bypassing __init__ and hass."""
    coordinator = object.__new__(ArubaIAPCoordinator)
    coordinator.config_entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        options={"cleanup_enabled": cleanup_enabled, "cleanup_days": cleanup_days},
        data={},
    )
    coordinator.last_seen = dict(last_seen or {})
    coordinator._store = FakeStore()
    coordinator._last_cleanup = None
    coordinator._removal_listeners = []
    coordinator.data = None
    coordinator.client = SimpleNamespace(get_clients=get_clients)

    async def _executor(func, *args):
        return func(*args)

    coordinator.hass = SimpleNamespace(
        async_add_executor_job=_executor,
        registry=registry or FakeRegistry({}),
    )
    return coordinator


@pytest.fixture(autouse=True)
def _patch_entity_registry(monkeypatch):
    """Route er.async_get to whichever FakeRegistry the fake hass carries."""
    monkeypatch.setattr(
        "custom_components.aruba_device_tracker.er.async_get",
        lambda hass: hass.registry,
    )


def run_cleanup(coordinator, **kwargs):
    """Run the cleanup coroutine."""
    return asyncio.run(coordinator._async_cleanup_stale_devices(**kwargs))


def iso_ago(days: float) -> str:
    """Return an ISO timestamp `days` days before now (UTC)."""
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


class TestCleanupRemoval:
    def test_stale_device_is_removed(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )

        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == [f"device_tracker.{MAC}"]
        assert MAC not in coordinator.last_seen

    def test_fresh_device_is_kept(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(5)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )

        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == []
        assert MAC in coordinator.last_seen

    def test_cleanup_disabled_removes_nothing(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(400)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
            cleanup_enabled=False,
        )

        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == []
        assert MAC in coordinator.last_seen

    def test_invalid_timestamp_is_skipped_not_removed(self):
        coordinator = make_coordinator(
            last_seen={MAC: "not-a-timestamp"},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )

        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == []
        assert MAC in coordinator.last_seen

    def test_other_entry_entity_is_not_touched(self):
        """async_get_entity_id is not entry-scoped, so ownership must be checked."""
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40)},
            registry=FakeRegistry({MAC: OTHER_ENTRY_ID}),
        )

        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == []
        # The MAC stays in our map too: it is not ours to forget.
        assert MAC in coordinator.last_seen


class TestCleanupThrottling:
    def test_second_run_within_the_hour_is_skipped(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40), "aa:bb:cc:dd:ee:02": iso_ago(40)},
            registry=FakeRegistry({MAC: ENTRY_ID, "aa:bb:cc:dd:ee:02": ENTRY_ID}),
        )

        run_cleanup(coordinator)
        first_pass = list(coordinator.hass.registry.removed)

        # Re-add a stale record; an immediate second pass must not act on it.
        coordinator.last_seen["aa:bb:cc:dd:ee:03"] = iso_ago(40)
        coordinator.hass.registry._entities["aa:bb:cc:dd:ee:03"] = ENTRY_ID
        run_cleanup(coordinator)

        assert coordinator.hass.registry.removed == first_pass
        assert "aa:bb:cc:dd:ee:03" in coordinator.last_seen

    def test_force_bypasses_the_throttle(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )
        run_cleanup(coordinator)

        coordinator.last_seen["aa:bb:cc:dd:ee:03"] = iso_ago(40)
        coordinator.hass.registry._entities["aa:bb:cc:dd:ee:03"] = ENTRY_ID
        run_cleanup(coordinator, force=True)

        assert "aa:bb:cc:dd:ee:03" not in coordinator.last_seen


class TestRemovalListeners:
    def test_listener_receives_removed_macs(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )
        seen: list[set[str]] = []
        coordinator.async_add_removal_listener(seen.append)

        run_cleanup(coordinator)

        assert seen == [{MAC}]

    def test_unsubscribe_stops_notifications(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(40)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )
        seen: list[set[str]] = []
        unsubscribe = coordinator.async_add_removal_listener(seen.append)
        unsubscribe()

        run_cleanup(coordinator)

        assert seen == []

    def test_no_listener_call_when_nothing_removed(self):
        coordinator = make_coordinator(
            last_seen={MAC: iso_ago(1)},
            registry=FakeRegistry({MAC: ENTRY_ID}),
        )
        seen: list[set[str]] = []
        coordinator.async_add_removal_listener(seen.append)

        run_cleanup(coordinator)

        assert seen == []


class TestUpdateData:
    def test_successful_poll_schedules_a_delayed_save_not_an_immediate_one(self):
        coordinator = make_coordinator(get_clients=lambda: {MAC: {"name": "TV"}})

        result = asyncio.run(coordinator._async_update_data())

        assert result == {MAC: {"name": "TV"}}
        assert MAC in coordinator.last_seen
        # Delayed save only: an immediate write on every poll is what we removed.
        assert len(coordinator._store.delayed) == 1
        assert coordinator._store.saved == []

    def test_empty_result_does_not_schedule_a_save(self):
        coordinator = make_coordinator(get_clients=dict)

        assert asyncio.run(coordinator._async_update_data()) == {}
        assert coordinator._store.delayed == []

    def test_none_result_keeps_last_known_data(self):
        coordinator = make_coordinator(get_clients=lambda: None)
        coordinator.data = {MAC: {"name": "TV"}}

        assert asyncio.run(coordinator._async_update_data()) == {MAC: {"name": "TV"}}

    def test_auth_error_becomes_config_entry_auth_failed(self):
        def _boom():
            raise ArubaAuthError("bad creds")

        coordinator = make_coordinator(get_clients=_boom)

        with pytest.raises(ConfigEntryAuthFailed):
            asyncio.run(coordinator._async_update_data())

    def test_connection_error_becomes_update_failed(self):
        def _boom():
            raise ArubaConnectionError("unreachable")

        coordinator = make_coordinator(get_clients=_boom)

        with pytest.raises(UpdateFailed):
            asyncio.run(coordinator._async_update_data())

    def test_unexpected_error_message_is_generic(self):
        def _boom():
            msg = "https://ap/rest/show-cmd?sid=super-secret-sid"
            raise RuntimeError(msg)

        coordinator = make_coordinator(get_clients=_boom)

        with pytest.raises(UpdateFailed) as excinfo:
            asyncio.run(coordinator._async_update_data())

        # The raw exception text can embed the URL, and the URL carries the sid.
        assert "super-secret-sid" not in str(excinfo.value)
