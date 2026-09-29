"""Shared fixtures for Aruba Device Tracker tests."""

from pathlib import Path

import pytest

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
