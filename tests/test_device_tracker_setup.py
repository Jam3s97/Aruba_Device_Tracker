"""
Tests for device_tracker.async_setup_entry's discovery of new devices.

Drives the real platform setup against small fakes rather than a running
Home Assistant instance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.aruba_device_tracker import device_tracker

ENTRY_ID = "entry-a"
FIRST_MAC = "aa:bb:cc:dd:ee:01"
SECOND_MAC = "aa:bb:cc:dd:ee:02"


class FakeCoordinator:
    """Exposes what the platform setup and its entities read."""

    def __init__(self, entry) -> None:
        self.config_entry = entry
        self.data: dict[str, dict] = {}
        self.last_seen: dict[str, str] = {}
        self.listeners: list = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: None

    def async_add_removal_listener(self, listener):  # noqa: ARG002
        return lambda: None


def test_track_new_is_read_when_each_device_is_discovered(monkeypatch):
    """Changing track-new at runtime applies to devices found afterwards."""
    monkeypatch.setattr(device_tracker.er, "async_get", lambda *_: None)
    monkeypatch.setattr(
        device_tracker.er, "async_entries_for_config_entry", lambda *_: []
    )
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        data={"track_new_devices": False},
        options={},
        async_on_unload=lambda *_: None,
    )
    coordinator = FakeCoordinator(entry)
    entry.runtime_data = coordinator
    added: list = []

    asyncio.run(
        device_tracker.async_setup_entry(SimpleNamespace(), entry, added.extend)
    )
    poll_listener = coordinator.listeners[0]

    # Off at setup: a device found now starts disabled.
    coordinator.data = {FIRST_MAC: {"name": "First"}}
    poll_listener()
    # The switch (or Configure) turns it on, without a reload.
    entry.options = {"track_new_devices": True}
    coordinator.data = {FIRST_MAC: {"name": "First"}, SECOND_MAC: {"name": "Second"}}
    poll_listener()

    enabled = {e.mac_address: e.entity_registry_enabled_default for e in added}
    assert enabled == {FIRST_MAC: False, SECOND_MAC: True}
