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
class SelectionTrace:
    """Compact record of one turn's capability-selection decision.

    The observability half of docs "Selection trace and observability": it answers why the
    roster was the size it was, without holding a `SelectionPlan` (which would pull the
    capabilities layer into this low-level state container). ``enforced`` distinguishes a
    live prune from a shadow observation; ``dropped`` names the catalogued tools selection
    removed (the saving) and ``passthrough`` the uncatalogued tools it had to retain.
    """

    enforced: bool
    budget: int
    exposed_before: int
    exposed_after: int
    admitted: tuple[str, ...]
    dropped: tuple[str, ...]
    passthrough: tuple[str, ...]


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
    selection: "SelectionTrace | None" = None
    tool_policy: list[ToolPolicyTrace] = field(default_factory=list)


@dataclass(slots=True)
class MagicMicSessionState:
    """Small deterministic state that must survive between conversation turns.

    The pending-operation contract is immutable and provider-neutral. Undo journal
    entries are typed and single-use; capability-specific compensation stays outside
    this state container. Turn metadata is keyed by exact turn ID so overlapping turns
    cannot replace one another's attribution target.
    """

    pending_operation: PendingOperation | None = None
    _cleanup_registered: bool = field(default=False, init=False, repr=False)
    _turn_metadata: dict[str, TurnMetadata] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
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
        if (metadata := self._turn_metadata.get(turn_id)) is None:
            metadata = TurnMetadata(
                device_id=device_id,
                is_continuation=is_continuation,
                principal=principal,
                satellite_id=satellite_id,
                turn_id=turn_id,
            )
            self._turn_metadata[turn_id] = metadata
        return metadata

    @callback
    def async_get_turn_metadata(self, turn_id: str) -> TurnMetadata | None:
        """Return metadata for an exact turn ID when retained by this session."""
        return self._turn_metadata.get(turn_id)


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
    "SelectionTrace",
    "ToolPolicyTrace",
    "TurnMetadata",
    "async_get_session_state",
]
