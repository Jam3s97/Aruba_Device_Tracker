"""Aruba Instant AP REST API client."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import ssl
from typing import Any

import requests
import urllib3
from homeassistant.helpers.device_registry import format_mac
from requests.adapters import HTTPAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_LOGGER = logging.getLogger(__name__)

# Substring present in the SSLError raised when a server requires legacy
# (insecure) TLS renegotiation and the local OpenSSL build has that
# disabled by default. Some Instant AOS versions hit this on modern
# Debian/Ubuntu-based hosts running OpenSSL 3.x.
_LEGACY_TLS_MARKER = "UNSAFE_LEGACY_RENEGOTIATION_DISABLED"

# Parses each client row from 'show clients' output.
#
# Columns: Name IP MAC OS ESSID AccessPoint Channel Type Role IPv6 Signal Speed
#
# Notes:
#   - Name may contain spaces (e.g. "My Smart TV"), so we match up to the
#     first run of 2+ spaces rather than using \S+.
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
    it never weakens TLS for APs that don't need it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the adapter with a legacy-renegotiation-friendly context."""
        ctx = ssl.create_default_context()
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
    ) -> None:
        """Initialise the client with connection parameters."""
        self.host = host
        self.username = username
        self.password = password
        self.base_url = f"https://{host}:{port}/rest"
        self._headers = {"Content-Type": "application/json"}
        self._sid: str | None = None
        self._session = requests.Session()
        self._legacy_ssl = False

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
        self._session.mount("https://", _LegacyTLSAdapter())
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
        kwargs.setdefault("verify", False)
        try:
            return self._session.request(method, url, **kwargs)
        except requests.exceptions.SSLError as err:
            if self._legacy_ssl or _LEGACY_TLS_MARKER not in str(err):
                raise
            self._enable_legacy_ssl()
            return self._session.request(method, url, **kwargs)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Login and store the session ID. Returns True on success."""
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
        except requests.exceptions.ConnectTimeout:
            _LOGGER.warning(
                "Aruba IAP login timed out connecting to %s — AP may be unreachable",
                self.host,
            )
            return False
        except requests.exceptions.ConnectionError:
            _LOGGER.warning(
                "Aruba IAP login connection error for %s — check host/network",
                self.host,
            )
            return False
        except requests.exceptions.JSONDecodeError:
            _LOGGER.warning(
                "Aruba IAP login returned an invalid response from %s",
                self.host,
            )
            return False
        except Exception:
            _LOGGER.exception("Aruba IAP login failed unexpectedly")
            return False
        else:
            if data.get("Status") == "Success" and data.get("sid"):
                self._sid = data["sid"]
                _LOGGER.debug("Aruba IAP login successful, sid=%s", self._sid)
                return True
            _LOGGER.warning("Aruba IAP login failed: %s", data.get("Error message"))
            return False

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

    def _ensure_session(self) -> bool:
        """Re-login if we have no active session."""
        if self._sid:
            return True
        return self.login()

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _show_cmd(self, cmd: str) -> str | None:
        """Run a show command and return raw CLI output, or None on failure."""
        if not self._ensure_session():
            return None

        encoded_cmd = cmd.replace(" ", "%20")
        url = (
            f"{self.base_url}/show-cmd"
            f"?iap_ip_addr={self.host}&cmd={encoded_cmd}&sid={self._sid}"
        )

        output: str | None = None
        try:
            resp = self._session_request(
                "get",
                url,
                headers=self._headers,
                timeout=15,
            )
            data = resp.json()

            # Session expired — re-login once and retry
            if data.get("Status-code") == 1:
                _LOGGER.debug("Session expired, re-logging in")
                self._sid = None
                if not self.login():
                    return None
                encoded_cmd = cmd.replace(" ", "%20")
                url = (
                    f"{self.base_url}/show-cmd"
                    f"?iap_ip_addr={self.host}&cmd={encoded_cmd}&sid={self._sid}"
                )
                resp = self._session_request(
                    "get",
                    url,
                    headers=self._headers,
                    timeout=15,
                )
                data = resp.json()

            if data.get("Status") != "Success":
                _LOGGER.warning(
                    "show-cmd '%s' failed (status-code %s): %s",
                    cmd,
                    data.get("Status-code"),
                    data.get("Error message"),
                )
            else:
                raw = data.get("Command output", "")
                output = raw.replace("\\n", "\n").replace("\\r", "\r")

        except requests.exceptions.JSONDecodeError:
            _LOGGER.warning(
                "Aruba IAP returned empty/invalid response for cmd '%s' "
                "(AP may be busy or session dropped) — will retry next poll",
                cmd,
            )
            self._sid = None
        except requests.exceptions.Timeout:
            _LOGGER.warning(
                "Aruba IAP timed out running cmd '%s' — will retry next poll",
                cmd,
            )
            self._sid = None
        except requests.exceptions.ConnectionError:
            _LOGGER.warning(
                "Aruba IAP connection error running cmd '%s' — AP may be unreachable",
                cmd,
            )
            self._sid = None
        except Exception:
            _LOGGER.exception("Aruba IAP show-cmd unexpected exception")
            self._sid = None

        return output

    def get_clients(self) -> dict[str, dict[str, Any]] | None:
        """
        Return connected clients keyed by MAC address.

        Returns None if the API call failed (e.g. no privilege).
        Returns {} if the call succeeded but no clients are connected.
        """
        output = self._show_cmd("show clients")
        if output is None:
            return None

        clients: dict[str, dict[str, Any]] = {}
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
                clients[mac] = {
                    "mac": mac,
                    "name": name,
                    "ip": match.group("ip"),
                    "os": match.group("os"),
                    "essid": match.group("essid"),
                    "access_point": match.group("access_point"),
                    "channel": match.group("channel"),
                    "signal": match.group("signal"),
                    "speed": match.group("speed"),
                }
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

    def test_connection(self) -> bool:
        """Test connectivity only (used by config flow login step)."""
        result = self.login()
        if result:
            self.logout()
        return result
