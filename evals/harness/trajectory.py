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

from homeassistant.components import conversation
from homeassistant.components.conversation.trace import async_get_traces
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

from .effects import ObservedEffect, effect_cursor, effects_since
from .runner import observe_from_trace
from .scoring import ToolCall


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
) -> list[TurnObservation]:
    """Drive ``utterances`` in order as one conversation, threading the conversation id.

    The caller scripts the provider's generations for the entire trajectory before
    calling this; each turn's `async_converse` consumes as many generations as its
    scripted responses declare.
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


def disambiguation_recovered(
    observations: list[TurnObservation], *, action_tool: str
) -> bool:
    """Return whether ``action_tool`` fired only after at least one clarifying turn.

    A recovered disambiguation is the round-trip working end to end: an earlier turn did
    not run the action (it asked instead), and a later turn in the same conversation did.
    An action on the very first turn is a direct hit, not a recovery.
    """
    fired_at = [
        index
        for index, observation in enumerate(observations)
        if observation.called(action_tool)
    ]
    return bool(fired_at) and fired_at[0] > 0
