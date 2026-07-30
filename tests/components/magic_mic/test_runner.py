"""Runner tests: drive the testbed agent with a mocked stream and score the turn."""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness import Bucket, run_case
from evals.harness.backing import ExecutableWorld, build_executable_world
from evals.harness.corpus import (
    Case,
    Entity,
    Expected,
    ExpectedAnswer,
    ExpectedTool,
    StateChange,
    World,
    load_corpus,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block, create_tool_use_block


def _testbed_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the testbed proxy agent."""
    ent_reg = er.async_get(hass)
    return next(
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.platform == "magic_mic"
        and entity.unique_id == f"{entry.entry_id}_testbed"
    )


def _knowledge_case() -> Case:
    """A text-only case: no tools, answer predicate only."""
    return Case(
        id="k",
        utterance="what's the capital of France?",
        category="knowledge",
        routing_truth="llm",
        resolves_at_wave0=True,
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )


async def test_run_knowledge_case_scores_llm_correct(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A correct text answer is captured, scored correct, and bucketed LLM_CORRECT."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_content_block(0, ["The capital is ", "Paris."])
    ]

    result = await run_case(hass, agent_id, _knowledge_case(), llm=True)

    assert result.bucket is Bucket.LLM_CORRECT
    assert result.correct is True
    assert result.observed.tools == ()
    assert result.observed.generations == 1
    assert "Paris" in result.observed.speech


async def test_run_knowledge_case_wrong_answer_scores_wrong_action(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A resolved turn with the wrong answer buckets as WRONG_ACTION."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [create_content_block(0, ["It is Lyon."])]

    result = await run_case(hass, agent_id, _knowledge_case(), llm=True)

    assert result.correct is False
    assert result.bucket is Bucket.WRONG_ACTION


def _device_case(expected_name: str = "Kitchen Light") -> Case:
    """A device-control case expecting a HassTurnOff on the given entity name."""
    return Case(
        id="d",
        utterance="turn off the kitchen light",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        expected=Expected(
            tools=(ExpectedTool("HassTurnOff", {"name": expected_name}),)
        ),
    )


async def test_run_device_case_captures_tool_and_counts_generations(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A tool_use round is captured from the trace; two rounds count as two generations."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassTurnOff", ['{"name": "Kitchen Light"}']
        ),
        create_content_block(0, ["Done."]),
    ]

    result = await run_case(hass, agent_id, _device_case(), llm=True)

    assert [tool.name for tool in result.observed.tools] == ["HassTurnOff"]
    assert result.observed.tools[0].args == {"name": "Kitchen Light"}
    assert result.observed.generations == 2
    assert result.correct is True
    assert result.bucket is Bucket.LLM_CORRECT


async def test_run_device_case_wrong_tool_scores_wrong_action(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Calling the wrong tool buckets as WRONG_ACTION even though the turn resolves."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassTurnOn", ['{"name": "Kitchen Light"}']
        ),
        create_content_block(0, ["Done."]),
    ]

    result = await run_case(hass, agent_id, _device_case(), llm=True)

    assert [tool.name for tool in result.observed.tools] == ["HassTurnOn"]
    assert result.correct is False
    assert result.bucket is Bucket.WRONG_ACTION


async def _build_cover(hass: HomeAssistant) -> ExecutableWorld:
    """Stand up a single positionable blind, exposed and executable."""
    return await build_executable_world(
        hass,
        World(
            areas=("living_room",),
            entities=(
                Entity(
                    entity_id="cover.blinds",
                    name="Blinds",
                    area="living_room",
                    device_class="blind",
                    state="open",
                ),
            ),
        ),
    )


def _close_blinds_state_case() -> Case:
    """A state-scored case: the blinds must end closed, by whatever tool."""
    return Case(
        id="close-blinds",
        utterance="close the living room blinds",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        expect_changes={"cover.blinds": StateChange(state="closed")},
    )


async def test_state_scored_case_passes_via_equivalent_tool(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Driving position to 0 closes the blind and scores correct, no tool-name match.

    This is the win over tool-name scoring: the model reaches the declared end state
    (`closed`) through `HassSetPosition`, which a tool-scored `close` case would have to
    absorb with `any_of`. State-diff passes it because the world is right.
    """
    await _build_cover(hass)
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassSetPosition", ['{"name": "Blinds", "position": 0}']
        ),
        create_content_block(0, ["Done."]),
    ]

    result = await run_case(hass, agent_id, _close_blinds_state_case(), llm=True)

    assert [tool.name for tool in result.observed.tools] == ["HassSetPosition"]
    assert result.observed.unexpected_changes == {}
    assert result.correct is True
    assert result.bucket is Bucket.LLM_CORRECT
    assert hass.states.get("cover.blinds").state == "closed"


async def test_state_scored_case_fails_when_world_unchanged(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A turn that does not reach the declared end state scores WRONG_ACTION.

    The model acknowledges but calls no tool, so the blind stays open; the declared
    `closed` never happens and the diff flags it.
    """
    await _build_cover(hass)
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [create_content_block(0, ["Sure."])]

    result = await run_case(hass, agent_id, _close_blinds_state_case(), llm=True)

    assert result.observed.unexpected_changes == {
        "cover.blinds": {"state": {"expected": "closed", "got": "open"}}
    }
    assert result.correct is False
    assert result.bucket is Bucket.WRONG_ACTION


async def test_world_reset_restores_baseline_between_cases(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Resetting the world returns an actuated entity to its fixture baseline.

    This is what makes each case order-independent: whatever a prior case did to the
    blind, the next case starts from open/100.
    """
    world = await _build_cover(hass)
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassSetPosition", ['{"name": "Blinds", "position": 0}']
        ),
        create_content_block(0, ["Done."]),
    ]

    await run_case(hass, agent_id, _close_blinds_state_case(), llm=True)
    assert hass.states.get("cover.blinds").state == "closed"

    await world.reset(hass)

    restored = hass.states.get("cover.blinds")
    assert restored.state == "open"
    assert restored.attributes["current_position"] == 100


async def test_corpus_world_matches_state_scored_assumptions(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """The executable fixture's initial states match what migrated cases assume.

    The deltas the state-scored cases declare only hold if the pre-turn world is as
    expected. Some of those values come from backing.py defaults (a media player's 0.5
    volume, an open cover's position 100), not the corpus YAML, so guard them here rather
    than discover a silent mis-score in the keyed baseline.
    """
    await build_executable_world(hass, load_corpus().world)

    speaker = hass.states.get("media_player.living_room")
    assert speaker.state == "playing"
    assert speaker.attributes["volume_level"] == 0.5

    blinds = hass.states.get("cover.living_room_blinds")
    assert blinds.state == "open"
    assert blinds.attributes["current_position"] == 100

    assert hass.states.get("light.living_room").state == "on"
    assert hass.states.get("cover.garage_door").state == "open"
    assert hass.states.get("fan.bedroom").state == "off"
