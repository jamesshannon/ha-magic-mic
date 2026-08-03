"""Score a device-control turn by the state it leaves the world in.

Adapted from home-assistant-datasets' entity-state diffing (Apache-2.0,
``home_assistant_datasets/entity_state/diff.py``): snapshot entity states, apply a case's
declared ``expect_changes`` to the snapshot to form the expected end state, then flag any
entity that differs from it. This scores the *outcome in the world*, so it is immune to the
model reaching that outcome through an equally-valid tool (closing a cover via
``HassTurnOff`` or ``HassSetPosition``), the brittleness tool-name matching needs ``any_of``
to absorb.

For an entity a case *declares* in ``expect_changes`` we check its state (unless suppressed)
and every attribute named in the change. For every other exposed entity we check state plus
a small domain-specific set of reproducible action attributes. This catches a wrong-target
brightness, position, setpoint, or volume change without comparing every noisy derived
attribute. Any entity created during the turn is forbidden unless explicitly declared.
``ignore_changes`` suppresses a per-entity check by name, or the whole state check via the
literal ``state``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import HomeAssistant

from .corpus import StateChange

# Cover/valve state and these attributes are two views of one fact: is_closed follows from
# state and position and is never declared, so a correctly actuated device would trip a
# phantom diff. State and (declared) position still gate the result.
_DERIVED_ATTRIBUTES = frozenset({"is_closed", "is_opening", "is_closing"})
_MEANINGFUL_ATTRIBUTES: dict[str, frozenset[str]] = {
    "climate": frozenset({"temperature"}),
    "cover": frozenset({"current_position", "current_tilt_position"}),
    "light": frozenset({"brightness"}),
    "media_player": frozenset({"is_volume_muted", "volume_level"}),
}


@dataclass(frozen=True)
class ObservedState:
    """A snapshot of one entity: its state string and attributes at a point in time."""

    state: str
    attributes: dict[str, Any]
    exposed: bool = True


def snapshot(hass: HomeAssistant) -> dict[str, ObservedState]:
    """Capture all states, marking which entities are exposed to the assistant.

    Existing unexposed infrastructure entities are ignored by the diff. Capturing their ids
    still lets the diff distinguish them from an entity newly created during the turn.
    """
    return {
        state.entity_id: ObservedState(
            state.state,
            dict(state.attributes),
            async_should_expose(hass, conversation.DOMAIN, state.entity_id),
        )
        for state in hass.states.async_all()
    }


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two state or attribute values, coercing equivalent shapes.

    Lists and tuples compare element-wise; everything else falls back to string equality so
    ``50`` matches ``"50"`` (``hass.states`` stringifies) without a type mismatch.
    """
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return list(a) == list(b)
        return False
    return a == b or str(a) == str(b)


def _expected_state(before: ObservedState | None, change: StateChange) -> ObservedState:
    """Apply a declared change onto an entity's pre-turn snapshot."""
    base_state = before.state if before else ""
    base_attributes = dict(before.attributes) if before else {}
    return ObservedState(
        state=change.state if change.state is not None else base_state,
        attributes={**base_attributes, **change.attributes},
    )


def _diff_declared(
    expected: ObservedState,
    actual: ObservedState | None,
    change: StateChange,
    ignore: frozenset[str],
) -> dict[str, Any]:
    """Diff an entity a case declares: its state plus each named attribute."""
    if actual is None:
        return {"expected": expected.state, "got": None}
    diff: dict[str, Any] = {}
    if "state" not in ignore and not _values_equal(expected.state, actual.state):
        diff["state"] = {"expected": expected.state, "got": actual.state}
    for name in change.attributes:
        if name in ignore:
            continue
        want = expected.attributes.get(name)
        got = actual.attributes.get(name)
        if not _values_equal(want, got):
            diff[name] = {"expected": want, "got": got}
    return diff


def _diff_undeclared(
    entity_id: str,
    before: ObservedState,
    actual: ObservedState | None,
    ignore: frozenset[str],
) -> dict[str, Any]:
    """Diff an undeclared exposed entity on state and meaningful action attributes."""
    if actual is None:
        return {"removed": {"expected": before.state, "got": None}}
    diff: dict[str, Any] = {}
    if "state" not in ignore and not _values_equal(before.state, actual.state):
        diff["state"] = {"expected": before.state, "got": actual.state}
    domain = entity_id.partition(".")[0]
    for name in _MEANINGFUL_ATTRIBUTES.get(domain, ()):
        if name in ignore:
            continue
        want = before.attributes.get(name)
        got = actual.attributes.get(name)
        if not _values_equal(want, got):
            diff[name] = {"expected": want, "got": got}
    return diff


def unexpected_changes(
    before: dict[str, ObservedState],
    after: dict[str, ObservedState],
    expect_changes: Mapping[str, StateChange],
    ignore_changes: Mapping[str, tuple[str, ...]],
) -> dict[str, dict[str, Any]]:
    """Return the entities whose post-turn state does not match the expectation.

    Empty means the turn left the world exactly as the case declared: every ``expect_changes``
    entity reached its stated state and named attributes, and no other entity's state moved.
    A non-empty result is the reason the case fails, keyed by entity id.
    """
    diffs: dict[str, dict[str, Any]] = {}
    for entity_id in set(before) | set(after) | set(expect_changes):
        ignore = frozenset(ignore_changes.get(entity_id, ())) | _DERIVED_ATTRIBUTES
        actual = after.get(entity_id)
        if entity_id in expect_changes:
            change = expect_changes[entity_id]
            expected = _expected_state(before.get(entity_id), change)
            diff = _diff_declared(expected, actual, change, ignore)
        elif (base := before.get(entity_id)) is None:
            diff = (
                {"created": {"expected": None, "got": actual.state}}
                if actual is not None
                else {}
            )
        elif base.exposed:
            diff = _diff_undeclared(entity_id, base, actual, ignore)
        else:
            diff = {}
        if diff:
            diffs[entity_id] = diff
    return diffs
