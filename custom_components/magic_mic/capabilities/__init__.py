"""Capability contract: the typed seam Wave 1+ capabilities implement.

There is no new contract to invent here. Home Assistant already defines it in
`homeassistant.helpers.llm`: a tool is an `llm.Tool` (name, description, parameters,
`async_call`), and a set of tools is served through an `llm.APIInstance` obtained from an
`llm.API` (`async_get_api_instance`). PRODUCT_PLAN §5.5's `async_get_tools(hass,
llm_context, api_id) -> LLMTools` is shorthand for that reality; `LLMTools` is not a real
core type. Magic Mic capabilities are provider-agnostic modules that produce `llm.Tool`s;
the Testbed Proxy aggregates them into the `APIInstance` it hands the model (see
`docs/testbed-proxy.md`).

Each capability exposes the shape below, so a module migrates to a core `llm.py` platform
close to copy/paste:

    async def async_get_tools(
        hass: HomeAssistant, llm_context: llm.LLMContext
    ) -> list[llm.Tool]:
        ...

Capabilities scope their data by the resolved principal, never by device or satellite. The
request adapter establishes identity with `async_resolve_user(...)`; tools and intents call
the source-agnostic `get_resolved_user(hass, context)` accessor (`..identity`). Capabilities
query its household or personal scope instead of interpreting a sentinel user ID. No
capability ships at Wave 0; this package is the empty, typed seam Wave 1's `find_entities` is
the first to fill.
"""

from collections.abc import Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm

# A capability is a callable producing HA LLM tools for a request's context. A type alias
# (not a base class) keeps a capability a plain function, exactly as a core `llm.py`
# platform's `async_get_tools` would be.
CapabilityTools = Callable[[HomeAssistant, llm.LLMContext], Awaitable[list[llm.Tool]]]

__all__ = ["CapabilityTools"]
