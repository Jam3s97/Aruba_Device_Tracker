"""Config flow for Aruba Device Tracker."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .aruba_client import ArubaIAPClient
from .const import (
    CONF_CLEANUP_DAYS,
    CONF_CLEANUP_ENABLED,
    CONF_SCAN_INTERVAL,
    CONF_TRACK_NEW,
    DEFAULT_CLEANUP_DAYS,
    DEFAULT_CLEANUP_ENABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TRACK_NEW,
    DOMAIN,
    MAX_CLEANUP_DAYS,
    MAX_SCAN_INTERVAL,
    MIN_CLEANUP_DAYS,
    MIN_SCAN_INTERVAL,
)

LOGGER = logging.getLogger(__name__)


async def _test_connection(
    hass: HomeAssistant,
    host: str,
    username: str,
    password: str,
) -> str | None:
    """Test connectivity and API privilege. Returns None on success or an error key."""
    client = ArubaIAPClient(host=host, username=username, password=password)
    try:
        logged_in = await hass.async_add_executor_job(client.login)
        if not logged_in:
            return "invalid_auth"
        clients = await hass.async_add_executor_job(client.get_clients)
        await hass.async_add_executor_job(client.logout)
        if clients is None:
            return "api_access_denied"
    except Exception:  # noqa: BLE001
        LOGGER.debug("Aruba IAP connection test exception", exc_info=True)
        return "cannot_connect"
    else:
        return None


class ArubaIAPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aruba Device Tracker."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial user step — connection details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            error_key = await _test_connection(self.hass, host, username, password)
            if error_key:
                errors["base"] = error_key
            else:
                self._connection_data = {
                    CONF_HOST: host,
                    CONF_USERNAME: username,
                    CONF_PASSWORD: password,
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
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
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
    ) -> config_entries.FlowResult:
        """Manage the options form."""
        errors: dict[str, str] = {}
        current = self.config_entry.data
        current_options = self.config_entry.options

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            track_new = user_input[CONF_TRACK_NEW]
            scan_interval = user_input[CONF_SCAN_INTERVAL]
            cleanup_enabled = user_input[CONF_CLEANUP_ENABLED]
            cleanup_days = user_input[CONF_CLEANUP_DAYS]

            connection_changed = (
                host != current.get(CONF_HOST)
                or username != current.get(CONF_USERNAME)
                or password != current.get(CONF_PASSWORD)
            )

            if connection_changed:
                error_key = await _test_connection(self.hass, host, username, password)
                if error_key:
                    errors["base"] = error_key

            if not errors:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        CONF_HOST: host,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                        CONF_TRACK_NEW: track_new,
                        CONF_SCAN_INTERVAL: scan_interval,
                        CONF_CLEANUP_ENABLED: cleanup_enabled,
                        CONF_CLEANUP_DAYS: cleanup_days,
                    },
                )
                if connection_changed:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(
                            self.config_entry.entry_id
                        )
                    )
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_TRACK_NEW: track_new,
                        CONF_SCAN_INTERVAL: scan_interval,
                        CONF_CLEANUP_ENABLED: cleanup_enabled,
                        CONF_CLEANUP_DAYS: cleanup_days,
                    },
                )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                    vol.Required(
                        CONF_USERNAME, default=current.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(
                        CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")
                    ): str,
                    vol.Optional(
                        CONF_TRACK_NEW,
                        default=current_options.get(
                            CONF_TRACK_NEW,
                            current.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=current_options.get(
                            CONF_SCAN_INTERVAL,
                            current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    ),
                    vol.Optional(
                        CONF_CLEANUP_ENABLED,
                        default=current_options.get(
                            CONF_CLEANUP_ENABLED,
                            current.get(CONF_CLEANUP_ENABLED, DEFAULT_CLEANUP_ENABLED),
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_CLEANUP_DAYS,
                        default=current_options.get(
                            CONF_CLEANUP_DAYS,
                            current.get(CONF_CLEANUP_DAYS, DEFAULT_CLEANUP_DAYS),
                        ),
                    ): vol.All(
                        int, vol.Range(min=MIN_CLEANUP_DAYS, max=MAX_CLEANUP_DAYS)
                    ),
                }
            ),
            errors=errors,
        )
