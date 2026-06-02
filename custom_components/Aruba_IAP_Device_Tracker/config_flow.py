"""Config flow for Aruba IAP Device Tracker."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .aruba_client import ArubaIAPClient
from .const import CONF_TRACK_NEW, DEFAULT_TRACK_NEW, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _test_connection(hass, host, username, password) -> str | None:
    """
    Test connection AND verify the account can actually run API commands.
    Returns None on success, or an error key string on failure.
    """
    client = ArubaIAPClient(host=host, username=username, password=password)
    try:
        # Step 1: can we log in?
        logged_in = await hass.async_add_executor_job(client.login)
        if not logged_in:
            return "invalid_auth"

        # Step 2: can we actually run a show command? Accounts with no API
        # privilege will log in but return errors on every command.
        clients = await hass.async_add_executor_job(client.get_clients)
        await hass.async_add_executor_job(client.logout)

        # get_clients returns {} on API error — but also on an empty network.
        # So we do one more check: if login succeeded but get_clients returned
        # None (which we map to {}), treat as privilege error.
        if clients is None:
            return "api_access_denied"

        return None  # success
    except Exception as err:
        _LOGGER.debug("Aruba IAP connection test exception: %s", err)
        return "cannot_connect"


class ArubaIAPConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aruba IAP Device Tracker."""

    VERSION = 1

    # ------------------------------------------------------------------
    # Initial setup — Step 1: connection details
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
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
                    vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                    vol.Required(CONF_USERNAME, default=(user_input or {}).get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Initial setup — Step 2: tracking preference
    # ------------------------------------------------------------------

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            data = {**self._connection_data, CONF_TRACK_NEW: user_input[CONF_TRACK_NEW]}
            return self.async_create_entry(
                title=f"Aruba IAP ({self._connection_data[CONF_HOST]})",
                data=data,
            )

        return self.async_show_form(
            step_id="tracking",
            data_schema=vol.Schema(
                {vol.Optional(CONF_TRACK_NEW, default=DEFAULT_TRACK_NEW): bool}
            ),
        )

    # ------------------------------------------------------------------
    # Options flow entry point
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ArubaIAPOptionsFlow:
        return ArubaIAPOptionsFlow(config_entry)


class ArubaIAPOptionsFlow(config_entries.OptionsFlow):
    """Options flow — change host/credentials/tracking after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        # NOTE: Do NOT call super().__init__() — it breaks on some HA versions.
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.data

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            track_new = user_input[CONF_TRACK_NEW]

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
                    },
                )
                return self.async_create_entry(title="", data={CONF_TRACK_NEW: track_new})

        track_new_current = self.config_entry.options.get(
            CONF_TRACK_NEW,
            current.get(CONF_TRACK_NEW, DEFAULT_TRACK_NEW),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
                    vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, "")): str,
                    vol.Required(CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")): str,
                    vol.Optional(CONF_TRACK_NEW, default=track_new_current): bool,
                }
            ),
            errors=errors,
        )
