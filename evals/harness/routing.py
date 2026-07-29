"""Measure how core's local (HASSIL) agent handles an utterance.

This is the keyless, model-free half of the scorecard's local-vs-LLM split: it drives
core's default conversation agent and reports the outcome, with no provider or key.

The harness runs a deliberately minimal core (``homeassistant`` + ``conversation``,
no per-domain platforms), so most device intents *recognize* but cannot *execute* (no
entity is backed by a real platform). Two signals survive that limitation cleanly and
are what the measurement asserts on:

- ``recognized`` (``error_code`` is not ``no_intent_match``): HASSIL matched a sentence
  template. The verification for a ``local`` label.
- ``resolved`` (a non-error response): the local path produced a useful outcome. No
  ``llm``-labelled utterance should resolve here.
"""

from dataclasses import dataclass

from homeassistant.components import conversation
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent


@dataclass(frozen=True)
class LocalOutcome:
    """The local agent's response to one utterance, reduced to what scoring needs."""

    response_type: str
    error_code: str | None
    speech: str

    @property
    def resolved(self) -> bool:
        """True when the local path produced a useful (non-error) outcome."""
        return self.response_type != intent.IntentResponseType.ERROR.value

    @property
    def recognized(self) -> bool:
        """True when HASSIL matched a sentence template (even if it then failed)."""
        return self.error_code != intent.IntentResponseErrorCode.NO_INTENT_MATCH.value


async def probe_local(hass: HomeAssistant, utterance: str) -> LocalOutcome:
    """Run one utterance through the default local agent and reduce the response.

    ``agent_id=None`` selects the default local agent.
    """
    result = await conversation.async_converse(hass, utterance, None, Context(), None)
    response = result.response
    speech = ""
    if response.speech:
        speech = response.speech.get("plain", {}).get("speech", "")
    return LocalOutcome(
        response_type=response.response_type.value,
        error_code=response.error_code.value if response.error_code else None,
        speech=speech,
    )
