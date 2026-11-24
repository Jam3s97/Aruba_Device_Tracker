"""
Tests for async_setup_entry's handling of a failed first refresh.

Like test_coordinator_cleanup.py, these drive the real coroutine against small
fakes rather than a live Home Assistant instance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

import custom_components.aruba_device_tracker as integration
from custom_components.aruba_device_tracker import coordinator as coordinator_module
from custom_components.aruba_device_tracker.const import LEGACY_STORAGE_KEY


class FakeClient:
    """Records the lifecycle calls async_setup_entry makes on the client."""

    calls: list[str]

    def __init__(self, **kwargs) -> None:  # noqa: ARG002
        self.calls = []

    def login(self) -> None:
        self.calls.append("login")

    def logout(self) -> None:
        self.calls.append("logout")

    def close(self) -> None:
        self.calls.append("close")


class FailingCoordinator:
    """A coordinator whose first refresh fails, as it does when the AP is down."""

    def __init__(self, *, client, **kwargs) -> None:  # noqa: ARG002
        self.client = client

    async def async_load_last_seen(self) -> None:
        pass

    async def async_config_entry_first_refresh(self) -> None:
        raise ConfigEntryNotReady("AP did not answer")


def test_failed_first_refresh_releases_the_ap_session(monkeypatch):
    """A failed first refresh must log out and close the client before raising."""
    clients: list[FakeClient] = []

    def _make_client(**kwargs) -> FakeClient:
        clients.append(FakeClient(**kwargs))
        return clients[-1]

    monkeypatch.setattr(integration, "ArubaIAPClient", _make_client)
    monkeypatch.setattr(integration, "ArubaIAPCoordinator", FailingCoordinator)

    async def _executor(func, *args):
        return func(*args)

    hass = SimpleNamespace(async_add_executor_job=_executor)
    entry = SimpleNamespace(
        entry_id="entry-a",
        data={"host": "192.168.1.1", "username": "admin", "password": "pw"},
        options={},
    )

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(integration.async_setup_entry(hass, entry))

    # Without the explicit release, each setup retry would leave a live session
    # on the AP, since async_unload_entry never runs for a failed setup.
    assert clients[0].calls == ["login", "logout", "close"]


class RecordingStore:
    """Stands in for HA's Store, recording which keys were created and removed."""

    created: ClassVar[list[str]] = []
    removed: ClassVar[list[str]] = []

    def __init__(self, hass, version, key) -> None:  # noqa: ARG002
        self.key = key
        RecordingStore.created.append(key)

    async def async_remove(self) -> None:
        RecordingStore.removed.append(self.key)


def test_remove_entry_deletes_its_last_seen_file(monkeypatch):
    """Removing an entry deletes its own last-seen file, and only that file."""
    RecordingStore.created = []
    RecordingStore.removed = []
    monkeypatch.setattr(coordinator_module, "Store", RecordingStore)

    entry = SimpleNamespace(entry_id="entry-a")
    asyncio.run(integration.async_remove_entry(SimpleNamespace(), entry))

    # The same key the coordinator writes to, so the removed file is the real one.
    assert RecordingStore.removed == ["aruba_device_tracker.entry-a.last_seen"]
    # The legacy shared file stays, so a downgrade still finds its data.
    assert LEGACY_STORAGE_KEY not in RecordingStore.created


def test_coordinator_and_removal_use_the_same_store_key(monkeypatch):
    """The coordinator's store and the one removed must share a key."""
    RecordingStore.created = []
    monkeypatch.setattr(coordinator_module, "Store", RecordingStore)

    store = coordinator_module._last_seen_store(SimpleNamespace(), "entry-b")

    assert store.key == "aruba_device_tracker.entry-b.last_seen"
