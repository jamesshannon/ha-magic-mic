"""Tests for typed undo descriptors, journaling, and single-use replay."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.magic_mic.identity import (
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    ResolvedPrincipal,
)
from custom_components.magic_mic.session_state import MagicMicSessionState
from custom_components.magic_mic.undo import (
    NO_MUTATION,
    InverseOperation,
    LocalizedDescription,
    NoUndoAvailable,
    UndoAction,
    UndoAlreadyReplayed,
    UndoExecutionFailed,
    UndoExecutorMissing,
    UndoExecutorRegistry,
    UndoExpired,
    UndoInProgress,
    UndoNotAuthorized,
    UndoNotAvailable,
    UndoPreviouslyFailed,
    UndoScopeBinding,
    UndoStatus,
    UndoStrategy,
    UndoUnavailable,
    UndoUnavailableReason,
    async_record_undo,
    async_replay_latest,
)

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
PERSON = ResolvedPrincipal(user_id="person-1")
OTHER_PERSON = ResolvedPrincipal(user_id="person-2")
DESCRIPTION = LocalizedDescription(
    translation_domain="magic_mic",
    translation_key="fixture_action",
    placeholders={"name": "kitchen"},
)


def _action(
    *,
    scope: DataScope = DataScope.HOUSEHOLD,
    principal: ResolvedPrincipal = UNIDENTIFIED_PRINCIPAL,
) -> UndoAction:
    """Create a fixture action with a deterministic inverse."""
    return UndoAction(
        authorization=UndoScopeBinding.capture(scope, principal),
        description=DESCRIPTION,
        inverse=InverseOperation.custom(
            "fixture.restore",
            {"entity_id": ["light.kitchen"]},
        ),
    )


def test_inverse_arguments_and_description_are_immutable_copies() -> None:
    """Producer-owned containers cannot alter a journaled compensation."""
    arguments = {"entity_id": ["light.kitchen"]}
    placeholders = {"name": "kitchen"}
    inverse = InverseOperation.custom("fixture.restore", arguments)
    description = LocalizedDescription("magic_mic", "fixture", placeholders)
    arguments["entity_id"].append("light.garage")
    placeholders["name"] = "garage"

    assert inverse.arguments == {"entity_id": ("light.kitchen",)}
    assert description.placeholders == {"name": "kitchen"}
    with pytest.raises(TypeError):
        inverse.arguments["other"] = True
    with pytest.raises(TypeError):
        description.placeholders["other"] = "value"


def test_inverse_factories_preserve_strategy() -> None:
    """Built-in and custom inverse descriptors stay inspectable."""
    intent_inverse = InverseOperation.intent("HassCancelTimer", {"timer_id": "1"})
    state_inverse = InverseOperation.state_snapshot(
        {"light.kitchen": {"state": "off", "attributes": {}}}
    )

    assert intent_inverse.strategy is UndoStrategy.INTENT
    assert intent_inverse.executor == "magic_mic.intent"
    assert intent_inverse.arguments["intent_type"] == "HassCancelTimer"
    assert state_inverse.strategy is UndoStrategy.STATE_SNAPSHOT
    assert state_inverse.executor == "magic_mic.state_snapshot"


def test_scope_binding_distinguishes_household_and_personal() -> None:
    """Shared actions and personal-owner actions authorize differently."""
    household = UndoScopeBinding.capture(
        DataScope.HOUSEHOLD,
        UNIDENTIFIED_PRINCIPAL,
    )
    personal = UndoScopeBinding.capture(DataScope.PERSONAL, PERSON)

    assert household.allows(UNIDENTIFIED_PRINCIPAL)
    assert household.allows(OTHER_PERSON)
    assert personal.allows(PERSON)
    assert not personal.allows(OTHER_PERSON)
    assert not personal.allows(UNIDENTIFIED_PRINCIPAL)
    with pytest.raises(PermissionError):
        UndoScopeBinding.capture(DataScope.PERSONAL, UNIDENTIFIED_PRINCIPAL)


def test_no_mutation_does_not_shadow_latest_action() -> None:
    """Read-only outcomes do not create journal entries."""
    state = MagicMicSessionState()
    recorded = async_record_undo(state, _action(), "turn-1", now=NOW)

    assert async_record_undo(state, NO_MUTATION, "turn-2", now=NOW) is None
    assert state.undo_journal == (recorded,)


def test_unavailable_mutation_is_an_explicit_latest_barrier() -> None:
    """An unsupported action prevents replay from falling through to older work."""
    state = MagicMicSessionState()
    first = async_record_undo(state, _action(), "turn-1", now=NOW)
    barrier = async_record_undo(
        state,
        UndoUnavailable(DESCRIPTION, UndoUnavailableReason.IMPOSSIBLE),
        "turn-2",
        now=NOW,
    )

    assert first is not None
    assert barrier is not None
    assert barrier.status is UndoStatus.UNAVAILABLE
    assert state.undo_journal == (first, barrier)


async def test_replay_executes_exact_arguments_once() -> None:
    """The latest inverse is claimed once without semantic deduplication."""
    state = MagicMicSessionState()
    entry = async_record_undo(state, _action(), "turn-1", now=NOW)
    assert entry is not None
    executor = AsyncMock()
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", executor)

    result = await async_replay_latest(state, PERSON, registry, now=NOW)

    assert result is entry
    assert entry.status is UndoStatus.UNDONE
    executor.assert_awaited_once_with({"entity_id": ["light.kitchen"]})
    with pytest.raises(UndoAlreadyReplayed):
        await async_replay_latest(state, PERSON, registry, now=NOW)
    assert executor.await_count == 1


async def test_repeating_live_action_creates_a_new_replay_target() -> None:
    """Identical forward commands remain distinct journal entries."""
    state = MagicMicSessionState()
    first = async_record_undo(state, _action(), "turn-1", now=NOW)
    second = async_record_undo(state, _action(), "turn-2", now=NOW)
    assert first is not None
    assert second is not None
    executor = AsyncMock()
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", executor)

    replayed = await async_replay_latest(state, PERSON, registry, now=NOW)

    assert replayed is second
    assert first.status is UndoStatus.AVAILABLE
    assert second.status is UndoStatus.UNDONE


async def test_unavailable_latest_does_not_skip_to_older_entry() -> None:
    """Undo declines the latest impossible mutation and preserves older entries."""
    state = MagicMicSessionState()
    older = async_record_undo(state, _action(), "turn-1", now=NOW)
    barrier = async_record_undo(
        state,
        UndoUnavailable(DESCRIPTION, UndoUnavailableReason.PROHIBITED),
        "turn-2",
        now=NOW,
    )
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", AsyncMock())

    with pytest.raises(UndoNotAvailable) as err:
        await async_replay_latest(state, PERSON, registry, now=NOW)

    assert err.value.reason is UndoUnavailableReason.PROHIBITED
    assert older is not None and older.status is UndoStatus.AVAILABLE
    assert barrier is not None and barrier.status is UndoStatus.UNAVAILABLE


async def test_personal_action_requires_same_owner_without_consuming() -> None:
    """A different principal cannot erase or replay a personal inverse."""
    state = MagicMicSessionState()
    entry = async_record_undo(
        state,
        _action(scope=DataScope.PERSONAL, principal=PERSON),
        "turn-1",
        now=NOW,
    )
    registry = UndoExecutorRegistry()
    executor = AsyncMock()
    registry.register("fixture.restore", executor)

    with pytest.raises(UndoNotAuthorized):
        await async_replay_latest(state, OTHER_PERSON, registry, now=NOW)

    assert entry is not None and entry.status is UndoStatus.AVAILABLE
    executor.assert_not_awaited()


async def test_expired_action_is_marked_and_cannot_execute() -> None:
    """Explicit undo lifetime is independent of the longer session lifetime."""
    state = MagicMicSessionState()
    entry = async_record_undo(
        state,
        _action(),
        "turn-1",
        lifetime=timedelta(seconds=30),
        now=NOW,
    )
    executor = AsyncMock()
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", executor)

    with pytest.raises(UndoExpired):
        await async_replay_latest(
            state,
            PERSON,
            registry,
            now=NOW + timedelta(seconds=30),
        )

    assert entry is not None and entry.status is UndoStatus.EXPIRED
    executor.assert_not_awaited()


async def test_missing_executor_does_not_claim_action() -> None:
    """A registration bug leaves the action available for a corrected setup."""
    state = MagicMicSessionState()
    entry = async_record_undo(state, _action(), "turn-1", now=NOW)

    with pytest.raises(UndoExecutorMissing):
        await async_replay_latest(state, PERSON, UndoExecutorRegistry(), now=NOW)

    assert entry is not None and entry.status is UndoStatus.AVAILABLE


async def test_failed_inverse_is_consumed_without_unsafe_retry() -> None:
    """An ambiguous partial compensation is never automatically replayed."""
    state = MagicMicSessionState()
    entry = async_record_undo(state, _action(), "turn-1", now=NOW)
    executor = AsyncMock(side_effect=RuntimeError("partial failure"))
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", executor)

    with pytest.raises(UndoExecutionFailed):
        await async_replay_latest(state, PERSON, registry, now=NOW)
    assert entry is not None and entry.status is UndoStatus.FAILED
    with pytest.raises(UndoPreviouslyFailed):
        await async_replay_latest(state, PERSON, registry, now=NOW)
    assert executor.await_count == 1


async def test_concurrent_replay_sees_claimed_entry() -> None:
    """The status transition occurs before awaiting the inverse executor."""
    state = MagicMicSessionState()
    entry = async_record_undo(state, _action(), "turn-1", now=NOW)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(arguments: object) -> None:
        started.set()
        await release.wait()

    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", execute)  # type: ignore[arg-type]
    first = asyncio.create_task(async_replay_latest(state, PERSON, registry, now=NOW))
    await started.wait()

    with pytest.raises(UndoInProgress):
        await async_replay_latest(state, PERSON, registry, now=NOW)
    release.set()
    assert await first is entry


async def test_empty_journal_rejects_replay() -> None:
    """Undo without a completed mutation fails deterministically."""
    with pytest.raises(NoUndoAvailable):
        await async_replay_latest(
            MagicMicSessionState(),
            PERSON,
            UndoExecutorRegistry(),
            now=NOW,
        )


def test_duplicate_executor_registration_is_rejected() -> None:
    """Executor ownership cannot depend on integration setup order."""
    registry = UndoExecutorRegistry()
    registry.register("fixture.restore", AsyncMock())

    with pytest.raises(ValueError):
        registry.register("fixture.restore", AsyncMock())
