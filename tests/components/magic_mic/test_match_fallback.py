"""Tests for the match-layer fuzzy fallback (Consumer 1, docs/find-entities.md).

Keyless and model-free: they build a real `MatchFailedError` and exposed entities, then
assert `resolve_name_miss` resolves decisively, returns candidates, or declines (None) so
the caller re-raises. The seam wiring into the proxy is covered in test_tool_interception.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.capabilities.localization import ConversationStrings
from custom_components.magic_mic.capabilities.match_fallback import resolve_name_miss
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    intent,
    llm,
)
from homeassistant.setup import async_setup_component

ASSISTANT = conversation.DOMAIN


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core components the exposed-entity store and matcher need."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


def _area(hass: HomeAssistant, name: str) -> str:
    """Create (or fetch) an area by its spoken name and return its id."""
    return ar.async_get(hass).async_get_or_create(name).id


def _register(
    hass: HomeAssistant,
    entity_id: str,
    name: str,
    *,
    area_id: str | None = None,
    expose: bool = True,
) -> str:
    """Register, state, place, and expose one named entity."""
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        domain, "test", entity_id, suggested_object_id=object_id, original_name=name
    )
    if area_id is not None:
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: name})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)
    return entry.entity_id


def _satellite_in(hass: HomeAssistant, area_id: str) -> str:
    """Create a device in ``area_id`` (the requesting satellite) and return its id."""
    entry = MockConfigEntry(domain="magic_mic")
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("magic_mic", "satellite")}
    )
    dev_reg.async_update_device(device.id, area_id=area_id)
    return device.id


def _llm_context(
    assistant: str | None = ASSISTANT, *, device_id: str | None = None
) -> llm.LLMContext:
    return llm.LLMContext(
        platform="magic_mic",
        context=None,
        language="en",
        assistant=assistant,
        device_id=device_id,
    )


def _name_miss(
    name: str | None,
    *,
    domains: list[str] | None = None,
    area_name: str | None = None,
    assistant: str | None = ASSISTANT,
    reason: intent.MatchFailedReason = intent.MatchFailedReason.NAME,
) -> intent.MatchFailedError:
    """Build the MatchFailedError an intent raises on an exact name miss."""
    return intent.MatchFailedError(
        result=intent.MatchTargetsResult(is_match=False, no_match_reason=reason),
        constraints=intent.MatchTargetsConstraints(
            name=name, domains=domains, area_name=area_name, assistant=assistant
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


async def test_unique_name_resolves_across_rooms_despite_echoed_area(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A uniquely named device in another room resolves; the device's own room is no filter.

    The model, standing in the living room, echoes ``area="living room"`` onto the call. That
    must not scope the search: "the floor lamp" is unique house-wide, so it resolves the den
    lamp regardless of the room the request came from.
    """
    living = _area(hass, "Living Room")
    den = _area(hass, "Den")
    den_lamp = _register(hass, "light.den_floor", "Corner Floor Lamp", area_id=den)
    _register(hass, "light.living_couch", "Sofa Reading Light", area_id=living)
    satellite = _satellite_in(hass, living)

    fallback = resolve_name_miss(
        hass,
        _llm_context(device_id=satellite),
        _name_miss("floor lamp", domains=["light"], area_name="living room"),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id == den_lamp


async def test_different_spoken_room_is_honored_as_scope(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A genuinely different spoken room scopes the search, so it does not cross rooms.

    "the reading light in the kitchen" from a living-room satellite must not reach the living
    room's reading light: the kitchen has none, so it comes back not-found.
    """
    living = _area(hass, "Living Room")
    _area(hass, "Kitchen")
    _register(hass, "light.living_couch", "Sofa Reading Light", area_id=living)
    _register(hass, "switch.kettle", "Kettle", area_id=_area(hass, "Kitchen"))
    satellite = _satellite_in(hass, living)

    fallback = resolve_name_miss(
        hass,
        _llm_context(device_id=satellite),
        _name_miss("reading light", domains=["light"], area_name="kitchen"),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id is None
    assert fallback.tool_result["error"] == "name_not_found"


async def test_ambiguous_name_broken_by_requesting_room(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Two like-named devices resolve to the one in the requesting room (context tiebreak)."""
    living = _area(hass, "Living Room")
    bedroom = _area(hass, "Bedroom")
    living_lamp = _register(hass, "light.lamp_living", "Lamp", area_id=living)
    _register(hass, "light.lamp_bedroom", "Lamp", area_id=bedroom)
    satellite = _satellite_in(hass, living)

    fallback = resolve_name_miss(
        hass,
        _llm_context(device_id=satellite),
        _name_miss("lamp", domains=["light"]),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id == living_lamp


async def test_ambiguous_name_stays_ambiguous_without_a_room(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """With no requesting room to break the tie, two like-named devices still ask."""
    living = _area(hass, "Living Room")
    bedroom = _area(hass, "Bedroom")
    a = _register(hass, "light.lamp_living", "Lamp", area_id=living)
    b = _register(hass, "light.lamp_bedroom", "Lamp", area_id=bedroom)

    fallback = resolve_name_miss(
        hass,
        _llm_context(),
        _name_miss("lamp", domains=["light"]),
        conversation_strings,
    )

    assert fallback is not None
    assert fallback.entity_id is None
    assert {row["entity_id"] for row in fallback.tool_result["candidates"]} == {a, b}


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
