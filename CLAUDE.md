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

**Data flow:** `ArubaIAPClient` (aruba_client.py) logs into the IAP's REST API, session-authenticates, and runs `show clients` via the `show-cmd` endpoint, parsing the CLI table output with a regex (`_CLIENT_REGEX`) into a `dict[mac, ArubaClientData]` (a `TypedDict`). `ArubaIAPCoordinator` (`__init__.py`, a `DataUpdateCoordinator[dict[str, ArubaClientData]]` subclass) polls this on `scan_interval` and holds the latest snapshot as `coordinator.data`. Entries are typed as `ArubaConfigEntry = ConfigEntry[ArubaIAPCoordinator]`, and the coordinator is reached via `entry.runtime_data`.

**Error contract:** the client raises `ArubaAuthError` / `ArubaConnectionError` / `ArubaCertificateError` (all `ArubaError`) rather than returning bools. `async_setup_entry` maps these to `ConfigEntryAuthFailed` (starts the reauth flow in `config_flow.py`), `ConfigEntryNotReady` (HA retries with backoff) and `ConfigEntryError` (permanent — retrying an untrusted certificate can never succeed). Inside `_show_cmd`, connection-class failures stay *soft* — log a warning, clear the session id, return `None`, keep last-known data — while `ArubaAuthError` and `ArubaCertificateError` propagate, the former so the coordinator can trigger reauth, the latter because it is a configuration problem needing user action. `ArubaCertificateError` is deliberately **not** a subclass of `ArubaConnectionError`, which is what keeps it out of that soft path. Never interpolate a raw `requests` exception into a user-visible message: the IAP takes `sid` as a URL query parameter and requests embeds the URL in its exception text. `ArubaIAPClient._redact` exists for this.

**Entities read from the coordinator, they don't fetch data themselves:**
- `device_tracker.py` — `ArubaClientEntity` per MAC. `is_connected` reflects presence in `coordinator.data`; entities for *offline* devices still exist (seeded from `coordinator.last_seen`) so devices correctly restore as "away" instead of "unavailable" on HA restart.
- `switch.py` / `number.py` — control entities (track-new-devices toggle, cleanup enabled/days, etc.) attached to a synthetic "IAP control device" (`utils.get_device_info`), not to any tracked client.

**Recorder write volume (why attributes look sparse):** HA writes a recorder row whenever an entity's state *or any attribute* differs from the previous one — `StateMachine.async_set` early-returns and fires only `EVENT_STATE_REPORTED` when both match, and the recorder subscribes to `EVENT_STATE_CHANGED` only. A tracker's state changes rarely, so attributes drive database growth: one per-poll attribute = one row per device per poll (~2,880/day/device at the default 30s interval).

`ArubaClientEntity.extra_state_attributes` is therefore built so a device whose situation is unchanged produces *no* rows:
- `signal` and `speed` are **not published**. `_CLIENT_REGEX` still captures them and `ArubaClientData` still carries them (the regex needs those columns to match the row layout, and they're useful in debug logs) — they're just never surfaced as attributes, because their values move almost every poll.
- `last_seen` / `days_until_cleanup` are published **only while the device is away**. For a connected device `last_seen` is by definition ~now, so it changed every poll while conveying nothing the `home` state didn't. Away devices have a frozen timestamp, so both attributes are stable.
- What's left (`access_point`, `essid`, `ip_address`, `os`, `channel`) only moves on a real change: roam, new IP, band switch.

`tests/test_device_tracker_attributes.py::TestRecorderChurn` pins this invariant — notably that changing `signal`/`speed` in coordinator data leaves the attribute dict byte-identical. Don't add a per-poll attribute without re-reading this; it is a silent, compounding cost that no test other than those will catch. Removing `signal`/`speed` and gating `last_seen` was a deliberate breaking change in 2.0.0, documented in the README.

**Persistent last-seen storage:** `coordinator.last_seen` (`dict[mac, iso_timestamp]`) is loaded/saved via HA's `Store` helper independently of the entity/device registries, so it survives restarts. The storage key is **per config entry** (`f"{DOMAIN}.{entry_id}.last_seen"`) — a shared key made two APs overwrite each other; `LEGACY_STORAGE_KEY` in const.py is the pre-2.0 shared key, still read once by `_async_migrate_legacy_store` to adopt existing installs (the old file is left in place). Writes go through `Store.async_delay_save` (`STORAGE_SAVE_DELAY`), never `async_save` on every poll. `last_seen` is loaded in `async_setup_entry` *before* `async_config_entry_first_refresh`, and is the source of truth both for seeding offline entities at startup and for stale-device cleanup.

**Stale-device cleanup:** `_async_cleanup_stale_devices` removes entity-registry entries — not just marks them unavailable — once a MAC hasn't been seen for `cleanup_days`. It is throttled to `CLEANUP_INTERVAL_HOURS` (staleness is measured in days, so a registry scan every poll buys nothing). Two subtleties: `er.async_get_entity_id` is **not** scoped to a config entry, so the owning `config_entry_id` is verified before removing anything; and removals are broadcast to `coordinator.async_add_removal_listener` subscribers so `device_tracker.py` can drop the MAC from its `tracked` set — otherwise a cleaned-up device that reconnected would never get a new entity.

**Config/options split:** Settings (host/credentials, scan interval, track-new, cleanup enabled/days, verify_ssl) are readable from either `entry.options` (runtime-changeable) or falling back to `entry.data` (set-once at config-flow time), via the pattern `entry.options.get(KEY, entry.data.get(KEY, DEFAULT))` — wrapped as `_option()` in `__init__.py` and used consistently in the coordinator and the switch/number entities. There is deliberately **no** `update_listener`/reload wiring for options changes — see the long comment in `__init__.py`'s `async_setup_entry`: switch/number entities write straight to `entry.options`, and reloading on every options write previously caused spurious "entity no longer provided" banners. A reload is only triggered when host/credentials change, handled explicitly inside `config_flow.py`'s options flow.

**TLS:** certificate verification is controlled by the `verify_ssl` option and defaults to **off**, because Instant APs ship a self-signed certificate. `urllib3.disable_warnings` is called lazily (once per process, only when an unverified request is actually made) rather than at import time, so installing this integration doesn't silence TLS warnings for the whole HA process.

When verification is on and the AP's certificate can't be validated, `_request_once` turns the `CERTIFICATE_VERIFY_FAILED` `SSLError` into `ArubaCertificateError` carrying an actionable message, and the config flow surfaces it as the `invalid_cert` error key instead of the misleading `cannot_connect`. The raw `SSLError` is never logged with a traceback — the cause is known and the urllib3/ssl stack is ~90 lines of noise.

Separately, `aruba_client.py` handles Instant AOS firmware whose TLS stack needs legacy renegotiation (`UNSAFE_LEGACY_RENEGOTIATION_DISABLED` on modern OpenSSL). `_LegacyTLSAdapter` is mounted only after that specific SSLError is actually observed, then stays active for the life of the client instance. Renegotiation and verification are orthogonal: the adapter relaxes only the renegotiation flag and honours `verify_ssl` for cert checking.

## Testing conventions

- `tests/conftest.py` defines the fake AP host/port/URLs and fixtures (`client`, `logged_in_client`, `raw_output`); `requests_mock` (via `requests-mock`) mocks the REST endpoints — no real network/AP needed.
- `tests/fixtures/*.txt` hold raw `show clients` CLI output samples used to test the parsing regex against real-world formatting edge cases (empty names, missing speed column, etc.).
- Entity-level tests (e.g. `test_device_tracker_attributes.py`) use a minimal `FakeCoordinator` dataclass rather than spinning up Home Assistant, since entities only read from the coordinator's public attributes.
- `test_coordinator_cleanup.py` follows the same philosophy for coordinator logic: it builds a *real* `ArubaIAPCoordinator` via `object.__new__`, assigns fakes for `hass`/`Store`/`config_entry`, patches `er.async_get`, and drives the coroutines with `asyncio.run`. There is no `pytest-asyncio` or `pytest-homeassistant-custom-component` in `requirements-test.txt` — adding the latter is the prerequisite for testing the config/options/reauth flows and real `Store` migration end to end.

## Deferred decisions

Things deliberately left alone, with the reasoning, so they get re-decided rather than re-discovered.

### Single HA version for dev and floor (revisit)

`requirements.txt` and `hacs.json` both pin `homeassistant==2026.3.2`, and they are kept equal on purpose. `hacs.json`'s `homeassistant` key is the minimum version HACS enforces as an install gate for end users; `requirements.txt` is what CI and `scripts/develop` install. Keeping them equal means CI actually tests the version users are promised.

A dev-on-latest / floor-on-oldest split was tried and reverted. It is the better long-term setup — newer cores emit the deprecation and `frame.report_usage` warnings that are the main early warning for breaking changes, which is how the coordinator `config_entry` issue was found — but it is only safe once **CI verifies both ends**. Right now nothing does, so a split silently turns the `hacs.json` minimum into an unverified claim: green CI while shipping a newer-only API to users on the floor version.

Revisit when worth the CI change. What it needs:
- A test matrix in `.github/workflows/test.yml` over `[floor, dev]`, installing `requirements.txt` then overriding the HA pin per matrix entry.
- The floor read from `hacs.json` (`jq -r .homeassistant hacs.json`) rather than duplicated, so the two cannot drift.
- `dependabot.yml` already ignores `homeassistant` so it stays matched to `hacs.json`; a split needs that reasoning revisited too.
- Decide how wide the support window should actually be. 2026.3 -> 2026.9 is ~6 months, which is only worth claiming if it is genuinely tested.

Verified 2026-09-29: the integration passes its full suite and imports cleanly on **both** 2026.3.2 and 2026.9.4, and every HA API it uses (`_get_reauth_entry`, `async_update_reload_and_abort(data_updates=...)`, coordinator `config_entry=`, `Store.async_delay_save`, `TextSelectorType.PASSWORD`) exists on the 2026.3.2 floor.

### pytest-homeassistant-custom-component (parked)

Not added. It is the only practical way to cover the config flow, options flow, reauth flow and the real `Store` per-entry migration, which currently have no automated tests. Costs measured 2026-09-29:
- Pins `pytest` exactly and conflicts with this repo's pin (`pip install phcc==0.13.318 pytest==9.1.1` -> `ResolutionImpossible`); PHCC must own the pytest version.
- Pins `homeassistant==` exactly, one release per HA release, which fights the deliberate HA pin. Dependabot runs daily on pip and would bump PHCC (and therefore HA) unless PHCC is added to the ignore list beside `homeassistant`.
- ~30 transitive deps (SQLAlchemy, numpy, pydantic, paho-mqtt, syrupy, respx, pytest-xdist).
- Breaks 25 of 63 existing tests on install: its autouse fixtures call `asyncio.get_event_loop()`, and `test_coordinator_cleanup.py` uses bare `asyncio.run()`, which closes the loop. Client (37) and entity (10) tests are unaffected — so the work is converting those 16 tests to `pytest-asyncio`, not a compatibility wall.

If picked up: match the PHCC release to the pinned HA version (`0.13.318` for 2026.3.2), drop the explicit `pytest` pin, set `asyncio_mode`, add PHCC to the dependabot ignore list, and convert the `asyncio.run()` tests.
