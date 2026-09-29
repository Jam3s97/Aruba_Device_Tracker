"""Config flow for Aruba Device Tracker."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .aruba_client import (
    ArubaAuthError,
    ArubaCertificateError,
    ArubaConnectionError,
    ArubaIAPClient,
)
from .const import (
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_TRACK_NEW,
    CONF_VERIFY_SSL,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TRACK_NEW,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_CLEANUP_DAYS,
    MAX_SCAN_INTERVAL,
    MIN_CLEANUP_DAYS,
    MIN_SCAN_INTERVAL,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)

# Credential fields use an explicit password selector so the frontend masks
# them regardless of field naming.
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


async def _test_connection(
    hass: HomeAssistant,
    host: str,
    username: str,
    password: str,
    *,
    verify_ssl: bool = DEFAULT_VERIFY_SSL,
) -> str | None:
    """Test connectivity and API privilege. Returns None on success or an error key."""
    client = ArubaIAPClient(
        host=host, username=username, password=password, verify_ssl=verify_ssl
    )
    try:
        await hass.async_add_executor_job(client.login)
        clients = await hass.async_add_executor_job(client.get_clients)
        await hass.async_add_executor_job(client.logout)
    except ArubaAuthError:
        LOGGER.debug("Aruba IAP rejected the supplied credentials")
        return "invalid_auth"
    except ArubaCertificateError:
        # No traceback: the cause is known and the ~90-line urllib3/ssl stack
        # adds nothing but noise to the log.
        LOGGER.debug("Aruba IAP certificate verification failed for %s", host)
        return "invalid_cert"
    except ArubaConnectionError as err:
        # No traceback: the client already built a one-line diagnosis naming the
        # host and the failure mode (timed out / unreachable / bad response), so
        # the ~95-line urllib3 stack underneath it is pure noise. str(err) is
        # built by the client and never embeds the request URL, so it carries no
        # session token.
        LOGGER.debug("Aruba IAP connection test failed: %s", err)
        return "cannot_connect"
    except Exception:
        LOGGER.exception("Unexpected error during Aruba IAP connection test")
        return "unknown"
    else:
        if clients is None:
            return "api_access_denied"
        return None
    finally:
        await hass.async_add_executor_job(client.close)


class ArubaIAPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aruba Device Tracker."""

    VERSION = 1

    # Populated by async_step_user and consumed by async_step_tracking. Declared
    # here so reaching step two out of order fails cleanly rather than with an
    # AttributeError.
    _connection_data: dict[str, Any] = {}  # noqa: RUF012

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial user step — connection details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            verify_ssl = user_input.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            error_key = await _test_connection(
                self.hass, host, username, password, verify_ssl=verify_ssl
            )
            if error_key:
                errors["base"] = error_key
            else:
                self._connection_data = {
                    CONF_HOST: host,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                    CONF_VERIFY_SSL: verify_ssl,
                }
                return await self.async_step_tracking()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=(user_input or {}).get(CONF_HOST, ""),
                    ): str,
                    vol.Required(
                        CONF_USERNAME,
                        default=(user_input or {}).get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=(user_input or {}).get(
                            CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
                        ),
                    ): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle step 2 — tracking and polling preferences."""
        if user_input is not None:
            data = {
                **self._connection_data,
                CONF_TRACK_NEW: user_input[CONF_TRACK_NEW],
                CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL],
                CONF_CLEANUP_ENABLED: user_input[CONF_CLEANUP_ENABLED],
                CONF_CLEANUP_DAYS: user_input[CONF_CLEANUP_DAYS],
            }
            return self.async_create_entry(
                title=f"Aruba IAP ({self._connection_data[CONF_HOST]})",
                data=data,
            )

        return self.async_show_form(
            step_id="tracking",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_TRACK_NEW, default=DEFAULT_TRACK_NEW): bool,
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): vol.All(
                        int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Optional(
                        CONF_CLEANUP_ENABLED, default=DEFAULT_CLEANUP_ENABLED
                    ): bool,
                    vol.Optional(
                        CONF_CLEANUP_DAYS, default=DEFAULT_CLEANUP_DAYS
                    ): vol.All(
                        int, vol.Range(min=MIN_CLEANUP_DAYS, max=MAX_CLEANUP_DAYS)
                    ),
                }
            ),
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],  # noqa: ARG002
    ) -> config_entries.ConfigFlowResult:
        """Handle re-authentication after the AP rejected our credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Prompt for fresh credentials and verify them before saving."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            error_key = await _test_connection(
                self.hass,
                entry.data[CONF_HOST],
                username,
                password,
                verify_ssl=entry.options.get(
                    CONF_VERIFY_SSL,
                    entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                ),
            )
            if error_key:
                errors["base"] = error_key
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
                }
            ),
            description_placeholders={"host": entry.data.get(CONF_HOST, "")},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,  # noqa: ARG004
    ) -> ArubaIAPOptionsFlow:
        """Return the options flow handler."""
        return ArubaIAPOptionsFlow()


class ArubaIAPOptionsFlow(config_entries.OptionsFlow):
    """Options flow — change host/credentials/tracking/polling after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Manage the options form."""
        errors: dict[str, str] = {}
        current = self.config_entry.data
        current_options = self.config_entry.options

        def _current(key: str, default: Any) -> Any:
            return current_options.get(key, current.get(key, default))

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            # The password is deliberately not pre-filled into the form, so a
            # blank submission means "keep the stored password".
            password = user_input.get(CONF_PASSWORD) or current.get(CONF_PASSWORD, "")
            verify_ssl = user_input[CONF_VERIFY_SSL]
            track_new = user_input[CONF_TRACK_NEW]
            scan_interval = user_input[CONF_SCAN_INTERVAL]
            cleanup_enabled = user_input[CONF_CLEANUP_ENABLED]
            cleanup_days = user_input[CONF_CLEANUP_DAYS]

            connection_changed = (
                host != current.get(CONF_HOST)
                or username != current.get(CONF_USERNAME)
                or password != current.get(CONF_PASSWORD)
                or verify_ssl != _current(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            )

            if connection_changed:
                error_key = await _test_connection(
                    self.hass, host, username, password, verify_ssl=verify_ssl
                )
                if error_key:
                    errors["base"] = error_key

            if not errors:
                # data holds connection fields only; options holds everything
                # runtime-changeable. Options here are written via async_update_entry
                # directly rather than via the return self.async_create_entry(...)
                # auto-options mechanism, so both dicts land in one call. Keeping
                # data scoped to connection fields (instead of also duplicating
                # track_new/scan_interval/cleanup into it, as before) avoids a
                # stale, unused copy of those settings sitting in entry.data.
                old_scan_interval = _current(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

                data_update = {
                    CONF_HOST: host,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
                }
                options_update = {
                    CONF_VERIFY_SSL: verify_ssl,
                    CONF_TRACK_NEW: track_new,
                    CONF_SCAN_INTERVAL: scan_interval,
                    CONF_CLEANUP_ENABLED: cleanup_enabled,
                    CONF_CLEANUP_DAYS: cleanup_days,
                }

                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=data_update,
                    options=options_update,
                )

                LOGGER.debug(
                    "Aruba Device Tracker options updated via options form: "
                    "track_new=%s, scan_interval=%ds, cleanup_enabled=%s, "
                    "cleanup_days=%d, verify_ssl=%s",
                    track_new,
                    scan_interval,
                    cleanup_enabled,
                    cleanup_days,
                    verify_ssl,
                )

                if connection_changed:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self.config_entry.entry_id
                        )
                    )
                elif scan_interval != old_scan_interval:
                    coordinator = self.config_entry.runtime_data
                    if coordinator is not None:
                        coordinator.update_interval = timedelta(seconds=scan_interval)

                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                    vol.Required(
                        CONF_USERNAME, default=current.get(CONF_USERNAME, "")
                    ): str,
                    vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        default=_current(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                    ): bool,
                    vol.Optional(
                        CONF_TRACK_NEW,
                        default=_current(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
                    ): bool,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=_current(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(
                        int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Optional(
                        CONF_CLEANUP_ENABLED,
                        default=_current(CONF_CLEANUP_ENABLED, DEFAULT_CLEANUP_ENABLED),
                    ): bool,
                    vol.Optional(
                        CONF_CLEANUP_DAYS,
                        default=_current(CONF_CLEANUP_DAYS, DEFAULT_CLEANUP_DAYS),
                    ): vol.All(
                        int, vol.Range(min=MIN_CLEANUP_DAYS, max=MAX_CLEANUP_DAYS)
                    ),
                }
            ),
            errors=errors,
        )
