"""Tests for the prompt interposition (prompt-context Tier 1 and Tier 2 wiring)."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.testbed.prompt import (
    EntitySummaryAssistAPI,
    PreparedLLMAPI,
    async_prepare_llm_api,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    llm,
)
from homeassistant.setup import async_setup_component

from .streaming import create_content_block

ASSISTANT = conversation.DOMAIN
ENTITY_SUMMARY_HEADER = "Each line lists an area and the number of devices"
NAME_INJECTION_HEADER = "Each line lists one request-relevant entity name and entity_id"
AREA_USAGE_INSTRUCTION = "Pass an area or floor to a device tool only when the user"


class ExtraAPI(llm.API):
    """A second registered capability bundle for merge-composition tests."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the fixture API."""
        super().__init__(hass=hass, id="extra", name="Extra")

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return a distinct prompt with no tools."""
        return llm.APIInstance(
            api=self,
            api_prompt="Extra API prompt",
            llm_context=llm_context,
            tools=[],
        )


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


async def test_api_prompt_carries_summary_not_roster(hass: HomeAssistant) -> None:
    """The summary API emits bounded counts, never the Static Context dump."""
    _expose_light(hass)

    instance = await EntitySummaryAssistAPI(hass).async_get_api_instance(_llm_context())

    assert ENTITY_SUMMARY_HEADER in instance.api_prompt
    assert 'area="Living Room"; domain="light"; count=1' in instance.api_prompt
    # The roster marker and the specific device name must be gone.
    assert "Static Context" not in instance.api_prompt
    assert "Reading Lamp" not in instance.api_prompt
    # The area-usage instruction rides with the summary, so the model does not echo its own
    # room onto a named request (find-entities.md "Room is a preference").
    assert AREA_USAGE_INSTRUCTION in instance.api_prompt


async def test_no_entities_prompt_preserved(hass: HomeAssistant) -> None:
    """With nothing exposed, the inherited no-entities prompt still applies."""
    instance = await EntitySummaryAssistAPI(hass).async_get_api_instance(_llm_context())

    assert ENTITY_SUMMARY_HEADER not in instance.api_prompt
    assert AREA_USAGE_INSTRUCTION not in instance.api_prompt
    assert instance.api_prompt == llm.NO_ENTITIES_PROMPT


def test_prepare_llm_api_substitutes_only_assist(hass: HomeAssistant) -> None:
    """The Assist contribution is summarized while unrelated selections pass through."""
    direct = async_prepare_llm_api(hass, llm.LLM_API_ASSIST, entity_summary=True)
    listed = async_prepare_llm_api(hass, [llm.LLM_API_ASSIST], entity_summary=True)
    other = async_prepare_llm_api(hass, "some_other_api", entity_summary=True)

    assert isinstance(direct.api, EntitySummaryAssistAPI)
    assert direct.entity_summary_applied
    assert isinstance(listed.api, EntitySummaryAssistAPI)
    assert listed.entity_summary_applied
    assert other == PreparedLLMAPI("some_other_api", entity_summary_applied=False)


async def test_prepare_llm_api_summarizes_assist_inside_registered_list(
    hass: HomeAssistant,
) -> None:
    """Assist is transformed before merging and another API remains unchanged."""
    _expose_light(hass)
    remove_extra = llm.async_register_api(hass, ExtraAPI(hass))
    try:
        prepared = async_prepare_llm_api(
            hass,
            [llm.LLM_API_ASSIST, "extra"],
            entity_summary=True,
        )
        assert isinstance(prepared.api, llm.MergedAPI)
        instance = await prepared.api.async_get_api_instance(_llm_context())
    finally:
        remove_extra()

    assert prepared.entity_summary_applied
    assert ENTITY_SUMMARY_HEADER in instance.api_prompt
    assert "Static Context" not in instance.api_prompt
    assert "Extra API prompt" in instance.api_prompt


def test_prepare_llm_api_respects_disabled_summary(hass: HomeAssistant) -> None:
    """A disabled request preserves Assist and reports no applied transformation."""
    prepared = async_prepare_llm_api(hass, llm.LLM_API_ASSIST, entity_summary=False)

    assert prepared == PreparedLLMAPI(llm.LLM_API_ASSIST, entity_summary_applied=False)


async def test_driven_testbed_turn_sends_summary_baseline_sends_roster(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """End to end: the testbed's system prompt swaps the roster for the summary.

    Drives a real turn through each agent (full `_async_handle_message`) with a light
    exposed, and inspects the system prompt actually handed to the model. The testbed
    carries the summary and drops the roster; the baseline is unchanged.
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

    assert ENTITY_SUMMARY_HEADER in testbed_system
    assert 'area="Living Room"; domain="light"; count=1' in testbed_system
    assert "Static Context" not in testbed_system
    assert "Reading Lamp" not in testbed_system

    assert "Static Context" in baseline_system
    assert "Reading Lamp" in baseline_system
    assert ENTITY_SUMMARY_HEADER not in baseline_system


def _expose_named_light(
    hass: HomeAssistant, area: ar.AreaEntry, object_id: str, name: str
) -> str:
    """Create, place in ``area``, state, and expose one named light; return its id."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        "light",
        "test",
        object_id,
        suggested_object_id=object_id,
        original_name=name,
    )
    ent_reg.async_update_entity(entry.entity_id, area_id=area.id)
    hass.states.async_set(entry.entity_id, "on", {ATTR_FRIENDLY_NAME: name})
    async_expose_entity(hass, ASSISTANT, entry.entity_id, True)
    return entry.entity_id


def _register_satellite(hass: HomeAssistant, area: ar.AreaEntry) -> str:
    """Stand up a voice-satellite device in ``area`` and return its device_id.

    Backed by its own throwaway config entry, as a real satellite integration is; the
    device holds the area so the proxy resolves "here" from the turn's device_id.
    """
    entry = MockConfigEntry(domain="satellite")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("satellite", "voice_sat")},
        name="Voice Satellite",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    return device.id


def _testbed_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the testbed proxy agent's entity id."""
    return next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.platform == "magic_mic" and e.unique_id == f"{entry.entry_id}_testbed"
    )


async def _two_room_home(hass: HomeAssistant) -> str:
    """Expose a light in each of two rooms; return the living-room satellite device_id."""
    area_reg = ar.async_get(hass)
    living = area_reg.async_create("Living Room")
    kitchen = area_reg.async_create("Kitchen")
    _expose_named_light(hass, living, "lr", "Reading Lamp")
    _expose_named_light(hass, kitchen, "kc", "Kitchen Ceiling")
    return _register_satellite(hass, living)


async def test_driven_turn_injects_relevant_in_room_name(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A named in-room device is injected as a fast-path name; other rooms are not.

    Drives a real turn from a living-room satellite whose utterance names the reading
    lamp. The Tier-2 block carries that entity's typed name/id record; the kitchen
    light, neither named nor in the room, stays out.
    """
    device_id = await _two_room_home(hass)
    testbed_id = _testbed_id(hass, setup_integration)

    mock_create_stream.return_value = [create_content_block(0, ["Done."])]
    await conversation.async_converse(
        hass,
        "turn on the reading lamp",
        None,
        Context(),
        device_id=device_id,
        agent_id=testbed_id,
    )
    system = _system_text(mock_create_stream)

    assert NAME_INJECTION_HEADER in system
    assert 'name="Reading Lamp"; entity_id="light.lr"' in system
    assert "light.kc" not in system


async def test_driven_turn_does_not_inject_names_when_summary_was_not_applied(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Tier-2 names follow the effective summary strategy, not its requested flag."""
    device_id = await _two_room_home(hass)
    testbed_id = _testbed_id(hass, setup_integration)
    mock_create_stream.return_value = [create_content_block(0, ["Done."])]

    with patch(
        "custom_components.magic_mic.testbed.entity.async_prepare_llm_api",
        return_value=PreparedLLMAPI(llm.LLM_API_ASSIST, entity_summary_applied=False),
    ):
        await conversation.async_converse(
            hass,
            "turn on the reading lamp",
            None,
            Context(),
            device_id=device_id,
            agent_id=testbed_id,
        )
    system = _system_text(mock_create_stream)

    assert "Static Context" in system
    assert NAME_INJECTION_HEADER not in system


async def test_driven_turn_omits_names_when_nothing_relevant(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """An utterance that names no device injects no Tier-2 block (a lookup would follow)."""
    device_id = await _two_room_home(hass)
    testbed_id = _testbed_id(hass, setup_integration)

    mock_create_stream.return_value = [create_content_block(0, ["You're welcome."])]
    await conversation.async_converse(
        hass,
        "thank you very much",
        None,
        Context(),
        device_id=device_id,
        agent_id=testbed_id,
    )
    system = _system_text(mock_create_stream)

    assert NAME_INJECTION_HEADER not in system
    # The entity summary (Tier 1) is unaffected by the empty Tier-2 result.
    assert ENTITY_SUMMARY_HEADER in system


async def test_driven_turn_gate_off_omits_names(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """With name injection disabled, even a matching utterance injects no names."""
    device_id = await _two_room_home(hass)
    testbed_id = _testbed_id(hass, setup_integration)

    mock_create_stream.return_value = [create_content_block(0, ["Done."])]
    with patch(
        "custom_components.magic_mic.testbed.entity.DEFAULT_NAME_INJECTION", False
    ):
        await conversation.async_converse(
            hass,
            "turn on the reading lamp",
            None,
            Context(),
            device_id=device_id,
            agent_id=testbed_id,
        )
    system = _system_text(mock_create_stream)

    assert NAME_INJECTION_HEADER not in system
    assert 'name="Reading Lamp"; entity_id="light.lr"' not in system
    # The entity summary (Tier 1) still applies; only the Tier-2 block is gated off.
    assert ENTITY_SUMMARY_HEADER in system
