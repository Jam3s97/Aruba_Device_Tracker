"""
Diagnostics download — what an issue report needs, without personal data.

The raw 'show aps' and 'show clients' tables are the most useful thing a user
can attach: they show exactly what their firmware prints. They are also full
of personal data (device names, MACs, IPs, SSIDs, serial numbers), and
async_redact_data can't help, because it redacts dict keys and each table is
one string. So identifying cells are replaced by placeholders *padded to the
cell's width*: column offsets survive, and a scrubbed table still parses
exactly like the original, which is what debugging a parser report needs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME

from .aruba_client import (
    _AP_COLUMN_ALIASES,
    _AP_REQUIRED_COLUMNS,
    _CLIENT_COLUMN_ALIASES,
    _CLIENT_REQUIRED_COLUMNS,
    _EMPTY_CELLS,
    _FOOTER_PREFIXES,
    _MAC_PATTERN,
    _NETWORK_COLUMN_ALIASES,
    _NETWORK_REQUIRED_COLUMNS,
    _find_columns,
    _is_rule,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from . import ArubaConfigEntry

TO_REDACT = {CONF_HOST, CONF_USERNAME, CONF_PASSWORD}

SHOW_APS = "show aps"
SHOW_CLIENTS = "show clients"
SHOW_NETWORK = "show network"

# Identifying columns, keyed as _find_columns names them, mapped to a
# placeholder family. A value seen in both tables gets the same placeholder in
# each (an AP's name in 'show aps' and in a client's Access Point column), so
# clients can still be matched to APs. Role shares SSIDs' family because
# Instant names a client's role after its SSID by default.
_CLIENT_FAMILIES = {
    "name": "client",
    "ip": "ipv4",
    "mac": "mac",
    "ipv6": "ipv6",
    "essid": "network",
    "role": "network",
    "accesspoint": "ap",
}
_AP_FAMILIES = {
    "name": "ap",
    "ip": "ipv4",
    "ipv6address": "ipv6",
    "serial": "serial",
    "zone": "zone",
    "linklocalipaddress": "ipv6",
}
# A network's ESSID shares the "network" family with the clients' ESSID
# column, so a client's SSID still matches its network.
_NETWORK_FAMILIES = {
    "profilename": "profile",
    "essid": "network",
    "zone": "zone",
}


def _documentation_ipv4(n: int) -> str:
    """Return the nth address from the IPv4 documentation ranges."""
    for prefix in ("192.0.2", "198.51.100", "203.0.113"):
        if n <= 254:  # noqa: PLR2004
            return f"{prefix}.{n}"
        n -= 254
    return "0.0.0.0"  # noqa: S104 - a placeholder, never bound to


_PLACEHOLDERS: dict[str, Callable[[int], str]] = {
    "client": lambda n: f"client-{n}",
    "ap": lambda n: f"AP-{n}",
    "network": lambda n: f"net-{n}",
    "ipv4": _documentation_ipv4,
    "ipv6": lambda n: f"2001:db8::{n:x}",
    # 00:00:5e:00:53:xx is the documentation MAC range (RFC 7042).
    "mac": lambda n: f"00:00:5e:00:{0x53 + (n >> 8):02x}:{n & 0xFF:02x}",
    "serial": lambda n: f"SERIAL{n:04d}",
    "zone": lambda n: f"zone-{n}",
    "profile": lambda n: f"profile-{n}",
}

_MAC_ANYWHERE = re.compile(_MAC_PATTERN, re.IGNORECASE)
_IPV4_ANYWHERE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_LETTER = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")


class _Scrubber:
    """Swaps identifying values for placeholders, consistently per family."""

    def __init__(self) -> None:
        self._maps: defaultdict[str, dict[str, str]] = defaultdict(dict)
        self._produced: set[str] = set()

    def placeholder(self, family: str, value: str) -> str:
        """Return the placeholder for ``value``, allocating one if new."""
        if value in _EMPTY_CELLS:
            return value
        # The conductor's IP carries a trailing '*'; keep the marker visible.
        core, marker = (value[:-1], "*") if value.endswith("*") else (value, "")
        mapping = self._maps[family]
        if core not in mapping:
            mapping[core] = _PLACEHOLDERS[family](len(mapping) + 1)
            self._produced.add(mapping[core])
        return mapping[core] + marker

    def table(
        self,
        output: str,
        required: frozenset[str],
        aliases: dict[str, str],
        families: dict[str, str],
    ) -> list[str]:
        """Scrub a CLI table cell by cell, keeping every column offset."""
        lines = output.splitlines()
        located = _find_columns(lines, required, aliases)
        if located is None:
            # No table to read by column, so nothing is known to be safe.
            # Keep only the layout: letters become 'a' and digits '9'.
            return [_DIGIT.sub("9", _LETTER.sub("a", line)) for line in lines]

        header_index, columns = located
        ends = [start for _, start in columns[1:]] + [None]
        scrubbed = lines[: header_index + 1]
        for line in lines[header_index + 1 :]:
            stripped = line.strip()
            if (
                not stripped
                or _is_rule(stripped)
                or stripped.lower().startswith(_FOOTER_PREFIXES)
            ):
                scrubbed.append(line)
                continue
            for (key, start), end in zip(columns, ends, strict=True):
                family = families.get(key)
                if family is not None and start < len(line):
                    line = self._cell(line, start, end, family)  # noqa: PLW2901
            scrubbed.append(self._safety_net(line.rstrip()))
        return scrubbed

    def _cell(self, line: str, start: int, end: int | None, family: str) -> str:
        """Replace one cell, padded or trimmed to the cell's original width."""
        cell = line[start:end]
        value = cell.strip()
        if not value:
            return line
        replacement = self.placeholder(family, value)
        if end is None:
            return line[:start] + replacement
        # Keep at least one space before the next column.
        width = len(cell)
        return line[:start] + replacement[: max(width - 1, 1)].ljust(width) + line[end:]

    def _safety_net(self, line: str) -> str:
        """Catch any MAC or IPv4 address outside the columns scrubbed above."""

        def _swap(family: str) -> Callable[[re.Match[str]], str]:
            def _replace(match: re.Match[str]) -> str:
                value = match.group()
                if value in self._produced:
                    return value
                return self.placeholder(family, value)[: len(value)].ljust(len(value))

            return _replace

        line = _MAC_ANYWHERE.sub(_swap("mac"), line)
        return _IPV4_ANYWHERE.sub(_swap("ipv4"), line)


def _raw_table(
    scrubber: _Scrubber,
    output: str | None,
    required: frozenset[str],
    aliases: dict[str, str],
    families: dict[str, str],
) -> dict[str, Any] | None:
    """Return a scrubbed table as a list of lines, or None if never fetched."""
    if output is None:
        return None
    lines = scrubber.table(output, required, aliases, families)
    return {"line_count": len(lines), "lines": lines}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ArubaConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    outputs = coordinator.client.last_outputs
    scrubber = _Scrubber()

    # 'show aps' first, so AP placeholders follow the AP table's order.
    show_aps = _raw_table(
        scrubber,
        outputs.get(SHOW_APS),
        _AP_REQUIRED_COLUMNS,
        _AP_COLUMN_ALIASES,
        _AP_FAMILIES,
    )
    # Then 'show network', so SSID placeholders follow the network list.
    show_network = _raw_table(
        scrubber,
        outputs.get(SHOW_NETWORK),
        _NETWORK_REQUIRED_COLUMNS,
        _NETWORK_COLUMN_ALIASES,
        _NETWORK_FAMILIES,
    )
    show_clients = _raw_table(
        scrubber,
        outputs.get(SHOW_CLIENTS),
        _CLIENT_REQUIRED_COLUMNS,
        _CLIENT_COLUMN_ALIASES,
        _CLIENT_FAMILIES,
    )

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "clients_connected": len(coordinator.data or {}),
            "devices_remembered": len(coordinator.last_seen),
            "ap_devices_enabled": coordinator.ap_devices,
            "ap_list_ok": coordinator.aps_ok,
            "network_list_ok": coordinator.networks_ok,
        },
        "access_points": [
            {
                "name": scrubber.placeholder("ap", ap["name"]),
                "model": ap["model"],
                "is_conductor": ap["is_conductor"],
                "clients_reported": ap["clients"],
                "clients_counted": coordinator.ap_client_counts.get(serial),
            }
            for serial, ap in coordinator.aps.items()
        ],
        "networks": [
            {
                "essid": scrubber.placeholder("network", network["essid"]),
                "enabled": network["enabled"],
                "clients_reported": network["clients"],
                "clients_counted": coordinator.network_client_counts.get(profile),
            }
            for profile, network in coordinator.networks.items()
        ],
        "show_aps": show_aps,
        "show_network": show_network,
        "show_clients": show_clients,
    }
