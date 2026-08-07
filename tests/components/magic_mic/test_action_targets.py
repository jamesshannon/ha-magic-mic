"""Tests for Consumer 3: entity_id arguments on tools that take ids.

`docs/find-entities.md` "Consumer 3", `docs/core-deltas.md` CD1. Keyless and model-free:
they build tools whose schema declares an `EntitySelector`, hand them the strings a model
actually produces, and assert which rung of the ladder resolves them. The end-to-end wiring
through the proxy is covered at the bottom, against a real `ActionTool` and a real service.
"""

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol
from voluptuous_openapi import convert

from custom_components.magic_mic.capabilities.action_targets import (
    annotate_entity_arguments,
    resolve_entity_arguments,
)
from custom_components.magic_mic.capabilities.localization import ConversationStrings
from custom_components.magic_mic.identity import UNIDENTIFIED_PRINCIPAL
from custom_components.magic_mic.session_state import MagicMicSessionState, TurnMetadata

# Imported as a module (not `from ... import TestbedAPI`) so the `Test`-prefixed
# class name doesn't trip pytest's test-class collection heuristic.
from custom_components.magic_mic.testbed import api as testbed_api
from custom_components.magic_mic.tool_policy import ToolPolicyContext
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    llm,
    selector,
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
    aliases: set[str] | None = None,
    area_id: str | None = None,
    device_class: str | None = None,
    expose: bool = True,
) -> str:
    """Register a named entity, place it, state it, and expose it."""
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        domain, "test", object_id, suggested_object_id=object_id, original_name=name
    )
    if aliases:
        ent_reg.async_update_entity(entry.entity_id, aliases=aliases)
    if area_id is not None:
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
    attributes: dict[str, Any] = {ATTR_FRIENDLY_NAME: name}
    if device_class is not None:
        attributes[ATTR_DEVICE_CLASS] = device_class
    hass.states.async_set(entry.entity_id, "on", attributes)
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)
    return entry.entity_id


def _tool(schema: dict[vol.Marker, Any], name: str = "script__targeted") -> llm.Tool:
    """Build a bare tool whose parameters carry the given selectors."""

    class _SchemaTool(llm.Tool):
        """A tool that exists only to carry a parameter schema."""

        def __init__(self) -> None:
            self.name = name
            self.description = "test"
            self.parameters = vol.Schema(schema)

        async def async_call(
            self,
            hass: HomeAssistant,
            tool_input: llm.ToolInput,
            llm_context: llm.LLMContext,
        ) -> dict[str, Any]:
            """Never called; resolution happens before execution."""
            raise AssertionError("not executed")

    return _SchemaTool()


def _context(
    hass: HomeAssistant, device_id: str | None = None, assistant: str | None = ASSISTANT
) -> llm.LLMContext:
    """Build an LLM context, optionally placed at a satellite."""
    return llm.LLMContext(
        platform="magic_mic",
        context=None,
        language="en",
        assistant=assistant,
        device_id=device_id,
    )


def _satellite_in(hass: HomeAssistant, area_id: str) -> str:
    """Register a device placed in the given area and return its id (the requesting room)."""
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", f"sat-{area_id}")},
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area_id)
    return device.id


def _entity_field() -> dict[vol.Marker, Any]:
    """The common single-entity schema."""
    return {vol.Required("target"): selector.EntitySelector()}


# Rung 1: a live entity_id is passed through untouched.


async def test_live_entity_id_is_left_alone(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """The backward-compatibility guarantee: a working call is not rewritten."""
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")

    assert (
        resolve_entity_arguments(
            hass,
            _context(hass),
            _tool(_entity_field()),
            {"target": entity_id},
            conversation_strings,
        )
        is None
    )


async def test_tool_without_entity_fields_is_left_alone(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A tool declaring no entity selector is never touched."""
    _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    tool = _tool({vol.Required("message"): str})

    assert (
        resolve_entity_arguments(
            hass, _context(hass), tool, {"message": "Office Lamp"}, conversation_strings
        )
        is None
    )


async def test_no_assistant_leaves_the_call_unchanged(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Without an assistant there is no exposure scope, so nothing resolves."""
    _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")

    assert (
        resolve_entity_arguments(
            hass,
            _context(hass, assistant=None),
            _tool(_entity_field()),
            {"target": "Office Lamp"},
            conversation_strings,
        )
        is None
    )


# Rung 2: an exact friendly name or alias.


async def test_friendly_name_resolves(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """The model passed a name where an id was asked for."""
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "Office Lamp"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args == {"target": entity_id}


async def test_alias_resolves(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Aliases are matched because the rung reuses HA's own matcher."""
    entity_id = _register(
        hass, "light.office_lamp_a1b2c3", "Office Lamp", aliases={"Desk Light"}
    )

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "Desk Light"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args == {"target": entity_id}


# Rung 3: the reported bug, an id the model slugified from a name.


async def test_slugified_id_resolves(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """`light.office_lamp` for an entity whose real id carries a suffix (core-deltas CD1).

    This is the case from the upstream issue: the object id is not a spaces-to-underscores
    translation of the friendly name, so the model's guess does not exist.
    """
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "light.office_lamp"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args == {"target": entity_id}


async def test_slugified_id_stays_in_its_domain(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """De-slugging honors the domain the model already committed to."""
    _register(hass, "switch.office_lamp_a1b2c3", "Office Lamp")

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "light.office_lamp"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    assert resolution.tool_result is not None
    assert resolution.tool_result["error"] == "ambiguous_entity_argument"


# Failure: fuzzy suggests, it never resolves.


async def test_unmatched_name_returns_candidates_without_executing(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A near miss is offered for the model to choose, not resolved on its behalf."""
    entity_id = _register(hass, "light.reading_lamp", "Reading Lamp")

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "reading light"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    result = resolution.tool_result
    assert result is not None
    assert result["success"] is False
    assert result["error"] == "ambiguous_entity_argument"
    assert result["argument"] == "target"
    assert [row["entity_id"] for row in result["candidates"]] == [entity_id]


async def test_nothing_close_reports_not_found(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """With no candidate above the floor the model is told to look it up."""
    _register(hass, "light.reading_lamp", "Reading Lamp")

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "garage door opener"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    result = resolution.tool_result
    assert result is not None
    assert result["error"] == "unresolved_entity_argument"
    assert "candidates" not in result


async def test_unexposed_entity_is_never_resolved(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Exposure scoping means this cannot become a path to a hidden entity."""
    _register(hass, "light.office_lamp_a1b2c3", "Office Lamp", expose=False)

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "Office Lamp"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    assert resolution.tool_result is not None


# Duplicate names, the requesting room, and the selector's own config.


async def test_duplicate_name_breaks_toward_the_requesting_room(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Core's own preference settles a duplicate name, exactly as it does for intents."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")
    office = area_reg.async_create("Office")
    _register(hass, "light.ceiling_kitchen", "Ceiling Light", area_id=kitchen.id)
    in_office = _register(
        hass, "light.ceiling_office", "Ceiling Light", area_id=office.id
    )

    resolution = resolve_entity_arguments(
        hass,
        _context(hass, device_id=_satellite_in(hass, office.id)),
        _tool(_entity_field()),
        {"target": "Ceiling Light"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args == {"target": in_office}


async def test_unsettled_duplicate_name_asks(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """With no room to prefer, a duplicate name is a question, not a coin flip."""
    area_reg = ar.async_get(hass)
    kitchen = area_reg.async_create("Kitchen")
    office = area_reg.async_create("Office")
    _register(hass, "light.ceiling_kitchen", "Ceiling Light", area_id=kitchen.id)
    _register(hass, "light.ceiling_office", "Ceiling Light", area_id=office.id)

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        _tool(_entity_field()),
        {"target": "Ceiling Light"},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    assert resolution.tool_result is not None
    assert len(resolution.tool_result["candidates"]) == 2


async def test_selector_domain_filter_is_honored(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """The author narrowed the field, so resolution may not step outside it."""
    _register(hass, "switch.porch_a1b2c3", "Porch")
    tool = _tool(
        {
            vol.Required("target"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="light")
            )
        }
    )

    resolution = resolve_entity_arguments(
        hass, _context(hass), tool, {"target": "Porch"}, conversation_strings
    )

    assert resolution is not None
    assert resolution.tool_args is None


async def test_selector_exclude_list_is_honored(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """`exclude_entities` names an entity outright, so the match is dropped."""
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    tool = _tool(
        {
            vol.Required("target"): selector.EntitySelector(
                selector.EntitySelectorConfig(exclude_entities=[entity_id])
            )
        }
    )

    resolution = resolve_entity_arguments(
        hass, _context(hass), tool, {"target": "Office Lamp"}, conversation_strings
    )

    assert resolution is not None
    assert resolution.tool_args is None


async def test_device_class_filter_is_honored(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A device-class-scoped field resolves within its class."""
    blind = _register(
        hass, "cover.kitchen_blind", "Kitchen Cover", device_class="blind"
    )
    _register(hass, "cover.garage_door", "Garage Cover", device_class="garage")
    tool = _tool(
        {
            vol.Required("target"): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="cover", device_class="blind")
            )
        }
    )

    resolution = resolve_entity_arguments(
        hass, _context(hass), tool, {"target": "Kitchen Cover"}, conversation_strings
    )

    assert resolution is not None
    assert resolution.tool_args == {"target": blind}


# multiple: true.


async def test_multiple_values_resolve_together(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Each member of a list resolves independently."""
    lamp = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    strip = _register(hass, "light.desk_strip_d4e5", "Desk Strip")
    tool = _tool(
        {
            vol.Required("targets"): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            )
        }
    )

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        tool,
        {"targets": ["Office Lamp", "light.desk_strip"]},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args == {"targets": [lamp, strip]}


async def test_one_bad_member_blocks_the_whole_call(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Acting on half a list is worse than acting on none: the model cannot see which."""
    _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    tool = _tool(
        {
            vol.Required("targets"): selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True)
            )
        }
    )

    resolution = resolve_entity_arguments(
        hass,
        _context(hass),
        tool,
        {"targets": ["Office Lamp", "the thing by the couch"]},
        conversation_strings,
    )

    assert resolution is not None
    assert resolution.tool_args is None
    assert resolution.tool_result is not None


# End to end, through the proxy and a real ActionTool.


def _seed_action_tool(hass: HomeAssistant, schema: dict[vol.Marker, Any]) -> None:
    """Pre-seed core's action-parameter cache so `ActionTool` builds without a real script."""
    hass.data[llm.ACTION_PARAMETERS_CACHE] = {
        "script": {"targeted": (None, vol.Schema(schema))}
    }


async def _wrapped_api(
    hass: HomeAssistant, strings: ConversationStrings
) -> testbed_api.TestbedAPI:
    """Wrap the Assist API with the proxy, carrying an `ActionTool` for `script.targeted`."""
    inner = await llm.async_get_api(hass, llm.LLM_API_ASSIST, _context(hass))
    inner.tools.append(llm.ActionTool(hass, "script", "targeted"))
    return testbed_api.TestbedAPI.wrap(
        inner,
        ToolPolicyContext(
            principal=UNIDENTIFIED_PRINCIPAL,
            session_state=MagicMicSessionState(),
            turn_metadata=TurnMetadata(turn_id="turn"),
        ),
        strings=strings,
    )


def _capture_service(hass: HomeAssistant) -> list[ServiceCall]:
    """Register `script.targeted` and return the list its calls land in."""
    calls: list[ServiceCall] = []

    @callback
    def handle(call: ServiceCall) -> dict[str, Any]:
        calls.append(call)
        return {}

    hass.services.async_register(
        "script", "targeted", handle, supports_response=SupportsResponse.OPTIONAL
    )
    return calls


async def test_proxy_resolves_a_script_argument_end_to_end(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """The reported bug, fixed through the real seam: the service gets the real entity.

    Without Consumer 3 this same call reaches `hass.services.async_call` holding
    `light.office_lamp`, which does not exist, and the script targets nothing.
    """
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    _seed_action_tool(hass, _entity_field())
    calls = _capture_service(hass)
    api = await _wrapped_api(hass, conversation_strings)

    await api.async_call_tool(
        llm.ToolInput(
            tool_name="script__targeted", tool_args={"target": "light.office_lamp"}
        )
    )

    assert len(calls) == 1
    assert calls[0].data["target"] == entity_id


async def test_proxy_returns_candidates_without_calling_the_service(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """An unresolvable argument is a tool_result, and the script does not run."""
    _register(hass, "light.reading_lamp", "Reading Lamp")
    _seed_action_tool(hass, _entity_field())
    calls = _capture_service(hass)
    api = await _wrapped_api(hass, conversation_strings)

    result = await api.async_call_tool(
        llm.ToolInput(
            tool_name="script__targeted", tool_args={"target": "reading light"}
        )
    )

    assert not calls
    assert result["success"] is False
    assert result["error"] == "ambiguous_entity_argument"


async def test_proxy_leaves_a_valid_id_untouched(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """A call that works today is byte-identical through the proxy."""
    entity_id = _register(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    _seed_action_tool(hass, _entity_field())
    calls = _capture_service(hass)
    api = await _wrapped_api(hass, conversation_strings)

    await api.async_call_tool(
        llm.ToolInput(tool_name="script__targeted", tool_args={"target": entity_id})
    )

    assert len(calls) == 1
    assert calls[0].data["target"] == entity_id


# Advertising the resolution: the field description tells the model a name is accepted.


def _described_field() -> dict[vol.Marker, Any]:
    """An entity field carrying the script author's own description."""
    return {
        vol.Required("target", description="The light to switch off."): (
            selector.EntitySelector()
        )
    }


def test_the_hint_extends_the_authors_own_description(
    conversation_strings: ConversationStrings,
) -> None:
    """The author's text survives; the hint is appended to it, not substituted."""
    annotated = annotate_entity_arguments(
        _tool(_described_field()), conversation_strings
    )

    description = next(iter(annotated.parameters.schema)).description
    assert description.startswith("The light to switch off.")
    assert conversation_strings.action_targets_accepts_name in description


def test_a_field_with_no_description_gets_the_hint_alone(
    conversation_strings: ConversationStrings,
) -> None:
    """Nothing to extend, so the hint stands by itself rather than after a stray period."""
    annotated = annotate_entity_arguments(_tool(_entity_field()), conversation_strings)

    key = next(iter(annotated.parameters.schema))
    assert key.description == conversation_strings.action_targets_accepts_name


def test_a_tool_without_entity_fields_is_returned_unchanged(
    conversation_strings: ConversationStrings,
) -> None:
    """No entity argument means no claim to make, so the tool is passed through as-is."""
    tool = _tool({vol.Required("area"): selector.AreaSelector()})

    assert annotate_entity_arguments(tool, conversation_strings) is tool


def test_annotating_does_not_mutate_the_tool_it_wraps(
    conversation_strings: ConversationStrings,
) -> None:
    """The inner tool is still executed, so its schema must come through untouched."""
    tool = _tool(_described_field())

    annotate_entity_arguments(tool, conversation_strings)

    assert next(iter(tool.parameters.schema)).description == "The light to switch off."


def test_the_annotated_field_still_serializes_as_an_entity_id(
    conversation_strings: ConversationStrings,
) -> None:
    """The hint is added to the description; it does not change the declared type.

    A model that already knows an id passes one, so the affordance is additive: the
    schema still says `format: entity_id` and the hint says a name works too.
    """
    annotated = annotate_entity_arguments(
        _tool(_described_field()), conversation_strings
    )

    schema = convert(annotated.parameters, custom_serializer=llm.selector_serializer)

    field = schema["properties"]["target"]
    assert field["format"] == "entity_id"
    assert conversation_strings.action_targets_accepts_name in field["description"]


def test_an_optional_field_stays_optional(
    conversation_strings: ConversationStrings,
) -> None:
    """The marker is copied, so required-ness and any default survive annotation."""
    tool = _tool({vol.Optional("target", default="light.a"): selector.EntitySelector()})

    annotated = annotate_entity_arguments(tool, conversation_strings)

    key = next(iter(annotated.parameters.schema))
    assert isinstance(key, vol.Optional)
    assert key.default() == "light.a"


async def test_the_annotated_tool_delegates_execution(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """Annotation is prompt-side: calling the wrapper runs the tool it wrapped."""
    called: list[llm.ToolInput] = []

    class _RecordingTool(llm.Tool):
        """Records the call it receives."""

        def __init__(self) -> None:
            self.name = "script__targeted"
            self.description = "test"
            self.parameters = vol.Schema(_entity_field())

        async def async_call(
            self,
            hass: HomeAssistant,
            tool_input: llm.ToolInput,
            llm_context: llm.LLMContext,
        ) -> dict[str, Any]:
            """Record and return."""
            called.append(tool_input)
            return {"success": True}

    annotated = annotate_entity_arguments(_RecordingTool(), conversation_strings)
    tool_input = llm.ToolInput(
        tool_name="script__targeted", tool_args={"target": "light.a"}
    )

    result = await annotated.async_call(hass, tool_input, _context(hass))

    assert result == {"success": True}
    assert called == [tool_input]


async def test_the_proxy_advertises_names_on_an_exposed_script(
    hass: HomeAssistant, conversation_strings: ConversationStrings
) -> None:
    """End to end: the roster the model sees carries the hint on the entity field."""
    _seed_action_tool(hass, _described_field())
    api = await _wrapped_api(hass, conversation_strings)

    tool = next(tool for tool in api.tools if tool.name == "script__targeted")

    description = next(iter(tool.parameters.schema)).description
    assert conversation_strings.action_targets_accepts_name in description


@pytest.mark.parametrize(
    ("entity_arguments", "entity_argument_hints"),
    [(False, True), (True, False)],
    ids=["resolution_off", "hints_off"],
)
async def test_the_proxy_promises_nothing_it_will_not_honor(
    hass: HomeAssistant,
    conversation_strings: ConversationStrings,
    entity_arguments: bool,
    entity_argument_hints: bool,
) -> None:
    """With resolution off, the hint must not appear: a name would target nothing."""
    _seed_action_tool(hass, _described_field())
    inner = await llm.async_get_api(hass, llm.LLM_API_ASSIST, _context(hass))
    inner.tools.append(llm.ActionTool(hass, "script", "targeted"))
    api = testbed_api.TestbedAPI.wrap(
        inner,
        ToolPolicyContext(
            principal=UNIDENTIFIED_PRINCIPAL,
            session_state=MagicMicSessionState(),
            turn_metadata=TurnMetadata(turn_id="turn"),
        ),
        entity_argument_hints=entity_argument_hints,
        entity_arguments=entity_arguments,
        strings=conversation_strings,
    )

    tool = next(tool for tool in api.tools if tool.name == "script__targeted")

    description = next(iter(tool.parameters.schema)).description
    assert conversation_strings.action_targets_accepts_name not in (description or "")
