"""Driven conversation tests for the testbed tool-interception boundary."""

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

from anthropic.types import RawMessageStreamEvent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.magic_mic.execution_result import ToolExecutionResult
from custom_components.magic_mic.identity import (
    DATA_RESOLVED_PRINCIPALS,
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    RequestSource,
    ResolvedPrincipal,
    async_resolve_user,
    get_resolved_user,
)
from custom_components.magic_mic.session_state import async_get_session_state
from custom_components.magic_mic.testbed import (
    api as testbed_api,
    entity as testbed_entity,
)
from custom_components.magic_mic.tool_policy import (
    StaticToolPolicy,
    ToolPolicyContext,
    ToolPolicyRegistry,
)
from custom_components.magic_mic.undo import (
    InverseOperation,
    LocalizedDescription,
    UndoAction,
    UndoScopeBinding,
    UndoUnavailable,
    UndoUnavailableReason,
)
from evals.harness.backing import build_executable_world
from evals.harness.corpus import Entity, World
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er, llm
from homeassistant.util.json import JsonObjectType

from .streaming import create_content_block, create_tool_use_block

_ENTITY_ID = "light.reading_lamp"
_PRIVATE_VALUE = "private-inverse-argument"


def _agent_ids(hass: HomeAssistant, entry: MockConfigEntry) -> tuple[str, str]:
    """Return the baseline and testbed conversation entity IDs."""
    by_unique = {
        entity.unique_id: entity.entity_id
        for entity in er.async_get(hass).entities.values()
        if entity.platform == "magic_mic"
    }
    return (
        by_unique[f"{entry.entry_id}_claude_baseline"],
        by_unique[f"{entry.entry_id}_testbed"],
    )


async def _setup_light(hass: HomeAssistant) -> None:
    """Create one executable, exposed light in the off state."""
    await build_executable_world(
        hass,
        World(
            areas=("living_room",),
            entities=(
                Entity(
                    entity_id=_ENTITY_ID,
                    name="Reading Lamp",
                    area="living_room",
                    state="off",
                ),
            ),
        ),
    )


def _tool_turn(final_text: str = "Done.") -> list[list]:
    """Return one provider tool-use generation and its follow-up generation."""
    return [
        create_tool_use_block(
            0,
            "toolu_turn_on",
            "HassTurnOn",
            ['{"name": "Reading Lamp"}'],
        ),
        create_content_block(0, [final_text]),
    ]


async def _converse(
    hass: HomeAssistant,
    agent_id: str,
    context: Context | None = None,
) -> conversation.ConversationResult:
    """Drive the provider loop with one device-control request."""
    return await conversation.async_converse(
        hass,
        "turn on the reading lamp",
        None,
        context or Context(),
        agent_id=agent_id,
    )


def _private_undo_action() -> UndoAction:
    """Return an undo action carrying a value that must never reach the model."""
    return UndoAction(
        authorization=UndoScopeBinding(scope=DataScope.HOUSEHOLD),
        description=LocalizedDescription("magic_mic", "undo_action_tool"),
        inverse=InverseOperation.custom(
            "fixture.restore_light",
            {"private_value": _PRIVATE_VALUE},
        ),
    )


async def test_provider_tool_use_crosses_stock_and_proxy_execution_paths(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Baseline executes stock; testbed intercepts, strips private data, and follows up."""
    await _setup_light(hass)
    baseline_id, testbed_id = _agent_ids(hass, setup_integration)

    mock_create_stream.return_value = _tool_turn("Baseline done.")
    baseline = await _converse(hass, baseline_id)

    assert hass.states.get(_ENTITY_ID).state == "on"
    assert baseline.response.speech["plain"]["speech"] == "Baseline done."
    assert mock_create_stream.call_count == 2

    await hass.services.async_call(
        "light",
        "turn_off",
        {"entity_id": _ENTITY_ID},
        blocking=True,
    )
    mock_create_stream.reset_mock()

    original_call_tool = llm.APIInstance.async_call_tool
    undo_action = _private_undo_action()

    async def async_call_with_private_outcome(
        api_instance: llm.APIInstance,
        tool_input: llm.ToolInput,
    ) -> JsonObjectType:
        """Let the stock executor act, then attach private test-only undo metadata."""
        result = await original_call_tool(api_instance, tool_input)
        if tool_input.tool_name == "HassTurnOn":
            return ToolExecutionResult(result, undo_action)
        return result

    mock_create_stream.return_value = _tool_turn("Testbed done.")
    with patch.object(
        llm.APIInstance,
        "async_call_tool",
        async_call_with_private_outcome,
    ):
        testbed = await _converse(hass, testbed_id)

    assert hass.states.get(_ENTITY_ID).state == "on"
    assert testbed.response.speech["plain"]["speech"] == "Testbed done."
    assert mock_create_stream.call_count == 2

    session_state = async_get_session_state(hass, testbed.conversation_id)
    assert session_state.undo_journal[-1].disposition is undo_action
    follow_up_messages = repr(mock_create_stream.call_args_list[1].kwargs["messages"])
    assert "tool_result" in follow_up_messages
    assert _PRIVATE_VALUE not in follow_up_messages


async def test_provider_hidden_tool_use_is_denied_before_execution_and_followed_up(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A provider-emitted hidden tool is rechecked, denied, and returned to gen2."""
    await _setup_light(hass)
    _, testbed_id = _agent_ids(hass, setup_integration)
    registry = ToolPolicyRegistry()
    registry.register_exact(
        llm.IntentTool,
        "HassTurnOn",
        StaticToolPolicy(required_scope=DataScope.PERSONAL),
    )
    original_wrap = testbed_api.TestbedAPI.wrap

    def wrap_with_policy(
        inner: llm.APIInstance,
        context: ToolPolicyContext,
        *,
        selector: testbed_api.CapabilitySelector | None = None,
    ) -> testbed_api.TestbedAPI:
        """Use the real decorator with this test's restrictive registry."""
        return original_wrap(inner, context, registry, selector=selector)

    mock_create_stream.return_value = _tool_turn("I couldn't do that.")
    with patch.object(testbed_api.TestbedAPI, "wrap", side_effect=wrap_with_policy):
        result = await _converse(hass, testbed_id)

    assert hass.states.get(_ENTITY_ID).state == "off"
    assert result.response.speech["plain"]["speech"] == "I couldn't do that."
    assert mock_create_stream.call_count == 2

    first_generation_tools = {
        tool["name"] for tool in mock_create_stream.call_args_list[0].kwargs["tools"]
    }
    assert "HassTurnOn" not in first_generation_tools
    follow_up_messages = repr(mock_create_stream.call_args_list[1].kwargs["messages"])
    assert "ToolPolicyDeniedError" in follow_up_messages
    assert "HassTurnOn" in follow_up_messages


@pytest.mark.parametrize(
    "termination", ["immediate_stream_error", "stream_error", "request_cancel"]
)
async def test_abnormal_stream_end_cancels_tools_before_clearing_identity(
    hass: HomeAssistant,
    hass_admin_user: MockUser,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
    termination: str,
) -> None:
    """A failed or cancelled stream cannot outlive request identity."""
    await _setup_light(hass)
    _, testbed_id = _agent_ids(hass, setup_integration)
    context = Context(user_id=hass_admin_user.id)
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()
    release_tool = asyncio.Event()
    release_stream = asyncio.Event()
    execution_principals: list[str | None] = []
    cleanup_principals: list[str | None] = []
    late_effects: list[str] = []
    policy_contexts: list[ToolPolicyContext] = []

    async def resolve_as_authenticated_text(
        hass_arg: HomeAssistant,
        context_arg: Context,
        *,
        request_source: RequestSource,
    ) -> ResolvedPrincipal:
        """Establish an identified principal for this failure-path fixture."""
        del request_source
        return await async_resolve_user(
            hass_arg,
            context_arg,
            request_source=RequestSource.TEXT,
        )

    async def blocking_tool_call(
        api_instance: llm.APIInstance,
        tool_input: llm.ToolInput,
    ) -> JsonObjectType:
        """Wait until cancelled, observing identity during execution and cleanup."""
        del api_instance
        assert tool_input.tool_name == "HassTurnOn"
        execution_principals.append(get_resolved_user(hass, context).user_id)
        tool_started.set()
        try:
            await release_tool.wait()
        except asyncio.CancelledError:
            tool_cancelled.set()
            raise
        else:
            late_effects.append("light changed")
            return {"success": True}
        finally:
            cleanup_principals.append(get_resolved_user(hass, context).user_id)

    async def broken_stream() -> AsyncGenerator[RawMessageStreamEvent]:
        """Yield a complete tool call, then fail while that call is blocked."""
        for event in create_tool_use_block(
            0,
            "toolu_stream_failure",
            "HassTurnOn",
            ['{"name": "Reading Lamp"}'],
        ):
            yield event
        if termination == "immediate_stream_error":
            raise RuntimeError("provider stream failed")
        await tool_started.wait()
        if termination == "stream_error":
            raise RuntimeError("provider stream failed")
        await release_stream.wait()

    original_wrap = testbed_api.TestbedAPI.wrap

    def capture_wrap(
        inner: llm.APIInstance,
        policy_context: ToolPolicyContext,
        *,
        selector: testbed_api.CapabilitySelector | None = None,
    ) -> testbed_api.TestbedAPI:
        """Retain the turn metadata used by the real wrapper."""
        policy_contexts.append(policy_context)
        return original_wrap(inner, policy_context, selector=selector)

    mock_create_stream.side_effect = lambda **_kwargs: broken_stream()
    try:
        with (
            patch.object(
                testbed_entity,
                "async_resolve_user",
                side_effect=resolve_as_authenticated_text,
            ),
            patch.object(llm.APIInstance, "async_call_tool", blocking_tool_call),
            patch.object(testbed_api.TestbedAPI, "wrap", side_effect=capture_wrap),
        ):
            if termination != "request_cancel":
                with pytest.raises(RuntimeError, match="provider stream failed"):
                    await _converse(hass, testbed_id, context)
            else:
                turn = asyncio.create_task(_converse(hass, testbed_id, context))
                await tool_started.wait()
                turn.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await turn
    finally:
        # If cancellation regresses, let the orphan finish so test teardown cannot hang.
        release_stream.set()
        release_tool.set()
        await asyncio.sleep(0)

    assert tool_started.is_set()
    assert tool_cancelled.is_set()
    assert execution_principals == [hass_admin_user.id]
    assert cleanup_principals == [hass_admin_user.id]
    assert late_effects == []
    effects = policy_contexts[0].turn_metadata.effects
    assert len(effects) == 1
    assert isinstance(effects[0].disposition, UndoUnavailable)
    assert effects[0].disposition.reason is UndoUnavailableReason.NOT_SUPPORTED
    assert get_resolved_user(hass, context) is UNIDENTIFIED_PRINCIPAL
    assert context.id not in hass.data.get(DATA_RESOLVED_PRINCIPALS, {})
