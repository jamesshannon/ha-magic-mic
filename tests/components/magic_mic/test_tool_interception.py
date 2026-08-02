"""Driven conversation tests for the testbed tool-interception boundary."""

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.execution_result import ToolExecutionResult
from custom_components.magic_mic.identity import DataScope
from custom_components.magic_mic.session_state import async_get_session_state
from custom_components.magic_mic.testbed import api as testbed_api
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
) -> conversation.ConversationResult:
    """Drive the provider loop with one device-control request."""
    return await conversation.async_converse(
        hass,
        "turn on the reading lamp",
        None,
        Context(),
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
    ) -> testbed_api.TestbedAPI:
        """Use the real decorator with this test's restrictive registry."""
        return original_wrap(inner, context, registry)

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
