"""Aruba Instant AP REST API client."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_LOGGER = logging.getLogger(__name__)

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

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> bool:
        """Login and store the session ID. Returns True on success."""
        url = f"{self.base_url}/login"
        payload = json.dumps({"user": self.username, "passwd": self.password})
        try:
            resp = requests.post(
                url,
                headers=self._headers,
                data=payload,
                verify=False,  # noqa: S501
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
        except Exception:  # noqa: BLE001
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
            requests.post(
                f"{self.base_url}/logout",
                headers=self._headers,
                data=json.dumps({"sid": self._sid}),
                verify=False,  # noqa: S501
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

        try:
            resp = requests.get(
                url,
                headers=self._headers,
                verify=False,  # noqa: S501
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
                resp = requests.get(
                    url,
                    headers=self._headers,
                    verify=False,  # noqa: S501
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
                return None

            output = data.get("Command output", "")
            return output.replace("\\n", "\n").replace("\\r", "\r")

        except requests.exceptions.JSONDecodeError:
            _LOGGER.warning(
                "Aruba IAP returned empty/invalid response for cmd '%s' "
                "(AP may be busy or session dropped) — will retry next poll",
                cmd,
            )
            self._sid = None
            return None
        except requests.exceptions.Timeout:
            _LOGGER.warning(
                "Aruba IAP timed out running cmd '%s' — will retry next poll",
                cmd,
            )
            self._sid = None
            return None
        except requests.exceptions.ConnectionError:
            _LOGGER.warning(
                "Aruba IAP connection error running cmd '%s' — AP may be unreachable",
                cmd,
            )
            self._sid = None
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Aruba IAP show-cmd unexpected exception")
            self._sid = None
            return None

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
                mac = match.group("mac").upper().replace("-", ":")
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
