"""Config flow for the Magic Mic integration.

Minimal: collect and validate a Claude API key. Model/prompt options are fixed for
the Wave 0 skeleton; per-agent configuration lands later.
"""

from __future__ import annotations

from typing import Any

import anthropic
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY

from .const import DOMAIN, LOGGER
from .internal.claude.coordinator import async_create_client

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})


class MagicMicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Magic Mic."""

    VERSION = 1

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
