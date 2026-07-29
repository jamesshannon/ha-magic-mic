"""The Magic Mic integration.

Wave 0 skeleton: a Testbed Proxy conversation agent over a Claude backend. Claude
is the testbed provider, not a dependency (docs/testbed-proxy.md). Sets up the
Claude client via a coordinator and forwards the conversation platform, which
registers the baseline and testbed agents.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import LOGGER
from .internal.claude.coordinator import ClaudeCoordinator, MagicMicConfigEntry

PLATFORMS = (Platform.CONVERSATION,)


async def async_setup_entry(hass: HomeAssistant, entry: MagicMicConfigEntry) -> bool:
    """Set up Magic Mic from a config entry."""
    coordinator = ClaudeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    LOGGER.debug("Available models: %s", coordinator.data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MagicMicConfigEntry) -> bool:
    """Unload a Magic Mic config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
