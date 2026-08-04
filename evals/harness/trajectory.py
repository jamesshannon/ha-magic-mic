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

    tools, _generations = observe_from_trace(async_get_traces()[-1].as_dict()["events"])
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
    stop_on_action: str | None = None,
) -> list[TurnObservation]:
    """Drive ``utterances`` in order as one conversation, threading the conversation id.

    With a scripted provider the caller scripts the generations for the whole trajectory
    up front; each turn's `async_converse` consumes as many as its scripted responses
    declare. Against a live model the same utterances are driven, but the model generates
    freely, so ``stop_on_action`` matters: pass the action tool's name and the drive stops
    the moment a turn fires it, leaving the remaining follow-up utterances unspoken. That
    makes the turn count emergent (how many turns the model actually needed), not a
    consequence of always speaking every authored line even after the task was done.
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
        if stop_on_action is not None and observation.called(stop_on_action):
            break
    return observations


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
# state, or tool policy regressed, the scripted follow-up would fail to actuate. Turn
# counts are descriptive (they are what the trajectory scripts), not an emergent metric;
# the emergent Δturns number is a later live-model run over the same worlds.


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
    observations: list[TurnObservation],
    final_state: dict[str, str],
) -> TrajectoryEval:
    """Score one driven trajectory against its expected action, state, and shape.

    ``final_state`` is the observed end state of the entities in ``case.final_state``,
    read from the world after the last turn (the driver does not carry world state).
    """
    fired_at = _first_action_turn(observations, action_tool=case.action_tool)
    correct = all(
        final_state.get(entity_id) == expected
        for entity_id, expected in case.final_state.items()
    )
    if fired_at is None:
        outcome = TrajectoryOutcome.NO_ACTION
    elif not correct:
        outcome = TrajectoryOutcome.MISFIRED
    elif fired_at > 0:
        outcome = TrajectoryOutcome.RECOVERED
    else:
        outcome = TrajectoryOutcome.DIRECT
    turns = fired_at + 1 if fired_at is not None else None
    return TrajectoryEval(case=case, outcome=outcome, turns_to_complete=turns)


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
