"""The Testbed Proxy conversation agent.

Reuses the Claude adapter's model loop but interposes at three seams before the loop
runs: the system prompt (swapping HA's entity roster for the entity summary at
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

from ..capabilities.capability_selection import EnforcedSelection, enforce_on_roster
from ..capabilities.localization import async_get_conversation_strings
from ..capabilities.prompt_context import (
    async_domain_keyword_map,
    language_ignores_whitespace,
    select_request_names,
)
from ..chat_log import MagicMicChatLog, upgrade_chat_log
from ..const import (
    CONF_CAPABILITY_SELECTION,
    CONF_ENTITY_SUMMARY,
    CONF_NAME_INJECTION,
    DEFAULT_CAPABILITY_SELECTION,
    DEFAULT_ENTITY_SUMMARY,
    DEFAULT_NAME_INJECTION,
    DOMAIN,
    LOGGER,
    NAME_INJECTION_LIMIT,
)
from ..identity import (
    RequestSource,
    ResolvedPrincipal,
    async_resolve_user,
    clear_resolved_user,
    get_resolved_user,
)
from ..internal.claude.agent import ClaudeConversationEntity
from ..tool_policy import ToolPolicyContext
from .api import CapabilitySelector, TestbedAPI
from .prompt import async_prepare_llm_api


class TestbedConversationEntity(ClaudeConversationEntity):
    """Neutral proxy agent over the provider agent."""

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Establish request identity while the proxied turn runs."""
        # ConversationInput does not expose whether its text came directly from an
        # authenticated text client or from STT. Fail closed until the request adapter
        # supplies that source explicitly; context.user_id may be a pipeline owner.
        await async_resolve_user(
            self.hass,
            user_input.context,
            request_source=RequestSource.UNKNOWN,
        )
        try:
            return await self._async_handle_resolved_message(user_input, chat_log)
        finally:
            try:
                if isinstance(chat_log.llm_api, TestbedAPI):
                    await chat_log.llm_api.async_cancel_and_drain_tool_calls()
            finally:
                # Tool cancellation and cleanup may still consult request identity. The
                # registry entry is removed only after every tracked call has stopped.
                clear_resolved_user(self.hass, user_input.context)

    async def _async_handle_resolved_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Set up the LLM data, wrap the seam, and run the provider loop."""
        principal = get_resolved_user(self.hass, user_input.context)
        magic_chat_log = upgrade_chat_log(chat_log)
        magic_chat_log.async_set_prompt_user_name(
            await _async_prompt_user_name(self.hass, principal)
        )
        session_state = magic_chat_log.session_state
        turn_metadata = session_state.async_begin_turn(
            user_input.context.id,
            device_id=user_input.device_id,
            principal=principal,
            satellite_id=user_input.satellite_id,
        )
        LOGGER.debug(
            "[testbed] resolved principal user_id=%s (unused in Wave 0)",
            principal.user_id,
        )

        # Prompt-context Tier 1: replace only the selected Assist API contribution
        # with its entity-summary variant. Other HA LLM APIs keep their own prompts
        # and tools, then merge normally. The roster is built into api_prompt here,
        # before the llm_api-wrap seam below, so this must happen before composition.
        llm_context = user_input.as_llm_context(DOMAIN)
        prepared_api = async_prepare_llm_api(
            self.hass,
            self._options.get(CONF_LLM_HASS_API),
            entity_summary=self._options.get(
                CONF_ENTITY_SUMMARY, DEFAULT_ENTITY_SUMMARY
            ),
        )

        try:
            await magic_chat_log.async_provide_llm_data(
                llm_context,
                prepared_api.api,
                self._options.get(CONF_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        # Prompt-context Tier 2: append the request-conditioned name block to the
        # composed system prompt (docs/prompt-context.md "Tier 2"). Names supply
        # exact ids removed by the entity summary, so gate them on the transformation
        # that actually happened rather than the requested configuration.
        if prepared_api.entity_summary_applied and self._options.get(
            CONF_NAME_INJECTION, DEFAULT_NAME_INJECTION
        ):
            await self._async_inject_request_names(
                magic_chat_log, user_input, llm_context
            )

        # The interposition seam. Reach for the proxy first; drop into
        # internal.claude only when the HA<->LLM contract itself is what needs
        # changing (docs/testbed-proxy.md). ``strings`` enables the match-layer fuzzy
        # fallback (docs/find-entities.md Consumer 1): an intent's exact name miss is
        # retried through the shared resolver before the error reaches the model.
        if magic_chat_log.llm_api is not None:
            strings = await async_get_conversation_strings(
                self.hass, llm_context.language
            )
            magic_chat_log.llm_api = TestbedAPI.wrap(
                magic_chat_log.llm_api,
                ToolPolicyContext(
                    is_continuation=turn_metadata.is_continuation,
                    principal=principal,
                    session_state=session_state,
                    turn_metadata=turn_metadata,
                ),
                selector=self._capability_selector(user_input.text),
                strings=strings,
            )

        await self._async_handle_chat_log(magic_chat_log)

        return conversation.async_get_result_from_chat_log(user_input, magic_chat_log)

    def _capability_selector(self, utterance: str) -> CapabilitySelector | None:
        """Return a per-turn capability selector, or None when enforcement is off.

        Off by default (docs "Gates": enforcement waits on the recall and task-success
        gates, and the demo catalog's retrieval documents are English). When enabled, the
        closure binds this turn's utterance so `TestbedAPI` can prune the policy-exposed
        roster to the plan; it uses the default demo catalog and the const budget/floor.
        """
        if not self._options.get(
            CONF_CAPABILITY_SELECTION, DEFAULT_CAPABILITY_SELECTION
        ):
            return None

        def select(exposed_tools: frozenset[str]) -> EnforcedSelection:
            return enforce_on_roster(utterance, exposed_tools)

        return select

    async def _async_inject_request_names(
        self,
        chat_log: MagicMicChatLog,
        user_input: conversation.ConversationInput,
        llm_context: LLMContext,
    ) -> None:
        """Append the Tier-2 name block to the composed system prompt, in place.

        The names ride the same single system block as the entity summary (Option 1): they
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
        language = llm_context.language
        keyword_map = await async_domain_keyword_map(self.hass, language)
        strings = await async_get_conversation_strings(self.hass, language)
        names = select_request_names(
            self.hass,
            assistant,
            user_input.text,
            area_id,
            ignore_whitespace=language_ignores_whitespace(llm_context.language),
            keyword_map=keyword_map,
            limit=NAME_INJECTION_LIMIT,
            strings=strings,
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


async def _async_prompt_user_name(
    hass: HomeAssistant, principal: ResolvedPrincipal
) -> str | None:
    """Return the display name for an explicitly resolved HA user."""
    if principal.user_id is None:
        return None
    user = await hass.auth.async_get_user(principal.user_id)
    return user.name if user is not None else None
