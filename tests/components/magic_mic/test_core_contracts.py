"""Pinned upstream behavior: one test per entry in `docs/core-deltas.md`.

These do not test Magic Mic. They assert that the Home Assistant behavior each entry in
the ledger compensates for still behaves that way, so a core fix surfaces as a failing
test on the next dependency bump instead of leaving a workaround in place forever.

**A failure here is not a bug.** It means core changed. Read the named ledger entry, then
either remove our compensation and retire the entry, or update the entry to whatever core
does now. Do not "fix" the test by loosening the assertion.

Verified against Home Assistant 2026.7.4. Update this line and the ledger together when
the pinned release moves (CLAUDE.md, "Treat a Home Assistant dependency bump as a
coordinated upgrade").
"""

from typing import Any

import pytest
import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.helpers import (
    area_registry as ar,
    entity_registry as er,
    intent,
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


def _register_light(hass: HomeAssistant, entity_id: str, name: str) -> None:
    """Register, name, state, and expose one light."""
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain, "test", object_id, suggested_object_id=object_id, original_name=name
    )
    hass.states.async_set(entity_id, "on", {ATTR_FRIENDLY_NAME: name})
    async_expose_entity(hass, ASSISTANT, entity_id, True)


# CD2: name matching in the LLM path is exact.


async def test_filter_by_name_is_exact(hass: HomeAssistant) -> None:
    """A one-word paraphrase fails to match (core-deltas.md CD2).

    If this starts matching, core grew fuzzy name resolution and
    `capabilities/match_fallback.py` is redundant.
    """
    _register_light(hass, "light.reading_lamp", "Reading Lamp")

    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(name="reading light", assistant=ASSISTANT),
    )

    assert not result.is_match
    assert result.no_match_reason is intent.MatchFailedReason.NAME


async def test_filter_by_name_matches_friendly_name_not_entity_id(
    hass: HomeAssistant,
) -> None:
    """An unguessable object id is irrelevant: matching reads the friendly name (CD2).

    The premise behind CD1's framing. If this fails, the claim that "an unguessable
    entity_id is not by itself a resolution failure" is wrong and both entries need
    rewriting.
    """
    _register_light(hass, "light.office_lamp_a1b2c3", "Office Lamp")

    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(name="Office Lamp", assistant=ASSISTANT),
    )

    assert result.is_match
    assert [state.entity_id for state in result.states] == ["light.office_lamp_a1b2c3"]


async def test_filter_by_name_accepts_entity_id(hass: HomeAssistant) -> None:
    """A literal entity_id short-circuits the name filter (CD2).

    This is the seam every compensation rides: `match_fallback` substitutes a resolved id
    into the retried intent because that id matches by construction. If it stops being
    accepted, Consumer 1's retry breaks.
    """
    _register_light(hass, "light.reading_lamp", "Reading Lamp")

    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(name="light.reading_lamp", assistant=ASSISTANT),
    )

    assert result.is_match
    assert [state.entity_id for state in result.states] == ["light.reading_lamp"]


# CD1: ActionTool resolves area and floor names but not entity names.


def test_entity_selector_serializes_as_entity_id() -> None:
    """Core asks the model for an entity_id it was never given (core-deltas.md CD1).

    The prompt-side half of the asymmetry. If this changes shape, revisit CD1 before
    building or keeping Consumer 3.
    """
    serialized = llm.selector_serializer(selector.EntitySelector())

    assert serialized == {"type": "string", "format": "entity_id"}


def _seed_action_tool(hass: HomeAssistant, schema: dict[vol.Marker, Any]) -> None:
    """Pre-seed core's action-parameter cache so `ActionTool` builds without a real script.

    `_get_cached_action_parameters` returns a cached (description, schema) pair verbatim,
    which is a cheaper way in than registering a script and its service description.
    """
    hass.data[llm.ACTION_PARAMETERS_CACHE] = {
        "script": {"targeted": (None, vol.Schema(schema))}
    }


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


async def test_action_tool_does_not_resolve_entity_names(hass: HomeAssistant) -> None:
    """An `EntitySelector` argument reaches the service unresolved (CD1).

    The model's invented id is passed through verbatim, so the call targets nothing. This
    is the failure the issue reported and the hole Consumer 3 fills. If core starts
    resolving it, Consumer 3 should come out rather than double-resolve.
    """
    _register_light(hass, "light.office_lamp_a1b2c3", "Office Lamp")
    _seed_action_tool(hass, {vol.Required("target"): selector.EntitySelector()})
    calls = _capture_service(hass)

    tool = llm.ActionTool(hass, "script", "targeted")
    await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="script__targeted", tool_args={"target": "light.office_lamp"}
        ),
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="en",
            assistant=ASSISTANT,
            device_id=None,
        ),
    )

    assert len(calls) == 1
    assert calls[0].data["target"] == "light.office_lamp"


async def test_action_tool_resolves_area_names(hass: HomeAssistant) -> None:
    """An `AreaSelector` argument is converted from name to area_id (CD1).

    The other half of the asymmetry, and the precedent Consumer 3 mirrors: core already
    accepts a name here and does the lookup itself.
    """
    kitchen = ar.async_get(hass).async_create("Kitchen")
    _seed_action_tool(hass, {vol.Required("where"): selector.AreaSelector()})
    calls = _capture_service(hass)

    tool = llm.ActionTool(hass, "script", "targeted")
    await tool.async_call(
        hass,
        llm.ToolInput(tool_name="script__targeted", tool_args={"where": "Kitchen"}),
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="en",
            assistant=ASSISTANT,
            device_id=None,
        ),
    )

    assert len(calls) == 1
    assert calls[0].data["where"] == kitchen.id


# CD4: nothing validates tool arguments against the tool schema.


async def test_async_call_tool_does_not_validate_arguments(
    hass: HomeAssistant,
) -> None:
    """A value the tool's own selector would reject still reaches the tool (CD4).

    `EntitySelector.__call__` runs `cv.entity_id_or_uuid` and would raise on a friendly
    name, but `APIInstance.async_call_tool` never calls it. Consumer 3 depends on that
    string surviving long enough to be resolved. If this starts raising, the resolution
    step has to move ahead of validation.
    """
    _seed_action_tool(hass, {vol.Required("target"): selector.EntitySelector()})
    calls = _capture_service(hass)

    # The selector rejects the value on its own, which is the half that is not wired up.
    with pytest.raises(vol.Invalid):
        selector.EntitySelector()("Office Lamp")

    api = await llm.async_get_api(
        hass,
        llm.LLM_API_ASSIST,
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="en",
            assistant=ASSISTANT,
            device_id=None,
        ),
    )
    api.tools.append(llm.ActionTool(hass, "script", "targeted"))
    await api.async_call_tool(
        llm.ToolInput(tool_name="script__targeted", tool_args={"target": "Office Lamp"})
    )

    assert len(calls) == 1
    assert calls[0].data["target"] == "Office Lamp"


# CD3: area resolution in ActionTool raises on no match.


async def test_action_tool_unknown_area_raises_index_error(
    hass: HomeAssistant,
) -> None:
    """A misnamed area raises `IndexError` out of the tool call (core-deltas.md CD3).

    Recorded so Consumer 3 does not copy the pattern: a resolution miss belongs in the
    tool_result the model reads, not in an exception from a registry helper.
    """
    ar.async_get(hass).async_create("Kitchen")
    _seed_action_tool(hass, {vol.Required("where"): selector.AreaSelector()})
    _capture_service(hass)

    tool = llm.ActionTool(hass, "script", "targeted")
    with pytest.raises(IndexError):
        await tool.async_call(
            hass,
            llm.ToolInput(
                tool_name="script__targeted", tool_args={"where": "kitchenette"}
            ),
            llm.LLMContext(
                platform="magic_mic",
                context=None,
                language="en",
                assistant=ASSISTANT,
                device_id=None,
            ),
        )
