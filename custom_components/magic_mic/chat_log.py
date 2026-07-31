"""A `ChatLog` enriched with instrumentation and deterministic session state.

This is the shape we propose for core: the base `ChatLog` gains a structured record of
each model round (tokens, cache), populated by the provider and read by the proxy, the
eval harness, and live/prod debug tracing alike. Building it here, provider-agnostic,
means swapping `internal.claude` for another dev LLM leaves every reader untouched (only
the provider-side adapter that fills a `GenerationRecord` changes). Deterministic values
that must survive a turn are exposed through this object but stored in a conversation-ID
sidecar; transcript content is never copied there. See `docs/evaluation.md` Part A (trace
enrichment) and Part F.

`MagicMicChatLog` deliberately adds **no dataclass fields** so an existing `ChatLog`
can be upgraded in place by reassigning `__class__` (`upgrade_chat_log`). That works
because `ChatLog` mutates its `content` list in place, so the upgraded object is the
same instance the caller and the session cache already hold, with no copy and no
divergence.
"""

from dataclasses import dataclass
from typing import cast

from homeassistant.components.conversation import ChatLog
from homeassistant.core import callback

from .session_state import MagicMicSessionState, async_get_session_state


@dataclass(frozen=True)
class GenerationRecord:
    """One model round's usage, in provider-neutral terms.

    Populated by the provider from its own usage object (the one Claude-bound seam),
    so readers never touch provider types. `cache_read_tokens` and
    `cache_creation_tokens` are kept distinct: a cache *hit* is not a cache *write*.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return the record as a plain dict for the conversation trace."""
        return {
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


class MagicMicChatLog(ChatLog):
    """Expose session state and record a `GenerationRecord` per model round.

    Adds behavior, not dataclass fields. The turn-local record list lives in the instance
    `__dict__`; conversation-lifetime state lives in the sidecar. The class therefore stays
    layout-compatible with `ChatLog` for in-place upgrade.
    """

    _GENERATIONS_KEY = "_magic_mic_generations"

    @property
    def session_state(self) -> MagicMicSessionState:
        """Return deterministic state for this conversation's HA chat session."""
        return async_get_session_state(self.hass, self.conversation_id)

    @property
    def generations(self) -> tuple[GenerationRecord, ...]:
        """The per-round usage records captured so far this turn."""
        return tuple(self.__dict__.get(self._GENERATIONS_KEY, ()))

    @property
    def generation_count(self) -> int:
        """The number of model round-trips in this turn."""
        return len(self.__dict__.get(self._GENERATIONS_KEY, ()))

    @callback
    def async_trace_generation(self, record: GenerationRecord) -> None:
        """Record one model round and mirror it into the conversation trace.

        The structured list serves in-turn readers (proxy, eval); the `async_trace`
        mirror keeps live/prod debug tracing fed through the existing path.
        """
        self.__dict__.setdefault(self._GENERATIONS_KEY, []).append(record)
        self.async_trace({"generation": record.as_dict()})


def upgrade_chat_log(chat_log: ChatLog) -> MagicMicChatLog:
    """Upgrade a `ChatLog` to `MagicMicChatLog` in place, idempotently.

    Reassigns `__class__` rather than copying: the caller, the session cache, and the
    provider loop all keep operating on the one instance, so content added during the
    turn and cross-turn history are unaffected. Safe only because `MagicMicChatLog` adds
    no dataclass fields (identical layout).
    """
    if not isinstance(chat_log, MagicMicChatLog):
        chat_log.__class__ = MagicMicChatLog
    return cast(MagicMicChatLog, chat_log)


# Re-exported for the provider adapter and readers.
__all__ = [
    "GenerationRecord",
    "MagicMicChatLog",
    "MagicMicSessionState",
    "upgrade_chat_log",
]
