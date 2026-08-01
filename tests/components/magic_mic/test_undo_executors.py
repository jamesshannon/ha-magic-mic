"""Tests for built-in intent and entity-state inverse execution."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.magic_mic.identity import UNIDENTIFIED_PRINCIPAL, DataScope
from custom_components.magic_mic.session_state import MagicMicSessionState
from custom_components.magic_mic.undo import (
    InverseOperation,
    LocalizedDescription,
    UndoAction,
    UndoExecutionContext,
    UndoExecutionFailed,
    UndoExecutorRegistry,
    UndoScopeBinding,
    UndoStatus,
    async_record_undo,
    async_replay_latest,
)
from custom_components.magic_mic.undo_executors import (
    UndoHelper,
    register_builtin_undo_executors,
)
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.helpers import intent

DESCRIPTION = LocalizedDescription("magic_mic", "fixture_action")


class FixtureIntentHandler(intent.IntentHandler):
    """Record normalized inverse slots received through intent.async_handle."""

    intent_type = "MagicMicUndoFixture"

    def __init__(self) -> None:
        """Initialize the observable handler."""
        self.received_slots: dict | None = None
        self.received_context: Context | None = None

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Capture the inverse invocation and report success."""
        self.received_slots = intent_obj.slots
        self.received_context = intent_obj.context
        return intent_obj.create_response()


def _execution_context(hass: HomeAssistant) -> UndoExecutionContext:
    """Build a replay context with distinct principal and HA auth context."""
    return UndoExecutionContext(
        assistant="conversation",
        context=Context(user_id="pipeline-owner"),
        hass=hass,
        language="en",
        principal=UNIDENTIFIED_PRINCIPAL,
    )


async def test_intent_inverse_replays_normalized_slots(hass: HomeAssistant) -> None:
    """An inverse intent executes directly without an LLM reconstruction."""
    handler = FixtureIntentHandler()
    intent.async_register(hass, handler)
    action = UndoAction(
        authorization=UndoScopeBinding.capture(
            DataScope.HOUSEHOLD,
            UNIDENTIFIED_PRINCIPAL,
        ),
        description=DESCRIPTION,
        inverse=InverseOperation.intent(
            handler.intent_type,
            {"item_id": "reminder-1"},
        ),
    )
    state = MagicMicSessionState()
    entry = async_record_undo(state, action, "turn-1")
    registry = UndoExecutorRegistry()
    register_builtin_undo_executors(registry)
    execution_context = _execution_context(hass)

    await async_replay_latest(state, execution_context, registry)

    assert entry is not None and entry.status is UndoStatus.UNDONE
    assert handler.received_slots == {"item_id": {"value": "reminder-1"}}
    assert handler.received_context is execution_context.context
    assert handler.received_context.user_id == "pipeline-owner"


def test_snapshot_helper_filters_to_successful_entities() -> None:
    """Failed targets are excluded from the captured inverse."""
    kitchen_before = State("light.kitchen", "off", {"brightness": 0})
    den_before = State("light.den", "on", {"brightness": 80})
    helper = UndoHelper.capture_entity_states(
        [kitchen_before, den_before],
        allowed_domains={"light"},
    )
    kitchen_after = State("light.kitchen", "on", {"brightness": 255})

    action = helper.action_for_successful_entities(
        ["light.kitchen"],
        authorization=UndoScopeBinding.capture(
            DataScope.HOUSEHOLD,
            UNIDENTIFIED_PRINCIPAL,
        ),
        description=DESCRIPTION,
        expected_states=[kitchen_after],
    )

    restore = action.inverse.arguments["restore"]
    expected = action.inverse.arguments["expected"]
    assert list(restore) == ["light.kitchen"]
    assert restore["light.kitchen"]["state"] == "off"
    assert expected["light.kitchen"]["state"] == "on"


def test_snapshot_helper_requires_explicit_safe_domains() -> None:
    """A technically reproducible lock cannot become undoable accidentally."""
    with pytest.raises(ValueError, match="lock"):
        UndoHelper.capture_entity_states(
            [State("lock.front_door", "locked")],
            allowed_domains={"light"},
        )


async def test_state_snapshot_reproduces_captured_state(
    hass: HomeAssistant,
) -> None:
    """The executor uses HA state reproduction without creating a scene entity."""
    hass.states.async_set("light.kitchen", "on", {"brightness": 255})
    helper = UndoHelper.capture_entity_states(
        [State("light.kitchen", "off", {"brightness": 0})],
        allowed_domains={"light"},
    )
    action = helper.action_for_successful_entities(
        ["light.kitchen"],
        authorization=UndoScopeBinding.capture(
            DataScope.HOUSEHOLD,
            UNIDENTIFIED_PRINCIPAL,
        ),
        description=DESCRIPTION,
        expected_states=[State("light.kitchen", "on", {"brightness": 255})],
    )
    state = MagicMicSessionState()
    async_record_undo(state, action, "turn-1")
    registry = UndoExecutorRegistry()
    register_builtin_undo_executors(registry)

    with patch(
        "custom_components.magic_mic.undo_executors.async_reproduce_state",
        new_callable=AsyncMock,
    ) as reproduce:
        await async_replay_latest(state, _execution_context(hass), registry)

    reproduced = list(reproduce.await_args.args[1])
    assert len(reproduced) == 1
    assert reproduced[0].entity_id == "light.kitchen"
    assert reproduced[0].state == "off"
    assert reproduced[0].attributes["brightness"] == 0
    assert reproduce.await_args.kwargs["context"].user_id == "pipeline-owner"


async def test_state_snapshot_fails_closed_after_world_moves_on(
    hass: HomeAssistant,
) -> None:
    """A changed post-action state is not overwritten by a stale snapshot."""
    hass.states.async_set("light.kitchen", "on", {"brightness": 100})
    helper = UndoHelper.capture_entity_states(
        [State("light.kitchen", "off", {"brightness": 0})],
        allowed_domains={"light"},
    )
    action = helper.action_for_successful_entities(
        ["light.kitchen"],
        authorization=UndoScopeBinding.capture(
            DataScope.HOUSEHOLD,
            UNIDENTIFIED_PRINCIPAL,
        ),
        description=DESCRIPTION,
        expected_states=[State("light.kitchen", "on", {"brightness": 255})],
    )
    state = MagicMicSessionState()
    entry = async_record_undo(state, action, "turn-1")
    registry = UndoExecutorRegistry()
    register_builtin_undo_executors(registry)

    with (
        patch(
            "custom_components.magic_mic.undo_executors.async_reproduce_state",
            new_callable=AsyncMock,
        ) as reproduce,
        pytest.raises(UndoExecutionFailed),
    ):
        await async_replay_latest(state, _execution_context(hass), registry)

    assert entry is not None and entry.status is UndoStatus.FAILED
    reproduce.assert_not_awaited()
