"""Tests for prompt-context Tier-2 request-conditioned name injection."""

import pytest

from custom_components.magic_mic.capabilities.prompt_context import (
    NAME_INJECTION_HEADER,
    async_domain_keyword_map,
    keyword_domains,
    select_request_names,
)
from custom_components.magic_mic.const import NAME_INJECTION_LIMIT
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component

ASSISTANT = conversation.DOMAIN


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core components the exposed-entity store needs."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


def _register(
    hass: HomeAssistant,
    entity_id: str,
    name: str,
    *,
    area_id: str | None = None,
    device_class: str | None = None,
    expose: bool = True,
) -> str:
    """Register a named entity, place it, state it, and expose it.

    Names it via ``original_name`` (as a real integration does) so the scorer matches
    against the friendly name, not the entity_id.
    """
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        domain, "test", entity_id, suggested_object_id=object_id, original_name=name
    )
    if area_id is not None:
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
    attributes: dict[str, object] = {ATTR_FRIENDLY_NAME: name}
    if device_class is not None:
        attributes[ATTR_DEVICE_CLASS] = device_class
    hass.states.async_set(entry.entity_id, "on", attributes)
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)
    return entry.entity_id


def _select(hass: HomeAssistant, utterance: str, area_id: str | None, **kwargs) -> str:
    """Run selection with an empty keyword map and the default limit unless overridden."""
    return select_request_names(
        hass,
        ASSISTANT,
        utterance,
        area_id,
        keyword_map=kwargs.get("keyword_map", {}),
        limit=kwargs.get("limit", NAME_INJECTION_LIMIT),
    )


async def test_keyword_map_derived_from_translations(hass: HomeAssistant) -> None:
    """The map comes from entity_component names, not a hardcoded English dict."""
    assert await async_setup_component(hass, "cover", {})
    assert await async_setup_component(hass, "lock", {})

    keyword_map = await async_domain_keyword_map(hass, "en")

    # Domain name and each device-class name map to their domain.
    assert keyword_map["cover"] == {"cover"}
    assert keyword_map["blind"] == {"cover"}
    assert keyword_map["garage"] == {"cover"}
    assert keyword_map["lock"] == {"lock"}


def test_keyword_domains_matches_exact_and_plural() -> None:
    """Exact tokens hit; plurals/variants match via the fuzzy scorer; misses do not."""
    keyword_map = {"light": {"light"}, "blind": {"cover"}}

    assert keyword_domains("turn off the light", keyword_map) == {"light"}
    assert keyword_domains("open the blinds", keyword_map) == {"cover"}
    assert keyword_domains("lock the front door", keyword_map) == set()


async def test_select_room_scoped_by_fuzzy_name(hass: HomeAssistant) -> None:
    """In-room, only name matches are injected; out-of-room, only strong matches are."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen").id
    living = area_reg.async_create("Living Room").id

    ceiling = _register(hass, "light.k_ceiling", "Ceiling Light", area_id=kitchen)
    _register(hass, "switch.k_kettle", "Kettle", area_id=kitchen)
    _register(hass, "light.lr", "Reading Lamp", area_id=living)

    block = _select(hass, "ceiling light", kitchen)

    assert block.startswith(NAME_INJECTION_HEADER)
    assert ceiling in block
    # The in-room but irrelevant kettle and the unrelated out-of-room lamp are excluded.
    assert "switch.k_kettle" not in block
    assert "light.lr" not in block


async def test_select_admits_strong_house_wide_match(hass: HomeAssistant) -> None:
    """An explicit cross-room reference reaches the entity, not just the current room."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen").id
    living = area_reg.async_create("Living Room").id
    bedroom = area_reg.async_create("Bedroom").id

    kitchen_ceiling = _register(hass, "light.k", "Ceiling Light", area_id=kitchen)
    _register(hass, "light.lr", "Floor Lamp", area_id=living)
    _register(hass, "light.br", "Reading Lamp", area_id=bedroom)

    # Spoken from the living room, but names the kitchen light explicitly.
    block = _select(hass, "turn off the kitchen ceiling light", living)

    assert kitchen_ceiling in block
    # A weak, incidental out-of-room match stays out.
    assert "light.br" not in block


async def test_select_room_ranks_above_equal_house(hass: HomeAssistant) -> None:
    """On an equal name match, the in-room entity sorts above the house-wide one."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen").id
    living = area_reg.async_create("Living Room").id

    kitchen_ceiling = _register(hass, "light.k", "Ceiling Light", area_id=kitchen)
    living_ceiling = _register(hass, "light.lr", "Ceiling Light", area_id=living)

    lines = _select(hass, "ceiling light", kitchen).splitlines()

    # Both identically-named lights are injected; the in-room one leads.
    assert kitchen_ceiling in lines[1]
    assert any(living_ceiling in line for line in lines[2:])


async def test_select_keyword_widening_only_within_a_room(hass: HomeAssistant) -> None:
    """Keyword widening injects a named domain in-room, but is skipped without a room.

    In-room the set is bounded, so a domain named by keyword is worth injecting even with
    no name match; with no room, widening would pull the whole domain, so it is skipped.
    """
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    sonos = _register(hass, "media_player.sonos", "Sonos", area_id=kitchen)
    keyword_map = {"media": {"media_player"}, "player": {"media_player"}}

    in_room = _select(hass, "the media player", kitchen, keyword_map=keyword_map)
    assert sonos in in_room

    # No area: keyword widening is skipped and the name does not fuzzy-match, so nothing.
    assert _select(hass, "the media player", None, keyword_map=keyword_map) is None


async def test_select_no_area_fallback_uses_fuzzy(hass: HomeAssistant) -> None:
    """With no room, fuzzy-name match over all exposed entities is the sole narrower."""
    bedroom = ar.async_get(hass).async_create("Bedroom").id
    lamp = _register(hass, "light.reading", "Reading Lamp", area_id=bedroom)
    _register(hass, "fan.ceiling", "Ceiling Fan", area_id=bedroom)

    block = _select(hass, "reading lamp", None)

    assert lamp in block
    assert "fan.ceiling" not in block


async def test_select_returns_none_when_nothing_relevant(hass: HomeAssistant) -> None:
    """No fuzzy match and no keyword hit yields None (skeleton stands alone)."""
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    _register(hass, "light.k_ceiling", "Ceiling Light", area_id=kitchen)

    assert _select(hass, "what's the weather", kitchen) is None


async def test_select_respects_limit(hass: HomeAssistant) -> None:
    """At most ``limit`` names are injected, most relevant first."""
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    for i in range(4):
        _register(hass, f"light.k_{i}", "Ceiling Light", area_id=kitchen)

    block = _select(hass, "ceiling light", kitchen, limit=2)

    # Header plus exactly two name lines.
    assert len(block.splitlines()) == 3
