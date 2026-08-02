"""Config flow for the Magic Mic integration."""

from typing import Any, override

import anthropic
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY
from homeassistant.core import callback

from .const import DOMAIN, LOGGER
from .internal.claude.const import CONF_WEB_FETCH, CONF_WEB_SEARCH, DEFAULT
from .internal.claude.coordinator import async_create_client

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})


class MagicMicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Magic Mic."""

    VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> "MagicMicOptionsFlow":
        """Return the provider options flow."""
        return MagicMicOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step: validate the API key, then create the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                client = await async_create_client(self.hass, user_input[CONF_API_KEY])
                await client.models.list(timeout=10.0)
            except anthropic.AuthenticationError:
                errors["base"] = "invalid_auth"
            except anthropic.AnthropicError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Unexpected exception validating the Claude API key")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="Magic Mic",
                    data={CONF_API_KEY: user_input[CONF_API_KEY]},
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )


class MagicMicOptionsFlow(OptionsFlowWithReload):
    """Configure the embedded Claude provider."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure provider-native web capabilities."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_WEB_SEARCH,
                        default=self.config_entry.options.get(
                            CONF_WEB_SEARCH, DEFAULT[CONF_WEB_SEARCH]
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_WEB_FETCH,
                        default=self.config_entry.options.get(
                            CONF_WEB_FETCH, DEFAULT[CONF_WEB_FETCH]
                        ),
                    ): bool,
                }
            ),
        )
