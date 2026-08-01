"""Immutable operations awaiting an explicit confirmation decision."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, Self

from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from .identity import ResolvedPrincipal
from .immutable_json import FrozenJsonValue, freeze_json_mapping, thaw_json_mapping


class ConsequenceClass(StrEnum):
    """Ordinal confirmation requirements implemented by the v1 policy layer."""

    LOW = "low"
    CONFIRM_ON_CONTINUATION = "confirm_on_continuation"
    ALWAYS_CONFIRM = "always_confirm"


class StageConflictPolicy(StrEnum):
    """Explicit behavior when another operation is already pending."""

    REJECT = "reject"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class PendingOperation:
    """One normalized provider-neutral action waiting for approval."""

    tool_name: str
    arguments: Mapping[str, FrozenJsonValue]
    principal: ResolvedPrincipal
    consequence: ConsequenceClass
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """Validate the record and recursively freeze a private argument copy."""
        if not self.tool_name.strip():
            raise ValueError("A pending operation requires a tool name")
        if self.expires_at <= self.created_at:
            raise ValueError("A pending operation must expire after its creation")
        object.__setattr__(self, "arguments", freeze_json_mapping(self.arguments))

    @classmethod
    def create(
        cls,
        tool_name: str,
        arguments: JsonObjectType,
        principal: ResolvedPrincipal,
        consequence: ConsequenceClass,
        *,
        lifetime: timedelta,
        now: datetime | None = None,
    ) -> Self:
        """Create an operation with an explicit bounded lifetime."""
        created_at = now or dt_util.utcnow()
        return cls(
            arguments=arguments,
            consequence=consequence,
            created_at=created_at,
            expires_at=created_at + lifetime,
            principal=principal,
            tool_name=tool_name,
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return whether the approval window has closed."""
        return self.expires_at <= (now or dt_util.utcnow())

    def mutable_arguments(self) -> JsonObjectType:
        """Return a new mutable JSON copy for the executor."""
        return thaw_json_mapping(self.arguments)


class PendingOperationError(Exception):
    """Base error for a failed pending-operation transition."""


class PendingOperationAlreadyStaged(PendingOperationError):
    """An active operation exists and replacement was not authorized."""


class NoPendingOperation(PendingOperationError):
    """There is no operation available for the requested transition."""


class PendingOperationExpired(PendingOperationError):
    """The operation expired before the transition."""


class PendingOperationPrincipalMismatch(PendingOperationError):
    """A different principal attempted to approve the operation."""


class PendingOperationPolicyDenied(PendingOperationError):
    """Execution policy no longer permits the stored operation."""


class PendingOperationState(Protocol):
    """Storage seam required by pending-operation transitions."""

    pending_operation: PendingOperation | None


@callback
def async_stage_pending(
    state: PendingOperationState,
    operation: PendingOperation,
    *,
    conflict_policy: StageConflictPolicy = StageConflictPolicy.REJECT,
    now: datetime | None = None,
) -> None:
    """Stage one operation, with explicit reject-or-replace conflict behavior."""
    effective_now = now or dt_util.utcnow()
    if operation.is_expired(effective_now):
        raise PendingOperationExpired

    existing = state.pending_operation
    if isinstance(existing, PendingOperation):
        if existing.is_expired(effective_now):
            state.pending_operation = None
        elif conflict_policy is StageConflictPolicy.REJECT:
            raise PendingOperationAlreadyStaged

    state.pending_operation = operation


@callback
def async_reject_pending(state: PendingOperationState) -> PendingOperation:
    """Reject and consume the current operation."""
    return _consume_pending(state)


@callback
def async_expire_pending(
    state: PendingOperationState, *, now: datetime | None = None
) -> PendingOperation | None:
    """Consume and return the current operation only if it has expired."""
    operation = state.pending_operation
    if not isinstance(operation, PendingOperation) or not operation.is_expired(now):
        return None
    state.pending_operation = None
    return operation


async def async_approve_pending[_ResultT](
    state: PendingOperationState,
    principal: ResolvedPrincipal,
    policy_check: Callable[[PendingOperation, ResolvedPrincipal], Awaitable[bool]],
    executor: Callable[[str, JsonObjectType], Awaitable[_ResultT]],
    *,
    now: datetime | None = None,
) -> _ResultT:
    """Consume, reauthorize, and execute the exact stored operation once."""
    operation = _consume_pending(state)
    if operation.is_expired(now):
        raise PendingOperationExpired
    if operation.principal != principal:
        raise PendingOperationPrincipalMismatch
    if not await policy_check(operation, principal):
        raise PendingOperationPolicyDenied
    return await executor(operation.tool_name, operation.mutable_arguments())


@callback
def _consume_pending(state: PendingOperationState) -> PendingOperation:
    """Atomically remove and return the current typed operation."""
    operation = state.pending_operation
    state.pending_operation = None
    if not isinstance(operation, PendingOperation):
        raise NoPendingOperation
    return operation


__all__ = [
    "ConsequenceClass",
    "NoPendingOperation",
    "PendingOperation",
    "PendingOperationAlreadyStaged",
    "PendingOperationError",
    "PendingOperationExpired",
    "PendingOperationPolicyDenied",
    "PendingOperationPrincipalMismatch",
    "StageConflictPolicy",
    "async_approve_pending",
    "async_expire_pending",
    "async_reject_pending",
    "async_stage_pending",
]
