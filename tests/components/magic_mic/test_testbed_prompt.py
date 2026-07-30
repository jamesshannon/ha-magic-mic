"""Tests for the skeleton prompt interposition (prompt-context Tier 1 wiring)."""

import pytest

from custom_components.magic_mic.capabilities.prompt_context import SKELETON_HEADER
from custom_components.magic_mic.testbed.prompt import (
    SkeletonAssistAPI,
    async_skeleton_llm_api,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, llm
from homeassistant.setup import async_setup_component

ASSISTANT = conversation.DOMAIN


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
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)
    living = area_reg.async_create("Living Room")
    entry = ent_reg.async_get_or_create(
        "light", "test", "light.lr", suggested_object_id="lr"
    )
    ent_reg.async_update_entity(entry.entity_id, area_id=living.id)
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: "Reading Lamp"})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, True)

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
