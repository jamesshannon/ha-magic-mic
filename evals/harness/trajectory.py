"""Drive a scripted multi-turn conversation and observe each turn.

`runner.observe_turn` proves one exchange. Disambiguation is a *round-trip*: an ambiguous
resolution ends a turn as a question, and a follow-up in the same conversation resolves it
(find-entities.md "the round-trip"). This driver threads the conversation id across turns
so the whole trajectory runs through the real agent, the real tools, the real session
state, and HA's continued-conversation history replay. Only the provider's per-generation
output is scripted, so it needs no live model: set the mock stream's per-generation
responses for the whole trajectory up front, then drive the utterances in order.

The driver is provider-agnostic. It never constructs Anthropic events itself; the caller
scripts the mock provider (the `mock_create_stream` fixture consumes one event-list per
generation, across every turn's `async_converse`). Recovery scoring is deliberate: an
action tool that fires only *after* a clarifying turn is a recovered disambiguation, which
is what the Δturns claim rests on.
"""

from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

import yaml

from homeassistant.components import conversation
from homeassistant.components.conversation.trace import async_get_traces
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

from .corpus import CORPUS_DIR, World, parse_world
from .effects import ObservedEffect, effect_cursor, effects_since
from .runner import observe_from_trace
from .scoring import ToolCall

WAVE1_DISAMBIGUATION = CORPUS_DIR / "wave1_disambiguation.yaml"


@dataclass(frozen=True)
class TurnObservation:
    """One turn of a trajectory, reduced from its trace and the world effects it caused."""

    utterance: str
    speech: str
    tools: tuple[ToolCall, ...]
    effects: tuple[ObservedEffect, ...]
    resolved: bool
    continue_conversation: bool
    conversation_id: str

    def called(self, tool_name: str) -> bool:
        """Return whether this turn invoked ``tool_name``."""
        return any(call.name == tool_name for call in self.tools)


async def drive_turn(
    hass: HomeAssistant,
    agent_id: str,
    utterance: str,
    *,
    conversation_id: str | None = None,
    device_id: str | None = None,
) -> TurnObservation:
    """Run one turn (optionally continuing ``conversation_id``) and reduce it.

    Passing the ``conversation_id`` of a prior turn continues that conversation, so the
    agent restores its history and any conversation-scoped session state. A fresh
    ``Context`` per turn matches the single-turn runner; the conversation id, not the
    context, is what threads continuity.
    """
    cursor = effect_cursor(hass)
    result = await conversation.async_converse(
        hass,
        utterance,
        conversation_id,
        Context(),
        agent_id=agent_id,
        device_id=device_id,
    )
    response = result.response
    speech = ""
    if response.speech:
        speech = response.speech.get("plain", {}).get("speech", "")

    tools, _generations, _rewrites = observe_from_trace(
        async_get_traces()[-1].as_dict()["events"]
    )
    return TurnObservation(
        utterance=utterance,
        speech=speech,
        tools=tools,
        effects=effects_since(hass, cursor),
        resolved=response.response_type is not intent.IntentResponseType.ERROR,
        continue_conversation=result.continue_conversation,
        conversation_id=result.conversation_id,
    )


async def drive_trajectory(
    hass: HomeAssistant,
    agent_id: str,
    utterances: list[str],
    *,
    device_id: str | None = None,
) -> list[TurnObservation]:
    """Drive ``utterances`` in order as one conversation, threading the conversation id.

    The caller scripts the provider's generations for the entire trajectory before
    calling this; each turn's `async_converse` consumes as many generations as its
    scripted responses declare. This drives every utterance; use `drive_until_goal` when
    the run should stop as soon as the task is done (the live turn-count case).
    """
    observations: list[TurnObservation] = []
    conversation_id: str | None = None
    for utterance in utterances:
        observation = await drive_turn(
            hass,
            agent_id,
            utterance,
            conversation_id=conversation_id,
            device_id=device_id,
        )
        observations.append(observation)
        conversation_id = observation.conversation_id
    return observations


async def drive_until_goal(
    hass: HomeAssistant,
    agent_id: str,
    utterances: list[str],
    *,
    watched: dict[str, str],
    goal: dict[str, str],
    device_id: str | None = None,
) -> tuple[list[TurnObservation], list[dict[str, str]]]:
    """Drive turns as one conversation, snapshotting ``watched`` until the world holds ``goal``.

    Returns the per-turn observations and, aligned to them, the state of the watched
    entities after each turn. ``watched`` maps each corpus entity id to the id it
    registered under; snapshots are keyed by the corpus id, as ``goal`` is.

    Stopping on the goal state, not on a tool firing, is what makes the live turn count
    both emergent and robust. A live model commonly emits a failed action first (e.g.
    ``HassTurnOn(name="lamp")`` against two lamps, which resolves nothing) and then asks;
    that failed call leaves the world unchanged, so the drive continues to the follow-up
    that actually resolves, and the turn where the world first reaches ``goal`` is the real
    turn-to-complete. A tool-in-trace stop would instead cut the recovery off at the failed
    call and misread it as acting.
    """
    observations: list[TurnObservation] = []
    states: list[dict[str, str]] = []
    conversation_id: str | None = None
    for utterance in utterances:
        observation = await drive_turn(
            hass,
            agent_id,
            utterance,
            conversation_id=conversation_id,
            device_id=device_id,
        )
        observations.append(observation)
        conversation_id = observation.conversation_id
        snapshot = {
            entity_id: hass.states.get(resolved).state
            for entity_id, resolved in watched.items()
        }
        states.append(snapshot)
        if all(snapshot.get(entity_id) == want for entity_id, want in goal.items()):
            break
    return observations, states


def disambiguation_recovered(
    observations: list[TurnObservation], *, action_tool: str
) -> bool:
    """Return whether ``action_tool`` fired only after at least one clarifying turn.

    A recovered disambiguation is the round-trip working end to end: an earlier turn did
    not run the action (it asked instead), and a later turn in the same conversation did.
    An action on the very first turn is a direct hit, not a recovery.
    """
    return _first_action_turn(observations, action_tool=action_tool) not in (None, 0)


def _first_action_turn(
    observations: list[TurnObservation], *, action_tool: str
) -> int | None:
    """Return the index of the first turn that ran ``action_tool``, or None."""
    for index, observation in enumerate(observations):
        if observation.called(action_tool):
            return index
    return None


# --- Trajectory corpus and scorecard ---------------------------------------------------
#
# A trajectory corpus turns the driver into a regression harness. Each case declares a
# world, an ordered set of user turns, and the provider's scripted generations for each
# turn, plus the expected end state. The recovery of the machinery (an ambiguous result
# ends a turn as a question, and a follow-up resolves and actuates the right entity) is a
# genuine pass/fail even though the generations are authored: if history replay, session
# state, or tool policy regressed, the scripted follow-up would fail to actuate. Scoring is
# world-based (`score_trajectory` over the per-turn snapshots from `drive_until_goal`), so
# the same corpus scores identically whether the generations are scripted or a live model's;
# only the turn counts differ, and against a live model they are the emergent Δturns number.


class TrajectoryCorpusError(ValueError):
    """Raised when a trajectory corpus file is malformed or inconsistent."""


@dataclass(frozen=True)
class ScriptedTool:
    """A scripted generation that calls one tool with fixed arguments."""

    tool: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ScriptedSpeech:
    """A scripted generation that speaks and ends the generation."""

    say: str


ScriptedGeneration = ScriptedTool | ScriptedSpeech


@dataclass(frozen=True)
class TrajectoryTurn:
    """One user utterance and the provider generations that answer it."""

    utterance: str
    generations: tuple[ScriptedGeneration, ...]


@dataclass(frozen=True)
class TrajectoryCase:
    """A labeled multi-turn trajectory: a world, the turns, and the expected outcome.

    ``action_tool`` is the tool whose firing completes the task. ``final_state`` is the
    end state each named entity must hold. ``recovered`` asserts the shape: True for a
    disambiguation (the action must fire only after a clarifying turn), False for a direct
    command that should complete on the first turn.
    """

    id: str
    world: World
    turns: tuple[TrajectoryTurn, ...]
    action_tool: str
    final_state: dict[str, str]
    recovered: bool
    tags: tuple[str, ...] = ()

    @property
    def utterances(self) -> list[str]:
        """The user utterances in order, for the driver."""
        return [turn.utterance for turn in self.turns]


@dataclass(frozen=True)
class TrajectoryCorpus:
    """A parsed trajectory corpus."""

    cases: tuple[TrajectoryCase, ...]


class TrajectoryOutcome(Enum):
    """How a trajectory turned out once scored.

    MISFIRED is the unsafe failure (actuated the wrong entity); NO_ACTION is a lost task
    (never actuated). RECOVERED and DIRECT both complete the task correctly; they differ
    only in whether a clarifying turn was needed.
    """

    RECOVERED = auto()
    DIRECT = auto()
    MISFIRED = auto()
    NO_ACTION = auto()


@dataclass(frozen=True)
class TrajectoryEval:
    """The scored result of one trajectory case."""

    case: TrajectoryCase
    outcome: TrajectoryOutcome
    turns_to_complete: int | None

    @property
    def passed(self) -> bool:
        """Whether the outcome matched the case's expected shape."""
        if self.case.recovered:
            return self.outcome is TrajectoryOutcome.RECOVERED
        return self.outcome is TrajectoryOutcome.DIRECT


def score_trajectory(
    case: TrajectoryCase,
    states: list[dict[str, str]],
    initial: dict[str, str],
) -> TrajectoryEval:
    """Score one driven trajectory from the world states it passed through.

    ``states`` is the watched entities' state after each driven turn, in order (from
    `drive_until_goal`); ``initial`` is their state before the first turn. Both are keyed
    like ``case.final_state``.

    Completion is world-based, not tool-based. The task is done on the turn the world first
    holds ``case.final_state``: reaching it on turn 1 is a DIRECT hit, on a later turn a
    RECOVERED disambiguation. If it is never reached, the world settling into a wrong,
    non-initial state is a MISFIRE (it actuated the wrong thing), and no change at all is a
    lost task (NO_ACTION). This is why a live model's failed ``HassTurnOn(name="lamp")``
    followed by a clarifying question does not score as a misfire: the failed call changed
    nothing, so the recovery on the follow-up is what counts.
    """
    goal = case.final_state
    goal_turn = next(
        (
            index
            for index, state in enumerate(states)
            if all(state.get(entity_id) == want for entity_id, want in goal.items())
        ),
        None,
    )
    if goal_turn is not None:
        outcome = (
            TrajectoryOutcome.DIRECT if goal_turn == 0 else TrajectoryOutcome.RECOVERED
        )
        return TrajectoryEval(
            case=case, outcome=outcome, turns_to_complete=goal_turn + 1
        )
    last = states[-1] if states else initial
    changed = any(last.get(entity_id) != initial.get(entity_id) for entity_id in goal)
    outcome = TrajectoryOutcome.MISFIRED if changed else TrajectoryOutcome.NO_ACTION
    return TrajectoryEval(case=case, outcome=outcome, turns_to_complete=None)


@dataclass(frozen=True)
class TrajectoryScorecard:
    """Aggregate metrics over a trajectory run, plus the per-case evals."""

    results: tuple[TrajectoryEval, ...]

    @property
    def total(self) -> int:
        """Total cases scored."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Cases whose outcome matched their expected shape."""
        return sum(1 for result in self.results if result.passed)

    def _count(self, outcome: TrajectoryOutcome) -> int:
        return sum(1 for result in self.results if result.outcome is outcome)

    @property
    def recovered(self) -> int:
        """Cases that completed only after a clarifying turn (the round-trip working)."""
        return self._count(TrajectoryOutcome.RECOVERED)

    @property
    def misfired(self) -> int:
        """Cases that actuated the wrong entity (the unsafe failure)."""
        return self._count(TrajectoryOutcome.MISFIRED)

    @property
    def no_action(self) -> int:
        """Cases that never actuated (a lost task)."""
        return self._count(TrajectoryOutcome.NO_ACTION)

    @property
    def mean_turns_to_complete(self) -> float | None:
        """Mean turns over the cases that completed the task (RECOVERED or DIRECT)."""
        completed = [
            result.turns_to_complete
            for result in self.results
            if result.turns_to_complete is not None
            and result.outcome
            in (TrajectoryOutcome.RECOVERED, TrajectoryOutcome.DIRECT)
        ]
        return sum(completed) / len(completed) if completed else None


def build_scorecard(results: list[TrajectoryEval]) -> TrajectoryScorecard:
    """Build a `TrajectoryScorecard` from per-case evals."""
    return TrajectoryScorecard(tuple(results))


def render_scorecard(scorecard: TrajectoryScorecard) -> str:
    """Render a trajectory scorecard as a short text block."""
    mean = scorecard.mean_turns_to_complete
    mean_text = f"{mean:.2f}" if mean is not None else "n/a"
    lines = [
        f"trajectories: {scorecard.total}",
        f"  passed:    {scorecard.passed}/{scorecard.total}",
        f"  recovered: {scorecard.recovered}",
        f"  misfired:  {scorecard.misfired}  (wrong entity, unsafe)",
        f"  no action: {scorecard.no_action}",
        f"  mean turns to complete: {mean_text}",
    ]
    for result in scorecard.results:
        flag = "ok " if result.passed else "XX "
        turns = result.turns_to_complete
        turns_text = f"{turns} turn(s)" if turns is not None else "no action"
        lines.append(
            f"  {flag}{result.case.id}: {result.outcome.name.lower()}, {turns_text}"
        )
    return "\n".join(lines)


def _parse_generation(raw: dict[str, Any]) -> ScriptedGeneration:
    if "tool" in raw:
        return ScriptedTool(tool=raw["tool"], args=dict(raw.get("args") or {}))
    if "say" in raw:
        return ScriptedSpeech(say=raw["say"])
    raise TrajectoryCorpusError(
        f"generation must have a 'tool' or 'say' key, got {sorted(raw)}"
    )


def _parse_turn(raw: dict[str, Any]) -> TrajectoryTurn:
    return TrajectoryTurn(
        utterance=raw["utterance"],
        generations=tuple(_parse_generation(g) for g in raw.get("generations") or ()),
    )


def _parse_trajectory_case(raw: dict[str, Any]) -> TrajectoryCase:
    expect = raw.get("expect") or {}
    return TrajectoryCase(
        id=raw["id"],
        world=parse_world(raw["world"]),
        turns=tuple(_parse_turn(turn) for turn in raw.get("turns") or ()),
        action_tool=raw["action_tool"],
        final_state=dict(expect.get("final_state") or {}),
        recovered=bool(expect.get("recovered", False)),
        tags=tuple(raw.get("tags") or ()),
    )


def load_trajectory_corpus(
    path: Path = WAVE1_DISAMBIGUATION,
) -> TrajectoryCorpus:
    """Load and validate a trajectory corpus from ``path``."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cases" not in raw:
        raise TrajectoryCorpusError(f"{path} must be a mapping with a 'cases' key")
    corpus = TrajectoryCorpus(
        cases=tuple(_parse_trajectory_case(case) for case in raw["cases"])
    )
    validate_trajectory_corpus(corpus)
    return corpus


def validate_trajectory_corpus(corpus: TrajectoryCorpus) -> None:
    """Check a trajectory corpus for internal consistency, failing loudly at load time."""
    seen: set[str] = set()
    for case in corpus.cases:
        if case.id in seen:
            raise TrajectoryCorpusError(f"duplicate case id {case.id!r}")
        seen.add(case.id)
        if not case.turns:
            raise TrajectoryCorpusError(f"{case.id}: needs at least one turn")
        if not case.final_state:
            raise TrajectoryCorpusError(f"{case.id}: needs an expected final_state")
        entity_ids = {entity.entity_id for entity in case.world.entities}
        for entity_id in case.final_state:
            if entity_id not in entity_ids:
                raise TrajectoryCorpusError(
                    f"{case.id}: final_state names {entity_id!r}, absent from the world"
                )
        actions = sum(
            isinstance(generation, ScriptedTool) and generation.tool == case.action_tool
            for turn in case.turns
            for generation in turn.generations
        )
        if actions == 0:
            raise TrajectoryCorpusError(
                f"{case.id}: no scripted generation calls action_tool "
                f"{case.action_tool!r}"
            )
