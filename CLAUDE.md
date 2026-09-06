# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (`custom_components/aruba_device_tracker`) that polls an Aruba Instant AP's local REST API for connected Wi-Fi clients and exposes them as `device_tracker` entities, plus `switch`/`number` entities for runtime configuration. Based on the `integration_blueprint_template`.

## Commands

```bash
scripts/setup    # install runtime + test deps (requirements.txt, requirements-test.txt)
scripts/develop  # run a local HA instance with this integration loaded, config in ./config/
scripts/lint     # ruff format . && ruff check . --fix
scripts/test     # pytest tests/ -v
```

Run a single test: `python -m pytest tests/test_aruba_client.py::test_name -v`

Linting is `ruff` with `select = ["ALL"]` (see `.ruff.toml`) — expect strict enforcement (docstrings, type annotations, complexity limits). Test files get relaxed rules (see `[lint.per-file-ignores]`). Target is `py314`. Run `scripts/lint` before considering a change done; CI runs both lint and tests on every push/PR.

## Architecture

**Data flow:** `ArubaIAPClient` (aruba_client.py) logs into the IAP's REST API, session-authenticates, and runs `show clients` via the `show-cmd` endpoint, parsing the CLI table output with a regex (`_CLIENT_REGEX`) into a `dict[mac, client_data]`. `ArubaIAPCoordinator` (`__init__.py`, a `DataUpdateCoordinator` subclass) polls this on `scan_interval` and holds the latest snapshot as `coordinator.data`.

**Entities read from the coordinator, they don't fetch data themselves:**
- `device_tracker.py` — `ArubaClientEntity` per MAC. `is_connected` reflects presence in `coordinator.data`; entities for *offline* devices still exist (seeded from `coordinator.last_seen`) so devices correctly restore as "away" instead of "unavailable" on HA restart.
- `switch.py` / `number.py` — control entities (track-new-devices toggle, cleanup enabled/days, etc.) attached to a synthetic "IAP control device" (`utils.get_device_info`), not to any tracked client.

**Persistent last-seen storage:** `coordinator.last_seen` (`dict[mac, iso_timestamp]`) is loaded/saved via HA's `Store` helper (`STORAGE_KEY`/`STORAGE_VERSION` in const.py) independently of the entity/device registries, so it survives restarts. It's loaded in `async_setup_entry` *before* `async_config_entry_first_refresh`, and is the source of truth both for seeding offline entities at startup and for stale-device cleanup (`_async_cleanup_stale_devices`, which removes entity-registry entries — not just marks them unavailable — once a MAC hasn't been seen for `cleanup_days`).

**Config/options split:** Settings (host/credentials, scan interval, track-new, cleanup enabled/days) are readable from either `entry.options` (runtime-changeable) or falling back to `entry.data` (set-once at config-flow time), via the pattern `entry.options.get(KEY, entry.data.get(KEY, DEFAULT))` — used consistently in the coordinator and the switch/number entities. There is deliberately **no** `update_listener`/reload wiring for options changes — see the long comment in `__init__.py`'s `async_setup_entry`: switch/number entities write straight to `entry.options`, and reloading on every options write previously caused spurious "entity no longer provided" banners. A reload is only triggered when host/credentials change, handled explicitly inside `config_flow.py`'s options flow.

**TLS quirk:** `aruba_client.py` handles Instant AOS firmware whose TLS stack needs legacy renegotiation (`UNSAFE_LEGACY_RENEGOTIATION_DISABLED` on modern OpenSSL). `_LegacyTLSAdapter` is mounted only after that specific SSLError is actually observed, then stays active for the life of the client instance.

## Testing conventions

- `tests/conftest.py` defines the fake AP host/port/URLs and fixtures (`client`, `logged_in_client`, `raw_output`); `requests_mock` (via `requests-mock`) mocks the REST endpoints — no real network/AP needed.
- `tests/fixtures/*.txt` hold raw `show clients` CLI output samples used to test the parsing regex against real-world formatting edge cases (empty names, missing speed column, etc.).
- Entity-level tests (e.g. `test_device_tracker_attributes.py`) use a minimal `FakeCoordinator` dataclass rather than spinning up Home Assistant, since entities only read from the coordinator's public attributes.
