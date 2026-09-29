"""Tests for the legacy TLS renegotiation fallback (issue #41)."""

import ssl

import pytest
from requests.exceptions import SSLError

from custom_components.aruba_device_tracker.aruba_client import (
    ArubaCertificateError,
    ArubaConnectionError,
    _LegacyTLSAdapter,
)
from tests.conftest import LOGIN_URL, show_cmd_url

_LEGACY_MARKER = "UNSAFE_LEGACY_RENEGOTIATION_DISABLED"


class TestLegacyTLSFallback:
    def test_legacy_ssl_error_triggers_fallback_and_retry_succeeds(
        self, client, requests_mock
    ):
        requests_mock.post(
            LOGIN_URL,
            [
                {
                    "exc": SSLError(
                        f"[SSL: {_LEGACY_MARKER}] unsafe legacy renegotiation disabled"
                    )
                },
                {"json": {"Status": "Success", "sid": "abc123"}},
            ],
        )

        client.login()

        assert client._sid == "abc123"
        assert client._legacy_ssl is True

    def test_legacy_fallback_logs_warning(self, client, requests_mock, caplog):
        requests_mock.post(
            LOGIN_URL,
            [
                {
                    "exc": SSLError(
                        f"[SSL: {_LEGACY_MARKER}] unsafe legacy renegotiation disabled"
                    )
                },
                {"json": {"Status": "Success", "sid": "abc123"}},
            ],
        )

        with caplog.at_level("WARNING"):
            client.login()

        assert "legacy TLS renegotiation" in caplog.text

    def test_non_legacy_ssl_error_is_not_swallowed_as_success(
        self, client, requests_mock
    ):
        requests_mock.post(
            LOGIN_URL,
            exc=SSLError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure"),
        )

        with pytest.raises(ArubaConnectionError):
            client.login()

        assert client._legacy_ssl is False

    def test_cert_verify_failure_is_not_mistaken_for_legacy_renegotiation(
        self, client, requests_mock
    ):
        requests_mock.post(
            LOGIN_URL,
            exc=SSLError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "unable to get local issuer certificate"
            ),
        )

        with pytest.raises(ArubaCertificateError):
            client.login()

        assert client._legacy_ssl is False

    def test_legacy_flag_prevents_repeated_retry_loop(self, client, requests_mock):
        client._legacy_ssl = True  # simulate an already-switched session

        requests_mock.post(
            LOGIN_URL,
            exc=SSLError(
                f"[SSL: {_LEGACY_MARKER}] unsafe legacy renegotiation disabled"
            ),
        )

        with pytest.raises(ArubaConnectionError):
            client.login()

    def test_subsequent_calls_after_fallback_dont_reattempt_normal_path(
        self, client, requests_mock
    ):
        requests_mock.post(
            LOGIN_URL,
            [
                {
                    "exc": SSLError(
                        f"[SSL: {_LEGACY_MARKER}] unsafe legacy renegotiation disabled"
                    )
                },
                {"json": {"Status": "Success", "sid": "abc123"}},
            ],
        )
        client.login()
        assert client._legacy_ssl is True

        requests_mock.get(
            show_cmd_url(sid="abc123"),
            json={"Status": "Success", "Command output": "Total Clients:0"},
        )

        assert client.get_clients() == {}


class TestLegacyAdapterVerification:
    """The adapter relaxes renegotiation only — not certificate verification."""

    def test_unverified_adapter_disables_cert_checks(self):
        adapter = _LegacyTLSAdapter(verify_ssl=False)

        assert adapter._ssl_context.check_hostname is False
        assert adapter._ssl_context.verify_mode is ssl.CERT_NONE

    def test_verifying_adapter_keeps_cert_checks(self):
        adapter = _LegacyTLSAdapter(verify_ssl=True)

        assert adapter._ssl_context.check_hostname is True
        assert adapter._ssl_context.verify_mode is ssl.CERT_REQUIRED

    def test_legacy_renegotiation_flag_set_in_both_modes(self):
        flag = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

        for verify in (True, False):
            adapter = _LegacyTLSAdapter(verify_ssl=verify)
            assert adapter._ssl_context.options & flag

    def test_fallback_honours_client_verify_setting(self, client):
        client.verify_ssl = True

        client._enable_legacy_ssl()

        adapter = client._session.get_adapter("https://example.invalid")
        assert isinstance(adapter, _LegacyTLSAdapter)
        assert adapter._ssl_context.verify_mode is ssl.CERT_REQUIRED
