"""Run the disambiguation trajectory corpus through the driver and score it.

Each corpus case drives a scripted multi-turn conversation through the real testbed agent
(offline: the provider generations are scripted, the tools and history replay are real),
then scores recovery and the end state. The scorecard math is checked separately with
synthetic evals, so this file covers both the live machinery and the aggregation.
"""

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness.backing import build_executable_world
from evals.harness.corpus import World
from evals.harness.scoring import ToolCall
from evals.harness.trajectory import (
    ScriptedGeneration,
    ScriptedSpeech,
    ScriptedTool,
    TrajectoryCase,
    TrajectoryCorpus,
    TrajectoryCorpusError,
    TrajectoryEval,
    TrajectoryOutcome,
    TrajectoryTurn,
    TurnObservation,
    build_scorecard,
    drive_trajectory,
    load_trajectory_corpus,
    render_scorecard,
    score_trajectory,
    validate_trajectory_corpus,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block, create_tool_use_block

_CORPUS = load_trajectory_corpus()


def _testbed_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the testbed conversation entity id."""
    for entity in er.async_get(hass).entities.values():
        if (
            entity.platform == "magic_mic"
            and entity.unique_id == f"{entry.entry_id}_testbed"
        ):
            return entity.entity_id
    raise AssertionError("testbed agent not registered")


def _events(generation: ScriptedGeneration, index: int) -> list:
    """Compile one scripted generation into the mock stream's event-list."""
    if isinstance(generation, ScriptedTool):
        return create_tool_use_block(
            0,
            f"toolu_{index}",
            generation.tool,
            [json.dumps(generation.args)],
        )
    return create_content_block(0, [generation.say])


def _script(case: TrajectoryCase) -> list[list]:
    """Flatten a case's turns into one event-list per generation, in order."""
    generations = [generation for turn in case.turns for generation in turn.generations]
    return [_events(generation, index) for index, generation in enumerate(generations)]


@pytest.mark.parametrize("case", _CORPUS.cases, ids=lambda case: case.id)
async def test_trajectory_case_completes_as_expected(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream,
    case: TrajectoryCase,
) -> None:
    """Each corpus trajectory completes its task with the expected shape and end state."""
    await build_executable_world(hass, case.world)
    testbed_id = _testbed_id(hass, setup_integration)
    mock_create_stream.return_value = _script(case)

    observations = await drive_trajectory(hass, testbed_id, case.utterances)

    final_state = {
        entity_id: hass.states.get(entity_id).state for entity_id in case.final_state
    }
    result = score_trajectory(case, observations, final_state)

    assert result.outcome is not TrajectoryOutcome.MISFIRED, "actuated the wrong entity"
    assert result.passed, (
        f"{case.id}: expected {'recovery' if case.recovered else 'a direct hit'}, "
        f"got {result.outcome.name}"
    )


def _observation(*tools: str, utterance: str = "u") -> TurnObservation:
    """A synthetic turn observation that called ``tools`` (for scorecard-logic tests)."""
    return TurnObservation(
        utterance=utterance,
        speech="",
        tools=tuple(ToolCall(name) for name in tools),
        effects=(),
        resolved=True,
        continue_conversation=False,
        conversation_id="conv",
    )


def _case(*, recovered: bool) -> TrajectoryCase:
    """A minimal case for scoring tests (world/turns unused by the scorer)."""
    return TrajectoryCase(
        id="c",
        world=None,
        turns=(),
        action_tool="HassTurnOn",
        final_state={"light.x": "on"},
        recovered=recovered,
        tags=(),
    )


def test_recovered_when_action_fires_after_a_clarifying_turn() -> None:
    """An action on turn 2, with the right end state, is a recovery."""
    observations = [_observation("find_entities"), _observation("HassTurnOn")]

    result = score_trajectory(_case(recovered=True), observations, {"light.x": "on"})

    assert result.outcome is TrajectoryOutcome.RECOVERED
    assert result.turns_to_complete == 2
    assert result.passed


def test_direct_when_action_fires_on_the_first_turn() -> None:
    """An action on turn 1 is a direct hit, and a case expecting recovery fails it."""
    observations = [_observation("HassTurnOn")]

    direct = score_trajectory(_case(recovered=False), observations, {"light.x": "on"})
    assert direct.outcome is TrajectoryOutcome.DIRECT
    assert direct.turns_to_complete == 1
    assert direct.passed
    # The same run fails a case that required a clarifying round-trip.
    assert not score_trajectory(
        _case(recovered=True), observations, {"light.x": "on"}
    ).passed


def test_misfired_when_end_state_is_wrong() -> None:
    """Acting but leaving the wrong end state is the unsafe MISFIRED outcome."""
    observations = [_observation("find_entities"), _observation("HassTurnOn")]

    result = score_trajectory(_case(recovered=True), observations, {"light.x": "off"})

    assert result.outcome is TrajectoryOutcome.MISFIRED
    assert not result.passed


def test_no_action_when_the_tool_never_fires() -> None:
    """Never calling the action tool is a lost task, not a completion."""
    observations = [_observation("find_entities"), _observation("GetLiveContext")]

    result = score_trajectory(_case(recovered=True), observations, {"light.x": "off"})

    assert result.outcome is TrajectoryOutcome.NO_ACTION
    assert result.turns_to_complete is None
    assert not result.passed


def test_scorecard_aggregates_outcomes_and_mean_turns() -> None:
    """The scorecard sums outcomes and averages turns only over completed tasks."""
    evals = [
        TrajectoryEval(_case(recovered=True), TrajectoryOutcome.RECOVERED, 2),
        TrajectoryEval(_case(recovered=False), TrajectoryOutcome.DIRECT, 1),
        TrajectoryEval(_case(recovered=True), TrajectoryOutcome.MISFIRED, 2),
        TrajectoryEval(_case(recovered=True), TrajectoryOutcome.NO_ACTION, None),
    ]

    scorecard = build_scorecard(evals)

    assert scorecard.total == 4
    assert scorecard.passed == 2
    assert scorecard.recovered == 1
    assert scorecard.misfired == 1
    assert scorecard.no_action == 1
    # Mean over the completed (RECOVERED 2 turns, DIRECT 1 turn) only.
    assert scorecard.mean_turns_to_complete == 1.5
    assert "misfired:  1" in render_scorecard(scorecard)


def test_corpus_has_both_disambiguation_and_direct_cases() -> None:
    """The shipped corpus exercises both the recovery and the direct-completion shapes."""
    shapes = {case.recovered for case in _CORPUS.cases}
    assert shapes == {True, False}


def test_validate_rejects_final_state_naming_an_absent_entity() -> None:
    """A final_state entity that is not in the world fails validation at load time."""
    # A well-formed turn that calls the action tool, so only final_state is at fault.
    turn = TrajectoryTurn(
        utterance="u",
        generations=(ScriptedTool("HassTurnOn", {}), ScriptedSpeech("ok")),
    )
    bad = TrajectoryCase(
        id="bad",
        world=World(areas=(), entities=()),
        turns=(turn,),
        action_tool="HassTurnOn",
        final_state={"light.ghost": "on"},
        recovered=False,
    )

    with pytest.raises(TrajectoryCorpusError, match="absent from the world"):
        validate_trajectory_corpus(TrajectoryCorpus((bad,)))
