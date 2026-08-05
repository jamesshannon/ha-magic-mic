"""Tests for the match-layer fuzzy fallback (Consumer 1, docs/find-entities.md).

Keyless and model-free: they build a real `MatchFailedError` and exposed entities, then
assert `resolve_name_miss` resolves decisively, returns candidates, or declines (None) so
the caller re-raises. The seam wiring into the proxy is covered in test_tool_interception.
"""

import pytest

from custom_components.magic_mic.capabilities.localization import ConversationStrings
from custom_components.magic_mic.capabilities.match_fallback import resolve_name_miss
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, intent, llm
from homeassistant.setup import async_setup_component

ASSISTANT = conversation.DOMAIN


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core components the exposed-entity store and matcher need."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


def _register(
    hass: HomeAssistant, entity_id: str, name: str, *, expose: bool = True
) -> str:
    """Register, state, and expose one named entity."""
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        domain, "test", entity_id, suggested_object_id=object_id, original_name=name
    )
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: name})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)
    return entry.entity_id


def _llm_context(assistant: str | None = ASSISTANT) -> llm.LLMContext:
    return llm.LLMContext(
        platform="magic_mic",
        context=None,
        language="en",
        assistant=assistant,
        device_id=None,
    )


def _name_miss(
    name: str | None,
    *,
    domains: list[str] | None = None,
    assistant: str | None = ASSISTANT,
    reason: intent.MatchFailedReason = intent.MatchFailedReason.NAME,
) -> intent.MatchFailedError:
    """Build the MatchFailedError an intent raises on an exact name miss."""
    return intent.MatchFailedError(
        result=intent.MatchTargetsResult(is_match=False, no_match_reason=reason),
        constraints=intent.MatchTargetsConstraints(
            name=name, domains=domains, assistant=assistant
        ),
        preferences=intent.MatchTargetsPreferences(),
    )


async def test_decisive_resolution_returns_the_canonical_entity_id(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A single strong fuzzy winner resolves to its entity_id for the caller to retry."""
    entity_id = _register(hass, "light.reading_lamp", "Reading Lamp")
    _register(hass, "light.kitchen_ceiling", "Kitchen Ceiling")

    fallback = resolve_name_miss(
        hass,
        _llm_context(),
        _name_miss("readng lamp", domains=["light"]),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id == entity_id
    assert fallback.tool_result is None


async def test_ambiguous_match_returns_scored_candidates(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Two equally-good matches come back as an ambiguous candidate list, no resolution."""
    a = _register(hass, "light.lamp_kitchen", "Lamp")
    b = _register(hass, "light.lamp_bedroom", "Lamp")

    fallback = resolve_name_miss(
        hass,
        _llm_context(),
        _name_miss("lamp", domains=["light"]),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id is None
    result = fallback.tool_result
    assert result["success"] is False
    assert result["error"] == "ambiguous_name"
    assert {row["entity_id"] for row in result["candidates"]} == {a, b}


async def test_no_fuzzy_hit_returns_not_found(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A name nothing scores above the floor comes back as a localized not-found."""
    _register(hass, "light.reading_lamp", "Reading Lamp")

    fallback = resolve_name_miss(
        hass,
        _llm_context(),
        _name_miss("thermostat", domains=["light"]),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id is None
    assert fallback.tool_result["error"] == "name_not_found"


async def test_non_name_failure_is_declined(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A non-NAME match failure returns None so the caller re-raises the original error."""
    _register(hass, "light.reading_lamp", "Reading Lamp")

    fallback = resolve_name_miss(
        hass,
        _llm_context(),
        _name_miss("reading lamp", reason=intent.MatchFailedReason.AREA),
        conversation_strings,
    )

    assert fallback is None


async def test_missing_name_or_assistant_is_declined(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Nothing to resolve without a name, or without an assistant to scope exposure."""
    _register(hass, "light.reading_lamp", "Reading Lamp")

    assert (
        resolve_name_miss(hass, _llm_context(), _name_miss(None), conversation_strings)
        is None
    )
    assert (
        resolve_name_miss(
            hass,
            _llm_context(assistant=None),
            _name_miss("reading lamp", assistant=None),
            conversation_strings,
        )
        is None
    )
