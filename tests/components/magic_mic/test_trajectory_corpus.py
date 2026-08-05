"""Run the disambiguation trajectory corpus through the driver and score it.

Each corpus case drives a scripted multi-turn conversation through the real testbed agent
(offline: the provider generations are scripted, the tools and history replay are real),
then scores recovery and the end state from the world states each turn left behind. Scoring
is world-based, so this file scores exactly as the live runner does; only the generations
differ. The scorecard math is checked separately with synthetic evals, so this file covers
both the live machinery and the aggregation.
"""

import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness.backing import build_executable_world
from evals.harness.corpus import World
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
    build_scorecard,
    drive_until_goal,
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
    world = await build_executable_world(hass, case.world)
    testbed_id = _testbed_id(hass, setup_integration)
    mock_create_stream.return_value = _script(case)

    watched = {
        entity_id: world.resolved.get(entity_id, entity_id)
        for entity_id in case.final_state
    }
    initial = {
        entity_id: hass.states.get(resolved).state
        for entity_id, resolved in watched.items()
    }
    _observations, states = await drive_until_goal(
        hass, testbed_id, case.utterances, watched=watched, goal=case.final_state
    )
    result = score_trajectory(case, states, initial)

    assert result.outcome is not TrajectoryOutcome.MISFIRED, "actuated the wrong entity"
    assert result.passed, (
        f"{case.id}: expected {'recovery' if case.recovered else 'a direct hit'}, "
        f"got {result.outcome.name}"
    )


def _case(*, recovered: bool) -> TrajectoryCase:
    """A minimal single-entity case for scoring tests (world/turns unused by the scorer)."""
    return TrajectoryCase(
        id="c",
        world=None,
        turns=(),
        action_tool="HassTurnOn",
        final_state={"light.x": "on"},
        recovered=recovered,
        tags=(),
    )


def test_recovered_when_goal_reached_after_a_clarifying_turn() -> None:
    """Reaching the goal on turn 2, having missed it on turn 1, is a recovery."""
    states = [{"light.x": "off"}, {"light.x": "on"}]

    result = score_trajectory(_case(recovered=True), states, {"light.x": "off"})

    assert result.outcome is TrajectoryOutcome.RECOVERED
    assert result.turns_to_complete == 2
    assert result.passed


def test_direct_when_goal_reached_on_the_first_turn() -> None:
    """Reaching the goal on turn 1 is a direct hit, and a recovery-case fails it."""
    states = [{"light.x": "on"}]

    direct = score_trajectory(_case(recovered=False), states, {"light.x": "off"})
    assert direct.outcome is TrajectoryOutcome.DIRECT
    assert direct.turns_to_complete == 1
    assert direct.passed
    # The same states fail a case that required a clarifying round-trip.
    assert not score_trajectory(
        _case(recovered=True), states, {"light.x": "off"}
    ).passed


def test_misfired_when_a_wrong_entity_is_actuated() -> None:
    """Changing the world but never to the goal is the unsafe MISFIRED outcome."""
    case = TrajectoryCase(
        id="c",
        world=None,
        turns=(),
        action_tool="HassTurnOn",
        final_state={"light.x": "on", "light.y": "off"},
        recovered=True,
        tags=(),
    )
    # Wanted x on and y off; the model turned on y instead and never reached the goal.
    states = [{"light.x": "off", "light.y": "on"}]

    result = score_trajectory(case, states, {"light.x": "off", "light.y": "off"})

    assert result.outcome is TrajectoryOutcome.MISFIRED
    assert not result.passed


def test_no_action_when_the_world_never_changes() -> None:
    """A world that never leaves its initial state is a lost task, not a completion."""
    states = [{"light.x": "off"}, {"light.x": "off"}]

    result = score_trajectory(_case(recovered=True), states, {"light.x": "off"})

    assert result.outcome is TrajectoryOutcome.NO_ACTION
    assert result.turns_to_complete is None
    assert not result.passed


def test_scorecard_aggregates_outcomes_and_mean_turns() -> None:
    """The scorecard sums outcomes and averages turns only over completed tasks."""
    evals = [
        TrajectoryEval(_case(recovered=True), TrajectoryOutcome.RECOVERED, 2),
        TrajectoryEval(_case(recovered=False), TrajectoryOutcome.DIRECT, 1),
        TrajectoryEval(_case(recovered=True), TrajectoryOutcome.MISFIRED, None),
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
