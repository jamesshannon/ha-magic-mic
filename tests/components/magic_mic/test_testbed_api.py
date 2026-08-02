"""Tests for the testbed policy and tool-execution seam."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from custom_components.magic_mic.execution_result import (
    ToolExecutionResult,
    set_intent_undo_disposition,
)

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
    EffectClass,
    ExposurePolicy,
    StaticToolPolicy,
    ToolPolicy,
    ToolPolicyContext,
    ToolPolicyDeniedError,
    ToolPolicyRegistry,
)
from custom_components.magic_mic.undo import (
    NO_MUTATION,
    InverseOperation,
    LocalizedDescription,
    UndoAction,
    UndoScopeBinding,
    UndoStatus,
    UndoUnavailable,
    UndoUnavailableReason,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
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


_DEFAULT_RESULT = object()


class RecordingAPIInstance(llm.APIInstance):
    """Inner API whose override proves the wrapper delegates correctly."""

    calls: list[llm.ToolInput]

    def __init__(
        self,
        tools: list[llm.Tool],
        *,
        error: Exception | None = None,
        result: object = _DEFAULT_RESULT,
    ) -> None:
        """Initialize the API with observable fields and calls."""
        self.calls = []
        self.error = error
        self.result = result

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
        if self.error is not None:
            raise self.error
        if self.result is not _DEFAULT_RESULT:
            assert isinstance(self.result, dict)
            return self.result
        return {"executor": "inner", "tool_name": tool_input.tool_name}


class BlockingAPIInstance(RecordingAPIInstance):
    """Inner API that pauses one tool call until its test releases it."""

    def __init__(self, tools: list[llm.Tool], *, result: object) -> None:
        """Initialize the blocking executor."""
        super().__init__(tools, result=result)
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def async_call_tool(self, tool_input: llm.ToolInput) -> JsonObjectType:
        """Pause after execution starts, then delegate to the recording API."""
        self.started.set()
        await self.release.wait()
        return await super().async_call_tool(tool_input)


def _undo_action() -> UndoAction:
    """Build one household-scoped fixture inverse."""
    return UndoAction(
        authorization=UndoScopeBinding(scope=DataScope.HOUSEHOLD),
        description=LocalizedDescription("magic_mic", "undo_action_tool"),
        inverse=InverseOperation.custom("fixture.undo", {"value": 1}),
    )


def _context(
    principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL,
    *,
    is_continuation: bool = False,
) -> ToolPolicyContext:
    """Build policy context with initialized turn metadata."""
    state = MagicMicSessionState()
    turn_metadata = state.async_begin_turn(
        "turn-1",
        is_continuation=is_continuation,
        principal=principal,
    )
    return ToolPolicyContext(
        is_continuation=is_continuation,
        principal=principal,
        session_state=state,
        turn_metadata=turn_metadata,
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
    context = _context()
    wrapped = testbed_api.TestbedAPI.wrap(inner, context)
    tool_input = llm.ToolInput(tool_name="unclassified", tool_args={"value": 1})

    result = await wrapped.async_call_tool(tool_input)

    assert result == {"executor": "inner", "tool_name": "unclassified"}
    assert inner.calls == [tool_input]
    barrier = context.session_state.undo_journal[-1]
    assert isinstance(barrier.disposition, UndoUnavailable)
    assert barrier.disposition.reason is UndoUnavailableReason.NOT_SUPPORTED


async def test_private_undo_result_is_journaled_but_not_returned() -> None:
    """Inverse arguments stay out of the mapping returned to the model."""
    action = _undo_action()
    inner = RecordingAPIInstance(
        [FixtureTool("mutating")],
        result=ToolExecutionResult({"success": True}, action),
    )
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "mutating",
        StaticToolPolicy(effect=EffectClass.MUTATING),
    )
    context = _context()

    result = await testbed_api.TestbedAPI.wrap(
        inner, context, registry
    ).async_call_tool(llm.ToolInput(tool_name="mutating", tool_args={}))

    assert type(result) is dict
    assert result == {"success": True}
    assert "undo" not in result
    entry = context.session_state.undo_journal[-1]
    assert entry.disposition is action
    assert entry.status is UndoStatus.AVAILABLE
    assert context.turn_metadata.effects == [entry]


async def test_overlapping_turn_keeps_effect_attribution_request_local() -> None:
    """A later turn cannot capture an earlier turn's delayed tool effect."""
    state = MagicMicSessionState()
    turn_a = state.async_begin_turn("turn-a")
    context_a = ToolPolicyContext(
        principal=UNIDENTIFIED_PRINCIPAL,
        session_state=state,
        turn_metadata=turn_a,
    )
    inner = BlockingAPIInstance(
        [FixtureTool("mutating")],
        result=ToolExecutionResult({"success": True}, _undo_action()),
    )
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "mutating",
        StaticToolPolicy(effect=EffectClass.MUTATING),
    )
    wrapped = testbed_api.TestbedAPI.wrap(inner, context_a, registry)
    call = asyncio.create_task(
        wrapped.async_call_tool(llm.ToolInput(tool_name="mutating", tool_args={}))
    )
    await inner.started.wait()

    turn_b = state.async_begin_turn("turn-b")
    inner.release.set()
    await call

    entry = state.undo_journal[-1]
    assert entry.turn_id == "turn-a"
    assert turn_a.effects == [entry]
    assert turn_b.effects == []
    assert state.async_get_turn_metadata("turn-a") is turn_a
    assert state.async_get_turn_metadata("turn-b") is turn_b


async def test_intent_response_undo_metadata_survives_ha_wrapper() -> None:
    """Intent-owned undo metadata remains private through IntentResponseDict."""
    response = intent.IntentResponse(language="en")
    action = _undo_action()
    set_intent_undo_disposition(response, action)
    wrapped_response = llm.IntentResponseDict(response)
    inner = RecordingAPIInstance(
        [FixtureTool("intent")],
        result=wrapped_response,
    )
    context = _context()

    result = await testbed_api.TestbedAPI.wrap(inner, context).async_call_tool(
        llm.ToolInput(tool_name="intent", tool_args={})
    )

    assert result is wrapped_response
    assert context.session_state.undo_journal[-1].disposition is action


@pytest.mark.parametrize("effect", [EffectClass.MUTATING, EffectClass.UNKNOWN])
async def test_missing_undo_metadata_records_barrier(effect: EffectClass) -> None:
    """A possible mutation cannot expose an older action as latest undo."""
    inner = RecordingAPIInstance([FixtureTool("possible_mutation")])
    registry = ToolPolicyRegistry()
    registry.register_exact(
        FixtureTool,
        "possible_mutation",
        StaticToolPolicy(effect=effect),
    )
    context = _context()

    await testbed_api.TestbedAPI.wrap(inner, context, registry).async_call_tool(
        llm.ToolInput(tool_name="possible_mutation", tool_args={})
    )

    entry = context.session_state.undo_journal[-1]
    assert isinstance(entry.disposition, UndoUnavailable)
    assert entry.disposition.reason is UndoUnavailableReason.NOT_SUPPORTED


async def test_explicit_no_mutation_and_read_only_do_not_shadow_undo() -> None:
    """Known non-effects leave the latest mutation unchanged."""
    context = _context()
    for tool_name, effect, result in (
        (
            "reported_noop",
            EffectClass.MUTATING,
            ToolExecutionResult({"success": True}, NO_MUTATION),
        ),
        ("read_only", EffectClass.READ_ONLY, {"success": True}),
    ):
        inner = RecordingAPIInstance([FixtureTool(tool_name)], result=result)
        registry = ToolPolicyRegistry()
        registry.register_exact(
            FixtureTool,
            tool_name,
            StaticToolPolicy(effect=effect),
        )
        await testbed_api.TestbedAPI.wrap(inner, context, registry).async_call_tool(
            llm.ToolInput(tool_name=tool_name, tool_args={})
        )

    assert context.session_state.undo_journal == ()


async def test_failed_possible_mutation_records_barrier() -> None:
    """A raised tool may have partially changed state, so undo fails closed."""
    inner = RecordingAPIInstance(
        [FixtureTool("failing")],
        error=RuntimeError("partial failure"),
    )
    context = _context()

    with pytest.raises(RuntimeError, match="partial failure"):
        await testbed_api.TestbedAPI.wrap(inner, context).async_call_tool(
            llm.ToolInput(tool_name="failing", tool_args={})
        )

    entry = context.session_state.undo_journal[-1]
    assert isinstance(entry.disposition, UndoUnavailable)
    assert entry.disposition.reason is UndoUnavailableReason.NOT_SUPPORTED


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
    assert [
        (trace.tool_name, trace.allowed, trace.stage)
        for trace in context.turn_metadata.tool_policy
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


async def test_undeclared_tool_cannot_reach_dynamic_inner_executor() -> None:
    """A call absent from the advertised API is rejected before delegation."""
    inner = RecordingAPIInstance([FixtureTool("declared")])
    context = _context()
    wrapped = testbed_api.TestbedAPI.wrap(inner, context)

    with pytest.raises(ToolPolicyDeniedError) as err:
        await wrapped.async_call_tool(
            llm.ToolInput(tool_name="dynamically_accepted", tool_args={})
        )

    assert err.value.translation_domain == "magic_mic"
    assert err.value.translation_key == "tool_not_available"
    assert inner.calls == []
    trace = context.turn_metadata.tool_policy[-1]
    assert trace.allowed is False
    assert trace.policy_source == "undeclared"
    assert trace.stage == "execution"
    assert trace.tool_name == "dynamically_accepted"


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
