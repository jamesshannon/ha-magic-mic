"""Conversation-turn tests for the Magic Mic agents."""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block


async def _converse(hass: HomeAssistant, agent_id: str) -> str:
    """Run one turn through the given agent and return its speech."""
    result = await conversation.async_converse(
        hass, "hello", None, Context(), agent_id=agent_id
    )
    return result.response.speech["plain"]["speech"]


async def test_testbed_turn_matches_baseline(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """The testbed proxy produces the same turn output as the baseline.

    At Wave 0 the interposition wrapper is pass-through, so driving the identical
    mocked model response through each agent must yield identical speech. This
    exercises the testbed's full `_async_handle_message` override (llm_api wrap +
    loop delegation) end to end, which the load test does not.
    """
    entry = setup_integration
    ent_reg = er.async_get(hass)
    by_unique = {
        entity.unique_id: entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.platform == "magic_mic"
    }
    baseline_id = by_unique[f"{entry.entry_id}_claude_baseline"]
    testbed_id = by_unique[f"{entry.entry_id}_testbed"]

    mock_create_stream.return_value = [create_content_block(0, ["Hello there."])]
    baseline_speech = await _converse(hass, baseline_id)

    mock_create_stream.return_value = [create_content_block(0, ["Hello there."])]
    testbed_speech = await _converse(hass, testbed_id)

    assert baseline_speech == "Hello there."
    assert testbed_speech == baseline_speech
    assert mock_create_stream.call_count == 2
