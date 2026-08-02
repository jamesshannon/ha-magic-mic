"""Tests for the find_entities capability tool and its testbed wiring."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.capabilities.entities import (
    FindEntitiesTool,
    async_get_tools,
)
from custom_components.magic_mic.identity import UNIDENTIFIED_PRINCIPAL
from custom_components.magic_mic.session_state import MagicMicSessionState
from custom_components.magic_mic.testbed.prompt import SkeletonAssistAPI
from custom_components.magic_mic.tool_policy import (
    DEFAULT_TOOL_POLICY_REGISTRY,
    EffectClass,
    PolicySource,
    ToolPolicyContext,
    evaluate_invocation,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    llm,
)
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
    state: str = "on",
    expose: bool = True,
) -> str:
    """Register a named entity, place it, state it, and expose it.

    Names it via ``original_name`` (as a real integration does) so the default
    COMPUTED_NAME alias resolves to the name the scorer will match against.
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
    hass.states.async_set(entry.entity_id, state, attributes)
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)
    return entry.entity_id


def _llm_context(assistant: str | None = ASSISTANT) -> llm.LLMContext:
    """Build a minimal LLMContext for the given assistant."""
    return llm.LLMContext(
        platform="magic_mic",
        context=None,
        language="en",
        assistant=assistant,
        device_id=None,
    )


async def _call(hass: HomeAssistant, args: dict, assistant: str | None = ASSISTANT):
    """Invoke find_entities with the given tool args."""
    tool = FindEntitiesTool()
    return await tool.async_call(
        hass,
        llm.ToolInput(tool_name="find_entities", tool_args=args),
        _llm_context(assistant),
    )


async def test_fuzzy_name_resolves_decisively(hass: HomeAssistant) -> None:
    """An approximate name resolves to the one canonical entity, with a score."""
    living = ar.async_get(hass).async_create("Living Room").id
    target = _register(hass, "light.lr", "Reading Lamp", area_id=living)
    _register(hass, "fan.ceiling", "Ceiling Fan")

    result = await _call(hass, {"name": "reading light"})

    assert result["success"] is True
    assert "ambiguous" not in result
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["entity_id"] == target
    assert row["name"] == "Reading Lamp"
    assert row["area"] == "Living Room"
    assert row["domain"] == "light"
    assert row["state"] == "on"
    assert isinstance(row["score"], float)


async def test_area_completes_the_name(hass: HomeAssistant) -> None:
    """The area supplies the discriminating token the name lacks ("kitchen light").

    The entity is named only "Ceiling Light" but lives in the Kitchen; a name-only
    scorer would rank the Kitchen Counter switch alongside it, so this proves the area
    is part of the scored document (find-entities.md area matching).
    """
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    target = _register(hass, "light.k_ceiling", "Ceiling Light", area_id=kitchen)
    _register(hass, "switch.k_counter", "Kitchen Counter", area_id=kitchen)
    _register(
        hass,
        "light.b_ceiling",
        "Ceiling Light",
        area_id=ar.async_get(hass).async_create("Bedroom").id,
    )

    result = await _call(hass, {"name": "kitchen light"})

    assert result["success"] is True
    assert "ambiguous" not in result
    assert [row["entity_id"] for row in result["results"]] == [target]


async def test_close_names_return_ambiguous_shortlist(hass: HomeAssistant) -> None:
    """Two entities matching the query equally well come back flagged ambiguous."""
    _register(hass, "light.lamp", "Bedroom Lamp")
    _register(hass, "light.ceiling", "Bedroom Light")

    result = await _call(hass, {"name": "bedroom"})

    assert result["success"] is True
    assert result["ambiguous"] is True
    ids = {row["entity_id"] for row in result["results"]}
    assert ids == {"light.lamp", "light.ceiling"}


async def test_unmatched_name_returns_empty(hass: HomeAssistant) -> None:
    """A name that clears nothing above the floor is an empty (not failed) result."""
    _register(hass, "light.kitchen", "Kitchen Light")

    result = await _call(hass, {"name": "xylophone"})

    assert result == {"success": True, "results": []}


async def test_structured_only_lists_without_scores(hass: HomeAssistant) -> None:
    """No name given: structured filters return the matched set, scoreless."""
    kitchen = ar.async_get(hass).async_create("Kitchen").id
    _register(hass, "light.k1", "Counter Light", area_id=kitchen)
    _register(hass, "light.k2", "Sink Light", area_id=kitchen)
    _register(hass, "switch.kettle", "Kettle", area_id=kitchen)
    _register(hass, "light.elsewhere", "Hall Light")

    result = await _call(hass, {"area": "Kitchen", "domain": "light"})

    assert result["success"] is True
    ids = {row["entity_id"] for row in result["results"]}
    assert ids == {"light.k1", "light.k2"}
    assert all("score" not in row for row in result["results"])


async def test_domain_accepts_a_list(hass: HomeAssistant) -> None:
    """The domain filter accepts a list, unioning the domains."""
    _register(hass, "light.a", "Alpha")
    _register(hass, "switch.b", "Bravo")
    _register(hass, "sensor.c", "Charlie")

    result = await _call(hass, {"domain": ["light", "switch"]})

    ids = {row["entity_id"] for row in result["results"]}
    assert ids == {"light.a", "switch.b"}


async def test_device_class_and_floor_reported(hass: HomeAssistant) -> None:
    """Device-class filtering works and the result carries the resolved floor."""
    floor = fr.async_get(hass).async_create("Upstairs")
    bedroom = ar.async_get(hass).async_create("Bedroom", floor_id=floor.floor_id).id
    _register(
        hass, "cover.blind", "Bedroom Blind", area_id=bedroom, device_class="blind"
    )
    _register(
        hass, "cover.shade", "Bedroom Shade", area_id=bedroom, device_class="shade"
    )

    result = await _call(hass, {"domain": "cover", "device_class": "blind"})

    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["entity_id"] == "cover.blind"
    assert row["floor"] == "Upstairs"


async def test_invalid_area_is_a_surfaced_error(hass: HomeAssistant) -> None:
    """Naming an area that does not exist is a fixable error, not an empty list."""
    _register(hass, "light.a", "Alpha")

    result = await _call(hass, {"area": "Nowhere"})

    assert result["success"] is False
    assert "Nowhere" in result["error"]
    assert "area" in result["error"]


async def test_valid_filter_no_matches_is_empty(hass: HomeAssistant) -> None:
    """A valid area with no matching entities is an empty result, not an error."""
    ar.async_get(hass).async_create("Empty Room")
    _register(hass, "light.a", "Alpha")

    result = await _call(hass, {"area": "Empty Room"})

    assert result == {"success": True, "results": []}


async def test_limit_caps_results(hass: HomeAssistant) -> None:
    """The limit bounds the number of candidates returned."""
    for i in range(4):
        _register(hass, f"light.l{i}", f"Lamp {i}")

    result = await _call(hass, {"domain": "light", "limit": 2})

    assert len(result["results"]) == 2


async def test_no_assistant_configured(hass: HomeAssistant) -> None:
    """Without an assistant in context the tool declines rather than leaking state."""
    result = await _call(hass, {"name": "anything"}, assistant=None)

    assert result == {"success": False, "error": "No assistant configured"}


async def test_device_area_fallback(hass: HomeAssistant) -> None:
    """An entity with no area inherits its device's area in the result."""
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    office = ar.async_get(hass).async_create("Office").id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "dev1")}
    )
    dr.async_get(hass).async_update_device(device.id, area_id=office)
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light", "test", "office_lamp", original_name="Desk Lamp", device_id=device.id
    )
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: "Desk Lamp"})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, True)

    result = await _call(hass, {"name": "desk lamp"})

    assert result["results"][0]["area"] == "Office"


def test_async_get_tools_returns_find_entities(hass: HomeAssistant) -> None:
    """The capability exposes find_entities in the core llm.py platform shape."""
    tools = async_get_tools(hass, _llm_context())

    assert [tool.name for tool in tools] == ["find_entities"]


def test_find_entities_declares_read_only_effect() -> None:
    """The owned lookup tool cannot shadow the latest mutation as unknown."""
    resolved = DEFAULT_TOOL_POLICY_REGISTRY.resolve(FindEntitiesTool())
    session_state = MagicMicSessionState()
    context = ToolPolicyContext(
        principal=UNIDENTIFIED_PRINCIPAL,
        session_state=session_state,
        turn_metadata=session_state.async_begin_turn("turn-1"),
    )

    decision = evaluate_invocation(resolved, {}, context)

    assert resolved.source is PolicySource.DECLARED
    assert decision.effect is EffectClass.READ_ONLY


async def test_testbed_roster_includes_find_entities(hass: HomeAssistant) -> None:
    """The skeleton API adds find_entities; the stock Assist API does not carry it."""
    _register(
        hass, "light.a", "Alpha", area_id=ar.async_get(hass).async_create("Den").id
    )

    skeleton = await SkeletonAssistAPI(hass).async_get_api_instance(_llm_context())
    stock = await llm.AssistAPI(hass).async_get_api_instance(_llm_context())

    assert "find_entities" in {tool.name for tool in skeleton.tools}
    assert "find_entities" not in {tool.name for tool in stock.tools}
