"""Prompt interposition: swap HA's entity roster for the taxonomy skeleton.

The Assist API composes its `api_prompt` (roster included) inside
`async_provide_llm_data`, *before* the proxy gets to wrap `chat_log.llm_api`. So the
skeleton cannot ride the `APIInstance` wrapper the way tool interception does; it has
to be substituted at the API that builds the prompt. `SkeletonAssistAPI` is that
substitution: an `AssistAPI` whose only change is emitting the taxonomy skeleton in
place of the "Static Context" entity dump. Everything else about the Assist prompt is
inherited unchanged.

Proxy-layer and provider-agnostic: depends only on `hass` and HA's `llm` helpers.
See `docs/prompt-context.md` and `docs/testbed-proxy.md`.
"""

from typing import override

from homeassistant.core import callback
from homeassistant.helpers import llm
from homeassistant.helpers.llm import AssistAPI, LLMContext

from ..capabilities.prompt_context import async_build_taxonomy_skeleton


class SkeletonAssistAPI(AssistAPI):
    """Assist API that injects the taxonomy skeleton instead of the roster.

    A fresh instance is built per request, so recording the request's assistant id
    on the instance (for the roster-replacement hook, which HA calls without the
    `LLMContext`) is safe.
    """

    _assistant: str | None = None

    @override
    async def async_get_api_instance(self, llm_context: LLMContext) -> llm.APIInstance:
        """Record the assistant id, then compose the instance as AssistAPI does."""
        self._assistant = llm_context.assistant
        return await super().async_get_api_instance(llm_context)

    @callback
    def _async_get_exposed_entities_prompt(
        self, exposed_entities: dict | None
    ) -> list[str]:
        """Return the taxonomy skeleton in place of the exposed-entity roster."""
        if (
            not exposed_entities
            or not exposed_entities["entities"]
            or self._assistant is None
        ):
            return []
        skeleton = async_build_taxonomy_skeleton(self.hass, self._assistant)
        return [skeleton] if skeleton else []


@callback
def async_skeleton_llm_api(
    hass, llm_hass_api: str | list[str] | llm.API
) -> str | list[str] | llm.API:
    """Return the LLM API to hand `async_provide_llm_data`, skeleton-substituted.

    Swaps `SkeletonAssistAPI` in for the plain Assist API so the composed system
    prompt carries the skeleton. Any other configuration (merged, custom, or an
    already-resolved `API`) passes through unchanged, roster intact.
    """
    if llm_hass_api in (llm.LLM_API_ASSIST, [llm.LLM_API_ASSIST]):
        return SkeletonAssistAPI(hass)
    return llm_hass_api
