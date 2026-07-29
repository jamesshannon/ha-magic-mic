"""The Magic Mic integration.

Wave 0 skeleton: a Testbed Proxy conversation agent over a Claude backend. Claude
is the testbed provider, not a dependency (docs/testbed-proxy.md). Sets up the
Claude client via a coordinator and forwards the conversation platform, which
registers the baseline and testbed agents.
"""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LOGGER
from .internal.claude.coordinator import ClaudeCoordinator, MagicMicConfigEntry
from .store import UserKeyedStore

PLATFORMS = (Platform.CONVERSATION,)


async def async_setup_entry(hass: HomeAssistant, entry: MagicMicConfigEntry) -> bool:
    """Set up Magic Mic from a config entry."""
    coordinator = ClaudeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    LOGGER.debug("Available models: %s", coordinator.data)

    # Neutral per-user store, threaded empty in Wave 0 (no consumer yet). Kept in
    # hass.data because runtime_data carries the provider coordinator, which the
    # vendored entity expects there.
    store = UserKeyedStore(hass, "store")
    await store.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = store

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MagicMicConfigEntry) -> bool:
    """Unload a Magic Mic config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
