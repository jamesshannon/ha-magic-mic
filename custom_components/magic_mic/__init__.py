"""The Magic Mic integration.

Placeholder scaffold. This integration installs and loads cleanly but does not
yet provide any capabilities — it exists so it can be installed via HACS today
and receive features as they land. See the vision and roadmap at
https://github.com/jamesshannon/ha-magic-mic
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Magic Mic from a config entry.

    Nothing to set up yet; the shell loads so the integration is installable and
    updatable while capabilities are built out.
    """
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Magic Mic config entry."""
    return True
