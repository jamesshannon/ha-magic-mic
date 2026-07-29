"""The Testbed Proxy conversation agent.

Reuses the Claude adapter's model loop but interposes at the `chat_log.llm_api`
seam before the loop runs. At Wave 0 the wrapper is pass-through, so behavior is
identical to the baseline; the trace hook and interception seam are in place for
later waves. See `docs/testbed-proxy.md`.
"""

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT

from ..const import DOMAIN
from ..internal.claude.agent import ClaudeConversationEntity
from .api import TestbedAPI


class TestbedConversationEntity(ClaudeConversationEntity):
    """Neutral proxy agent over the provider agent."""

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Set up the LLM data, wrap the API seam, then run the provider loop."""
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                self._options.get(CONF_LLM_HASS_API),
                self._options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # The interposition seam. Reach for the proxy first; drop into
        # internal.claude only when the HA<->LLM contract itself is what needs
        # changing (docs/testbed-proxy.md).
        if chat_log.llm_api is not None:
            chat_log.llm_api = TestbedAPI.wrap(chat_log.llm_api)

        await self._async_handle_chat_log(chat_log)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)
