"""Prepare HA LLM APIs with an Assist-scoped entity summary.

The Assist API composes its `api_prompt` (roster included) inside
`async_provide_llm_data`, before the proxy wraps `chat_log.llm_api`. The entity
summary must therefore replace the selected Assist API contribution before prompt
composition. Other HA LLM APIs retain their own prompts and tools unchanged.

Proxy-layer and provider-agnostic: depends only on `hass` and HA's `llm` helpers.
See `docs/prompt-context.md` and `docs/testbed-proxy.md`.
"""

from dataclasses import dataclass
from typing import override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.llm import AssistAPI, LLMContext, Tool

from ..capabilities.entities import async_get_tools as async_get_entity_tools
from ..capabilities.localization import (
    ConversationStrings,
    async_get_conversation_strings,
)
from ..capabilities.prompt_context import async_build_entity_summary

type LLMAPIConfig = str | list[str] | llm.API | None


@dataclass(frozen=True, slots=True)
class PreparedLLMAPI:
    """Configured LLM APIs plus the prompt strategy actually applied."""

    api: LLMAPIConfig
    entity_summary_applied: bool


class EntitySummaryAssistAPI(AssistAPI):
    """Assist API that injects the entity summary instead of the roster.

    A fresh instance is built per request, so recording the request's assistant id
    on the instance (for the roster-replacement hook, which HA calls without the
    `LLMContext`) is safe.
    """

    _assistant: str | None = None
    _strings: ConversationStrings | None = None

    @override
    async def async_get_api_instance(self, llm_context: LLMContext) -> llm.APIInstance:
        """Record the assistant id, then compose the instance as AssistAPI does."""
        self._assistant = llm_context.assistant
        self._strings = await async_get_conversation_strings(
            self.hass, llm_context.language
        )
        return await super().async_get_api_instance(llm_context)

    @override
    @callback
    def _async_get_tools(
        self, llm_context: LLMContext, exposed_entities: dict | None
    ) -> list[Tool]:
        """Add find_entities alongside the stock Assist tools.

        The summary retires the name roster, so the model needs a lookup tool to
        recover exact names on demand (find-entities.md): the entity summary and
        find_entities ship as a pair. The stock intent tools are unchanged; their
        fuzzy fallback is the separate match-layer track.
        """
        tools = super()._async_get_tools(llm_context, exposed_entities)
        if self._strings is not None:
            tools.extend(async_get_entity_tools(self.hass, llm_context, self._strings))
        return tools

    @callback
    def _async_get_exposed_entities_prompt(
        self, exposed_entities: dict | None
    ) -> list[str]:
        """Return the entity summary in place of the exposed-entity roster.

        The summary retires the exact names, so the model reasons about rooms from counts; the
        area-usage instruction rides alongside it to keep the model from echoing its own room
        onto a named request (find-entities.md "Room is a preference"): the room is supplied
        from context, and a named device is matched house-wide.
        """
        if (
            not exposed_entities
            or not exposed_entities["entities"]
            or self._assistant is None
            or self._strings is None
        ):
            return []
        summary = async_build_entity_summary(
            self.hass,
            self._assistant,
            self._strings,
        )
        return [summary, self._strings.area_usage_instruction] if summary else []


@callback
def async_prepare_llm_api(
    hass: HomeAssistant,
    configured_api: LLMAPIConfig,
    *,
    entity_summary: bool,
) -> PreparedLLMAPI:
    """Apply the entity summary only to a selected Assist API contribution.

    The result records whether Assist was actually replaced so request-conditioned
    entity names cannot be injected alongside an unchanged full roster.
    """
    if not entity_summary:
        return PreparedLLMAPI(configured_api, entity_summary_applied=False)

    prepared_api, applied = _async_replace_assist_api(hass, configured_api)
    return PreparedLLMAPI(prepared_api, entity_summary_applied=applied)


@callback
def _async_replace_assist_api(
    hass: HomeAssistant, configured_api: LLMAPIConfig
) -> tuple[LLMAPIConfig, bool]:
    """Replace only the Assist member of one configured LLM API selection."""
    if configured_api == llm.LLM_API_ASSIST:
        return EntitySummaryAssistAPI(hass), True

    if isinstance(configured_api, list):
        if llm.LLM_API_ASSIST not in configured_api:
            return configured_api, False
        if configured_api == [llm.LLM_API_ASSIST]:
            return EntitySummaryAssistAPI(hass), True

        registered = {api.id: api for api in llm.async_get_apis(hass)}
        if any(api_id not in registered for api_id in configured_api):
            # Preserve core's normal unknown-API error in async_provide_llm_data.
            return configured_api, False
        members = [
            EntitySummaryAssistAPI(hass)
            if api_id == llm.LLM_API_ASSIST
            else registered[api_id]
            for api_id in configured_api
        ]
        return llm.MergedAPI(members), True

    if isinstance(configured_api, llm.MergedAPI):
        members, applied = _replace_assist_members(hass, configured_api.llm_apis)
        return (llm.MergedAPI(members), True) if applied else (configured_api, False)

    if isinstance(configured_api, llm.API) and configured_api.id == llm.LLM_API_ASSIST:
        return EntitySummaryAssistAPI(hass), True

    return configured_api, False


def _replace_assist_members(
    hass: HomeAssistant, members: list[llm.API]
) -> tuple[list[llm.API], bool]:
    """Replace Assist within an already-resolved merged API."""
    applied = False
    prepared: list[llm.API] = []
    for member in members:
        if member.id == llm.LLM_API_ASSIST:
            prepared.append(EntitySummaryAssistAPI(hass))
            applied = True
        else:
            prepared.append(member)
    return prepared, applied
