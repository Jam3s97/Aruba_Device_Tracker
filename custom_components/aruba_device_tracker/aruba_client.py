"""Aruba Instant AP REST API client."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import ssl
from typing import Any, TypedDict
from urllib.parse import quote, urlencode

import requests
import urllib3
from homeassistant.helpers.device_registry import format_mac
from requests.adapters import HTTPAdapter

_LOGGER = logging.getLogger(__name__)

# Substring present in the SSLError raised when a server requires legacy
# (insecure) TLS renegotiation and the local OpenSSL build has that
# disabled by default. Some Instant AOS versions hit this on modern
# Debian/Ubuntu-based hosts running OpenSSL 3.x.
_LEGACY_TLS_MARKER = "UNSAFE_LEGACY_RENEGOTIATION_DISABLED"

# Substring present in the SSLError raised when the AP's certificate cannot be
# validated against the local trust store. Instant APs ship a self-signed
# certificate, so this is what verify_ssl=True hits on a stock AP. Unlike a
# renegotiation failure it is never transient: retrying cannot fix it, only a
# configuration change can.
_CERT_VERIFY_MARKER = "CERTIFICATE_VERIFY_FAILED"

# How many characters of a bad response body to include in debug logs.
_DEBUG_BODY_SNIPPET_LEN = 300

# Placeholder substituted for the session token in anything that may be
# logged or surfaced to the user.
_REDACTED = "***"

# urllib3 emits InsecureRequestWarning once per unverified request. Silencing
# it is a process-global mutation, so it is done lazily — only if an
# unverified request is actually about to be made, and only once — rather
# than unconditionally at import time.
_warnings_disabled = False


class ArubaError(Exception):
    """Base class for every error raised by this client."""


class ArubaConnectionError(ArubaError):
    """The AP could not be reached, or returned an unusable response."""


class ArubaAuthError(ArubaError):
    """The AP rejected the supplied credentials."""


class ArubaCertificateError(ArubaError):
    """
    The AP's TLS certificate could not be verified.

    Deliberately *not* an ArubaConnectionError: the AP is reachable and
    answering, and no amount of retrying will change the outcome. Like
    ArubaAuthError this propagates out of the soft-failure path in
    ``_show_cmd`` so the caller can surface it as a configuration problem
    needing user action rather than a transient blip.
    """


class ArubaClientData(TypedDict):
    """A single Wi-Fi client as parsed from 'show clients' output."""

    mac: str
    name: str
    ip: str
    os: str
    essid: str
    access_point: str
    channel: str
    signal: str
    speed: str | None


# Parses each client row from 'show clients' output.
#
# Columns: Name IP MAC OS ESSID AccessPoint Channel Type Role IPv6 Signal Speed
#
# Notes:
#   - Name may contain spaces (e.g. "My Smart TV"), so we match up to the
#     first run of 2+ spaces rather than using \\S+.
#   - The regex is anchored with a lookahead requiring an IP after the spaces,
#     so lazy matching cannot short-circuit on an empty name field.
#   - IPv6 may be "--" when not assigned.
#   - Speed is optional — some rows omit it.
#   - MAC separators may be ":" or "-".
_CLIENT_REGEX = re.compile(
    r"^(?P<name>[^\n]*?)\s{2,}"
    r"(?=(?:\d{1,3}\.){3}\d{1,3}\s)"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\s+"
    r"(?P<mac>(?:[0-9a-f]{2}[:\-]){5}[0-9a-f]{2})\s+"
    r"(?P<os>\S+)\s+"
    r"(?P<essid>\S+)\s+"
    r"(?P<access_point>\S+)\s+"
    r"(?P<channel>\S+)\s+"
    r"(?P<type>\S+)\s+"
    r"(?P<role>\S+)\s+"
    r"(?P<ipv6>\S+)\s+"
    r"(?P<signal>\S+)"
    r"(?:\s+(?P<speed>\S+))?",
    re.IGNORECASE,
)

# Lines starting with these strings are header/separator/footer rows.
_SKIP_PREFIXES = (
    "name",
    "----",
    "client list",
    "num ",
    "total",
    "cli output",
    "command=",
    "number of",
    "info timestamp",
)


class _LegacyTLSAdapter(HTTPAdapter):
    """
    Transport adapter that allows legacy/insecure TLS renegotiation.

    Some Aruba Instant AP firmware versions expose a TLS stack that
    doesn't support secure renegotiation (RFC 5746). OpenSSL 3.x on
    several modern Linux distributions disables legacy renegotiation by
    default, which surfaces as:

        ssl.SSLError: [SSL: UNSAFE_LEGACY_RENEGOTIATION_DISABLED]

    This adapter re-enables it for hosts that need it. It's only mounted
    onto a session after that specific error has actually been seen, so
    APs that don't need it keep the stock OpenSSL renegotiation policy.

    Certificate verification is orthogonal and is controlled by the
    caller's ``verify_ssl`` setting: when verification is on, this adapter
    keeps hostname checking and the default trust store intact and relaxes
    *only* the renegotiation flag.
    """

    def __init__(self, *args: Any, verify_ssl: bool = False, **kwargs: Any) -> None:
        """Build the adapter with a legacy-renegotiation-friendly context."""
        ctx = ssl.create_default_context()
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        # SSL_OP_LEGACY_SERVER_CONNECT. Named constant only exists on
        # Python 3.12+, so fall back to the raw OpenSSL flag value.
        ctx.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        self._ssl_context = ctx
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        """Inject the legacy SSL context into the connection pool."""
        kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(*args, **kwargs)


class ArubaIAPClient:
    """Client for the Aruba Instant AP REST API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 4343,
        *,
        verify_ssl: bool = False,
    ) -> None:
        """Initialise the client with connection parameters."""
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.base_url = f"https://{host}:{port}/rest"
        self._headers = {"Content-Type": "application/json"}
        self._sid: str | None = None
        self._session = requests.Session()
        self._legacy_ssl = False

    # ------------------------------------------------------------------
    # Redaction
    # ------------------------------------------------------------------

    def _redact(self, value: object) -> str:
        """
        Return ``value`` as a string with the live session token removed.

        The Aruba API takes ``sid`` as a URL query parameter, and requests'
        exception messages embed the full request URL. Without this, the
        session token ends up in the Home Assistant log and in the
        coordinator's user-visible error text.
        """
        text = str(value)
        if self._sid:
            text = text.replace(self._sid, _REDACTED)
        return text

    def _log_bad_response_debug(self, cmd: str, resp: requests.Response) -> None:
        """
        Log diagnostic details for a response that failed JSON decoding.

        Never called for the login endpoint, whose body is the auth response.
        """
        content_length = resp.headers.get("Content-Length", "unknown")
        body_len = len(resp.content) if resp.content is not None else 0
        snippet = resp.text[:_DEBUG_BODY_SNIPPET_LEN] if resp.text else "<empty body>"
        _LOGGER.debug(
            "Aruba IAP bad response debug for cmd '%s': status_code=%s "
            "content-length header=%s actual body bytes=%d body snippet=%r",
            cmd,
            resp.status_code,
            content_length,
            body_len,
            self._redact(snippet),
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _enable_legacy_ssl(self) -> None:
        """Switch the shared session to a legacy-TLS-compatible adapter."""
        _LOGGER.warning(
            "Aruba IAP at %s requires legacy TLS renegotiation; "
            "switching to compatibility mode for this connection",
            self.host,
        )
        self._session.mount("https://", _LegacyTLSAdapter(verify_ssl=self.verify_ssl))
        self._legacy_ssl = True

    def _session_request(
        self, method: str, url: str, **kwargs: Any
    ) -> requests.Response:
        """
        Issue a request via the shared session.

        If the AP requires legacy TLS renegotiation, the first attempt
        raises an SSLError containing UNSAFE_LEGACY_RENEGOTIATION_DISABLED.
        On that specific error, switch to the legacy adapter and retry
        once. Once switched, the session stays in legacy mode for the
        lifetime of this client instance, so later calls go straight
        through without re-attempting the normal path first.
        """
        kwargs.setdefault("verify", self.verify_ssl)
        if not kwargs["verify"]:
            self._disable_insecure_warnings()
        try:
            return self._request_once(method, url, **kwargs)
        except requests.exceptions.SSLError as err:
            if self._legacy_ssl or _LEGACY_TLS_MARKER not in str(err):
                raise
            self._enable_legacy_ssl()
            return self._request_once(method, url, **kwargs)

    def _request_once(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """
        Issue one request, mapping a certificate failure to a clear error.

        Raises:
            ArubaCertificateError: The AP's certificate failed verification.

        """
        try:
            return self._session.request(method, url, **kwargs)
        except requests.exceptions.SSLError as err:
            if _CERT_VERIFY_MARKER not in str(err):
                raise
            # The raw error is never interpolated: requests embeds the request
            # URL in its message and that URL carries the sid.
            msg = (
                f"The certificate presented by the Aruba IAP at {self.host} "
                "could not be verified. Instant APs ship a self-signed "
                "certificate, so either install a certificate your Home "
                "Assistant host trusts, or turn off 'Verify SSL certificate'."
            )
            raise ArubaCertificateError(msg) from err

    @staticmethod
    def _disable_insecure_warnings() -> None:
        """Silence urllib3's InsecureRequestWarning, at most once per process."""
        global _warnings_disabled  # noqa: PLW0603
        if not _warnings_disabled:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _warnings_disabled = True

    def _build_url(self, path: str, params: dict[str, str]) -> str:
        """
        Build a fully-encoded URL for ``path``.

        ``quote_via=quote`` keeps spaces as ``%20`` rather than ``+``; the
        IAP's show-cmd endpoint does not accept ``+`` as a space.
        """
        return f"{self.base_url}/{path}?{urlencode(params, quote_via=quote)}"

    def close(self) -> None:
        """Close the underlying HTTP session and release its connection pool."""
        self._session.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> None:
        """
        Login and store the session ID.

        Raises:
            ArubaAuthError: The AP rejected the credentials.
            ArubaConnectionError: The AP was unreachable or unintelligible.

        """
        url = f"{self.base_url}/login"
        payload = json.dumps({"user": self.username, "passwd": self.password})
        try:
            resp = self._session_request(
                "post",
                url,
                headers=self._headers,
                data=payload,
                timeout=10,
            )
            data = resp.json()
        except requests.exceptions.ConnectTimeout as err:
            msg = f"Timed out connecting to Aruba IAP at {self.host}"
            raise ArubaConnectionError(msg) from err
        except requests.exceptions.ConnectionError as err:
            msg = f"Could not connect to Aruba IAP at {self.host}"
            raise ArubaConnectionError(msg) from err
        except requests.exceptions.JSONDecodeError as err:
            # Deliberately not logging the body: for /login it is the auth
            # response.
            msg = f"Aruba IAP at {self.host} returned an invalid login response"
            raise ArubaConnectionError(msg) from err
        except requests.exceptions.RequestException as err:
            msg = f"Unexpected error talking to Aruba IAP at {self.host}"
            raise ArubaConnectionError(msg) from err

        if data.get("Status") == "Success" and data.get("sid"):
            self._sid = data["sid"]
            _LOGGER.debug("Aruba IAP login successful")
            return

        msg = f"Aruba IAP rejected the credentials: {data.get('Error message')}"
        raise ArubaAuthError(msg)

    def logout(self) -> None:
        """Logout and clear the session ID."""
        if not self._sid:
            return
        with contextlib.suppress(Exception):
            self._session_request(
                "post",
                f"{self.base_url}/logout",
                headers=self._headers,
                data=json.dumps({"sid": self._sid}),
                timeout=10,
            )
        self._sid = None

    def _ensure_session(self) -> None:
        """Re-login if we have no active session."""
        if not self._sid:
            self.login()

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _request_show_cmd(self, cmd: str) -> dict[str, Any]:
        """Issue a single show-cmd request and return the decoded JSON body."""
        url = self._build_url(
            "show-cmd",
            {"iap_ip_addr": self.host, "cmd": cmd, "sid": self._sid or ""},
        )
        resp = self._session_request("get", url, headers=self._headers, timeout=15)
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            self._log_bad_response_debug(cmd, resp)
            raise

    @staticmethod
    def _failure_reason(err: Exception) -> str:
        """Describe a soft transport failure in user-facing terms."""
        if isinstance(err, requests.exceptions.JSONDecodeError):
            return (
                "returned an empty or invalid response "
                "(the AP may be busy, or the session was dropped)"
            )
        if isinstance(err, requests.exceptions.Timeout):
            return "timed out"
        if isinstance(err, requests.exceptions.ConnectionError | ArubaConnectionError):
            return "was unreachable"
        return "failed unexpectedly"

    def _show_cmd(self, cmd: str) -> str | None:
        """
        Run a show command and return raw CLI output.

        Returns None if the AP was momentarily unreachable or unintelligible —
        the caller keeps its last known data and retries on the next poll.

        Raises:
            ArubaAuthError: The AP rejected the credentials, so retrying
                without user intervention cannot help.

        """
        try:
            data = self._fetch_show_cmd(cmd)
        except (requests.exceptions.RequestException, ArubaConnectionError) as err:
            reason = self._failure_reason(err)
            if reason == "failed unexpectedly":
                # A genuine bug rather than an AP hiccup — keep the traceback.
                _LOGGER.exception("Aruba IAP show-cmd '%s' failed unexpectedly", cmd)
            else:
                _LOGGER.warning(
                    "Aruba IAP %s running cmd '%s' — will retry next poll",
                    reason,
                    cmd,
                )
            self._sid = None
            return None

        if data.get("Status") != "Success":
            _LOGGER.warning(
                "show-cmd '%s' failed (status-code %s): %s",
                cmd,
                data.get("Status-code"),
                self._redact(data.get("Error message")),
            )
            return None

        raw: str = data.get("Command output", "")
        return raw.replace("\\n", "\n").replace("\\r", "\r")

    def _fetch_show_cmd(self, cmd: str) -> dict[str, Any]:
        """
        Fetch a show command's JSON body, re-logging in once if the session died.

        Raises:
            ArubaAuthError: The AP rejected the credentials.
            ArubaConnectionError: The AP was unreachable.

        """
        self._ensure_session()
        data = self._request_show_cmd(cmd)

        # Status-code 1 means the session expired — re-login once and retry.
        if data.get("Status-code") == 1:
            _LOGGER.debug("Session expired, re-logging in")
            self._sid = None
            self.login()
            data = self._request_show_cmd(cmd)

        return data

    def get_clients(self) -> dict[str, ArubaClientData] | None:
        """
        Return connected clients keyed by MAC address.

        Returns None if the API call failed (e.g. no privilege).
        Returns {} if the call succeeded but no clients are connected.

        Raises:
            ArubaAuthError: The AP rejected the credentials.

        """
        output = self._show_cmd("show clients")
        if output is None:
            return None

        clients: dict[str, ArubaClientData] = {}
        skipped: list[str] = []

        for line in output.splitlines():
            stripped = line.strip()

            if not stripped or stripped.lower().startswith(_SKIP_PREFIXES):
                continue

            # Match on the RAW line (not stripped) so that empty-name rows
            # retain their leading whitespace for the regex to anchor against.
            match = _CLIENT_REGEX.match(line)
            if match:
                mac = format_mac(match.group("mac"))
                name = match.group("name").strip() or mac
                clients[mac] = ArubaClientData(
                    mac=mac,
                    name=name,
                    ip=match.group("ip"),
                    os=match.group("os"),
                    essid=match.group("essid"),
                    access_point=match.group("access_point"),
                    channel=match.group("channel"),
                    signal=match.group("signal"),
                    speed=match.group("speed"),
                )
            elif stripped:
                skipped.append(stripped)

        if skipped:
            _LOGGER.debug(
                "Aruba IAP: %d line(s) did not match client pattern:\n%s",
                len(skipped),
                "\n".join(f"  > {s}" for s in skipped),
            )

        _LOGGER.debug("Aruba IAP found %d clients", len(clients))
        return clients
