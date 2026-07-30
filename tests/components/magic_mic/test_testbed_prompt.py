"""Tests for the skeleton prompt interposition (prompt-context Tier 1 wiring)."""

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.capabilities.prompt_context import SKELETON_HEADER
from custom_components.magic_mic.testbed.prompt import (
    SkeletonAssistAPI,
    async_skeleton_llm_api,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, llm
from homeassistant.setup import async_setup_component

from .streaming import create_content_block

ASSISTANT = conversation.DOMAIN


def _system_text(mock_create: AsyncMock) -> str:
    """Return the system prompt of the most recent model call as flat text.

    Prompt caching turns `system` into a list of typed blocks; a plain run leaves it
    a string. Normalize both to one string for substring assertions.
    """
    system = mock_create.call_args.kwargs["system"]
    if isinstance(system, str):
        return system
    return "\n".join(block["text"] for block in system)


def _expose_light(hass: HomeAssistant) -> None:
    """Create, place, state, and expose one named light for a driven turn.

    Names the entity via ``original_name``, as a real integration does: the default
    COMPUTED_NAME alias then resolves to it, so the roster carries the name without a
    synthetic user alias.
    """
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    living = area_reg.async_create("Living Room")
    entry = ent_reg.async_get_or_create(
        "light",
        "test",
        "light.lr",
        suggested_object_id="lr",
        original_name="Reading Lamp",
    )
    ent_reg.async_update_entity(entry.entity_id, area_id=living.id)
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: "Reading Lamp"})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, True)


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core components the exposed-entity store needs."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


def _llm_context() -> llm.LLMContext:
    """Build a minimal LLMContext for the assist assistant."""
    return llm.LLMContext(
        platform="magic_mic",
        context=None,
        language="en",
        assistant=ASSISTANT,
        device_id=None,
    )


async def test_api_prompt_carries_skeleton_not_roster(hass: HomeAssistant) -> None:
    """The skeleton API emits the taxonomy skeleton, never the Static Context dump."""
    _expose_light(hass)

    instance = await SkeletonAssistAPI(hass).async_get_api_instance(_llm_context())

    assert SKELETON_HEADER in instance.api_prompt
    assert "Living Room: light x1" in instance.api_prompt
    # The roster marker and the specific device name must be gone.
    assert "Static Context" not in instance.api_prompt
    assert "Reading Lamp" not in instance.api_prompt


async def test_no_entities_prompt_preserved(hass: HomeAssistant) -> None:
    """With nothing exposed, the inherited no-entities prompt still applies."""
    instance = await SkeletonAssistAPI(hass).async_get_api_instance(_llm_context())

    assert SKELETON_HEADER not in instance.api_prompt
    assert instance.api_prompt == llm.NO_ENTITIES_PROMPT


def test_async_skeleton_llm_api_substitutes_only_assist(hass: HomeAssistant) -> None:
    """The Assist API is swapped for the skeleton variant; others pass through."""
    assert isinstance(
        async_skeleton_llm_api(hass, llm.LLM_API_ASSIST), SkeletonAssistAPI
    )
    assert isinstance(
        async_skeleton_llm_api(hass, [llm.LLM_API_ASSIST]), SkeletonAssistAPI
    )
    assert async_skeleton_llm_api(hass, "some_other_api") == "some_other_api"
    assert async_skeleton_llm_api(hass, ["a", "b"]) == ["a", "b"]


async def test_driven_testbed_turn_sends_skeleton_baseline_sends_roster(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """End to end: the testbed's system prompt swaps the roster for the skeleton.

    Drives a real turn through each agent (full `_async_handle_message`) with a light
    exposed, and inspects the system prompt actually handed to the model. The testbed
    carries the skeleton and drops the roster; the baseline is unchanged.
    """
    _expose_light(hass)
    entry = setup_integration
    by_unique = {
        e.unique_id: e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == "magic_mic"
    }
    baseline_id = by_unique[f"{entry.entry_id}_claude_baseline"]
    testbed_id = by_unique[f"{entry.entry_id}_testbed"]

    mock_create_stream.return_value = [create_content_block(0, ["Done."])]
    await conversation.async_converse(
        hass, "hello", None, Context(), agent_id=testbed_id
    )
    testbed_system = _system_text(mock_create_stream)

    mock_create_stream.return_value = [create_content_block(0, ["Done."])]
    await conversation.async_converse(
        hass, "hello", None, Context(), agent_id=baseline_id
    )
    baseline_system = _system_text(mock_create_stream)

    assert SKELETON_HEADER in testbed_system
    assert "Living Room: light x1" in testbed_system
    assert "Static Context" not in testbed_system
    assert "Reading Lamp" not in testbed_system

    assert "Static Context" in baseline_system
    assert "Reading Lamp" in baseline_system
    assert SKELETON_HEADER not in baseline_system
