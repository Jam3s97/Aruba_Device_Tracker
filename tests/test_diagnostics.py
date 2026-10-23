"""Tests for the diagnostics download and its scrubbing of CLI tables."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.aruba_device_tracker import diagnostics
from custom_components.aruba_device_tracker.aruba_client import (
    _find_columns,
    _parse_aps,
    _parse_clients,
    _slice_row,
)
from tests.conftest import show_cmd_url


def scrub_both(raw_output):
    """Scrub the AP and client fixtures with one scrubber, as the download does."""
    scrubber = diagnostics._Scrubber()
    aps = scrubber.table(
        raw_output("show_aps_real.txt"),
        diagnostics._AP_REQUIRED_COLUMNS,
        diagnostics._AP_COLUMN_ALIASES,
        diagnostics._AP_FAMILIES,
    )
    clients = scrubber.table(
        raw_output("show_clients_real.txt"),
        diagnostics._CLIENT_REQUIRED_COLUMNS,
        diagnostics._CLIENT_COLUMN_ALIASES,
        diagnostics._CLIENT_FAMILIES,
    )
    return "\n".join(aps), "\n".join(clients)


def unchanged_identifying_cells(original: str, scrubbed: str, kind: str):
    """
    Return identifying cells that the scrubber left as they were.

    Compared row by row and column by column, not by searching for the
    original values: the fixtures are already anonymised in the same style as
    the placeholders, so a placeholder can coincide with another row's value.
    """
    if kind == "aps":
        required, aliases = (
            diagnostics._AP_REQUIRED_COLUMNS,
            diagnostics._AP_COLUMN_ALIASES,
        )
        families = diagnostics._AP_FAMILIES
    else:
        required, aliases = (
            diagnostics._CLIENT_REQUIRED_COLUMNS,
            diagnostics._CLIENT_COLUMN_ALIASES,
        )
        families = diagnostics._CLIENT_FAMILIES

    def rows(text):
        lines = text.splitlines()
        header_index, columns = _find_columns(lines, required, aliases)
        return [
            _slice_row(line, columns)
            for line in lines[header_index + 2 :]
            if line.strip() and not line.lower().startswith(("number of", "info"))
        ]

    unchanged = []
    for before, after in zip(rows(original), rows(scrubbed), strict=True):
        for key in families:
            value = before.get(key, "")
            if value and value not in {"-", "--"} and after.get(key) == value:
                unchanged.append((key, value))
    return unchanged


class TestScrubbing:
    def test_every_identifying_cell_is_replaced(self, raw_output):
        aps, clients = scrub_both(raw_output)

        assert (
            unchanged_identifying_cells(raw_output("show_aps_real.txt"), aps, "aps")
            == []
        )
        assert (
            unchanged_identifying_cells(
                raw_output("show_clients_real.txt"), clients, "clients"
            )
            == []
        )

    def test_real_style_values_are_gone(self):
        """Values unlike any placeholder: none may survive anywhere."""
        text = (
            "Name         IP Address     MAC Address        OS     ESSID      "
            "Access Point  Role\n"
            "----         ----------     -----------        --     -----      "
            "------------  ----\n"
            "jos-iphone   192.168.4.23   a4:c3:f0:85:ac:2d  iOS    SmithHome  "
            "AtticAP       SmithHome\n"
        )

        scrubbed = "\n".join(
            diagnostics._Scrubber().table(
                text,
                diagnostics._CLIENT_REQUIRED_COLUMNS,
                diagnostics._CLIENT_COLUMN_ALIASES,
                diagnostics._CLIENT_FAMILIES,
            )
        )

        for value in ("jos-iphone", "192.168.4.23", "a4:c3:f0", "Smith", "Attic"):
            assert value not in scrubbed
        assert "iOS" in scrubbed  # OS isn't identifying, and helps debugging

    def test_scrubbed_tables_still_parse_the_same(self, raw_output):
        """Placeholders are padded to each cell's width, so offsets survive."""
        aps, clients = scrub_both(raw_output)

        parsed_aps, ap_skipped = _parse_aps(aps)
        parsed_clients, client_skipped = _parse_clients(clients)
        assert ap_skipped == client_skipped == []
        assert len(parsed_aps) == 6
        assert len(parsed_clients) == 32
        assert sorted(
            Counter(c["access_point"] for c in parsed_clients.values()).values()
        ) == [1, 4, 9, 9, 9]

    def test_blank_cells_and_the_conductor_marker_survive(self, raw_output):
        """Exactly the details a parser bug report needs to keep."""
        aps, clients = scrub_both(raw_output)

        parsed_aps, _ = _parse_aps(aps)
        assert [ap["is_conductor"] for ap in parsed_aps.values()].count(True) == 1
        parsed_clients, _ = _parse_clients(clients)
        assert [c["os"] for c in parsed_clients.values()].count(None) == 1

    def test_an_ap_gets_the_same_placeholder_in_both_tables(self, raw_output):
        aps, clients = scrub_both(raw_output)

        ap_names = {ap["name"] for ap in _parse_aps(aps)[0].values()}
        client_aps = {c["access_point"] for c in _parse_clients(clients)[0].values()}
        # Every AP a client is on is one of the scrubbed AP names.
        assert client_aps <= ap_names

    def test_title_and_footer_lines_are_kept(self, raw_output):
        _, clients = scrub_both(raw_output)

        assert "Number of Clients   :32" in clients
        assert clients.startswith("Client List")

    def test_output_without_a_table_keeps_only_its_shape(self):
        lines = diagnostics._Scrubber().table(
            "my-phone 192.168.1.5 aa:bb:cc:dd:ee:ff\n",
            diagnostics._CLIENT_REQUIRED_COLUMNS,
            diagnostics._CLIENT_COLUMN_ALIASES,
            diagnostics._CLIENT_FAMILIES,
        )

        assert lines == ["aa-aaaaa 999.999.9.9 aa:aa:aa:aa:aa:aa"]

    def test_stray_address_outside_a_scrubbed_column_is_caught(self):
        text = (
            "Name   IP Address  MAC Address        OS\n"
            "phone  192.0.2.9   02:00:00:00:00:01  seen via 10.1.2.3\n"
        )

        lines = diagnostics._Scrubber().table(
            text,
            diagnostics._CLIENT_REQUIRED_COLUMNS,
            diagnostics._CLIENT_COLUMN_ALIASES,
            diagnostics._CLIENT_FAMILIES,
        )

        assert "10.1.2.3" not in "\n".join(lines)


class TestLastOutputs:
    def test_successful_output_is_kept_for_diagnostics(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        logged_in_client.get_clients()

        assert logged_in_client.last_outputs == {"show clients": "Total Clients:0"}

    def test_failed_command_keeps_the_previous_output(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            [
                {"json": {"Status": "Success", "Command output": "Total Clients:0"}},
                {"json": {"Status": "Fail", "Status-code": 2}},
            ],
        )

        logged_in_client.get_clients()
        logged_in_client.get_clients()

        assert logged_in_client.last_outputs == {"show clients": "Total Clients:0"}


class TestDownload:
    @pytest.fixture
    def download(self, raw_output):
        def _download(*, with_aps: bool = True):
            outputs = {"show clients": raw_output("show_clients_real.txt")}
            if with_aps:
                outputs["show aps"] = raw_output("show_aps_real.txt")
            aps, _ = _parse_aps(raw_output("show_aps_real.txt"))
            coordinator = SimpleNamespace(
                client=SimpleNamespace(last_outputs=outputs),
                last_update_success=True,
                update_interval=timedelta(seconds=30),
                data={"m1": {}, "m2": {}},
                last_seen={"m1": "", "m2": "", "m3": ""},
                ap_devices=with_aps,
                aps_ok=with_aps,
                aps=aps if with_aps else {},
                ap_client_counts=dict.fromkeys(aps, 1) if with_aps else {},
            )
            entry = SimpleNamespace(
                runtime_data=coordinator,
                version=1,
                minor_version=2,
                data={
                    "host": "192.0.2.5",
                    "username": "admin",
                    "password": "hunter2",
                    "verify_ssl": False,
                },
                options={"scan_interval": 30},
            )
            return asyncio.run(
                diagnostics.async_get_config_entry_diagnostics(SimpleNamespace(), entry)
            )

        return _download

    def test_credentials_and_host_are_redacted(self, download):
        result = download()

        assert result["entry"]["data"] == {
            "host": "**REDACTED**",
            "username": "**REDACTED**",
            "password": "**REDACTED**",
            "verify_ssl": False,
        }
        assert "hunter2" not in str(result)
        assert result["entry"]["options"] == {"scan_interval": 30}

    def test_both_tables_in_the_download_are_scrubbed(self, download, raw_output):
        result = download()

        aps = "\n".join(result["show_aps"]["lines"])
        clients = "\n".join(result["show_clients"]["lines"])
        assert (
            unchanged_identifying_cells(raw_output("show_aps_real.txt"), aps, "aps")
            == []
        )
        assert (
            unchanged_identifying_cells(
                raw_output("show_clients_real.txt"), clients, "clients"
            )
            == []
        )

    def test_access_points_use_the_tables_placeholders(self, download):
        result = download()

        names = [ap["name"] for ap in result["access_points"]]
        assert names == [f"AP-{n}" for n in range(1, 7)]
        assert result["access_points"][0]["is_conductor"] is True
        assert result["access_points"][0]["model"] == "AP-505"
        assert "AP-1" in "\n".join(result["show_aps"]["lines"])

    def test_coordinator_summary(self, download):
        summary = download()["coordinator"]

        assert summary == {
            "last_update_success": True,
            "update_interval_seconds": 30.0,
            "clients_connected": 2,
            "devices_remembered": 3,
            "ap_devices_enabled": True,
            "ap_list_ok": True,
        }

    def test_option_off_has_no_ap_table(self, download):
        result = download(with_aps=False)

        assert result["show_aps"] is None
        assert result["access_points"] == []
        assert result["show_clients"]["line_count"] > 0
