"""Config flow for the Magic Mic integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class MagicMicConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Magic Mic.

    Single instance, no options yet. `single_config_entry` in the manifest keeps
    it to one instance; there is nothing to configure until capabilities land.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(title="Magic Mic", data={})

        return self.async_show_form(step_id="user")
