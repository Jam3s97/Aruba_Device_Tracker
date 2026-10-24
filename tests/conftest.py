"""Shared fixtures for Aruba Device Tracker tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.aruba_device_tracker.aruba_client import ArubaIAPClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

AP_HOST = "192.168.1.1"
AP_PORT = 4343
BASE_URL = f"https://{AP_HOST}:{AP_PORT}/rest"

LOGIN_URL = f"{BASE_URL}/login"
LOGOUT_URL = f"{BASE_URL}/logout"


def show_cmd_url(sid: str, cmd: str = "show%20clients") -> str:
    """
    Build the show-cmd URL the client will actually request.

    The client builds its query string with ``urlencode(..., quote_via=quote)``,
    so spaces arrive as ``%20`` rather than ``+``. requests_mock matches on
    path plus query parameters, not on literal string order.
    """
    return f"{BASE_URL}/show-cmd?iap_ip_addr={AP_HOST}&cmd={cmd}&sid={sid}"


@pytest.fixture
def raw_output():
    """Load a fixture file's raw text, given its filename."""

    def _load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text()

    return _load


@pytest.fixture
def client():
    """Return a fresh, unauthenticated ArubaIAPClient."""
    return ArubaIAPClient(
        host=AP_HOST,
        username="admin",
        password="test-password",  # noqa: S106
    )


@pytest.fixture
def logged_in_client(client, requests_mock):
    """Return a client that has already completed a successful login()."""
    requests_mock.post(LOGIN_URL, json={"Status": "Success", "sid": "fake-sid-123"})
    client.login()
    assert client._sid == "fake-sid-123"
    return client


# ---------------------------------------------------------------------------
# Registry fakes, shared by the AP, network and area tests. The fixtures patch
# the HA helper functions the integration calls, so no Home Assistant runs.
# ---------------------------------------------------------------------------


class FakeDeviceRegistry:
    """Records device updates and removals instead of storing them."""

    def __init__(self, devices=()) -> None:
        self.devices = list(devices)
        self.updates: list[tuple[str, dict]] = []

    def async_get_device(self, identifiers):  # noqa: ARG002
        # Deprecated on HA 2026.9: identifiers are only unique per config
        # entry. Use utils.find_ap_device; fail loudly so a new call is caught
        # here rather than as a warning in a user's log.
        msg = "async_get_device is deprecated; use utils.find_ap_device"
        raise AssertionError(msg)

    def async_update_device(self, device_id, **changes) -> None:
        self.updates.append((device_id, changes))

    def async_remove_device(self, device_id) -> None:
        self.updates.append((device_id, "removed"))


class FakeEntityRegistry:
    """Holds entity entries and records removals."""

    def __init__(self, entries=()) -> None:
        self.entries = list(entries)
        self.removed: list[str] = []

    def async_remove(self, entity_id) -> None:
        self.removed.append(entity_id)

    def async_get_entity_id(self, domain, platform, unique_id):  # noqa: ARG002
        return next(
            (e.entity_id for e in self.entries if e.unique_id == unique_id), None
        )

    def async_get(self, entity_id):
        return next((e for e in self.entries if e.entity_id == entity_id), None)


@pytest.fixture
def dev_reg(monkeypatch):
    """Route the device registry helpers to a fake."""
    registry = FakeDeviceRegistry()
    monkeypatch.setattr(dr, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        dr,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [d for d in reg.devices if entry_id in d.config_entries],
    )
    return registry


@pytest.fixture
def ent_reg(monkeypatch):
    """Route the entity registry helpers to a fake."""
    registry = FakeEntityRegistry()
    monkeypatch.setattr(er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(
        er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [e for e in reg.entries if e.config_entry_id == entry_id],
    )
    return registry


class FakeAreaRegistry:
    """Areas by id."""

    def __init__(self) -> None:
        self.areas: dict[str, object] = {}

    def add(self, area_id: str, name: str) -> None:
        self.areas[area_id] = SimpleNamespace(id=area_id, name=name)

    def async_get_area(self, area_id):
        return self.areas.get(area_id)


@pytest.fixture
def area_reg(monkeypatch):
    """Route the area registry to a fake."""
    registry = FakeAreaRegistry()
    monkeypatch.setattr(ar, "async_get", lambda _hass: registry)
    return registry


@pytest.fixture(autouse=True)
def issues(monkeypatch):
    """
    Record Repairs issues instead of writing them to a real issue registry.

    Autouse, because any poll that reaches the Area sensors' platform raises
    or clears one. Maps issue id -> translation placeholders.
    """
    raised: dict[str, dict] = {}

    def _create(_hass, _domain, issue_id, **kwargs) -> None:
        raised[issue_id] = kwargs.get("translation_placeholders") or {}

    def _delete(_hass, _domain, issue_id) -> None:
        raised.pop(issue_id, None)

    monkeypatch.setattr(ir, "async_create_issue", _create)
    monkeypatch.setattr(ir, "async_delete_issue", _delete)
    return raised
