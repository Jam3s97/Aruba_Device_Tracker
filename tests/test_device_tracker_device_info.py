"""Tests for the device info trackers give Home Assistant 2026.11+."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo

from .test_device_tracker_attributes import (
    TEST_ENTRY_ID,
    TEST_MAC,
    FakeCoordinator,
    make_entity,
)

CONNECTIONS = {(CONNECTION_NETWORK_MAC, TEST_MAC)}


def tracker(data: dict | None = None):
    """Build a tracker attached to a placeholder hass (registries are faked)."""
    entity = make_entity(FakeCoordinator(data=data))
    entity.hass = SimpleNamespace()
    return entity


def mac_device(entry_id: str) -> SimpleNamespace:
    """Build a device with the test MAC, owned by `entry_id`."""
    return SimpleNamespace(
        id=f"dev-{entry_id}", connections=CONNECTIONS, config_entry_id=entry_id
    )


def test_named_after_hostname(dev_reg):
    """A connected device with a hostname names the device after it."""
    dev_reg.devices.append(mac_device(TEST_ENTRY_ID))
    entity = tracker({TEST_MAC: {"name": "phone"}})

    assert entity._tracked_device_info() == DeviceInfo(
        connections=CONNECTIONS, name="phone"
    )


def test_new_device_without_hostname_named_after_mac(dev_reg):  # noqa: ARG001
    """With no hostname and no device yet, the MAC names the new device."""
    assert tracker()._tracked_device_info() == DeviceInfo(
        connections=CONNECTIONS, name=TEST_MAC
    )


def test_known_device_keeps_its_name(dev_reg):
    """An offline device already in this entry is not renamed to its MAC."""
    dev_reg.devices.append(mac_device(TEST_ENTRY_ID))

    assert tracker()._tracked_device_info() == DeviceInfo(connections=CONNECTIONS)


def test_other_entrys_device_does_not_count(dev_reg):
    """Another entry's device for the MAC isn't ours, so ours is still new."""
    dev_reg.devices.append(mac_device("entry-b"))

    assert tracker()._tracked_device_info() == DeviceInfo(
        connections=CONNECTIONS, name=TEST_MAC
    )


def test_device_info_set_before_scanner_entity_hook(dev_reg, monkeypatch):  # noqa: ARG001
    """
    Device info is in place before ScannerEntity's hook runs.

    That hook fills in its own device info, and warns, only when there is
    none. It exists only on HA 2026.11+, so it is stubbed here.
    """
    seen = []

    async def base_hook(self) -> None:
        seen.append(self._attr_device_info)

    monkeypatch.setattr(
        ScannerEntity, "async_prepare_to_add_to_hass", base_hook, raising=False
    )

    asyncio.run(tracker().async_prepare_to_add_to_hass())

    assert seen == [DeviceInfo(connections=CONNECTIONS, name=TEST_MAC)]
