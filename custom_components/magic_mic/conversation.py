"""Conversation platform: registers the baseline and testbed agents.

Both run the same Claude backend. `Claude (baseline)` is the stock provider agent;
`Magic Mic (testbed)` is the neutral proxy. The eval harness measures the testbed
as a delta against the baseline (docs/testbed-proxy.md).
"""

from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_PROMPT
from .internal.claude.agent import ClaudeConversationEntity
from .internal.claude.const import CONF_CHAT_MODEL, DEFAULT
from .internal.claude.coordinator import MagicMicConfigEntry
from .testbed.entity import TestbedConversationEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MagicMicConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the baseline and testbed conversation agents."""
    options = {
        CONF_CHAT_MODEL: DEFAULT[CONF_CHAT_MODEL],
        CONF_LLM_HASS_API: llm.LLM_API_ASSIST,
        CONF_PROMPT: DEFAULT_PROMPT,
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
