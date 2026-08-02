"""Prompt-context: the bounded entity summary that replaces the entity roster.

The input half of the prompt-context primitive (PRODUCT_PLAN §5.6,
`docs/prompt-context.md` "Prompt budget"). Home Assistant's Assist API injects the
full exposed-entity roster as "Static Context" (names + aliases + domain + areas for
every exposed entity), re-prefilled per generation and scaling linearly with entity
count. This module builds the Tier-1 replacement: a floor → area → domain →
device-class summary with counts, bounded by home *structure* rather than entity
*count*. It grounds the model in what exists and where, so area/floor/domain
commands need no names, and tells it what to reach a lookup tool for. Tier-2
request-conditioned name injection rides the volatile tail elsewhere; this is the
stable, cacheable anchor.

Provider-agnostic and core-shaped: depends only on `hass`, the assistant id, and HA
registries (§5.5).
"""

from functools import lru_cache
import re

from hassil import normalize_text
from home_assistant_intents import get_intents, get_languages

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import language as language_util

from ..const import (
    FUZZY_TOKEN_MATCH_SCORE,
    NAME_INJECTION_FLOOR,
    NAME_INJECTION_HOUSE_FLOOR,
    NAME_INJECTION_ROOM_BONUS,
)
from ..entity_candidates import Registries, build_candidate, resolve_area
from ..fuzzy import Candidate, score, score_candidates, tokenize

# Leads the summary so the model reads the counts as structure, not as an
# actionable name list. Kept tool-agnostic on purpose: the lookup tool it points to
# (find_entities) is offered in the tool schema, so the prose need not name it.
ENTITY_SUMMARY_HEADER = (
    "Home structure below lists each area and how many devices of each type it "
    "contains. Specific device names are not included; look them up by name with "
    "the available tools when a command needs one."
)
UNASSIGNED_LABEL = "Unassigned"

# Leads the Tier-2 name block. Frames the names as a fast path (use directly) with the
# lookup tool as the fallback for anything not listed, so a miss is a lookup, not a dead
# end. Kept tool-agnostic like ENTITY_SUMMARY_HEADER.
NAME_INJECTION_HEADER = (
    "Entities relevant to this request, with their exact names and entity_ids. Use these "
    "directly when they fit; look up anything else by name with the available tools."
)

# entity_component translations flatten to component.<domain>.entity_component.<class>.name
# = the localized display name for a domain (class "_") or one of its device classes. We
# read those names to learn, per language, which words denote which domain.
_ENTITY_COMPONENT_NAME = re.compile(
    r"^component\.(?P<domain>[^.]+)\.entity_component\.[^.]+\.name$"
)


@callback
def async_build_entity_summary(hass: HomeAssistant, assistant: str) -> str:
    """Return the floor → area → domain → device-class summary with counts.

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

    lines = [ENTITY_SUMMARY_HEADER]
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


async def async_domain_keyword_map(
    hass: HomeAssistant, language: str
) -> dict[str, set[str]]:
    """Map localized domain/device-class name tokens to the domains they denote.

    Derived from HA's ``entity_component`` translations (each domain's display name plus
    its device-class names), so the Tier-2 keyword booster is localized per HA convention
    (PRODUCT_PLAN §5.7) rather than a hardcoded English dict, which would both fail
    non-English homes and block a core merge. The map is deliberately thin: it carries
    canonical names ("Light", "Blind"), not colloquial synonyms ("lamp", "music"), and
    those misses degrade to fuzzy-name match plus one ``find_entities`` lookup, never a
    failure (prompt-context.md Tier 2). A single token can denote several domains (a
    shared "sensor"), so values are sets.
    """
    translations = await async_get_translations(hass, language, "entity_component")
    keyword_map: dict[str, set[str]] = {}
    for key, value in translations.items():
        match = _ENTITY_COMPONENT_NAME.match(key)
        # Skip unresolved [%key%] references defensively; the cache normally resolves them.
        if match is None or "[%" in value:
            continue
        domain = match.group("domain")
        for token in tokenize(value):
            keyword_map.setdefault(token, set()).add(domain)
    return keyword_map


def keyword_domains(
    utterance: str,
    keyword_map: dict[str, set[str]],
    *,
    ignore_whitespace: bool = False,
) -> set[str]:
    """Return the domains whose localized name tokens the utterance mentions.

    Exact token hits win outright; otherwise each utterance token is compared to the map's
    tokens with the shared fuzzy scorer, so a plural or minor variant ("lights" ↔ "light",
    "blinds" ↔ "blind") still lands on its domain without a hardcoded stemmer.
    """
    domains: set[str] = set()
    if ignore_whitespace:
        compact_utterance = "".join(normalize_text(utterance).casefold().split())
        for map_token, map_domains in keyword_map.items():
            compact_token = "".join(normalize_text(map_token).casefold().split())
            if compact_token and compact_token in compact_utterance:
                domains |= map_domains

    for token in tokenize(utterance):
        if (exact := keyword_map.get(token)) is not None:
            domains |= exact
            continue
        for map_token, map_domains in keyword_map.items():
            if score(token, map_token) >= FUZZY_TOKEN_MATCH_SCORE:
                domains |= map_domains
    return domains


@callback
def select_request_names(
    hass: HomeAssistant,
    assistant: str,
    utterance: str,
    area_id: str | None,
    *,
    ignore_whitespace: bool = False,
    keyword_map: dict[str, set[str]],
    limit: int,
) -> str | None:
    """Return the Tier-2 request-conditioned name block, or None when nothing is relevant.

    Room scope is a soft prior, not a hard gate (prompt-context.md Tier 2, Refinement B).
    The candidate set is every exposed entity; where an entity sits decides how easily it
    qualifies:

    - **In the requesting area** (device area inherited as HA does): admitted at the normal
      floor, plus keyword widening (its domain being named floors it in even with no name
      match), plus a ranking bonus so it sorts above equal-scoring elsewhere.
    - **Elsewhere in the house**: admitted only above the higher house floor, so a strong
      explicit reference ("the kitchen ceiling light" from the living room) still gets a
      fast path while an incidental one-token overlap does not.
    - **No area at all** (typed input, no satellite): every entity is scored at the normal
      floor with no widening, since there is no room to prefer.

    Rendered as ``name (entity_id)`` lines, capped at ``limit``, most relevant first.
    """
    registries = Registries(hass)
    candidates: dict[str, Candidate] = {}
    domains: dict[str, str] = {}
    in_room: dict[str, bool] = {}
    for state in hass.states.async_all():
        if not async_should_expose(hass, assistant, state.entity_id):
            continue
        entity_id = state.entity_id
        area = resolve_area(registries, entity_id) if area_id is not None else None
        in_room[entity_id] = area is not None and area.id == area_id
        candidates[entity_id] = build_candidate(hass, registries, state)
        domains[entity_id] = state.domain

    if not candidates:
        return None

    fuzzy = {
        scored.key: scored.score for scored in score_candidates(utterance, candidates)
    }
    # Keyword widening applies only to in-room entities; unbounded it would inject a whole
    # domain, so it is empty when there is no room to bound it.
    widened = (
        keyword_domains(
            utterance,
            keyword_map,
            ignore_whitespace=ignore_whitespace,
        )
        if area_id is not None
        else set()
    )

    ranked: list[tuple[float, str]] = []
    for entity_id in candidates:
        relevance = fuzzy.get(entity_id, 0.0)
        if in_room[entity_id]:
            if domains[entity_id] in widened:
                # A domain match alone earns inclusion at the floor; a better name match
                # keeps its higher score and so still outranks bare keyword hits.
                relevance = max(relevance, NAME_INJECTION_FLOOR)
            if relevance >= NAME_INJECTION_FLOOR:
                ranked.append((relevance + NAME_INJECTION_ROOM_BONUS, entity_id))
        elif area_id is None:
            if relevance >= NAME_INJECTION_FLOOR:
                ranked.append((relevance, entity_id))
        elif relevance >= NAME_INJECTION_HOUSE_FLOOR:
            ranked.append((relevance, entity_id))

    if not ranked:
        return None

    ranked.sort(key=lambda row: (-row[0], row[1]))
    lines = [NAME_INJECTION_HEADER]
    for _relevance, entity_id in ranked[:limit]:
        state = hass.states.get(entity_id)
        name = state.name if state else entity_id
        lines.append(f"{name} ({entity_id})")
    return "\n".join(lines)


@lru_cache
def language_ignores_whitespace(language: str | None) -> bool:
    """Return Hassil's configured whitespace behavior for one HA language."""
    if language is None or not (
        matches := language_util.matches(language, set(get_languages()))
    ):
        return False
    if (intents := get_intents(matches[0])) is None:
        return False
    return bool(intents.get("settings", {}).get("ignore_whitespace", False))


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
