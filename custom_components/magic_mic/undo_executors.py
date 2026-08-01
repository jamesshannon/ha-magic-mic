"""Built-in inverse executors and entity-state snapshot helper."""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from homeassistant.core import State
from homeassistant.helpers import intent
from homeassistant.helpers.state import async_reproduce_state
from homeassistant.util.json import JsonObjectType, JsonValueType

from .const import DOMAIN
from .immutable_json import FrozenJsonValue, freeze_json_mapping, thaw_json_mapping
from .undo import (
    InverseOperation,
    LocalizedDescription,
    UndoAction,
    UndoExecutionContext,
    UndoExecutorRegistry,
    UndoScopeBinding,
)

INTENT_UNDO_EXECUTOR = "magic_mic.intent"
STATE_SNAPSHOT_UNDO_EXECUTOR = "magic_mic.state_snapshot"


@dataclass(frozen=True, slots=True)
class UndoHelper:
    """A pre-action entity snapshot that can become a filtered inverse."""

    _states: Mapping[str, FrozenJsonValue]

    @classmethod
    def capture_entity_states(
        cls,
        states: Iterable[State],
        *,
        allowed_domains: Collection[str],
    ) -> "UndoHelper":
        """Capture state for explicitly undo-safe domains before mutation."""
        captured: JsonObjectType = {}
        for state in states:
            if state.domain not in allowed_domains:
                raise ValueError(
                    f"Entity domain {state.domain} is not enabled for snapshot undo"
                )
            captured[state.entity_id] = _serialize_state(state)
        if not captured:
            raise ValueError("Snapshot undo requires at least one entity")
        return cls(freeze_json_mapping(captured))

    def action_for_successful_entities(
        self,
        entity_ids: Collection[str],
        *,
        authorization: UndoScopeBinding,
        description: LocalizedDescription,
        expected_states: Iterable[State] | None = None,
    ) -> UndoAction:
        """Build an inverse containing only targets the forward action changed."""
        captured = thaw_json_mapping(self._states)
        missing = set(entity_ids).difference(captured)
        if missing:
            raise ValueError(
                f"Successful entities were not snapshotted: {sorted(missing)}"
            )
        restore: JsonObjectType = {
            entity_id: captured[entity_id] for entity_id in entity_ids
        }
        if not restore:
            raise ValueError("Snapshot undo requires at least one successful entity")

        expected: JsonObjectType | None = None
        if expected_states is not None:
            expected_by_id = {
                state.entity_id: _serialize_state(state) for state in expected_states
            }
            missing_expected = set(entity_ids).difference(expected_by_id)
            if missing_expected:
                raise ValueError(
                    "Successful entities have no expected post-action state: "
                    f"{sorted(missing_expected)}"
                )
            expected = {
                entity_id: expected_by_id[entity_id] for entity_id in entity_ids
            }

        return UndoAction(
            authorization=authorization,
            description=description,
            inverse=InverseOperation.state_snapshot(restore, expected=expected),
        )


def register_builtin_undo_executors(registry: UndoExecutorRegistry) -> None:
    """Register deterministic intent and state-snapshot inverse executors."""
    registry.register(INTENT_UNDO_EXECUTOR, _async_execute_intent)
    registry.register(STATE_SNAPSHOT_UNDO_EXECUTOR, _async_restore_state_snapshot)


async def _async_execute_intent(
    arguments: JsonObjectType,
    execution_context: UndoExecutionContext,
) -> None:
    """Execute a normalized inverse through Home Assistant's intent handler."""
    intent_type = arguments.get("intent_type")
    raw_slots = arguments.get("slots")
    if not isinstance(intent_type, str) or not isinstance(raw_slots, dict):
        raise TypeError("An intent inverse requires intent_type and slots")
    slots = {key: {"value": value} for key, value in raw_slots.items()}
    response = await intent.async_handle(
        execution_context.hass,
        platform=DOMAIN,
        intent_type=intent_type,
        slots=slots,
        context=execution_context.context,
        language=execution_context.language,
        assistant=execution_context.assistant,
        device_id=execution_context.device_id,
        satellite_id=execution_context.satellite_id,
    )
    if response.response_type is intent.IntentResponseType.ERROR:
        raise intent.IntentHandleError("The inverse intent returned an error")


async def _async_restore_state_snapshot(
    arguments: JsonObjectType,
    execution_context: UndoExecutionContext,
) -> None:
    """Fail on state drift, then reproduce the captured pre-action states."""
    restore = _deserialize_states(arguments.get("restore"))
    expected_value = arguments.get("expected")
    if expected_value is not None:
        expected = _deserialize_states(expected_value)
        for entity_id, expected_state in expected.items():
            current = execution_context.hass.states.get(entity_id)
            if current is None or not _same_state(current, expected_state):
                raise ValueError(
                    f"Entity {entity_id} changed after the original action"
                )

    await async_reproduce_state(
        execution_context.hass,
        restore.values(),
        context=execution_context.context,
    )


def _serialize_state(state: State) -> JsonObjectType:
    """Return the state and attributes needed by HA state reproduction."""
    return {
        "attributes": cast(JsonValueType, dict(state.attributes)),
        "state": state.state,
    }


def _deserialize_states(value: Any) -> dict[str, State]:
    """Validate a snapshot payload and reconstruct HA State objects."""
    if not isinstance(value, dict) or not value:
        raise ValueError("A state snapshot requires entity data")
    states: dict[str, State] = {}
    for entity_id, state_value in value.items():
        if not isinstance(entity_id, str) or not isinstance(state_value, dict):
            raise TypeError("Invalid state snapshot entity")
        state = state_value.get("state")
        attributes = state_value.get("attributes")
        if not isinstance(state, str) or not isinstance(attributes, dict):
            raise TypeError("Invalid state snapshot value")
        states[entity_id] = State(entity_id, state, attributes)
    return states


def _same_state(current: State, expected: State) -> bool:
    """Compare the complete reproducible snapshot conservatively."""
    return current.state == expected.state and dict(current.attributes) == dict(
        expected.attributes
    )


__all__ = [
    "INTENT_UNDO_EXECUTOR",
    "STATE_SNAPSHOT_UNDO_EXECUTOR",
    "UndoHelper",
    "register_builtin_undo_executors",
]
