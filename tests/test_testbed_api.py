"""Unit test for the testbed interposition seam."""

from homeassistant.helpers import llm

# Imported as a module (not `from ... import TestbedAPI`) so the `Test`-prefixed
# class name doesn't trip pytest's test-class collection heuristic.
from custom_components.magic_mic.testbed import api as testbed_api


def test_wrap_preserves_api_instance_fields() -> None:
    """`TestbedAPI.wrap` carries every field of the inner APIInstance through.

    This is the pass-through guarantee: at Wave 0 the wrapper must be behaviorally
    identical to the real API, so a testbed turn matches the baseline.
    """
    api = object()
    llm_context = object()
    tools: list[llm.Tool] = []

    def serializer(value: object) -> object:
        return value

    inner = llm.APIInstance(
        api=api,  # type: ignore[arg-type]
        api_prompt="the exposed-entity prompt",
        llm_context=llm_context,  # type: ignore[arg-type]
        tools=tools,
        custom_serializer=serializer,
    )

    wrapped = testbed_api.TestbedAPI.wrap(inner)

    assert isinstance(wrapped, testbed_api.TestbedAPI)
    assert wrapped.api is api
    assert wrapped.api_prompt == "the exposed-entity prompt"
    assert wrapped.llm_context is llm_context
    assert wrapped.tools is tools
    assert wrapped.custom_serializer is serializer
