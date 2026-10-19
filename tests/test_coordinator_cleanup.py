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
from custom_components.aruba_device_tracker.const import (
    UNAVAILABLE_AFTER_FAILED_POLLS,
)
from custom_components.aruba_device_tracker.utils import tracker_unique_id

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
    """Minimal entity registry covering the two methods cleanup uses."""

    def __init__(self, entities: dict[str, str]) -> None:
        # MAC -> owning config entry id, held under the entry-scoped unique ID
        # the real registry stores.
        self._unique_ids: set[str] = set()
        self.removed: list[str] = []
        for mac, owner in entities.items():
            self.add(mac, owner)

    def add(self, mac: str, owner: str) -> None:
        self._unique_ids.add(tracker_unique_id(owner, mac))

    def async_get_entity_id(self, domain, platform, unique_id):  # noqa: ARG002
        return f"device_tracker.{unique_id}" if unique_id in self._unique_ids else None

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)
        self._unique_ids.discard(entity_id.removeprefix("device_tracker."))


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
    coordinator._failed_polls = 0
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

        assert coordinator.hass.registry.removed == [
            f"device_tracker.{tracker_unique_id(ENTRY_ID, MAC)}"
        ]
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

    def test_other_entry_entity_for_the_same_mac_is_not_touched(self):
        """Two APs seeing one device each own an entity; only ours is removed."""
        registry = FakeRegistry({MAC: OTHER_ENTRY_ID})
        registry.add(MAC, ENTRY_ID)
        coordinator = make_coordinator(last_seen={MAC: iso_ago(40)}, registry=registry)

        run_cleanup(coordinator)

        assert registry.removed == [
            f"device_tracker.{tracker_unique_id(ENTRY_ID, MAC)}"
        ]
        assert tracker_unique_id(OTHER_ENTRY_ID, MAC) in registry._unique_ids


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
        coordinator.hass.registry.add("aa:bb:cc:dd:ee:03", ENTRY_ID)
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
        coordinator.hass.registry.add("aa:bb:cc:dd:ee:03", ENTRY_ID)
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

    def test_none_result_on_first_refresh_fails_instead_of_marking_all_away(self):
        # No last-known data yet: returning {} would flip every restored
        # device to away, so the first refresh must fail and be retried.
        coordinator = make_coordinator(get_clients=lambda: None)

        with pytest.raises(UpdateFailed):
            asyncio.run(coordinator._async_update_data())

    def test_sustained_failure_raises_on_the_third_poll(self):
        coordinator = make_coordinator(get_clients=lambda: None)
        coordinator.data = {MAC: {"name": "TV"}}

        # Blips keep last-known data...
        for _ in range(UNAVAILABLE_AFTER_FAILED_POLLS - 1):
            assert asyncio.run(coordinator._async_update_data()) == {
                MAC: {"name": "TV"}
            }

        # ...but a sustained outage fails, so the trackers go unavailable.
        with pytest.raises(UpdateFailed):
            asyncio.run(coordinator._async_update_data())
        # And keeps failing until the AP answers again.
        with pytest.raises(UpdateFailed):
            asyncio.run(coordinator._async_update_data())

    def test_successful_poll_resets_the_failure_count(self):
        responses = iter([None, None, {MAC: {"name": "TV"}}, None, None])
        coordinator = make_coordinator(get_clients=lambda: next(responses))
        coordinator.data = {MAC: {"name": "TV"}}

        # Two failures, a success, then two more failures: never three in a
        # row, so none of these polls may raise.
        for _ in range(5):
            assert asyncio.run(coordinator._async_update_data()) == {
                MAC: {"name": "TV"}
            }

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
