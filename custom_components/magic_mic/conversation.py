"""Conversation platform: registers the baseline and testbed agents.

Both run the same Claude backend. `Claude (baseline)` is the stock provider agent;
`Magic Mic (testbed)` is the neutral proxy. The eval harness measures the testbed
as a delta against the baseline (docs/testbed-proxy.md).
"""

from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .internal.claude.agent import ClaudeConversationEntity
from .internal.claude.const import (
    CONF_CHAT_MODEL,
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
    DEFAULT,
)
from .internal.claude.coordinator import MagicMicConfigEntry
from .testbed.entity import TestbedConversationEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MagicMicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the baseline and testbed conversation agents."""
    # No CONF_PROMPT: leaving it unset lets chat_log fall back to core's
    # llm.DEFAULT_INSTRUCTIONS_PROMPT ("Answer in plain text. Keep it simple and to
    # the point."). Re-typing our own base prompt here only invited drift from core.
    options = {
        CONF_CHAT_MODEL: DEFAULT[CONF_CHAT_MODEL],
        CONF_LLM_HASS_API: llm.LLM_API_ASSIST,
        CONF_WEB_FETCH: config_entry.options.get(
            CONF_WEB_FETCH, DEFAULT[CONF_WEB_FETCH]
        ),
        CONF_WEB_SEARCH: config_entry.options.get(
            CONF_WEB_SEARCH, DEFAULT[CONF_WEB_SEARCH]
        ),
    }
    entry_id = config_entry.entry_id
    async_add_entities(
        [
            ClaudeConversationEntity(
                config_entry,
                f"{entry_id}_claude_baseline",
                "Claude (baseline)",
                options,
            ),
            TestbedConversationEntity(
                config_entry, f"{entry_id}_testbed", "Magic Mic (testbed)", options
            ),
        ]
    )
