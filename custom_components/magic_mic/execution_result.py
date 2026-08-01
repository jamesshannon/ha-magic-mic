"""Out-of-band undo metadata for successful tool and intent execution."""

from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from .undo import NoMutation, UndoAction, UndoDisposition, UndoUnavailable

_UNDO_ATTRIBUTE = "magic_mic_undo_disposition"


class ToolExecutionResult(dict):
    """Public tool result carrying private execution metadata.

    The mapping contains only the result returned to the model. The undo disposition
    is deliberately an attribute, so inverse arguments cannot enter model context or
    provider serialization accidentally.
    """

    undo_disposition: UndoDisposition

    def __init__(
        self,
        public_result: JsonObjectType,
        undo_disposition: UndoDisposition,
    ) -> None:
        """Copy the public result and retain its private disposition."""
        super().__init__(public_result)
        self.undo_disposition = undo_disposition


def set_intent_undo_disposition(
    response: intent.IntentResponse,
    disposition: UndoDisposition,
) -> None:
    """Attach private undo metadata to an intent response before it is wrapped."""
    setattr(response, _UNDO_ATTRIBUTE, disposition)


def get_undo_disposition(result: JsonObjectType) -> UndoDisposition | None:
    """Extract a valid private disposition from a tool or wrapped intent result."""
    candidate: object | None = None
    if isinstance(result, ToolExecutionResult):
        candidate = result.undo_disposition
    elif isinstance(result, llm.IntentResponseDict):
        candidate = getattr(result.original, _UNDO_ATTRIBUTE, None)

    if isinstance(candidate, (NoMutation, UndoUnavailable, UndoAction)):
        return candidate
    return None


def public_tool_result(result: JsonObjectType) -> JsonObjectType:
    """Return a plain mapping with no private execution metadata."""
    return dict(result) if isinstance(result, ToolExecutionResult) else result


__all__ = [
    "ToolExecutionResult",
    "get_undo_disposition",
    "public_tool_result",
    "set_intent_undo_disposition",
]
