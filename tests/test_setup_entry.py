"""
Tests for async_setup_entry's handling of a failed first refresh.

Like test_coordinator_cleanup.py, these drive the real coroutine against small
fakes rather than a live Home Assistant instance.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

import custom_components.aruba_device_tracker as integration


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
