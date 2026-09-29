"""Tests for ArubaIAPClient: auth, show-cmd parsing, and failure signatures."""

from unittest.mock import patch

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, Timeout

from custom_components.aruba_device_tracker.aruba_client import (
    ArubaAuthError,
    ArubaConnectionError,
    ArubaIAPClient,
)
from tests.conftest import AP_HOST, LOGIN_URL, show_cmd_url


class TestLogin:
    def test_login_success_stores_sid(self, client, requests_mock):
        requests_mock.post(LOGIN_URL, json={"Status": "Success", "sid": "abc123"})

        client.login()

        assert client._sid == "abc123"

    def test_login_bad_credentials_raises_auth_error(self, client, requests_mock):
        requests_mock.post(
            LOGIN_URL, json={"Status": "Fail", "Error message": "Invalid credentials"}
        )

        with pytest.raises(ArubaAuthError):
            client.login()

        assert client._sid is None

    def test_login_success_without_sid_raises_auth_error(self, client, requests_mock):
        """A 'Success' status with no sid is not a usable session."""
        requests_mock.post(LOGIN_URL, json={"Status": "Success"})

        with pytest.raises(ArubaAuthError):
            client.login()

        assert client._sid is None

    def test_login_connect_timeout_raises_connection_error(self, client, requests_mock):
        requests_mock.post(LOGIN_URL, exc=ConnectTimeout)

        with pytest.raises(ArubaConnectionError):
            client.login()

    def test_login_connection_reset_raises_connection_error(
        self, client, requests_mock
    ):
        requests_mock.post(
            LOGIN_URL,
            exc=RequestsConnectionError(
                ConnectionResetError("Connection reset by peer")
            ),
        )

        with pytest.raises(ArubaConnectionError):
            client.login()

    def test_login_invalid_json_raises_connection_error(self, client, requests_mock):
        requests_mock.post(LOGIN_URL, text="not json at all", status_code=200)

        with pytest.raises(ArubaConnectionError):
            client.login()


class TestShowCmdFailureSignatures:
    """The three known intermittent failure modes, at the _show_cmd layer."""

    def test_empty_body_returns_none_and_clears_sid(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            text="",
            status_code=200,
            headers={"Content-Length": "0"},
        )

        assert logged_in_client.get_clients() is None
        assert logged_in_client._sid is None

    def test_read_timeout_returns_none_and_clears_sid(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"), exc=Timeout("Read timed out")
        )

        assert logged_in_client.get_clients() is None
        assert logged_in_client._sid is None

    def test_connection_error_returns_none_and_clears_sid(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            exc=RequestsConnectionError(
                ConnectionResetError("Connection reset by peer")
            ),
        )

        assert logged_in_client.get_clients() is None
        assert logged_in_client._sid is None

    def test_session_expired_triggers_relogin_and_retry(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status-code": 1},
        )
        requests_mock.post(LOGIN_URL, json={"Status": "Success", "sid": "new-sid-456"})
        requests_mock.get(
            show_cmd_url(sid="new-sid-456"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        result = logged_in_client.get_clients()

        assert result == {}
        assert logged_in_client._sid == "new-sid-456"


class TestGetClientsParsing:
    def test_parses_two_clients_and_skips_headers(
        self, logged_in_client, requests_mock, raw_output
    ):
        cli_text = raw_output("show_clients_ok.txt")
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": cli_text.replace("\n", "\\n")},
        )

        clients = logged_in_client.get_clients()

        assert len(clients) == 2
        kitchen = clients["aa:bb:cc:dd:ee:01"]
        assert kitchen["name"] == "Kitchen Echo"
        assert kitchen["ip"] == "192.168.1.50"
        assert kitchen["speed"] == "130M"

        phone = clients["aa:bb:cc:dd:ee:02"]
        assert phone["name"] == "johns-phone"

    def test_missing_speed_column_is_none(
        self, logged_in_client, requests_mock, raw_output
    ):
        cli_text = raw_output("show_clients_no_speed.txt")
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": cli_text.replace("\n", "\\n")},
        )

        clients = logged_in_client.get_clients()

        assert clients["aa:bb:cc:dd:ee:03"]["speed"] is None

    def test_empty_name_falls_back_to_mac(
        self, logged_in_client, requests_mock, raw_output
    ):
        cli_text = raw_output("show_clients_empty_name.txt")
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": cli_text.replace("\n", "\\n")},
        )

        clients = logged_in_client.get_clients()

        assert clients["aa:bb:cc:dd:ee:04"]["name"] == "aa:bb:cc:dd:ee:04"

    def test_no_clients_returns_empty_dict(self, logged_in_client, requests_mock):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        assert logged_in_client.get_clients() == {}


class TestAuthErrorPropagation:
    """Auth failures must escape get_clients so the coordinator can reauth."""

    def test_auth_failure_on_first_login_propagates(self, client, requests_mock):
        requests_mock.post(
            LOGIN_URL, json={"Status": "Fail", "Error message": "Invalid credentials"}
        )

        with pytest.raises(ArubaAuthError):
            client.get_clients()

    def test_auth_failure_during_session_refresh_propagates(
        self, logged_in_client, requests_mock
    ):
        """Credentials rotated on the AP mid-session must surface, not go quiet."""
        requests_mock.get(show_cmd_url(sid="fake-sid-123"), json={"Status-code": 1})
        requests_mock.post(
            LOGIN_URL, json={"Status": "Fail", "Error message": "Invalid credentials"}
        )

        with pytest.raises(ArubaAuthError):
            logged_in_client.get_clients()

    def test_connection_failure_on_first_login_returns_none(
        self, client, requests_mock
    ):
        """A transient connection failure stays soft — None, not an exception."""
        requests_mock.post(LOGIN_URL, exc=ConnectTimeout)

        assert client.get_clients() is None


class TestTokenRedaction:
    def test_sid_is_not_logged_on_successful_login(self, client, requests_mock, caplog):
        requests_mock.post(
            LOGIN_URL, json={"Status": "Success", "sid": "super-secret-sid"}
        )

        with caplog.at_level("DEBUG"):
            client.login()

        assert "super-secret-sid" not in caplog.text

    def test_sid_is_redacted_from_show_cmd_error_message(
        self, logged_in_client, requests_mock, caplog
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={
                "Status": "Fail",
                "Status-code": 2,
                "Error message": "failed for sid=fake-sid-123",
            },
        )

        with caplog.at_level("WARNING"):
            assert logged_in_client.get_clients() is None

        assert "fake-sid-123" not in caplog.text
        assert "***" in caplog.text


class TestRequestEncoding:
    def test_command_space_is_percent_encoded_not_plus(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        logged_in_client.get_clients()

        query = requests_mock.request_history[-1].url.split("?", 1)[1]
        assert "cmd=show%20clients" in query
        assert "+" not in query

    def test_verify_ssl_default_is_false(self, logged_in_client, requests_mock):
        requests_mock.get(
            show_cmd_url(sid="fake-sid-123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        logged_in_client.get_clients()

        assert requests_mock.request_history[-1].verify is False

    def test_verify_ssl_true_is_passed_through(self, requests_mock):
        verifying = ArubaIAPClient(
            host=AP_HOST,
            username="admin",
            password="test-password",  # noqa: S106
            verify_ssl=True,
        )
        requests_mock.post(LOGIN_URL, json={"Status": "Success", "sid": "abc123"})

        verifying.login()

        assert requests_mock.request_history[-1].verify is True


class TestSessionLifecycle:
    def test_close_closes_the_underlying_session(self, client):
        with patch.object(client._session, "close") as mock_close:
            client.close()

        mock_close.assert_called_once()
