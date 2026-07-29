"""The interposition seam: a decorator over `chat_log.llm_api`.

HA routes tool exposure (`.tools`), tool execution (`.async_call_tool`, run by the
ChatLog, not the provider) and the exposed-entity prompt (`.api_prompt`) through one
`llm.APIInstance`. Wrapping that single object interposes on all three,
provider-agnostically. See `docs/testbed-proxy.md`.

At Wave 0 this is pass-through plus tracing. Tool filtering/replacement and prompt
rewriting land here in later waves.
"""

from dataclasses import fields

from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from ..const import LOGGER


class TestbedAPI(llm.APIInstance):
    """Pass-through `APIInstance` that traces tool calls.

    This is the neutral seam. Nothing provider-specific belongs here.
    """

    @classmethod
    def wrap(cls, inner: llm.APIInstance) -> "TestbedAPI":
        """Build a testbed wrapper carrying the inner instance's fields."""
        return cls(**{f.name: getattr(inner, f.name) for f in fields(inner)})

    async def async_call_tool(self, tool_input: llm.ToolInput) -> JsonObjectType:
        """Execute a tool call, tracing the call and its result.

        Later waves intercept here (e.g. route `find_entities` to the fuzzy
        resolver); for now every call falls through to the real API.
        """
        LOGGER.debug(
            "[testbed] tool_call %s args=%s", tool_input.tool_name, tool_input.tool_args
        )
        result = await super().async_call_tool(tool_input)
        LOGGER.debug("[testbed] tool_result %s -> %s", tool_input.tool_name, result)
        return result
