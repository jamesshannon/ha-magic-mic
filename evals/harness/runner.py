"""Drive a turn through an agent, observe it from the trace, and score it.

The observation source is the conversation trace that every turn produces: `TOOL_CALL`
events give the action-level detail (name + args, for any tool), and the per-round
`GenerationRecord`s the provider mirrors in as `AGENT_DETAIL` give generations and token
cost. The result gives speech and whether it resolved. This is provider-agnostic and
identical for the mock and the live model; only what the agent returns differs.

A state-scored case (`case.expect_changes`) also observes the world directly: the runner
stages the case's `setup`, snapshots entity states around the turn, and reduces the change
to the entities that ended up wrong (`statediff`). This needs an executable fixture world
(``backing.py``), not the bare-state routing world, since the turn must actually actuate.
"""

from dataclasses import replace

from homeassistant.components import conversation
from homeassistant.components.conversation.trace import (
    ConversationTraceEventType,
    async_get_traces,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

from .corpus import Case, StateChange
from .effects import effect_cursor, effects_since
from .scoring import CaseResult, ObservedTurn, ToolCall, score_case
from .statediff import snapshot, unexpected_changes


def observe_from_trace(
    trace_events: list[dict],
) -> tuple[tuple[ToolCall, ...], list[dict[str, int | float | None]]]:
    """Split a turn's trace events into tool calls and generation records.

    Shared with the multi-turn trajectory driver (`trajectory.py`), which reduces each
    turn of a conversation the same way. A generation record carries integer token counts
    and float-or-None round timing (`ttft_ms`, `duration_ms`).
    """
    tools: list[ToolCall] = []
    generations: list[dict[str, int | float | None]] = []
    for event in trace_events:
        data = event.get("data") or {}
        if event["event_type"] == ConversationTraceEventType.TOOL_CALL:
            tools.append(ToolCall(data["tool_name"], dict(data.get("tool_args") or {})))
        elif event["event_type"] == ConversationTraceEventType.AGENT_DETAIL and (
            generation := data.get("generation")
        ):
            generations.append(generation)
    return tuple(tools), generations


async def observe_turn(
    hass: HomeAssistant,
    agent_id: str,
    utterance: str,
    *,
    routed_locally: bool = False,
    device_id: str | None = None,
) -> ObservedTurn:
    """Run one turn through ``agent_id`` and reduce it to an ObservedTurn.

    ``device_id`` is the requesting device. It gates device-scoped tools: timer intents
    are only exposed when the turn carries a timer-capable device (see `helpers/llm.py`).
    """
    cursor = effect_cursor(hass)
    result = await conversation.async_converse(
        hass, utterance, None, Context(), agent_id=agent_id, device_id=device_id
    )
    response = result.response
    speech = ""
    if response.speech:
        speech = response.speech.get("plain", {}).get("speech", "")

    tools, generations = observe_from_trace(async_get_traces()[-1].as_dict()["events"])
    # TTFT is the first round's time to first content delta (what the user waits for);
    # round duration sums the model's per-round compute across the turn. Both are None on a
    # producer that did not clock its stream, so guard with .get and skip Nones.
    ttfts = [g["ttft_ms"] for g in generations if g.get("ttft_ms") is not None]
    durations = [
        g["duration_ms"] for g in generations if g.get("duration_ms") is not None
    ]
    return ObservedTurn(
        speech=speech,
        tools=tools,
        effects=effects_since(hass, cursor),
        routed_locally=routed_locally,
        resolved=response.response_type is not intent.IntentResponseType.ERROR,
        generations=len(generations),
        input_tokens=sum(g["input_tokens"] for g in generations),
        output_tokens=sum(g["output_tokens"] for g in generations),
        cache_read_tokens=sum(g["cache_read_tokens"] for g in generations),
        cache_creation_tokens=sum(g["cache_creation_tokens"] for g in generations),
        ttft_ms=ttfts[0] if ttfts else None,
        round_duration_ms=sum(durations),
    )


def _apply_setup(hass: HomeAssistant, setup: dict[str, StateChange]) -> None:
    """Stage a case's pre-turn state, preserving each entity's other attributes.

    Writes straight to the state machine. On an executable entity this only sets the
    pre-turn snapshot the diff reads; the actuation under test forces the entity's own
    values, so a stale internal attribute does not affect the outcome.
    """
    for entity_id, change in setup.items():
        current = hass.states.get(entity_id)
        base_attributes = dict(current.attributes) if current else {}
        state = (
            change.state
            if change.state is not None
            else (current.state if current else "unknown")
        )
        hass.states.async_set(
            entity_id, state, {**base_attributes, **change.attributes}
        )


def stage_case(hass: HomeAssistant, case: Case) -> dict[str, object] | None:
    """Apply a case's pre-turn setup and snapshot the world if it is state-scored.

    Returns the pre-turn snapshot for a state-scored case (the baseline `finalize_score`
    diffs against) or ``None`` for a tool/answer-scored case. Shared by the LLM runner and
    the local-first driver so both stage a case identically.
    """
    if case.setup:
        _apply_setup(hass, case.setup)
    return snapshot(hass) if case.state_scored else None


def finalize_score(
    hass: HomeAssistant,
    case: Case,
    observed: ObservedTurn,
    before: dict[str, object] | None,
    *,
    llm: bool,
) -> CaseResult:
    """Fold the post-turn world diff into ``observed`` (if staged) and score the case.

    ``llm`` selects the scope-specific expectation (`expected_for`): the LLM expectation
    for a model-handled turn, the local one for a turn the deterministic path handled.
    """
    if before is not None:
        observed = replace(
            observed,
            unexpected_changes=unexpected_changes(
                before, snapshot(hass), case.expect_changes, case.ignore_changes
            ),
        )
    return score_case(case, observed, expected=case.expected_for(llm=llm))


async def run_case(
    hass: HomeAssistant,
    agent_id: str,
    case: Case,
    *,
    llm: bool,
    device_id: str | None = None,
) -> CaseResult:
    """Drive a case through ``agent_id`` at the given scope and score it.

    ``llm=True`` scores against the LLM-scope expectation (``expected_for``) and treats
    the outcome as LLM-routed; ``llm=False`` scores the local expectation. ``device_id``
    is threaded to the turn so device-scoped tools (timers) are exposed.

    A state-scored case is staged (`setup`) and snapshotted around the turn, and the
    resulting `unexpected_changes` drives correctness instead of the tool/answer match.
    """
    before = stage_case(hass, case)
    observed = await observe_turn(
        hass, agent_id, case.utterance, routed_locally=not llm, device_id=device_id
    )
    return finalize_score(hass, case, observed, before, llm=llm)
