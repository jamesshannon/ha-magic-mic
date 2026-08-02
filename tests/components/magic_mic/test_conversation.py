"""Conversation-turn tests for the Magic Mic agents."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.internal.claude.const import (
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
)
from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
    assert "web_fetch" not in {
        tool["name"] for tool in mock_create_stream.call_args.kwargs["tools"]
    }
    assert "web_search" not in {
        tool["name"] for tool in mock_create_stream.call_args.kwargs["tools"]
    }


@pytest.mark.parametrize(
    ("web_search", "web_fetch"),
    [(True, False), (False, True), (True, True)],
)
async def test_provider_web_options_control_native_tools(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
    web_search: bool,
    web_fetch: bool,
) -> None:
    """Provider options independently control Claude's native web tools."""
    entry = setup_integration
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["data_schema"]({}) == {
        CONF_WEB_FETCH: False,
        CONF_WEB_SEARCH: False,
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_WEB_FETCH: web_fetch,
            CONF_WEB_SEARCH: web_search,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_WEB_FETCH: web_fetch,
        CONF_WEB_SEARCH: web_search,
    }

    ent_reg = er.async_get(hass)
    testbed_id = next(
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.unique_id == f"{entry.entry_id}_testbed"
    )
    mock_create_stream.return_value = [create_content_block(0, ["Hello there."])]

    await _converse(hass, testbed_id)

    tool_names = {tool["name"] for tool in mock_create_stream.call_args.kwargs["tools"]}
    assert ("web_fetch" in tool_names) is web_fetch
    assert ("web_search" in tool_names) is web_search
