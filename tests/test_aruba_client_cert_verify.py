"""Tests for certificate-verification failure handling (verify_ssl=True)."""

import pytest
from requests.exceptions import SSLError

from custom_components.aruba_device_tracker.aruba_client import (
    ArubaCertificateError,
    ArubaConnectionError,
)
from tests.conftest import LOGIN_URL, show_cmd_url

_CERT_ERROR = SSLError(
    "HTTPSConnectionPool(host='192.168.1.1', port=4343): Max retries exceeded "
    "with url: /rest/login (Caused by SSLError(SSLCertVerificationError(1, "
    "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to "
    "get local issuer certificate (_ssl.c:1082)')))"
)


class TestCertVerificationAtLogin:
    def test_login_raises_certificate_error(self, client, requests_mock):
        requests_mock.post(LOGIN_URL, exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError):
            client.login()

    def test_certificate_error_is_not_a_connection_error(self, client, requests_mock):
        """
        The distinction is what stops HA retrying forever.

        A cert failure is permanent until the user changes something, so it must
        not be classified as the transient, retry-with-backoff case.
        """
        requests_mock.post(LOGIN_URL, exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError) as excinfo:
            client.login()

        assert not isinstance(excinfo.value, ArubaConnectionError)

    def test_message_is_actionable_and_names_the_host(self, client, requests_mock):
        requests_mock.post(LOGIN_URL, exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError) as excinfo:
            client.login()

        message = str(excinfo.value)
        assert "192.168.1.1" in message
        assert "self-signed" in message
        assert "Verify SSL certificate" in message

    def test_message_does_not_leak_the_request_url(self, client, requests_mock):
        """
        The raw error must never reach a message the user sees.

        Requests embeds the request URL in its exception text, and the IAP
        takes the sid as a URL query parameter.
        """
        requests_mock.post(LOGIN_URL, exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError) as excinfo:
            client.login()

        message = str(excinfo.value)
        assert "sid" not in message
        assert "/rest/login" not in message
        assert "HTTPSConnectionPool" not in message


class TestCertVerificationDuringPolling:
    def test_cert_failure_propagates_out_of_show_cmd(self, client, requests_mock):
        """
        A cert failure escapes the soft-failure path instead of returning None.

        Connection-class failures are soft (return None, keep last data), but a
        cert failure must reach the caller — otherwise the integration silently
        retries something that can never succeed.
        """
        requests_mock.post(LOGIN_URL, json={"Status": "Success", "sid": "abc123"})
        requests_mock.get(show_cmd_url(sid="abc123"), exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError):
            client.get_clients()

    def test_cert_failure_mid_session_is_not_swallowed_as_no_data(
        self, logged_in_client, requests_mock
    ):
        requests_mock.get(show_cmd_url(sid="fake-sid-123"), exc=_CERT_ERROR)

        with pytest.raises(ArubaCertificateError):
            logged_in_client.get_clients()
