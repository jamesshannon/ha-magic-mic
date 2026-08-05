"""Consumer 1 of the resolver primitive: fuzzy fallback inside the match layer.

`docs/find-entities.md`. When an intent's exact name match fails
(`MatchFailedReason.NAME`), re-run the structured filters without the name to recover the
exposed candidate set HA had already narrowed, fuzzy-score the failed name against it, and
either resolve to a single canonical `entity_id` (so the model's next generation speaks a
normal confirmation and the user can still correct) or hand back a scored candidate list for
the model to ask about. The happy path is untouched: this runs only after an exact NAME miss,
so a correct name costs nothing, and the resolved retry rides the `tool_use` that was already
happening rather than adding a generation.

This is the component-side evidence for what the doc frames as a core change to the match
layer (behind an opt-in `fuzzy` constraint). Core-shaped (§5.5): depends only on `hass`,
`llm.LLMContext`, the `intent` helper, the shared scorer, and localized strings; never on the
conversation shell or the provider client.
"""

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from ..const import FIND_ENTITIES_DEFAULT_LIMIT
from ..entity_candidates import (
    Registries,
    device_area_id,
    entity_result,
    resolve_name_over_states,
)
from .localization import ConversationStrings


@dataclass(frozen=True)
class NameFallback:
    """Outcome of fuzzy-resolving an intent's failed name match.

    ``entity_id`` set means a decisive single winner: the caller retries the intent with the
    name replaced by this canonical id, which `_filter_by_name` matches exactly by
    construction (`intent.py:432`). ``tool_result`` set means ambiguous or not-found: the
    payload to hand the model instead of acting, so its next generation asks. Exactly one is
    set.
    """

    entity_id: str | None = None
    tool_result: JsonObjectType | None = None


def resolve_name_miss(
    hass: HomeAssistant,
    llm_context: llm.LLMContext,
    error: intent.MatchFailedError,
    strings: ConversationStrings,
    *,
    limit: int = FIND_ENTITIES_DEFAULT_LIMIT,
) -> NameFallback | None:
    """Fuzzy-resolve the name an intent failed to match, or return None to re-raise.

    Returns None when there is nothing to try, so the caller re-raises the original error:
    the failure was not a NAME miss, no name was given, or no assistant is configured to
    scope exposure. Otherwise returns a decisive `entity_id`, or a tool_result for the
    ambiguous and not-found cases.

    A named entity resolves house-wide, the way HA core's `_filter_by_name` matches before any
    area logic (`intent.py`): a uniquely named device resolves from any room. When the request
    carries no spoken area, the requesting room (from context, the satellite `device_id`) is
    applied only as a tiebreak for a genuinely ambiguous name, mirroring core's soft
    `preferred_area_id`. A spoken `area`/`floor` is honored as a hard scope, exactly as core
    would, so "the reading light in the kitchen" does not cross rooms.

    The model is expected to pass `area`/`floor` only when the user names a location, not to
    echo its own room onto a named request (see the prompt guidance in `localization`); if it
    does echo, the echoed room scopes the search and a device elsewhere misses, which is a
    prompt-adherence signal for the evals to catch rather than something this layer papers over.
    """
    if error.result.no_match_reason is not intent.MatchFailedReason.NAME:
        return None
    constraints = error.constraints
    name = constraints.name
    assistant = constraints.assistant or llm_context.assistant
    if not name or assistant is None:
        return None

    registries = Registries(hass)
    # A spoken area/floor scopes the search; absent one, the requesting room is a tiebreak only.
    scoped = bool(constraints.area_name) or bool(constraints.floor_name)
    prefer_area_id = (
        None if scoped else device_area_id(registries, llm_context.device_id)
    )

    # Recover the candidate set with the structured filters (domain/device_class/state/
    # exposure, plus any spoken area/floor) but without the name, so the fuzzy match runs over
    # the set the exact name match ran against.
    match = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(
            area_name=constraints.area_name,
            floor_name=constraints.floor_name,
            domains=constraints.domains,
            device_classes=constraints.device_classes,
            features=constraints.features,
            states=constraints.states,
            assistant=assistant,
            allow_duplicate_names=True,
        ),
    )
    if not match.is_match:
        return NameFallback(tool_result=_not_found(name, strings))

    resolution = resolve_name_over_states(
        hass, registries, match.states, name, prefer_area_id=prefer_area_id, limit=limit
    )
    if resolution.match is not None:
        return NameFallback(entity_id=resolution.match.key)

    rows = [
        entity_result(hass, registries, scored.key, score=scored.score)
        for scored in resolution.candidates
    ]
    if not rows:
        return NameFallback(tool_result=_not_found(name, strings))
    return NameFallback(
        tool_result={
            "candidates": rows,
            "error": "ambiguous_name",
            "error_text": strings.match_fallback_ambiguous.format(name=name),
            "success": False,
        }
    )


def _not_found(name: str, strings: ConversationStrings) -> JsonObjectType:
    """The tool_result when fuzzy scoring finds nothing above the floor."""
    return {
        "error": "name_not_found",
        "error_text": strings.match_fallback_not_found.format(name=name),
        "success": False,
    }
