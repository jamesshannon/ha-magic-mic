"""Prompt-context: the taxonomy skeleton that replaces the entity roster.

The input half of the prompt-context primitive (PRODUCT_PLAN §5.6,
`docs/prompt-context.md` "Prompt budget"). Home Assistant's Assist API injects the
full exposed-entity roster as "Static Context" (names + aliases + domain + areas for
every exposed entity), re-prefilled per generation and scaling linearly with entity
count. This module builds the Tier-1 replacement: a floor → area → domain →
device-class tree with counts, bounded by home *structure* rather than entity
*count*. It grounds the model in what exists and where, so area/floor/domain
commands need no names, and tells it what to reach a lookup tool for. Tier-2
request-conditioned name injection rides the volatile tail elsewhere; this is the
stable, cacheable anchor.

Provider-agnostic and core-shaped: depends only on `hass`, the assistant id, and HA
registries (§5.5).
"""

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

# Leads the skeleton so the model reads the counts as structure, not as an
# actionable name list. Kept tool-agnostic: the lookup tool it points to
# (find_entities) lands later in Wave 1.
SKELETON_HEADER = (
    "Home structure below lists each area and how many devices of each type it "
    "contains. Specific device names are not included; look them up by name with "
    "the available tools when a command needs one."
)
UNASSIGNED_LABEL = "Unassigned"


@callback
def async_build_taxonomy_skeleton(hass: HomeAssistant, assistant: str) -> str:
    """Return the floor → area → domain → device-class skeleton with counts.

    One line per area, prefixed by its floor when it has one, then a trailing
    ``Unassigned`` line for exposed entities with no area. Empty string when
    nothing is exposed (the caller then falls back to the no-entities prompt).
    """
    area_reg = ar.async_get(hass)
    floor_reg = fr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    # area_id (or None for unassigned) -> domain -> device_class (or None) -> count.
    counts: dict[str | None, dict[str, dict[str | None, int]]] = {}

    for state in hass.states.async_all():
        if not async_should_expose(hass, assistant, state.entity_id):
            continue
        area_id = _resolve_area_id(entity_reg, device_reg, state.entity_id)
        device_class = state.attributes.get("device_class")
        domain_counts = counts.setdefault(area_id, {}).setdefault(state.domain, {})
        domain_counts[device_class] = domain_counts.get(device_class, 0) + 1

    if not counts:
        return ""

    lines = [SKELETON_HEADER]
    rows: list[tuple[tuple[int, str, str], str]] = []
    unassigned_line: str | None = None

    for area_id, domain_map in counts.items():
        body = ", ".join(
            _render_domain(domain, domain_map[domain]) for domain in sorted(domain_map)
        )
        if area_id is None:
            unassigned_line = f"{UNASSIGNED_LABEL}: {body}"
            continue

        area = area_reg.async_get_area(area_id)
        area_name = area.name if area else area_id
        floor = (
            floor_reg.async_get_floor(area.floor_id) if area and area.floor_id else None
        )
        if floor:
            rows.append(
                (
                    (0, floor.name.casefold(), area_name.casefold()),
                    f"{floor.name} / {area_name}: {body}",
                )
            )
        else:
            rows.append(((1, "", area_name.casefold()), f"{area_name}: {body}"))

    rows.sort()
    lines.extend(line for _, line in rows)
    if unassigned_line is not None:
        lines.append(unassigned_line)
    return "\n".join(lines)


def _resolve_area_id(
    entity_reg: er.EntityRegistry,
    device_reg: dr.DeviceRegistry,
    entity_id: str,
) -> str | None:
    """Resolve an entity's area, falling back to its device's area (as HA does)."""
    entry = entity_reg.async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id is not None:
        return entry.area_id
    if entry.device_id is not None and (
        device := device_reg.async_get(entry.device_id)
    ):
        return device.area_id
    return None


def _render_domain(domain: str, device_class_counts: dict[str | None, int]) -> str:
    """Render one domain's count, breaking out device classes when present.

    ``light x4`` when the domain has no device classes; ``cover x3 (blind x2)`` when
    some do (the unclassed remainder is implied by the total).
    """
    total = sum(device_class_counts.values())
    classes = sorted(
        (device_class, count)
        for device_class, count in device_class_counts.items()
        if device_class is not None
    )
    if classes:
        detail = ", ".join(
            f"{device_class} x{count}" for device_class, count in classes
        )
        return f"{domain} x{total} ({detail})"
    return f"{domain} x{total}"
