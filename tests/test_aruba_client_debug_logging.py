"""Tests for _log_bad_response_debug — the JSONDecodeError diagnostics."""

import re

import pytest

from custom_components.aruba_device_tracker.aruba_client import ArubaConnectionError
from tests.conftest import LOGIN_URL, show_cmd_url


class TestBadResponseDebugLogging:
    def test_empty_body_logs_content_length_and_zero_bytes(
        self, logged_in_client, requests_mock, caplog
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            text="",
            status_code=200,
            headers={"Content-Length": "0"},
        )

        with caplog.at_level("DEBUG"):
            logged_in_client.get_clients()

        assert "content-length header=0" in caplog.text
        assert "actual body bytes=0" in caplog.text
        assert "<empty body>" in caplog.text

    def test_truncated_body_logs_mismatched_length_and_snippet(
        self, logged_in_client, requests_mock, caplog
    ):
        truncated_body = '{"Status": "Success", "Command output": "partial dat'
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            text=truncated_body,
            status_code=200,
            headers={"Content-Length": "500"},
        )

        with caplog.at_level("DEBUG"):
            logged_in_client.get_clients()

        assert "content-length header=500" in caplog.text
        assert f"actual body bytes={len(truncated_body)}" in caplog.text
        assert "partial dat" in caplog.text

    def test_snippet_is_truncated_at_300_chars(
        self, logged_in_client, requests_mock, caplog
    ):
        oversized_body = "x" * 1000
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            text=oversized_body,
            status_code=200,
            headers={"Content-Length": "1000"},
        )

        with caplog.at_level("DEBUG"):
            logged_in_client.get_clients()

        match = re.search(r"body snippet=('x+')", caplog.text)
        assert match is not None
        assert len(match.group(1).strip("'")) == 300

    def test_login_bad_response_body_is_never_logged(
        self, client, requests_mock, caplog
    ):
        """
        The /login body is the auth response, so it must not be logged.

        Other endpoints get a body snippet for diagnostics; login deliberately
        does not, even at DEBUG level.
        """
        requests_mock.post(
            LOGIN_URL,
            text="not json at all",
            status_code=200,
            headers={"Content-Length": "16"},
        )

        with caplog.at_level("DEBUG"), pytest.raises(ArubaConnectionError):
            client.login()

        assert "bad response debug" not in caplog.text
        assert "not json at all" not in caplog.text

    def test_healthy_response_does_not_trigger_debug_log(
        self, logged_in_client, requests_mock, caplog
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        with caplog.at_level("DEBUG"):
            logged_in_client.get_clients()

        assert "bad response debug" not in caplog.text
