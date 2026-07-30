"""The Testbed Proxy conversation agent.

Reuses the Claude adapter's model loop but interposes at two seams before the loop
runs: the system prompt (swapping HA's entity roster for the taxonomy skeleton at
prompt-composition time, prompt-context §5.2) and `chat_log.llm_api` (the tool-call
trace/interception wrapper, still pass-through for tool calls). See
`docs/testbed-proxy.md` and `docs/prompt-context.md`.
"""

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT

from ..const import CONF_TAXONOMY_SKELETON, DEFAULT_TAXONOMY_SKELETON, DOMAIN, LOGGER
from ..identity import get_resolved_user
from ..internal.claude.agent import ClaudeConversationEntity
from .api import TestbedAPI
from .prompt import async_skeleton_llm_api


class TestbedConversationEntity(ClaudeConversationEntity):
    """Neutral proxy agent over the provider agent."""

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Get the resolved user, set up the LLM data, wrap the seam, run the loop."""
        # Get the resolved per-user scope key for this turn (§5.1). Threaded empty in
        # Wave 0: nothing consumes it yet. The user-keyed store lives in
        # hass.data[DOMAIN][entry_id]; capabilities will read both.
        user_id = await get_resolved_user(
            self.hass,
            user_input.context,
            user_input.device_id,
            user_input.satellite_id,
        )
        LOGGER.debug("[testbed] resolved user_id=%s (unused in Wave 0)", user_id)

        # Prompt-context §5.2: hand async_provide_llm_data a skeleton-substituted
        # Assist API so the composed system prompt carries the bounded taxonomy
        # skeleton instead of HA's full entity roster (docs/prompt-context.md). The
        # roster is built into api_prompt here, before the llm_api-wrap seam below,
        # so this substitution must happen at prompt-composition time, not at wrap.
        llm_hass_api = self._options.get(CONF_LLM_HASS_API)
        if llm_hass_api and self._options.get(
            CONF_TAXONOMY_SKELETON, DEFAULT_TAXONOMY_SKELETON
        ):
            llm_hass_api = async_skeleton_llm_api(self.hass, llm_hass_api)

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                llm_hass_api,
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
