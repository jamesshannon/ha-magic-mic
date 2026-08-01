"""find_entities: fuzzy name → canonical ``entity_id`` resolution as an LLM tool.

Consumer 2 of the resolver primitive in `docs/find-entities.md`: resolution decoupled
from an intent firing right now (authoring reminders/automations, browsing "what's in
the garage", targeting non-intent capabilities). Structured filters (area/floor/domain/
device_class/state/exposure) reuse `intent.async_match_targets` unchanged; the one thing
HA can't do exactly, fuzzy name matching, runs on top via the shared scorer + ambiguity
guard (`..fuzzy`). The match-layer fuzzy fallback for device control (Consumer 1) is a
separate, core-side change and does not live here.

Provider-agnostic and core-shaped (§5.5): depends only on `hass`, `llm.LLMContext` /
`ToolInput`, and HA helpers, never on the conversation shell or the Anthropic client.
"""

from typing import override

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, intent, llm
from homeassistant.util.json import JsonObjectType

from ..const import FIND_ENTITIES_DEFAULT_LIMIT, FIND_ENTITIES_MAX_LIMIT
from ..entity_candidates import Registries, build_candidate, resolve_area
from ..fuzzy import resolve_candidates
from ..tool_policy import EffectClass, StaticToolPolicy, tool_policy

# async_match_targets reports why a match produced nothing; these two mean the model
# named an area/floor that does not exist (a fixable mistake worth surfacing), as
# opposed to "the filters were valid, nothing matched" (an empty result, not an error).
_INVALID_FILTER_REASONS = {
    intent.MatchFailedReason.INVALID_AREA: "area",
    intent.MatchFailedReason.INVALID_FLOOR: "floor",
}


@tool_policy(StaticToolPolicy(effect=EffectClass.READ_ONLY))
class FindEntitiesTool(llm.Tool):
    """Resolve a fuzzy name and/or structured filters to canonical entity ids."""

    name = "find_entities"
    description = (
        "Look up Home Assistant entities and their entity_ids by fuzzy name and/or by "
        "structured filters (area, floor, domain, device class, state). Use it when "
        "you need entity_ids as data rather than to act now: authoring a reminder or "
        "automation, listing what exists in an area, or resolving an approximate name "
        "before another step. Returns scored candidates; when several are close it "
        "flags them ambiguous so you can ask which one."
    )
    parameters = vol.Schema(
        {
            vol.Optional(
                "name",
                description="Approximate entity name or alias; matched fuzzily.",
            ): cv.string,
            vol.Optional(
                "area",
                description="Restrict to an area by name, id, or alias.",
            ): cv.string,
            vol.Optional(
                "floor",
                description="Restrict to a floor by name, id, or alias.",
            ): cv.string,
            vol.Optional(
                "domain",
                description="Restrict by domain (e.g. 'light'); one or a list.",
            ): vol.Any(cv.string, [cv.string]),
            vol.Optional(
                "device_class",
                description="Restrict by device class (e.g. 'blind'); one or a list.",
            ): vol.Any(cv.string, [cv.string]),
            vol.Optional(
                "state",
                description="Restrict to entities currently in this state.",
            ): cv.string,
            vol.Optional(
                "limit",
                description="Maximum candidates to return.",
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=FIND_ENTITIES_MAX_LIMIT)),
        }
    )

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Resolve the request to a scored, guarded candidate list."""
        if llm_context.assistant is None:
            return {"success": False, "error": "No assistant configured"}

        args = self.parameters(tool_input.tool_args)
        name = args.get("name")
        limit = args.get("limit", FIND_ENTITIES_DEFAULT_LIMIT)

        # Everything except the name goes to HA's matcher, exact and exposure-aware;
        # allow_duplicate_names keeps same-named entities as distinct candidates so the
        # fuzzy pass (or the caller) can choose between them.
        match_result = intent.async_match_targets(
            hass,
            intent.MatchTargetsConstraints(
                area_name=args.get("area"),
                floor_name=args.get("floor"),
                domains=_as_list(args.get("domain")),
                device_classes=_as_list(args.get("device_class")),
                states=_as_list(args.get("state")),
                assistant=llm_context.assistant,
                allow_duplicate_names=True,
            ),
        )

        if not match_result.is_match:
            if (
                bad := _INVALID_FILTER_REASONS.get(match_result.no_match_reason)
            ) is not None:
                return {
                    "success": False,
                    "error": f"No {bad} named {match_result.no_match_name!r} exists",
                }
            return {"success": True, "results": []}

        registries = Registries(hass)
        if name is None:
            # Pure structured list: return the matched set as-is, most relevant first
            # is undefined, so keep HA's order and just cap it.
            results = [
                _entity_result(hass, registries, state.entity_id)
                for state in match_result.states[:limit]
            ]
            return {"success": True, "results": results}

        candidates = {
            state.entity_id: build_candidate(hass, registries, state)
            for state in match_result.states
        }
        resolution = resolve_candidates(name, candidates, limit)

        chosen = (
            [resolution.match]
            if resolution.match is not None
            else resolution.candidates
        )
        results = [
            _entity_result(hass, registries, scored.key, score=scored.score)
            for scored in chosen
        ]
        response: JsonObjectType = {"success": True, "results": results}
        if resolution.ambiguous:
            response["ambiguous"] = True
        return response


def async_get_tools(hass: HomeAssistant, llm_context: llm.LLMContext) -> list[llm.Tool]:
    """Return this capability's tools, in the core `llm.py` platform shape (§5.5)."""
    return [FindEntitiesTool()]


def _entity_result(
    hass: HomeAssistant,
    registries: Registries,
    entity_id: str,
    score: float | None = None,
) -> JsonObjectType:
    """Build one result row: id, friendly name, place, domain, state, optional score."""
    state = hass.states.get(entity_id)
    result: JsonObjectType = {
        "entity_id": entity_id,
        "name": state.name if state else entity_id,
        "domain": entity_id.partition(".")[0],
    }
    if state is not None:
        result["state"] = state.state

    if (area := resolve_area(registries, entity_id)) is not None:
        result["area"] = area.name
        if area.floor_id and (
            floor := registries.floors.async_get_floor(area.floor_id)
        ):
            result["floor"] = floor.name

    if score is not None:
        result["score"] = round(score, 1)
    return result


def _as_list(value: str | list[str] | None) -> list[str] | None:
    """Normalize an optional string-or-list filter to a list, or None."""
    if value is None:
        return None
    return [value] if isinstance(value, str) else value
