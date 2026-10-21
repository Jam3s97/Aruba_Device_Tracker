"""
Tests for the IAP control device's switch and number entities.

Like the other entity tests, these use small fakes rather than a running
Home Assistant instance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.aruba_device_tracker import switch as switch_platform
from custom_components.aruba_device_tracker.number import ArubaPollIntervalNumber
from custom_components.aruba_device_tracker.switch import (
    ArubaCleanupSwitch,
    ArubaTrackNewSwitch,
)

ENTRY_ID = "entry-a"


class FakeEntry:
    """A config entry that records update listeners."""

    def __init__(self, options=None) -> None:
        self.entry_id = ENTRY_ID
        self.data = {"host": "192.168.1.1"}
        self.options = dict(options or {})
        self.update_listeners: list = []

    def add_update_listener(self, listener):
        self.update_listeners.append(listener)
        return lambda: self.update_listeners.remove(listener)


class FakeRegistry:
    """Records the disabled_by updates the switch makes."""

    def __init__(self, entries) -> None:
        self.entries = entries
        self.enabled: list[str] = []

    def async_update_entity(self, entity_id, *, disabled_by):
        assert disabled_by is None
        self.enabled.append(entity_id)


class TestTrackNewSwitchReEnable:
    @pytest.fixture
    def registry(self, monkeypatch):
        entries = [
            SimpleNamespace(
                domain="device_tracker",
                entity_id="device_tracker.found_while_off",
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            ),
            SimpleNamespace(
                domain="device_tracker",
                entity_id="device_tracker.hidden_by_user",
                disabled_by=er.RegistryEntryDisabler.USER,
            ),
            SimpleNamespace(
                domain="device_tracker",
                entity_id="device_tracker.device_disabled",
                disabled_by=er.RegistryEntryDisabler.DEVICE,
            ),
            SimpleNamespace(
                domain="device_tracker",
                entity_id="device_tracker.already_enabled",
                disabled_by=None,
            ),
            SimpleNamespace(
                domain="switch",
                entity_id="switch.some_control",
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            ),
        ]
        registry = FakeRegistry(entries)
        monkeypatch.setattr(switch_platform.er, "async_get", lambda *_: registry)
        monkeypatch.setattr(
            switch_platform.er,
            "async_entries_for_config_entry",
            lambda reg, *_: reg.entries,
        )
        return registry

    def test_only_integration_disabled_trackers_are_enabled(self, registry):
        """A tracker the user hid on purpose must stay hidden."""
        entity = ArubaTrackNewSwitch(FakeEntry())
        entity.hass = SimpleNamespace()

        asyncio.run(entity._enable_all_tracker_entities())

        assert registry.enabled == ["device_tracker.found_while_off"]

    def test_configure_turning_track_new_on_enables_integration_disabled(
        self, registry
    ):
        """Changing the option under Configure also restores tracked entities."""
        entry = FakeEntry({"track_new_devices": False})
        entity = ArubaTrackNewSwitch(entry)
        entity.hass = SimpleNamespace()
        entity.async_write_ha_state = lambda: None

        asyncio.run(entity.async_added_to_hass())
        entry.options = {"track_new_devices": True}

        asyncio.run(entry.update_listeners[0](SimpleNamespace(), entry))

        assert registry.enabled == ["device_tracker.found_while_off"]


class TestOptionsEntitiesRedraw:
    @pytest.mark.parametrize(
        "entity_cls", [ArubaTrackNewSwitch, ArubaCleanupSwitch, ArubaPollIntervalNumber]
    )
    def test_options_change_redraws_the_entity(self, entity_cls):
        """A change made under Configure must show without a state write."""
        initial_options = (
            {"track_new_devices": True} if entity_cls is ArubaTrackNewSwitch else {}
        )
        entry = FakeEntry(initial_options)
        entity = entity_cls(entry)
        writes: list[None] = []
        entity.async_write_ha_state = lambda: writes.append(None)

        asyncio.run(entity.async_added_to_hass())
        assert len(entry.update_listeners) == 1

        entry.options = {"track_new_devices": True, "scan_interval": 60}
        asyncio.run(entry.update_listeners[0](SimpleNamespace(), entry))

        assert writes == [None]

    def test_listener_is_removed_with_the_entity(self):
        entry = FakeEntry()
        entity = ArubaPollIntervalNumber(entry)

        asyncio.run(entity.async_added_to_hass())
        asyncio.run(entity.async_will_remove_from_hass())
        for remove in entity._on_remove or []:
            remove()

        assert entry.update_listeners == []
