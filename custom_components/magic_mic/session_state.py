"""Deterministic state scoped to one Home Assistant chat session."""

from collections import deque
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.chat_session import current_session
from homeassistant.util.hass_dict import HassKey

from .identity import UNIDENTIFIED_PRINCIPAL, ResolvedPrincipal
from .pending_operation import PendingOperation
from .undo import UndoJournalEntry

UNDO_JOURNAL_LIMIT = 10


@dataclass(slots=True)
class ToolPolicyTrace:
    """One deterministic tool exposure or invocation policy decision."""

    allowed: bool
    consequence: str
    policy_source: str
    stage: str
    tool_name: str


@dataclass(slots=True)
class TurnMetadata:
    """Deterministic policy and effect metadata for the current turn."""

    turn_id: str
    device_id: str | None = None
    effects: list[object] = field(default_factory=list)
    is_continuation: bool = False
    principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL
    provenance: set[str] = field(default_factory=set)
    satellite_id: str | None = None
    tool_policy: list[ToolPolicyTrace] = field(default_factory=list)


@dataclass(slots=True)
class MagicMicSessionState:
    """Small deterministic state that must survive between conversation turns.

    The pending-operation contract is immutable and provider-neutral. Undo journal
    entries are typed and single-use; capability-specific compensation stays outside
    this state container.
    """

    pending_operation: PendingOperation | None = None
    turn_metadata: TurnMetadata | None = None
    _cleanup_registered: bool = field(default=False, init=False, repr=False)
    _undo_journal: deque[UndoJournalEntry] = field(
        default_factory=lambda: deque(maxlen=UNDO_JOURNAL_LIMIT),
        init=False,
        repr=False,
    )

    @property
    def undo_journal(self) -> tuple[UndoJournalEntry, ...]:
        """Return the bounded undo journal from oldest to newest."""
        return tuple(self._undo_journal)

    @callback
    def async_append_undo(self, entry: UndoJournalEntry) -> None:
        """Append an entry, evicting the oldest entry at the fixed limit."""
        self._undo_journal.append(entry)

    @callback
    def async_begin_turn(
        self,
        turn_id: str,
        *,
        device_id: str | None = None,
        is_continuation: bool = False,
        principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL,
        satellite_id: str | None = None,
    ) -> TurnMetadata:
        """Create fresh metadata for a turn, idempotently for the same turn ID."""
        if self.turn_metadata is None or self.turn_metadata.turn_id != turn_id:
            self.turn_metadata = TurnMetadata(
                device_id=device_id,
                is_continuation=is_continuation,
                principal=principal,
                satellite_id=satellite_id,
                turn_id=turn_id,
            )
        return self.turn_metadata


DATA_SESSION_STATES: HassKey[dict[str, MagicMicSessionState]] = HassKey(
    "magic_mic_session_states"
)


@callback
def async_get_session_state(
    hass: HomeAssistant, conversation_id: str
) -> MagicMicSessionState:
    """Return session state and bind its cleanup to the active HA chat session."""
    states = hass.data.setdefault(DATA_SESSION_STATES, {})
    state = states.setdefault(conversation_id, MagicMicSessionState())

    session = current_session.get()
    if (
        not state._cleanup_registered  # noqa: SLF001
        and session is not None
        and session.conversation_id == conversation_id
    ):

        @callback
        def async_cleanup_state() -> None:
            """Remove this sidecar when Home Assistant expires the chat session."""
            states.pop(conversation_id, None)

        session.async_on_cleanup(async_cleanup_state)
        state._cleanup_registered = True  # noqa: SLF001

    return state


__all__ = [
    "DATA_SESSION_STATES",
    "UNDO_JOURNAL_LIMIT",
    "MagicMicSessionState",
    "ToolPolicyTrace",
    "TurnMetadata",
    "async_get_session_state",
]
