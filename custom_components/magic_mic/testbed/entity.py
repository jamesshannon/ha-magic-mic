"""The Testbed Proxy conversation agent.

Reuses the Claude adapter's model loop but interposes at three seams before the loop
runs: the system prompt (swapping HA's entity roster for the taxonomy skeleton at
prompt-composition time, prompt-context Tier 1), Tier-2 request-conditioned name
injection (appending a small, relevant name subset to the composed system prompt), and
`chat_log.llm_api` (the tool-call trace/interception wrapper, still pass-through for tool
calls). See `docs/testbed-proxy.md` and `docs/prompt-context.md`.
"""

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.llm import LLMContext

from ..capabilities.prompt_context import async_domain_keyword_map, select_request_names
from ..const import (
    CONF_NAME_INJECTION,
    CONF_TAXONOMY_SKELETON,
    DEFAULT_NAME_INJECTION,
    DEFAULT_TAXONOMY_SKELETON,
    DOMAIN,
    LOGGER,
    NAME_INJECTION_LIMIT,
)
from ..identity import RequestSource, get_resolved_user
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
        # ConversationInput does not expose whether its text came directly from an
        # authenticated text client or from STT. Fail closed until the request adapter
        # supplies that source explicitly; context.user_id may be a pipeline owner.
        principal = await get_resolved_user(
            self.hass,
            user_input.context,
            request_source=RequestSource.UNKNOWN,
        )
        LOGGER.debug(
            "[testbed] resolved principal user_id=%s (unused in Wave 0)",
            principal.user_id,
        )

        # Prompt-context Tier 1: hand async_provide_llm_data a skeleton-substituted
        # Assist API so the composed system prompt carries the bounded taxonomy
        # skeleton instead of HA's full entity roster (docs/prompt-context.md). The
        # roster is built into api_prompt here, before the llm_api-wrap seam below,
        # so this substitution must happen at prompt-composition time, not at wrap.
        llm_context = user_input.as_llm_context(DOMAIN)
        llm_hass_api = self._options.get(CONF_LLM_HASS_API)
        skeleton_on = bool(llm_hass_api) and self._options.get(
            CONF_TAXONOMY_SKELETON, DEFAULT_TAXONOMY_SKELETON
        )
        if skeleton_on:
            llm_hass_api = async_skeleton_llm_api(self.hass, llm_hass_api)

        try:
            await chat_log.async_provide_llm_data(
                llm_context,
                llm_hass_api,
                self._options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # Prompt-context Tier 2: append the request-conditioned name block to the
        # composed system prompt (docs/prompt-context.md "Tier 2"). Gated on the
        # skeleton being on: names supply the exact ids the skeleton drops, so
        # names-without-skeleton would duplicate a roster that is still present.
        if skeleton_on and self._options.get(
            CONF_NAME_INJECTION, DEFAULT_NAME_INJECTION
        ):
            await self._async_inject_request_names(chat_log, user_input, llm_context)

        # The interposition seam. Reach for the proxy first; drop into
        # internal.claude only when the HA<->LLM contract itself is what needs
        # changing (docs/testbed-proxy.md).
        if chat_log.llm_api is not None:
            chat_log.llm_api = TestbedAPI.wrap(chat_log.llm_api)

        await self._async_handle_chat_log(chat_log)

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_inject_request_names(
        self,
        chat_log: conversation.ChatLog,
        user_input: conversation.ConversationInput,
        llm_context: LLMContext,
    ) -> None:
        """Append the Tier-2 name block to the composed system prompt, in place.

        The names ride the same single system block as the skeleton (Option 1): they
        change per turn, so the block re-caches across turns, but the within-command
        loop (gen2/gen3 share this turn's names) still hits the cache. A second,
        cache-isolated system block is the follow-on only if the eval shows the
        cross-turn re-prefill costs enough to earn it (docs/prompt-context.md cache
        model). `SystemContent` is frozen, so replace the entry rather than mutate it.
        """
        system = chat_log.content[0]
        if not isinstance(system, conversation.SystemContent):
            return
        assistant = llm_context.assistant
        if assistant is None:
            return
        area_id = _requesting_area_id(self.hass, user_input.device_id)
        keyword_map = await async_domain_keyword_map(self.hass, llm_context.language)
        names = select_request_names(
            self.hass,
            assistant,
            user_input.text,
            area_id,
            keyword_map=keyword_map,
            limit=NAME_INJECTION_LIMIT,
        )
        if names:
            chat_log.content[0] = conversation.SystemContent(
                f"{system.content}\n\n{names}"
            )


@callback
def _requesting_area_id(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Resolve the area the request came from: the satellite device's area.

    Text input has no device, so no room to prefer (name selection then scores the
    whole house). This mirrors how HA derives a satellite's preferred area.
    """
    if device_id is None:
        return None
    device = dr.async_get(hass).async_get(device_id)
    return device.area_id if device else None
