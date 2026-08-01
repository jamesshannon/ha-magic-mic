"""Tests for the testbed policy and tool-execution seam."""

from collections.abc import Mapping
from typing import Any

import pytest

# Imported as a module (not `from ... import TestbedAPI`) so the `Test`-prefixed
# class name doesn't trip pytest's test-class collection heuristic.
from custom_components.magic_mic.identity import (
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    ResolvedPrincipal,
)
from custom_components.magic_mic.pending_operation import ConsequenceClass
from custom_components.magic_mic.session_state import MagicMicSessionState
from custom_components.magic_mic.testbed import api as testbed_api
from custom_components.magic_mic.tool_policy import (
    CallPolicy,
    ExposurePolicy,
    StaticToolPolicy,
    ToolPolicy,
    ToolPolicyContext,
    ToolPolicyDeniedError,
    ToolPolicyRegistry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType


class FixtureTool(llm.Tool):
    """Minimal tool represented by the inner API."""

    def __init__(self, name: str) -> None:
        """Initialize a named fixture tool."""
        self.name = name

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Return an unused result because the recording API owns delegation."""
        return {"executor": "tool"}


class ArgumentPolicy(ToolPolicy):
    """Classify a mixed-scope legacy tool from its normalized arguments."""

    def exposure_policy(self, context: ToolPolicyContext) -> ExposurePolicy:
        """Expose the tool to household callers."""
        return ExposurePolicy(required_scope=DataScope.HOUSEHOLD)

    def classify_call(
        self,
        arguments: Mapping[str, Any],
        context: ToolPolicyContext,
    ) -> CallPolicy:
        """Require personal scope for the fixture's personal operation."""
        return CallPolicy(
            required_scope=(
                DataScope.PERSONAL
                if arguments.get("scope") == "personal"
                else DataScope.HOUSEHOLD
            )
        )


class RecordingAPIInstance(llm.APIInstance):
    """Inner API whose override proves the wrapper delegates correctly."""

    calls: list[llm.ToolInput]

    def __init__(self, tools: list[llm.Tool]) -> None:
        """Initialize the API with observable fields and calls."""
        self.calls = []

        def serializer(value: object) -> object:
            return value

        super().__init__(
            api=object(),  # type: ignore[arg-type]
            api_prompt="the exposed-entity prompt",
            custom_serializer=serializer,
            llm_context=object(),  # type: ignore[arg-type]
            tools=tools,
        )

    async def async_call_tool(self, tool_input: llm.ToolInput) -> JsonObjectType:
        """Record calls instead of using the base API executor."""
        self.calls.append(tool_input)
        return {"executor": "inner", "tool_name": tool_input.tool_name}


def _context(
    principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL,
    *,
    is_continuation: bool = False,
) -> ToolPolicyContext:
    """Build policy context with initialized turn metadata."""
    state = MagicMicSessionState()
    state.async_begin_turn(
        "turn-1",
        is_continuation=is_continuation,
        principal=principal,
    )
    return ToolPolicyContext(
        is_continuation=is_continuation,
        principal=principal,
        session_state=state,
    )


def test_wrap_preserves_api_fields_and_unfiltered_tool_list() -> None:
    """An unrestricted wrapper preserves the inner API's exposed field values."""
    tools: list[llm.Tool] = [FixtureTool("unclassified")]
    inner = RecordingAPIInstance(tools)

    wrapped = testbed_api.TestbedAPI.wrap(inner, _context())

    assert isinstance(wrapped, testbed_api.TestbedAPI)
    assert wrapped.api is inner.api
    assert wrapped.api_prompt == inner.api_prompt
    assert wrapped.llm_context is inner.llm_context
    assert wrapped.tools is tools
    assert wrapped.custom_serializer is inner.custom_serializer


async def test_unclassified_call_delegates_to_inner_override() -> None:
    """Pass-through calls preserve arbitrary custom APIInstance execution."""
    inner = RecordingAPIInstance([FixtureTool("unclassified")])
    wrapped = testbed_api.TestbedAPI.wrap(inner, _context())
    tool_input = llm.ToolInput(tool_name="unclassified", tool_args={"value": 1})

    result = await wrapped.async_call_tool(tool_input)

    assert result == {"executor": "inner", "tool_name": "unclassified"}
    assert inner.calls == [tool_input]


def test_personal_tool_is_filtered_for_unidentified_principal() -> None:
    """An unavailable personal tool is absent before the model generation."""
    personal = FixtureTool("personal")
    unrestricted = FixtureTool("unrestricted")
    inner = RecordingAPIInstance([personal, unrestricted])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "personal",
        StaticToolPolicy(required_scope=DataScope.PERSONAL),
    )
    context = _context()

    wrapped = testbed_api.TestbedAPI.wrap(inner, context, registry)

    assert wrapped.tools == [unrestricted]
    assert inner.tools == [personal, unrestricted]
    assert context.session_state.turn_metadata is not None
    assert [
        (trace.tool_name, trace.allowed, trace.stage)
        for trace in context.session_state.turn_metadata.tool_policy
    ] == [
        ("personal", False, "exposure"),
        ("unrestricted", True, "exposure"),
    ]


def test_personal_tool_is_exposed_for_identified_principal() -> None:
    """The same personal tool is available to a resolved person."""
    personal = FixtureTool("personal")
    inner = RecordingAPIInstance([personal])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "personal",
        StaticToolPolicy(required_scope=DataScope.PERSONAL),
    )

    wrapped = testbed_api.TestbedAPI.wrap(
        inner,
        _context(ResolvedPrincipal(user_id="person-1")),
        registry,
    )

    assert wrapped.tools is inner.tools


async def test_direct_call_to_filtered_tool_is_rejected() -> None:
    """A stale or constructed hidden call cannot bypass exposure filtering."""
    inner = RecordingAPIInstance([FixtureTool("personal")])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "personal",
        StaticToolPolicy(required_scope=DataScope.PERSONAL),
    )
    wrapped = testbed_api.TestbedAPI.wrap(inner, _context(), registry)

    with pytest.raises(ToolPolicyDeniedError) as err:
        await wrapped.async_call_tool(llm.ToolInput(tool_name="personal", tool_args={}))

    assert err.value.translation_domain == "magic_mic"
    assert err.value.translation_key == "tool_not_available"
    assert inner.calls == []


async def test_argument_dependent_scope_is_rechecked() -> None:
    """An exposed generic tool may still reject a restricted concrete call."""
    inner = RecordingAPIInstance([FixtureTool("mixed")])
    registry = ToolPolicyRegistry()
    registry.register_exact(FixtureTool, "mixed", ArgumentPolicy())
    wrapped = testbed_api.TestbedAPI.wrap(inner, _context(), registry)

    assert [tool.name for tool in wrapped.tools] == ["mixed"]
    with pytest.raises(ToolPolicyDeniedError):
        await wrapped.async_call_tool(
            llm.ToolInput(tool_name="mixed", tool_args={"scope": "personal"})
        )
    assert inner.calls == []


async def test_confirm_on_continuation_executes_on_ordinary_turn() -> None:
    """The representative policy remains frictionless after a wake word."""
    inner = RecordingAPIInstance([FixtureTool("demo")])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "demo",
        StaticToolPolicy(
            consequence=ConsequenceClass.CONFIRM_ON_CONTINUATION,
            required_scope=DataScope.HOUSEHOLD,
        ),
    )
    context = _context()
    wrapped = testbed_api.TestbedAPI.wrap(inner, context, registry)

    result = await wrapped.async_call_tool(
        llm.ToolInput(tool_name="demo", tool_args={"entity_id": "light.kitchen"})
    )

    assert result == {"executor": "inner", "tool_name": "demo"}
    assert len(inner.calls) == 1
    assert context.session_state.pending_operation is None


async def test_confirm_on_continuation_stages_exact_operation() -> None:
    """A continuation call is frozen for confirmation without inner execution."""
    inner = RecordingAPIInstance([FixtureTool("demo")])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "demo",
        StaticToolPolicy(
            consequence=ConsequenceClass.CONFIRM_ON_CONTINUATION,
            required_scope=DataScope.HOUSEHOLD,
        ),
    )
    context = _context(is_continuation=True)
    wrapped = testbed_api.TestbedAPI.wrap(inner, context, registry)
    arguments = {"entity_id": ["light.kitchen"]}

    result = await wrapped.async_call_tool(
        llm.ToolInput(tool_name="demo", tool_args=arguments)
    )
    arguments["entity_id"].append("light.garage")

    assert result == {
        "confirmation_required": True,
        "success": False,
        "tool_name": "demo",
    }
    assert inner.calls == []
    pending = context.session_state.pending_operation
    assert pending is not None
    assert pending.arguments == {"entity_id": ("light.kitchen",)}
    assert pending.principal is UNIDENTIFIED_PRINCIPAL
    assert pending.consequence is ConsequenceClass.CONFIRM_ON_CONTINUATION


async def test_always_confirm_stages_on_an_ordinary_turn() -> None:
    """The strongest implemented consequence always takes the pending path."""
    inner = RecordingAPIInstance([FixtureTool("always")])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "always",
        StaticToolPolicy(consequence=ConsequenceClass.ALWAYS_CONFIRM),
    )
    context = _context()
    wrapped = testbed_api.TestbedAPI.wrap(inner, context, registry)

    await wrapped.async_call_tool(
        llm.ToolInput(tool_name="always", tool_args={"value": 1})
    )

    assert inner.calls == []
    assert context.session_state.pending_operation is not None
