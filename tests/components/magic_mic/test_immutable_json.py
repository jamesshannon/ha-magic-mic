"""Tests for the validated immutable JSON boundary."""

import math

import pytest

from custom_components.magic_mic.immutable_json import (
    freeze_json_mapping,
    thaw_json_mapping,
)


def test_valid_values_are_copied_frozen_and_thawed_independently() -> None:
    """Valid aliases cannot mutate either the frozen or thawed records."""
    shared = [{"enabled": True, "level": 0.5, "value": None}]
    source = {"first": shared, "second": shared}

    frozen = freeze_json_mapping(source)
    shared[0]["enabled"] = False
    mutable = thaw_json_mapping(frozen)
    mutable["first"][0]["level"] = 1

    expected = ({"enabled": True, "level": 0.5, "value": None},)
    assert frozen["first"] == expected
    assert frozen["second"] == expected
    assert mutable["second"][0]["level"] == 0.5


@pytest.mark.parametrize("value", [b"bytes", {"set"}, object()])
def test_unsupported_nested_leaf_is_rejected(value: object) -> None:
    """Python-only values cannot cross the provider-neutral boundary."""
    with pytest.raises(TypeError, match="Unsupported JSON value type"):
        freeze_json_mapping({"outer": [value]})  # type: ignore[list-item]


def test_non_string_nested_key_is_rejected() -> None:
    """Every object key is validated, including keys below the root."""
    with pytest.raises(TypeError, match="JSON object keys must be strings"):
        freeze_json_mapping({"outer": {1: "value"}})  # type: ignore[dict-item]


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_non_finite_number_is_rejected(value: float) -> None:
    """Numbers that JSON cannot replay consistently fail at construction."""
    with pytest.raises(ValueError, match="JSON numbers must be finite"):
        freeze_json_mapping({"outer": [value]})


def test_circular_container_is_rejected() -> None:
    """A recursive Python container fails as invalid JSON instead of recursing."""
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(ValueError, match="circular reference"):
        freeze_json_mapping({"outer": circular})  # type: ignore[dict-item]
