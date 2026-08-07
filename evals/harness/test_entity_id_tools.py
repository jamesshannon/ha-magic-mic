"""Tests for the entity_id-argument corpus and its argument-source classifier.

Model-free. They pin the two things the live run's conclusion rests on: the corpus is
actually shaped to force an `entity_id` (its object ids are not guessable from friendly
names, and its scripts declare entity selectors that reach the model as real tools), and
`classify_source` tells apart the three ways a model can fill that argument.
"""

import pytest

from custom_components.magic_mic.capabilities.localization import (
    ConversationStrings,
    async_get_conversation_strings,
)
from custom_components.magic_mic.identity import UNIDENTIFIED_PRINCIPAL
from custom_components.magic_mic.session_state import MagicMicSessionState, TurnMetadata
from custom_components.magic_mic.testbed import api as testbed_api
from custom_components.magic_mic.tool_policy import ToolPolicyContext
from evals.harness.corpus import CorpusError, Script, load_corpus, parse_world
from evals.harness.entity_id_tools import (
    ENTITY_ID_CORPUS,
    ArgumentSource,
    classify_source,
)
from evals.harness.scoring import Bucket, CaseResult, ObservedTurn, ToolCall
from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm, selector
from homeassistant.setup import async_setup_component

from .backing import async_setup_scripts, build_executable_world
from .test_corpus import _case  # reuse the minimal case builder

SCRIPT_TOOLS = frozenset({"evening_dim"})
LIVE_IDS = frozenset({"light.hue_00a3", "light.hue_00b7"})


async def _conversation_strings(hass: HomeAssistant) -> ConversationStrings:
    """Load Magic Mic's model-facing strings through HA's loader."""
    return await async_get_conversation_strings(hass, "en")


def _result(tools: tuple[ToolCall, ...]) -> CaseResult:
    """Wrap observed tool calls in a scored case result the classifier can read."""
    return CaseResult(
        case=_case(),
        observed=ObservedTurn(speech="ok", tools=tools),
        bucket=Bucket.LLM_CORRECT,
        correct=True,
    )


def test_live_entity_id_is_recognized() -> None:
    """The model produced a real target on its own; resolution had nothing to do."""
    result = _result((ToolCall(name="evening_dim", args={"light": "light.hue_00a3"}),))

    source, supplied = classify_source(result, SCRIPT_TOOLS, LIVE_IDS)

    assert source == ArgumentSource.LIVE_ID
    assert supplied == ("light.hue_00a3",)


def test_invented_entity_id_is_recognized() -> None:
    """CD1's bug in the act: id-shaped, no such entity, so stock HA targets nothing."""
    result = _result(
        (ToolCall(name="evening_dim", args={"light": "light.reading_lamp"}),)
    )

    source, supplied = classify_source(result, SCRIPT_TOOLS, LIVE_IDS)

    assert source == ArgumentSource.INVENTED_ID
    assert supplied == ("light.reading_lamp",)


def test_spoken_name_is_recognized() -> None:
    """The model never tried to produce an id, so only resolution can land the call."""
    result = _result((ToolCall(name="evening_dim", args={"light": "Reading Lamp"}),))

    source, _supplied = classify_source(result, SCRIPT_TOOLS, LIVE_IDS)

    assert source == ArgumentSource.NAME


def test_no_script_call_is_recognized() -> None:
    """A turn that only looked things up never filled the argument at all."""
    result = _result((ToolCall(name="find_entities", args={"name": "reading lamp"}),))

    source, supplied = classify_source(result, SCRIPT_TOOLS, LIVE_IDS)

    assert source == ArgumentSource.ABSENT
    assert supplied == ()


def test_a_call_is_judged_by_its_weakest_argument() -> None:
    """One unusable value means the call could not have done what it claimed."""
    result = _result(
        (
            ToolCall(
                name="evening_dim",
                args={"light": ["light.hue_00a3", "light.does_not_exist"]},
            ),
        )
    )

    source, supplied = classify_source(result, SCRIPT_TOOLS, LIVE_IDS)

    assert source == ArgumentSource.INVENTED_ID
    assert len(supplied) == 2


# The corpus itself.


def test_corpus_loads_with_its_scripts() -> None:
    """The shipped corpus parses, and every case targets one of its scripts."""
    corpus = load_corpus(ENTITY_ID_CORPUS)

    assert corpus.cases
    tool_names = {script.tool_name for script in corpus.world.scripts}
    assert tool_names
    for case in corpus.cases:
        permitted = {tool.name for tool in case.permitted_tools}
        assert permitted & tool_names, f"{case.id} permits no script tool"


def test_every_script_field_is_an_entity_selector() -> None:
    """The corpus only measures what it claims if the arguments are entity_id-typed."""
    corpus = load_corpus(ENTITY_ID_CORPUS)

    for script in corpus.world.scripts:
        assert script.fields, f"{script.object_id} declares no fields"
        for name, field in script.fields.items():
            assert "entity" in field.selector, (
                f"{script.object_id}.{name} is not entity"
            )


def test_no_object_id_is_guessable_from_its_name() -> None:
    """The load-bearing property: the model cannot derive the id from the prompt.

    A corpus where `light.reading_lamp` is named "Reading Lamp" would let every arm pass by
    guessing, measuring nothing. This is also the shape the upstream report described.
    """
    corpus = load_corpus(ENTITY_ID_CORPUS)

    for entity in corpus.world.entities:
        _, _, object_id = entity.entity_id.partition(".")
        slugified = entity.name.lower().replace(" ", "_")
        assert object_id != slugified, f"{entity.entity_id} is guessable from its name"


def test_no_script_description_names_a_fixture_entity() -> None:
    """A script that names its own target would leak the answer into the tool schema."""
    corpus = load_corpus(ENTITY_ID_CORPUS)

    names = [entity.name.lower() for entity in corpus.world.entities]
    for script in corpus.world.scripts:
        text = f"{script.name} {script.description}".lower()
        for name in names:
            assert name not in text, f"{script.object_id} names {name!r}"


def test_a_script_without_a_sequence_is_rejected() -> None:
    """A script that cannot move the world could never be scored by state."""
    with pytest.raises(CorpusError, match="sequence"):
        parse_world(
            {
                "scripts": [
                    {"object_id": "noop", "name": "No Op", "description": "nothing"}
                ]
            }
        )


def test_a_field_without_a_selector_is_rejected() -> None:
    """An untyped field would not exercise the serializer the corpus is measuring."""
    with pytest.raises(CorpusError, match="selector"):
        parse_world(
            {
                "scripts": [
                    {
                        "object_id": "dim",
                        "name": "Dim",
                        "description": "dim it",
                        "sequence": [{"action": "light.turn_off"}],
                        "fields": {"light": {"description": "the light"}},
                    }
                ]
            }
        )


def test_a_digit_leading_object_id_takes_has_tool_name() -> None:
    """HA prefixes a digit-leading script name, and the corpus must agree on the tool id."""
    script = Script(
        object_id="2nd_pass",
        name="Second Pass",
        description="x",
        sequence=({"action": "light.turn_off"},),
    )

    assert script.tool_name == "_2nd_pass"


async def test_corpus_scripts_reach_the_model_as_entity_typed_tools(
    hass: HomeAssistant,
) -> None:
    """End to end through HA: each script becomes a tool whose field is an EntitySelector.

    This is what makes the corpus an `entity_id`-only fixture rather than a claim about one.
    If HA ever resolves these arguments itself (core-deltas CD1 fixed upstream), the tool is
    still here but Consumer 3 becomes redundant, and `test_core_contracts.py` is where that
    shows up first.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})
    corpus = load_corpus(ENTITY_ID_CORPUS)

    await async_setup_scripts(hass, corpus.world)

    api = await llm.async_get_api(
        hass,
        llm.LLM_API_ASSIST,
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="en",
            assistant=conversation.DOMAIN,
            device_id=None,
        ),
    )
    exposed = {tool.name: tool for tool in api.tools}
    for script in corpus.world.scripts:
        assert script.tool_name in exposed, f"{script.tool_name} not exposed"
        schema = exposed[script.tool_name].parameters.schema
        assert any(
            isinstance(validator, selector.EntitySelector)
            for validator in schema.values()
        ), f"{script.tool_name} exposes no entity-typed argument"


@pytest.mark.parametrize(
    ("entity_arguments", "expected_state"),
    [(False, "on"), (True, "off")],
    ids=["resolution_off_targets_nothing", "resolution_on_lands_the_call"],
)
async def test_the_two_arms_differ_on_this_fixture(
    hass: HomeAssistant,
    entity_arguments: bool,
    expected_state: str,
) -> None:
    """The arms the live run pairs produce different worlds, proven without a model.

    Feeds the script tool the id a model plausibly invents (`light.reading_lamp`, from the
    friendly name "Reading Lamp") against the real corpus fixture, where the entity is
    actually `light.hue_00a3`. Off, the service call targets nothing and the lamp stays on,
    which is stock Home Assistant. On, Consumer 3 de-slugs the id and the lamp goes off.

    If this ever stops differing, the live run has nothing to measure and the corpus or the
    feature has changed underneath it.
    """
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})
    strings = await _conversation_strings(hass)
    corpus = load_corpus(ENTITY_ID_CORPUS)
    world = await build_executable_world(hass, corpus.world)
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.hue_00a3"}, blocking=True
    )
    assert hass.states.get("light.hue_00a3").state == "on"

    inner = await llm.async_get_api(
        hass,
        llm.LLM_API_ASSIST,
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="en",
            assistant=conversation.DOMAIN,
            device_id=None,
        ),
    )
    api = testbed_api.TestbedAPI.wrap(
        inner,
        ToolPolicyContext(
            principal=UNIDENTIFIED_PRINCIPAL,
            session_state=MagicMicSessionState(),
            turn_metadata=TurnMetadata(turn_id="turn"),
        ),
        entity_arguments=entity_arguments,
        strings=strings,
    )

    await api.async_call_tool(
        llm.ToolInput(
            tool_name="evening_dim", tool_args={"light": "light.reading_lamp"}
        )
    )
    await hass.async_block_till_done()

    assert hass.states.get(world.resolved["light.hue_00a3"]).state == expected_state


def test_corpus_path_is_where_the_driver_expects() -> None:
    """The driver's constant and the shipped file agree."""
    assert ENTITY_ID_CORPUS.parent.name == "corpus"
    assert ENTITY_ID_CORPUS.exists()
