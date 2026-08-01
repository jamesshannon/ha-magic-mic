"""Provider-neutral undo descriptors, journal transitions, and replay dispatch."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, Self

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType
from homeassistant.util.ulid import ulid_now

from .const import DOMAIN
from .identity import DataScope, ResolvedPrincipal
from .immutable_json import FrozenJsonValue, freeze_json_mapping, thaw_json_mapping

UNDO_LIFETIME = timedelta(minutes=2)


class UndoStrategy(StrEnum):
    """Deterministic mechanism used to compensate one completed mutation."""

    CUSTOM = "custom"
    INTENT = "intent"
    STATE_SNAPSHOT = "state_snapshot"


class UndoUnavailableReason(StrEnum):
    """Why a completed mutation cannot be reversed."""

    IMPOSSIBLE = "impossible"
    NOT_SUPPORTED = "not_supported"
    PROHIBITED = "prohibited"


class UndoStatus(StrEnum):
    """Single-use replay state for one journal entry."""

    AVAILABLE = "available"
    EXECUTING = "executing"
    EXPIRED = "expired"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    UNDONE = "undone"


@dataclass(frozen=True, slots=True)
class LocalizedDescription:
    """Translation reference describing the completed mutation."""

    translation_domain: str
    translation_key: str
    placeholders: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate and freeze the translation reference."""
        if not self.translation_domain.strip() or not self.translation_key.strip():
            raise ValueError(
                "An undo description requires a translation domain and key"
            )
        object.__setattr__(
            self,
            "placeholders",
            MappingProxyType(dict(self.placeholders)),
        )


@dataclass(frozen=True, slots=True)
class UndoScopeBinding:
    """Data scope and owner authorized to replay an inverse."""

    scope: DataScope
    owner_id: str | None = None

    def __post_init__(self) -> None:
        """Require an owner only for personal actions."""
        if self.scope is DataScope.HOUSEHOLD and self.owner_id is not None:
            raise ValueError("A household undo binding cannot have a personal owner")
        if self.scope is DataScope.PERSONAL and not self.owner_id:
            raise ValueError("A personal undo binding requires an owner")

    @classmethod
    def capture(cls, scope: DataScope, principal: ResolvedPrincipal) -> Self:
        """Capture an authorization binding from the acting principal."""
        if scope is DataScope.HOUSEHOLD:
            return cls(scope=scope)
        if principal.personal_owner_id is None:
            raise PermissionError("An unidentified principal cannot bind personal undo")
        return cls(scope=scope, owner_id=principal.personal_owner_id)

    def allows(self, principal: ResolvedPrincipal) -> bool:
        """Return whether a principal may replay this action."""
        return self.scope is DataScope.HOUSEHOLD or (
            principal.personal_owner_id == self.owner_id
        )


@dataclass(frozen=True, slots=True)
class InverseOperation:
    """Inspectable immutable invocation understood by an undo executor."""

    executor: str
    arguments: Mapping[str, FrozenJsonValue]
    strategy: UndoStrategy = UndoStrategy.CUSTOM

    def __post_init__(self) -> None:
        """Validate and freeze a private copy of inverse arguments."""
        if not self.executor.strip():
            raise ValueError("An inverse operation requires an executor")
        object.__setattr__(self, "arguments", freeze_json_mapping(self.arguments))

    def mutable_arguments(self) -> JsonObjectType:
        """Return fresh mutable arguments for deterministic execution."""
        return thaw_json_mapping(self.arguments)

    @classmethod
    def custom(cls, executor: str, arguments: JsonObjectType) -> Self:
        """Create a capability-specific compensating invocation."""
        return cls(
            arguments=arguments,
            executor=executor,
            strategy=UndoStrategy.CUSTOM,
        )

    @classmethod
    def intent(cls, intent_type: str, slots: JsonObjectType) -> Self:
        """Create an inverse that calls a normalized HA intent."""
        return cls(
            arguments={"intent_type": intent_type, "slots": slots},
            executor="magic_mic.intent",
            strategy=UndoStrategy.INTENT,
        )

    @classmethod
    def state_snapshot(cls, entities: JsonObjectType) -> Self:
        """Create an inverse that reproduces captured entity states."""
        return cls(
            arguments={"entities": entities},
            executor="magic_mic.state_snapshot",
            strategy=UndoStrategy.STATE_SNAPSHOT,
        )


@dataclass(frozen=True, slots=True)
class NoMutation:
    """Declare that a successful operation made no stateful change."""


NO_MUTATION = NoMutation()


@dataclass(frozen=True, slots=True)
class UndoUnavailable:
    """Describe a mutation that must remain as an unundoable journal barrier."""

    description: LocalizedDescription
    reason: UndoUnavailableReason


@dataclass(frozen=True, slots=True)
class UndoAction:
    """Describe one authorized deterministic compensation."""

    authorization: UndoScopeBinding
    description: LocalizedDescription
    inverse: InverseOperation


type UndoDisposition = NoMutation | UndoUnavailable | UndoAction
type JournaledUndoDisposition = UndoUnavailable | UndoAction


@dataclass(slots=True)
class UndoJournalEntry:
    """One completed mutation and its single-use replay state."""

    created_at: datetime
    disposition: JournaledUndoDisposition
    execution_id: str
    expires_at: datetime
    turn_id: str
    status: UndoStatus

    @classmethod
    def create(
        cls,
        disposition: JournaledUndoDisposition,
        turn_id: str,
        *,
        lifetime: timedelta = UNDO_LIFETIME,
        now: datetime | None = None,
    ) -> Self:
        """Create a bounded entry with an explicit initial replay state."""
        if not turn_id.strip():
            raise ValueError("An undo journal entry requires a turn ID")
        if lifetime <= timedelta(0):
            raise ValueError("An undo journal entry requires a positive lifetime")
        created_at = now or dt_util.utcnow()
        return cls(
            created_at=created_at,
            disposition=disposition,
            execution_id=ulid_now(),
            expires_at=created_at + lifetime,
            status=(
                UndoStatus.AVAILABLE
                if isinstance(disposition, UndoAction)
                else UndoStatus.UNAVAILABLE
            ),
            turn_id=turn_id,
        )

    @property
    def description(self) -> LocalizedDescription:
        """Return the capability-owned description for this mutation."""
        return self.disposition.description

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether the explicit replay window has closed."""
        return self.expires_at <= (now or dt_util.utcnow())


class UndoJournalState(Protocol):
    """Session-state operations required by undo transitions."""

    @property
    def undo_journal(self) -> tuple[UndoJournalEntry, ...]:
        """Return journal entries from oldest to newest."""

    def async_append_undo(self, entry: UndoJournalEntry) -> None:
        """Append one completed mutation."""


type UndoExecutor = Callable[[JsonObjectType], Awaitable[None]]


class UndoExecutorRegistry:
    """Dispatch inverse descriptors without capability logic in the journal."""

    def __init__(self) -> None:
        """Initialize an empty executor registry."""
        self._executors: dict[str, UndoExecutor] = {}

    def register(self, executor_id: str, executor: UndoExecutor) -> None:
        """Register one stable inverse executor."""
        if not executor_id.strip():
            raise ValueError("An undo executor requires an ID")
        if executor_id in self._executors:
            raise ValueError(f"Undo executor {executor_id} is already registered")
        self._executors[executor_id] = executor

    def resolve(self, executor_id: str) -> UndoExecutor | None:
        """Return a registered executor without mutating the registry."""
        return self._executors.get(executor_id)


class UndoError(HomeAssistantError):
    """Base error for a failed undo transition."""


class NoUndoAvailable(UndoError):
    """No mutation has been journaled for this conversation."""

    def __init__(self) -> None:
        """Initialize a localizable empty-journal error."""
        super().__init__(
            translation_domain=DOMAIN, translation_key="undo_none_available"
        )


class UndoNotAvailable(UndoError):
    """The latest mutation is impossible, prohibited, or unsupported."""

    def __init__(self, reason: UndoUnavailableReason) -> None:
        """Initialize a localizable unavailable-action error."""
        self.reason = reason
        super().__init__(
            translation_domain=DOMAIN,
            translation_key=f"undo_{reason}",
        )


class UndoExpired(UndoError):
    """The latest mutation exceeded its replay lifetime."""

    def __init__(self) -> None:
        """Initialize a localizable expiry error."""
        super().__init__(translation_domain=DOMAIN, translation_key="undo_expired")


class UndoNotAuthorized(UndoError):
    """The current principal cannot replay the latest mutation."""

    def __init__(self) -> None:
        """Initialize a localizable authorization error."""
        super().__init__(
            translation_domain=DOMAIN, translation_key="undo_not_authorized"
        )


class UndoAlreadyReplayed(UndoError):
    """The latest mutation has already been compensated."""

    def __init__(self) -> None:
        """Initialize a localizable single-use error."""
        super().__init__(
            translation_domain=DOMAIN, translation_key="undo_already_replayed"
        )


class UndoInProgress(UndoError):
    """Another replay has already claimed the latest mutation."""

    def __init__(self) -> None:
        """Initialize a localizable concurrent-replay error."""
        super().__init__(translation_domain=DOMAIN, translation_key="undo_in_progress")


class UndoPreviouslyFailed(UndoError):
    """The latest inverse failed and cannot be retried safely."""

    def __init__(self) -> None:
        """Initialize a localizable prior-failure error."""
        super().__init__(
            translation_domain=DOMAIN, translation_key="undo_previously_failed"
        )


class UndoExecutionFailed(UndoError):
    """The claimed inverse failed during this replay."""

    def __init__(self) -> None:
        """Initialize a localizable execution error."""
        super().__init__(
            translation_domain=DOMAIN, translation_key="undo_execution_failed"
        )


class UndoExecutorMissing(UndoError):
    """No deterministic executor is registered for the inverse descriptor."""

    def __init__(self, executor_id: str) -> None:
        """Initialize a developer-facing contract error."""
        self.executor_id = executor_id
        super().__init__(f"Undo executor {executor_id} is not registered")


@callback
def async_record_undo(
    state: UndoJournalState,
    disposition: UndoDisposition,
    turn_id: str,
    *,
    lifetime: timedelta = UNDO_LIFETIME,
    now: datetime | None = None,
) -> UndoJournalEntry | None:
    """Record a mutation or ignore an explicit no-mutation outcome."""
    if isinstance(disposition, NoMutation):
        return None
    entry = UndoJournalEntry.create(
        disposition,
        turn_id,
        lifetime=lifetime,
        now=now,
    )
    state.async_append_undo(entry)
    return entry


async def async_replay_latest(
    state: UndoJournalState,
    principal: ResolvedPrincipal,
    executors: UndoExecutorRegistry,
    *,
    now: datetime | None = None,
) -> UndoJournalEntry:
    """Authorize, claim, and execute the latest mutation exactly once."""
    if not state.undo_journal:
        raise NoUndoAvailable
    entry = state.undo_journal[-1]

    if isinstance(entry.disposition, UndoUnavailable):
        raise UndoNotAvailable(entry.disposition.reason)
    if entry.is_expired(now):
        entry.status = UndoStatus.EXPIRED
        raise UndoExpired
    if entry.status is UndoStatus.EXECUTING:
        raise UndoInProgress
    if entry.status is UndoStatus.UNDONE:
        raise UndoAlreadyReplayed
    if entry.status is UndoStatus.FAILED:
        raise UndoPreviouslyFailed
    if entry.status is UndoStatus.EXPIRED:
        raise UndoExpired
    if not entry.disposition.authorization.allows(principal):
        raise UndoNotAuthorized

    inverse = entry.disposition.inverse
    if (executor := executors.resolve(inverse.executor)) is None:
        raise UndoExecutorMissing(inverse.executor)

    entry.status = UndoStatus.EXECUTING
    try:
        await executor(inverse.mutable_arguments())
    except Exception as err:
        entry.status = UndoStatus.FAILED
        raise UndoExecutionFailed from err
    entry.status = UndoStatus.UNDONE
    return entry


__all__ = [
    "NO_MUTATION",
    "UNDO_LIFETIME",
    "InverseOperation",
    "LocalizedDescription",
    "NoMutation",
    "NoUndoAvailable",
    "UndoAction",
    "UndoAlreadyReplayed",
    "UndoDisposition",
    "UndoError",
    "UndoExecutionFailed",
    "UndoExecutorMissing",
    "UndoExecutorRegistry",
    "UndoExpired",
    "UndoInProgress",
    "UndoJournalEntry",
    "UndoNotAuthorized",
    "UndoNotAvailable",
    "UndoPreviouslyFailed",
    "UndoScopeBinding",
    "UndoStatus",
    "UndoStrategy",
    "UndoUnavailable",
    "UndoUnavailableReason",
    "async_record_undo",
    "async_replay_latest",
]
