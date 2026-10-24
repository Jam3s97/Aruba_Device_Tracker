"""Tests for ArubaIAPClient.get_aps: 'show aps' parsing and failure handling."""

import logging

import pytest
from requests.exceptions import ReadTimeout

from custom_components.aruba_device_tracker.aruba_client import ArubaAuthError
from tests.conftest import LOGIN_URL, show_cmd_url

APS_URL = show_cmd_url(sid="fake-sid-123", cmd="show%20aps")

# Minimal hand-made table in the same left-aligned layout as the AP's output.
HEADER = (
    "Name      IP Address     Clients  Type          Serial #    Uplink Port\n"
    "----      ----------     -------  ----          --------    -----------\n"
)


@pytest.fixture
def get_aps(logged_in_client, requests_mock, raw_output):
    """Serve 'show aps' output (a fixture file or literal text) and parse it."""

    def _get(name: str | None = None, text: str | None = None):
        cli_text = raw_output(name) if name else text
        requests_mock.get(
            APS_URL,
            json={"Status": "Success", "Command output": cli_text.replace("\n", "\\n")},
        )
        return logged_in_client.get_aps()

    return _get


class TestRealOutput:
    """Pinned against an anonymised capture from a 6-AP cluster over REST."""

    def test_every_ap_is_parsed_and_keyed_by_serial(self, get_aps, caplog):
        with caplog.at_level(logging.DEBUG):
            aps = get_aps("show_aps_real.txt")

        assert sorted(aps) == [f"TESTSN000{n}" for n in range(1, 7)]
        assert {ap["name"] for ap in aps.values()} == {
            "LoungeAP",
            "HallwayAP",
            "KitchenAP",
            "ExternalAP",
            "GardenAP",
            "OfficeAP",
        }
        # The "cli output:" / "COMMAND=" preamble, title and rules are skipped.
        assert "did not match" not in caplog.text

    def test_conductor_is_flagged_and_its_ip_marker_stripped(self, get_aps):
        aps = get_aps("show_aps_real.txt")

        assert aps["TESTSN0001"]["ip"] == "203.0.113.20"
        assert aps["TESTSN0001"]["is_conductor"] is True
        assert [ap["name"] for ap in aps.values() if ap["is_conductor"]] == ["LoungeAP"]

    def test_type_becomes_a_model_name(self, get_aps):
        aps = get_aps("show_aps_real.txt")

        assert aps["TESTSN0001"]["model"] == "AP-505"
        assert aps["TESTSN0004"]["model"] == "AP-365"

    def test_reported_client_counts(self, get_aps):
        aps = get_aps("show_aps_real.txt")

        assert {ap["name"]: ap["clients"] for ap in aps.values()} == {
            "LoungeAP": 9,
            "HallwayAP": 9,
            "KitchenAP": 9,
            "ExternalAP": 4,
            "GardenAP": 0,
            "OfficeAP": 1,
        }

    def test_mesh_point_ap_is_parsed(self, get_aps):
        """GardenAP uplinks over Wi-Fi mesh rather than eth0."""
        garden = get_aps("show_aps_real.txt")["TESTSN0005"]

        assert garden["name"] == "GardenAP"
        assert garden["ip"] == "203.0.113.30"


class TestLayout:
    def test_columns_are_found_by_heading_not_position(self, get_aps):
        """Firmware that reorders or adds columns must still parse."""
        text = (
            "Serial #    Extra Column  Type         Name     IP Address\n"
            "--------    ------------  ----         ----     ----------\n"
            "TESTSN0009  anything      515(indoor)  StairAP  203.0.113.40\n"
        )

        ap = get_aps(text=text)["TESTSN0009"]

        assert ap["name"] == "StairAP"
        assert ap["ip"] == "203.0.113.40"
        assert ap["model"] == "AP-515"
        assert ap["clients"] is None  # no Clients column at all

    def test_dash_placeholders_become_none(self, get_aps):
        text = (
            HEADER
            + "StairAP   --             -        -             TESTSN0009  eth0\n"
        )

        ap = get_aps(text=text)["TESTSN0009"]

        assert ap["ip"] is None
        assert ap["is_conductor"] is False
        assert ap["clients"] is None
        assert ap["model"] is None

    def test_booting_ap_row_is_parsed(self, get_aps):
        """
        Captured live: an AP rejoining after power-on is listed as 'monitor'.

        For about 15 seconds its Clients cell is blank and its radio channels
        are '-', before it settles as 'access'.
        """
        text = (
            "Name      IP Address    Mode     Clients  Type         Serial #"
            "    radio0 Channel\n"
            "----      ----------    ----     -------  ----         --------"
            "    --------------\n"
            "OfficeAP  203.0.113.31  monitor           505(indoor)  TESTSN0006"
            "  -\n"
        )

        ap = get_aps(text=text)["TESTSN0006"]

        assert ap["name"] == "OfficeAP"
        assert ap["clients"] is None
        assert ap["model"] == "AP-505"

    def test_unrecognised_type_is_kept_as_is(self, get_aps):
        text = (
            HEADER
            + "StairAP   203.0.113.40   2        AP-635        TESTSN0009  eth0\n"
        )

        assert get_aps(text=text)["TESTSN0009"]["model"] == "AP-635"

    def test_row_without_a_serial_is_skipped(self, get_aps, caplog):
        text = (
            HEADER
            + "StairAP   203.0.113.40   2        505(indoor)   -           eth0\n"
            + "HallAP    203.0.113.41   3        505(indoor)   TESTSN0010  eth0\n"
        )

        with caplog.at_level(logging.DEBUG):
            aps = get_aps(text=text)

        assert list(aps) == ["TESTSN0010"]
        assert "did not match the access points table" in caplog.text
        assert "StairAP" in caplog.text

    def test_header_with_no_aps_is_an_empty_dict(self, get_aps):
        assert get_aps(text="0 Access Points\n---------------\n" + HEADER) == {}

    def test_output_without_a_header_is_none(self, get_aps):
        assert get_aps(text="Invalid input detected\n") is None


class TestFailures:
    def test_refusal_warns_once_then_logs_at_debug(
        self, logged_in_client, requests_mock, caplog
    ):
        """A restricted API account would otherwise warn on every poll."""
        requests_mock.get(
            APS_URL,
            json={"Status": "Fail", "Status-code": 2, "Error message": "denied"},
        )

        with caplog.at_level(logging.DEBUG):
            for _ in range(3):
                assert logged_in_client.get_aps() is None

        refusals = [r for r in caplog.records if "show-cmd 'show aps'" in r.message]
        assert [r.levelno for r in refusals] == [
            logging.WARNING,
            logging.DEBUG,
            logging.DEBUG,
        ]

    def test_success_rearms_the_refusal_warning(
        self, logged_in_client, requests_mock, caplog
    ):
        refused = {"Status": "Fail", "Status-code": 2, "Error message": "denied"}
        ok = {"Status": "Success", "Command output": "0 Access Points\n" + HEADER}
        requests_mock.get(APS_URL, [{"json": refused}, {"json": ok}, {"json": refused}])

        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                logged_in_client.get_aps()

        assert len(caplog.records) == 2

    def test_refusals_are_tracked_per_command(
        self, logged_in_client, requests_mock, caplog
    ):
        """A refused 'show aps' must not silence a later 'show clients' refusal."""
        refused = {"Status": "Fail", "Status-code": 2, "Error message": "denied"}
        requests_mock.get(APS_URL, json=refused)
        requests_mock.get(show_cmd_url(sid="fake-sid-123"), json=refused)

        with caplog.at_level(logging.WARNING):
            logged_in_client.get_aps()
            logged_in_client.get_clients()

        assert len(caplog.records) == 2

    def test_timeout_is_soft(self, logged_in_client, requests_mock):
        requests_mock.get(APS_URL, exc=ReadTimeout)

        assert logged_in_client.get_aps() is None
        assert logged_in_client._sid is None

    def test_auth_failure_propagates(self, logged_in_client, requests_mock):
        requests_mock.get(APS_URL, json={"Status-code": 1})
        requests_mock.post(
            LOGIN_URL, json={"Status": "Fail", "Error message": "Invalid credentials"}
        )

        with pytest.raises(ArubaAuthError):
            logged_in_client.get_aps()
