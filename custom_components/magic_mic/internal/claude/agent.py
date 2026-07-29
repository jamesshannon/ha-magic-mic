"""Baseline Claude conversation agent.

The stock, unoptimized provider agent. Registered as its own conversation entity
so the eval harness can measure the testbed proxy as a delta against it. Nothing
Magic-Mic-specific lives here; the neutral logic is in the testbed layer.
"""

from typing import Any, Literal

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT, MATCH_ALL

from ...const import DOMAIN
from .coordinator import MagicMicConfigEntry
from .entity import ClaudeBaseLLMEntity


class ClaudeConversationEntity(
    conversation.ConversationEntity,
    conversation.AbstractConversationAgent,
    ClaudeBaseLLMEntity,
):
    """Claude conversation agent (the unoptimized baseline)."""

    _attr_supports_streaming = True

    def __init__(
        self,
        entry: MagicMicConfigEntry,
        unique_id: str,
        name: str,
        options: dict[str, Any],
    ) -> None:
        """Initialize the agent."""
        super().__init__(entry, unique_id, name, options)
        if options.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Handle a message: set up the LLM data, then run the model loop."""
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                self._options.get(CONF_LLM_HASS_API),
                self._options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_handle_chat_log(chat_log)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)
