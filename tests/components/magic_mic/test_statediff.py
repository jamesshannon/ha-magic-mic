"""Unit tests for the state-diff scorer (pure functions, no Home Assistant)."""

from evals.harness.corpus import StateChange
from evals.harness.statediff import ObservedState, unexpected_changes


def _snap(**entities: ObservedState) -> dict[str, ObservedState]:
    """Build a snapshot from keyword entity ids (dots written as double underscores)."""
    return {key.replace("__", "."): value for key, value in entities.items()}


def test_declared_state_reached_is_clean() -> None:
    """The declared entity reaching its stated state yields no diff."""
    before = _snap(cover__blinds=ObservedState("open", {}))
    after = _snap(cover__blinds=ObservedState("closed", {}))
    assert (
        unexpected_changes(
            before, after, {"cover.blinds": StateChange(state="closed")}, {}
        )
        == {}
    )


def test_declared_state_not_reached_fails() -> None:
    """A declared change the turn never made is flagged (the model did nothing)."""
    before = _snap(cover__blinds=ObservedState("open", {}))
    after = _snap(cover__blinds=ObservedState("open", {}))
    diffs = unexpected_changes(
        before, after, {"cover.blinds": StateChange(state="closed")}, {}
    )
    assert diffs == {"cover.blinds": {"state": {"expected": "closed", "got": "open"}}}


def test_equally_valid_tool_reaches_same_state() -> None:
    """Driving a cover to position 0 lands state 'closed' just like the close intent.

    This is the case tool-name matching needs any_of for: two tools, one outcome. State-diff
    passes both because it scores the state, not the tool.
    """
    before = _snap(cover__blinds=ObservedState("open", {"current_position": 100}))
    after = _snap(cover__blinds=ObservedState("closed", {"current_position": 0}))
    assert (
        unexpected_changes(
            before, after, {"cover.blinds": StateChange(state="closed")}, {}
        )
        == {}
    )


def test_declared_attribute_checked() -> None:
    """A named attribute must match; the state stays unchanged and is not flagged."""
    before = _snap(
        media__lr=ObservedState("playing", {"volume_level": 0.5}),
    )
    after = _snap(
        media__lr=ObservedState("playing", {"volume_level": 0.4}),
    )
    change = {"media.lr": StateChange(attributes={"volume_level": 0.4})}
    assert unexpected_changes(before, after, change, {}) == {}

    wrong = _snap(media__lr=ObservedState("playing", {"volume_level": 0.9}))
    diffs = unexpected_changes(before, wrong, change, {})
    assert diffs == {"media.lr": {"volume_level": {"expected": 0.4, "got": 0.9}}}


def test_string_and_numeric_values_coerced() -> None:
    """A declared 50 matches a stringified '50' from hass.states."""
    before = _snap(cover__blinds=ObservedState("open", {"current_position": 100}))
    after = _snap(cover__blinds=ObservedState("open", {"current_position": "50"}))
    change = {"cover.blinds": StateChange(attributes={"current_position": 50})}
    assert unexpected_changes(before, after, change, {}) == {}


def test_undeclared_side_effect_caught_by_state() -> None:
    """Turning off a light the case did not name shows up as an unexpected state change."""
    before = _snap(
        light__kitchen=ObservedState("on", {}),
        light__bedroom=ObservedState("on", {"color_mode": "brightness"}),
    )
    # The turn correctly turns off the kitchen light but also flips the bedroom light off.
    after = _snap(
        light__kitchen=ObservedState("off", {}),
        light__bedroom=ObservedState("off", {"color_mode": None}),
    )
    diffs = unexpected_changes(
        before, after, {"light.kitchen": StateChange(state="off")}, {}
    )
    assert diffs == {"light.bedroom": {"state": {"expected": "on", "got": "off"}}}


def test_derived_attribute_change_ignored() -> None:
    """A light's color_mode following its state off does not trip an undeclared entity."""
    before = _snap(light__bedroom=ObservedState("on", {"color_mode": "brightness"}))
    # Same state, only the derived attribute moved; state-only check on an undeclared entity.
    after = _snap(light__bedroom=ObservedState("on", {"color_mode": "brightness"}))
    assert unexpected_changes(before, after, {}, {}) == {}


def test_ignore_changes_suppresses_state_check() -> None:
    """Listing 'state' in ignore_changes drops that entity's state comparison."""
    before = _snap(climate__t=ObservedState("heat", {}))
    after = _snap(climate__t=ObservedState("cool", {}))
    change = {"climate.t": StateChange()}
    ignore = {"climate.t": ("state",)}
    assert unexpected_changes(before, after, change, ignore) == {}
