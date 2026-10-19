"""
Tests for async_migrate_entry's per-entry tracker unique-ID migration.

The registry walk is faked: er.async_migrate_entries is patched to apply the
migration callback to a small list of registry entries, which is the part of
the behaviour this integration owns.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.aruba_device_tracker as integration

ENTRY_ID = "entry-a"
MAC = "aa:bb:cc:dd:ee:ff"


class FakeConfigEntries:
    """Records async_update_entry calls and applies them to the entry."""

    def async_update_entry(self, entry, **changes) -> None:
        for key, value in changes.items():
            setattr(entry, key, value)


@pytest.fixture
def registry_entries(monkeypatch):
    """Patch er.async_migrate_entries to run against an in-memory list."""
    entries = [
        SimpleNamespace(domain="device_tracker", unique_id=MAC),
        SimpleNamespace(domain="switch", unique_id=f"{ENTRY_ID}_track_new_devices"),
    ]

    async def _migrate(hass, config_entry_id, callback):  # noqa: ARG001
        for reg_entry in entries:
            if (updates := callback(reg_entry)) is not None:
                reg_entry.unique_id = updates["new_unique_id"]

    monkeypatch.setattr(integration.er, "async_migrate_entries", _migrate)
    return entries


def make_entry(version=1, minor_version=1):
    """Build a config entry at the given version."""
    return SimpleNamespace(
        entry_id=ENTRY_ID, version=version, minor_version=minor_version
    )


def migrate(entry):
    """Run async_migrate_entry against a fake hass."""
    hass = SimpleNamespace(config_entries=FakeConfigEntries())
    return asyncio.run(integration.async_migrate_entry(hass, entry))


class TestMigrateEntry:
    def test_tracker_unique_ids_are_scoped_to_the_entry(self, registry_entries):
        entry = make_entry()

        assert migrate(entry) is True

        tracker, switch = registry_entries
        assert tracker.unique_id == f"{ENTRY_ID}_{MAC}"
        # Control entities were already entry-scoped and must not be rewritten.
        assert switch.unique_id == f"{ENTRY_ID}_track_new_devices"
        assert entry.minor_version == 2

    def test_current_entry_is_left_alone(self, registry_entries):
        entry = make_entry(minor_version=2)

        assert migrate(entry) is True

        assert registry_entries[0].unique_id == MAC

    def test_newer_major_version_is_refused(self, registry_entries):
        """An entry written by a future major version can't be read here."""
        assert migrate(make_entry(version=2)) is False
        assert registry_entries[0].unique_id == MAC
