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
# Columns: Name  IP  MAC  OS  ESSID  AccessPoint  Channel  Type  Role  IPv6  Signal  Speed
_CLIENT_REGEX = re.compile(
    r"^(?P<name>\S+)\s+"
    r"(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\s+"
    r"(?P<mac>(?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+"
    r"(?P<os>\S+)\s+"
    r"(?P<essid>\S+)\s+"
    r"(?P<access_point>\S+)\s+"
    r"(?P<channel>\S+)\s+"
    r"(?P<type>\S+)\s+"
    r"(?P<role>\S+)\s+"
    r"(?P<ipv6>\S+)\s+"
    r"(?P<signal>\S+)\s+"
    r"(?P<speed>\S+)",
    re.IGNORECASE,
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
        """
        Run a show command and return the raw CLI output string.
        Returns None if the command fails (including privilege errors).
        """
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
                return None  # None = command failed (permissions, bad cmd, etc.)

            output = data.get("Command output", "")
            return output.replace("\\n", "\n").replace("\\r", "\r")

        except Exception as err:
            _LOGGER.error("Aruba IAP show-cmd exception: %s", err)
            self._sid = None
            return None

    def get_clients(self) -> dict[str, dict[str, Any]] | None:
        """
        Return a dict of connected clients keyed by MAC address.
        Returns None if the API call itself failed (e.g. no privilege).
        Returns {} if the call succeeded but no clients are connected.
        """
        output = self._show_cmd("show clients")
        if output is None:
            return None  # Distinguish API failure from empty network

        clients: dict[str, dict[str, Any]] = {}
        for line in output.splitlines():
            match = _CLIENT_REGEX.match(line.strip())
            if not match:
                continue
            mac = match.group("mac").upper()
            clients[mac] = {
                "mac": mac,
                "name": match.group("name"),
                "ip": match.group("ip"),
                "os": match.group("os"),
                "essid": match.group("essid"),
                "access_point": match.group("access_point"),
                "channel": match.group("channel"),
                "signal": match.group("signal"),
                "speed": match.group("speed"),
            }
        _LOGGER.debug("Aruba IAP found %d clients", len(clients))
        return clients

    def test_connection(self) -> bool:
        """Test connectivity only (used by config flow login step)."""
        result = self.login()
        if result:
            self.logout()
        return result
