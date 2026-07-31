"""Tests for immutable pending-operation transitions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.magic_mic.identity import ResolvedPrincipal
from custom_components.magic_mic.pending_operation import (
    ConsequenceClass,
    NoPendingOperation,
    PendingOperation,
    PendingOperationAlreadyStaged,
    PendingOperationExpired,
    PendingOperationPolicyDenied,
    PendingOperationPrincipalMismatch,
    StageConflictPolicy,
    async_approve_pending,
    async_expire_pending,
    async_reject_pending,
    async_stage_pending,
)
from custom_components.magic_mic.session_state import MagicMicSessionState
from homeassistant.util.json import JsonObjectType

NOW = datetime(2026, 7, 31, 12, tzinfo=UTC)
LIFETIME = timedelta(seconds=30)
PRINCIPAL = ResolvedPrincipal(user_id="user-1")


def _operation(
    *,
    arguments: JsonObjectType | None = None,
    created_at: datetime = NOW,
    principal: ResolvedPrincipal = PRINCIPAL,
    tool_name: str = "light.turn_off",
) -> PendingOperation:
    """Create a pending operation for a deterministic test time."""
    return PendingOperation.create(
        arguments=(
            arguments if arguments is not None else {"entity_id": ["light.kitchen"]}
        ),
        consequence=ConsequenceClass.ALWAYS_CONFIRM,
        lifetime=LIFETIME,
        now=created_at,
        principal=principal,
        tool_name=tool_name,
    )


def test_arguments_are_deeply_copied_and_immutable() -> None:
    """Neither the model's input objects nor readers can alter staged arguments."""
    arguments = {
        "entity_id": ["light.kitchen"],
        "options": {"transition": 2},
    }
    operation = _operation(arguments=arguments)
    arguments["entity_id"].append("light.garage")
    arguments["options"]["transition"] = 10

    assert operation.arguments["entity_id"] == ("light.kitchen",)
    assert operation.arguments["options"]["transition"] == 2
    with pytest.raises(TypeError):
        operation.arguments["other"] = True

    mutable = operation.mutable_arguments()
    mutable["entity_id"].append("light.bedroom")
    assert operation.arguments["entity_id"] == ("light.kitchen",)


def test_stage_requires_explicit_conflict_policy() -> None:
    """A second active operation is rejected unless replacement is explicit."""
    state = MagicMicSessionState()
    first = _operation()
    second = _operation(tool_name="light.turn_on")
    async_stage_pending(state, first, now=NOW)

    with pytest.raises(PendingOperationAlreadyStaged):
        async_stage_pending(state, second, now=NOW)
    assert state.pending_operation is first

    async_stage_pending(
        state,
        second,
        conflict_policy=StageConflictPolicy.REPLACE,
        now=NOW,
    )
    assert state.pending_operation is second


def test_stage_replaces_expired_operation_without_override() -> None:
    """An expired record does not block a newly staged operation."""
    state = MagicMicSessionState()
    expired = _operation()
    replacement = _operation(created_at=NOW + timedelta(minutes=1))
    state.pending_operation = expired

    async_stage_pending(state, replacement, now=NOW + timedelta(minutes=1))

    assert state.pending_operation is replacement


def test_cannot_stage_an_already_expired_operation() -> None:
    """Staging fails closed if its approval window has already elapsed."""
    state = MagicMicSessionState()

    with pytest.raises(PendingOperationExpired):
        async_stage_pending(state, _operation(), now=NOW + LIFETIME)
    assert state.pending_operation is None


async def test_approve_executes_exact_stored_operation_once() -> None:
    """Approval accepts no replacement arguments and consumes before execution."""
    state = MagicMicSessionState()
    source_arguments = {"entity_id": ["light.kitchen"]}
    operation = _operation(arguments=source_arguments)
    async_stage_pending(state, operation, now=NOW)
    source_arguments["entity_id"][0] = "light.garage"
    policy_check = AsyncMock(return_value=True)
    executor = AsyncMock(return_value={"success": True})

    result = await async_approve_pending(
        state,
        PRINCIPAL,
        policy_check,
        executor,
        now=NOW + timedelta(seconds=1),
    )

    assert result == {"success": True}
    policy_check.assert_awaited_once_with(operation, PRINCIPAL)
    executor.assert_awaited_once_with(
        "light.turn_off", {"entity_id": ["light.kitchen"]}
    )
    assert state.pending_operation is None
    with pytest.raises(NoPendingOperation):
        await async_approve_pending(state, PRINCIPAL, policy_check, executor, now=NOW)
    assert executor.await_count == 1


def test_reject_consumes_without_executing() -> None:
    """A locally recognized no rejects the pending operation once."""
    state = MagicMicSessionState()
    operation = _operation()
    async_stage_pending(state, operation, now=NOW)

    assert async_reject_pending(state) is operation
    assert state.pending_operation is None
    with pytest.raises(NoPendingOperation):
        async_reject_pending(state)


def test_expire_only_consumes_after_deadline() -> None:
    """The explicit expiry transition leaves an active operation intact."""
    state = MagicMicSessionState()
    operation = _operation()
    async_stage_pending(state, operation, now=NOW)

    assert async_expire_pending(state, now=NOW + timedelta(seconds=29)) is None
    assert state.pending_operation is operation
    assert async_expire_pending(state, now=NOW + LIFETIME) is operation
    assert state.pending_operation is None


async def test_expired_approval_fails_without_policy_or_execution() -> None:
    """An expired yes consumes the stale operation and performs no work."""
    state = MagicMicSessionState()
    async_stage_pending(state, _operation(), now=NOW)
    policy_check = AsyncMock(return_value=True)
    executor = AsyncMock()

    with pytest.raises(PendingOperationExpired):
        await async_approve_pending(
            state,
            PRINCIPAL,
            policy_check,
            executor,
            now=NOW + LIFETIME,
        )

    policy_check.assert_not_awaited()
    executor.assert_not_awaited()
    assert state.pending_operation is None


async def test_different_principal_cannot_approve() -> None:
    """Approval from another principal consumes the operation and fails closed."""
    state = MagicMicSessionState()
    async_stage_pending(state, _operation(), now=NOW)
    policy_check = AsyncMock(return_value=True)
    executor = AsyncMock()

    with pytest.raises(PendingOperationPrincipalMismatch):
        await async_approve_pending(
            state,
            ResolvedPrincipal(user_id="user-2"),
            policy_check,
            executor,
            now=NOW,
        )

    policy_check.assert_not_awaited()
    executor.assert_not_awaited()
    assert state.pending_operation is None


async def test_policy_is_rechecked_at_approval() -> None:
    """Lost permission consumes the operation without calling its executor."""
    state = MagicMicSessionState()
    operation = _operation()
    async_stage_pending(state, operation, now=NOW)
    policy_check = AsyncMock(return_value=False)
    executor = AsyncMock()

    with pytest.raises(PendingOperationPolicyDenied):
        await async_approve_pending(state, PRINCIPAL, policy_check, executor, now=NOW)

    policy_check.assert_awaited_once_with(operation, PRINCIPAL)
    executor.assert_not_awaited()
    assert state.pending_operation is None
