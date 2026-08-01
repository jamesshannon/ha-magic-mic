"""Deep-copy JSON-compatible values into immutable provider-neutral records."""

from collections.abc import Mapping
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
    """Recursively copy and freeze a JSON mapping."""
    return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})


def thaw_json_mapping(value: Mapping[str, FrozenJsonValue]) -> JsonObjectType:
    """Return a new mutable JSON object from a frozen mapping."""
    return cast(
        JsonObjectType,
        {key: _thaw_json(item) for key, item in value.items()},
    )


def _freeze_json(value: JsonValueType | FrozenJsonValue) -> FrozenJsonValue:
    """Recursively copy and freeze one JSON value."""
    if isinstance(value, Mapping):
        return freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


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
