"""Tests for ArubaIAPClient.get_networks: 'show network' parsing."""

import logging

import pytest
from requests.exceptions import ReadTimeout

from tests.conftest import show_cmd_url

NETWORKS_URL = show_cmd_url(sid="fake-sid-123", cmd="show%20network")


@pytest.fixture
def get_networks(logged_in_client, requests_mock, raw_output):
    """Serve 'show network' output (a fixture file or literal text) and parse it."""

    def _get(name: str | None = None, text: str | None = None):
        cli_text = raw_output(name) if name else text
        requests_mock.get(
            NETWORKS_URL,
            json={"Status": "Success", "Command output": cli_text.replace("\n", "\\n")},
        )
        return logged_in_client.get_networks()

    return _get


class TestRealOutput:
    """Pinned against an anonymised capture from a real cluster over REST."""

    def test_every_network_is_listed_by_profile(self, get_networks, caplog):
        with caplog.at_level(logging.DEBUG):
            networks = get_networks("show_network_real.txt")

        assert sorted(networks) == [
            "Guest",
            "Home",
            "Home2.4",
            "IoT",
            "Office",
            "Visitors",
            "spare",
        ]
        # The preamble, the stray carriage-return line and the rules are
        # skipped without complaint.
        assert "did not match" not in caplog.text

    def test_profile_and_essid_can_differ(self, get_networks):
        networks = get_networks("show_network_real.txt")

        assert networks["Visitors"]["essid"] == "VisitorNet"
        assert networks["spare"]["essid"] == "Spare"

    def test_quiet_and_disabled_networks_are_still_listed(self, get_networks):
        networks = get_networks("show_network_real.txt")

        assert networks["Office"] == {
            "profile": "Office",
            "essid": "Office",
            "enabled": True,
            "clients": 0,
        }
        assert networks["spare"]["enabled"] is False

    def test_reported_client_counts(self, get_networks):
        networks = get_networks("show_network_real.txt")

        assert {n["essid"]: n["clients"] for n in networks.values()} == {
            "VisitorNet": 0,
            "Home": 18,
            "Home2.4": 6,
            "Office": 0,
            "Guest": 2,
            "IoT": 8,
            "Spare": 0,
        }


class TestLayout:
    def test_a_profile_name_with_spaces(self, get_networks):
        text = (
            "Profile Name   ESSID        Clients  Status\n"
            "------------   -----        -------  ------\n"
            "Guest Network  Guest WiFi   3        Enabled\n"
        )

        network = get_networks(text=text)["Guest Network"]

        assert network["essid"] == "Guest WiFi"
        assert network["clients"] == 3

    def test_no_status_column_counts_as_enabled(self, get_networks):
        text = "Profile Name  ESSID\n------------  -----\nHome          Home\n"

        network = get_networks(text=text)["Home"]

        assert network["enabled"] is True
        assert network["clients"] is None

    def test_row_without_an_essid_is_skipped(self, get_networks, caplog):
        text = (
            "Profile Name  ESSID  Clients\n"
            "------------  -----  -------\n"
            "broken        --     0\n"
            "Home          Home   2\n"
        )

        with caplog.at_level(logging.DEBUG):
            networks = get_networks(text=text)

        assert list(networks) == ["Home"]
        assert "did not match the networks table" in caplog.text

    def test_output_without_a_header_is_none(self, get_networks):
        assert get_networks(text="Invalid input detected\n") is None


def test_timeout_is_soft(logged_in_client, requests_mock):
    """A timeout keeps the previous list rather than raising."""
    requests_mock.get(NETWORKS_URL, exc=ReadTimeout)

    assert logged_in_client.get_networks() is None
