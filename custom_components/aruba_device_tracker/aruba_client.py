"""Aruba Instant AP REST API client."""

from __future__ import annotations

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
# Columns: Name  IP  MAC  OS  ESSID  AccessPoint  Channel  Type  Role  IPv6  Signal  Speed
#
# Notes:
#   - Name may contain spaces (e.g. "My Smart TV"), so we match up to the first
#     run of 2+ spaces rather than using \S+.
#   - IPv6 may be "--" when not assigned.
#   - Speed is optional — some rows omit it.
#   - MAC separators may be ":" or "-".
_CLIENT_REGEX = re.compile(
    r"^(?P<name>[^\n]*?)\s{2,}"
    r"(?=(?:\d{1,3}\.){3}\d{1,3}\s)"     # lookahead: must be followed by an IP
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

# Lines that start with these strings are header/separator rows — skip silently
_SKIP_PREFIXES = (
    "name", "----", "client list", "num ", "total",
    "cli output", "command=", "number of", "info timestamp",
)


class ArubaIAPClient:
    """Client for the Aruba Instant AP REST API."""

    def __init__(self, host: str, username: str, password: str, port: int = 4343) -> None:
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
                url, headers=self._headers, data=payload, verify=False, timeout=10
            )
            data = resp.json()
            if data.get("Status") == "Success" and data.get("sid"):
                self._sid = data["sid"]
                _LOGGER.debug("Aruba IAP login successful, sid=%s", self._sid)
                return True
            _LOGGER.warning("Aruba IAP login failed: %s", data.get("Error message"))
            return False
        except Exception as err:
            _LOGGER.error("Aruba IAP login exception: %s", err)
            return False

    def logout(self) -> None:
        """Logout and clear the session ID."""
        if not self._sid:
            return
        try:
            requests.post(
                f"{self.base_url}/logout",
                headers=self._headers,
                data=json.dumps({"sid": self._sid}),
                verify=False,
                timeout=10,
            )
        except Exception:
            pass
        self._sid = None

    def _ensure_session(self) -> bool:
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
        url = f"{self.base_url}/show-cmd?iap_ip_addr={self.host}&cmd={encoded_cmd}&sid={self._sid}"

        try:
            resp = requests.get(url, headers=self._headers, verify=False, timeout=15)
            data = resp.json()

            # Session expired — re-login once and retry
            if data.get("Status-code") == 1:
                _LOGGER.debug("Session expired, re-logging in")
                self._sid = None
                if not self.login():
                    return None
                encoded_cmd = cmd.replace(" ", "%20")
                url = f"{self.base_url}/show-cmd?iap_ip_addr={self.host}&cmd={encoded_cmd}&sid={self._sid}"
                resp = requests.get(url, headers=self._headers, verify=False, timeout=15)
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

        except Exception as err:
            _LOGGER.error("Aruba IAP show-cmd exception: %s", err)
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

            # Skip known header/separator/footer lines silently
            if not stripped or stripped.lower().startswith(_SKIP_PREFIXES):
                continue

            # Match on the RAW line (not stripped) so that empty-name rows
            # retain their leading whitespace, allowing the regex to find
            # the name=="" + spaces + IP pattern correctly.
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
