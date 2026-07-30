"""HA-aware adapter from entity states to scorer `Candidate`s.

The `fuzzy` primitive is deliberately `hass`-free (it scores opaque `Candidate`s), so
the step that turns a Home Assistant entity into a `Candidate` - gathering its names and
aliases and appending its location context - lives here, one level up, where `hass` and
the registries are available. It is shared by every resolver consumer that scores real
entities: the `find_entities` tool and prompt-context Tier-2 name injection both build
their candidate sets through `build_candidate`, so the load-bearing scoring-input shape
(per-alias documents, location on each - see `Candidate`) is defined once, not copied.

Provider-agnostic and core-shaped (§5.5): depends only on `hass`, HA registries, and the
`intent` helper.
"""

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    intent,
)

from .fuzzy import Candidate


class Registries:
    """The four registries a candidate build needs, fetched once per lookup."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Cache the registry handles for the duration of one lookup."""
        self.areas = ar.async_get(hass)
        self.devices = dr.async_get(hass)
        self.entities = er.async_get(hass)
        self.floors = fr.async_get(hass)


def build_candidate(
    hass: HomeAssistant, registries: Registries, state: State
) -> Candidate:
    """Build the scorer input for one entity: its names/aliases and location context.

    Names/aliases are matched as separate documents; the area (and floor) become shared
    context appended to each, so an entity named "Ceiling Light" in the "Kitchen"
    resolves for "kitchen light" without a bare area token resolving anything on its own
    (find-entities.md area matching).
    """
    entry = registries.entities.async_get(state.entity_id)
    names = intent.async_get_entity_aliases(hass, entry, state=state)
    context: list[str] = []
    if (area := resolve_area(registries, state.entity_id)) is not None:
        context.append(area.name)
        if area.floor_id and (
            floor := registries.floors.async_get_floor(area.floor_id)
        ):
            context.append(floor.name)
    return Candidate(names=tuple(names), context=tuple(context))


def resolve_area(registries: Registries, entity_id: str) -> ar.AreaEntry | None:
    """Resolve an entity's area, falling back to its device's area (as HA does)."""
    entry = registries.entities.async_get(entity_id)
    if entry is None:
        return None
    area_id = entry.area_id
    if area_id is None and entry.device_id is not None:
        if (device := registries.devices.async_get(entry.device_id)) is not None:
            area_id = device.area_id
    if area_id is None:
        return None
    return registries.areas.async_get_area(area_id)
