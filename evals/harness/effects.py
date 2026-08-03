"""Record durable or external effects that do not appear in HA entity state."""

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

_DATA_EFFECTS = "magic_mic_eval_effects"


@dataclass(frozen=True)
class ObservedEffect:
    """One durable or external effect produced while executing an eval turn."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


def effect_cursor(hass: HomeAssistant) -> int:
    """Return a cursor that can later isolate effects from the next turn."""
    return len(hass.data.setdefault(_DATA_EFFECTS, []))


def record_effect(hass: HomeAssistant, effect: ObservedEffect) -> None:
    """Append an effect from an instrumented eval fixture boundary."""
    hass.data.setdefault(_DATA_EFFECTS, []).append(effect)


def effects_since(hass: HomeAssistant, cursor: int) -> tuple[ObservedEffect, ...]:
    """Return effects recorded after ``cursor`` in execution order."""
    return tuple(hass.data.setdefault(_DATA_EFFECTS, [])[cursor:])


__all__ = ["ObservedEffect", "effect_cursor", "effects_since", "record_effect"]
