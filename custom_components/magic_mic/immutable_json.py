"""Validate and freeze JSON values into immutable provider-neutral records."""

from collections.abc import Mapping
import math
from types import MappingProxyType
from typing import cast

from homeassistant.util.json import JsonObjectType, JsonValueType

type FrozenJsonValue = (
    Mapping[str, FrozenJsonValue]
    | tuple[FrozenJsonValue, ...]
    | str
    | int
    | float
    | bool
    | None
)


def freeze_json_mapping(
    value: Mapping[str, JsonValueType | FrozenJsonValue],
) -> Mapping[str, FrozenJsonValue]:
    """Validate, recursively copy, and freeze a JSON mapping."""
    return _freeze_json_mapping(value, set())


def thaw_json_mapping(value: Mapping[str, FrozenJsonValue]) -> JsonObjectType:
    """Return a new mutable JSON object from a frozen mapping."""
    return cast(
        JsonObjectType,
        {key: _thaw_json(item) for key, item in value.items()},
    )


def _freeze_json(
    value: JsonValueType | FrozenJsonValue, active_containers: set[int]
) -> FrozenJsonValue:
    """Validate, recursively copy, and freeze one JSON value."""
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value, active_containers)
    if isinstance(value, (list, tuple)):
        container_id = _claim_container(value, active_containers)
        try:
            return tuple(_freeze_json(item, active_containers) for item in value)
        finally:
            active_containers.remove(container_id)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")


def _freeze_json_mapping(
    value: Mapping[str, JsonValueType | FrozenJsonValue],
    active_containers: set[int],
) -> Mapping[str, FrozenJsonValue]:
    """Validate and freeze a mapping while detecting circular references."""
    container_id = _claim_container(value, active_containers)
    try:
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    f"JSON object keys must be strings, got {type(key).__name__}"
                )
            frozen[key] = _freeze_json(item, active_containers)
        return MappingProxyType(frozen)
    finally:
        active_containers.remove(container_id)


def _claim_container(value: object, active_containers: set[int]) -> int:
    """Claim a container on the active recursion path."""
    container_id = id(value)
    if container_id in active_containers:
        raise ValueError("JSON value contains a circular reference")
    active_containers.add(container_id)
    return container_id


def _thaw_json(value: FrozenJsonValue) -> JsonValueType:
    """Recursively return mutable JSON containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "FrozenJsonValue",
    "freeze_json_mapping",
    "thaw_json_mapping",
]
