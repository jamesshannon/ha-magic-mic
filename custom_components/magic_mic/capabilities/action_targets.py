"""Consumer 3 of the resolver primitive: entity arguments on tools that take ids.

`docs/find-entities.md` "Consumer 3" and [`core-deltas.md`](../../../docs/core-deltas.md)
CD1. A tool whose parameter schema declares an `EntitySelector` field asks the model for an
`entity_id`, and nothing in the prompt supplies one. Core resolves an `AreaSelector` field
from a name to an `area_id` before the service call (`helpers/llm.py:1011-1035`) but leaves
entity fields alone, so the model's invented id reaches `hass.services.async_call` and the
call targets nothing. This resolves those arguments first.

**Exact-first, and fuzzy never acts here.** Consumer 1 scores the user's own words after an
exact miss; this consumer scores an identifier the *model synthesized*, so a fuzzy hit would
resolve a guess to a real device. The ladder is three exact rungs (already-live id, exact
name, de-slugged id). When all three miss, fuzzy scoring runs only to populate the candidate
list the model is asked to choose from: it suggests, it does not resolve.

Core-shaped (§5.5): depends only on `hass`, `llm.LLMContext` / `llm.Tool`, HA helpers, the
shared scorer, and localized strings; never on the conversation shell or the provider client.
"""

from collections.abc import Callable
import copy
from dataclasses import dataclass, replace
from typing import Any, override

import voluptuous as vol

from homeassistant.core import HomeAssistant, split_entity_id, valid_entity_id
from homeassistant.helpers import intent, llm, selector
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
class ArgumentResolution:
    """Outcome of resolving a tool call's entity arguments.

    ``tool_args`` set means every entity argument now holds a canonical `entity_id` and the
    call proceeds with these arguments in place of the model's. ``tool_result`` set means at
    least one argument could not be resolved decisively: the payload to hand the model
    instead of executing, so its next generation asks or looks the entity up. Exactly one is
    set.
    """

    tool_args: dict[str, Any] | None = None
    tool_result: JsonObjectType | None = None


def resolve_entity_arguments(
    hass: HomeAssistant,
    llm_context: llm.LLMContext,
    tool: llm.Tool,
    tool_args: dict[str, Any],
    strings: ConversationStrings,
    *,
    limit: int = FIND_ENTITIES_DEFAULT_LIMIT,
) -> ArgumentResolution | None:
    """Resolve a call's `EntitySelector` arguments, or return None to leave it untouched.

    None means there is nothing to do and the caller uses the model's arguments verbatim:
    the tool declares no entity fields, no assistant is configured to scope exposure, or
    every supplied value is already a live `entity_id`. That last case is the
    backward-compatibility guarantee. A call that works today is byte-identical afterwards,
    because only a value that would otherwise have targeted nothing is ever rewritten.

    Runs *before* argument validation and policy evaluation, both deliberately. A friendly
    name is not a valid `entity_id`, so validating first would reject the very input this
    exists to interpret; and tool policy must judge the entity actually being acted on, not
    the model's guess at its id.
    """
    if llm_context.assistant is None:
        return None
    fields = _entity_fields(tool)
    if not fields:
        return None

    registries = Registries(hass)
    prefer_area_id = device_area_id(registries, llm_context.device_id)
    resolved_args = dict(tool_args)
    changed = False

    for field, entity_selector in fields.items():
        if (supplied := tool_args.get(field)) is None:
            continue
        values = supplied if isinstance(supplied, list) else [supplied]
        if not all(isinstance(value, str) for value in values):
            continue

        resolved_values: list[str] = []
        for value in values:
            outcome = _resolve_one(
                hass,
                registries,
                entity_selector,
                value,
                assistant=llm_context.assistant,
                prefer_area_id=prefer_area_id,
                limit=limit,
            )
            if isinstance(outcome, str):
                resolved_values.append(outcome)
                continue
            # A partial result must not execute: acting on three of four targets is worse
            # than acting on none, because the model cannot see which half ran.
            return ArgumentResolution(
                tool_result=_failure(field, value, outcome, strings)
            )

        if resolved_values != values:
            changed = True
        resolved_args[field] = (
            resolved_values if isinstance(supplied, list) else resolved_values[0]
        )

    if not changed:
        return None
    return ArgumentResolution(tool_args=resolved_args)


@dataclass(frozen=True, slots=True)
class _Scope:
    """The candidate set one argument may resolve within.

    Everything narrowing the search for a single field: the assistant whose exposure
    applies, the structured filters the author declared on the selector, the requesting
    room used as core's duplicate-name tiebreak, and the selector's explicit allow/deny
    lists. Carried as one value so each rung searches exactly the same space.
    """

    allowed: Callable[[str], bool]
    assistant: str
    device_classes: list[str] | None
    domains: list[str] | None
    prefer_area_id: str | None


def _resolve_one(
    hass: HomeAssistant,
    registries: Registries,
    entity_selector: selector.EntitySelector,
    value: str,
    *,
    assistant: str,
    prefer_area_id: str | None,
    limit: int,
) -> str | list[JsonObjectType]:
    """Resolve one supplied value to an `entity_id`, or return candidates to offer instead.

    The ladder, first rung that hits wins. Rungs 1 to 3 are exact and cannot invent a
    target; the fourth step only *suggests*.
    """
    # Rung 1: already a live entity id. The model got it right (or read it from
    # find_entities), so nothing is matched and nothing is rewritten.
    if valid_entity_id(value) and hass.states.get(value) is not None:
        return value

    domains, device_classes = _selector_filters(entity_selector)
    scope = _Scope(
        allowed=_allowed_predicate(entity_selector),
        assistant=assistant,
        device_classes=device_classes,
        domains=domains,
        prefer_area_id=prefer_area_id,
    )

    # Rung 2: the model passed a friendly name or alias instead of an id.
    if (hit := _match_exact(hass, value, scope)) is not None:
        return hit

    # Rung 3: the value is id-shaped but no such entity exists, which is the reported bug:
    # the model slugified a friendly name it saw. Un-slugify and match exactly, scoped to
    # the domain the model already committed to.
    if valid_entity_id(value):
        domain, object_id = split_entity_id(value)
        in_domain = replace(scope, domains=[domain])
        if (
            hit := _match_exact(hass, object_id.replace("_", " "), in_domain)
        ) is not None:
            return hit

    # Nothing matched exactly. Fuzzy scoring runs here only to name plausible candidates for
    # the model to choose between; it never resolves the argument itself.
    return _suggest(hass, registries, value, scope, limit=limit)


def _match_exact(hass: HomeAssistant, name: str, scope: _Scope) -> str | None:
    """Return the single entity exactly named ``name``, or None.

    Reuses HA's own matcher, so exposure filtering, alias handling, and duplicate-name
    disambiguation toward the requesting room are core's semantics rather than a second
    implementation. A duplicate name core cannot settle is not a match: it falls through to
    the candidate list, where the model asks.
    """
    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(
            assistant=scope.assistant,
            device_classes=scope.device_classes,
            domains=scope.domains,
            name=name,
        ),
        intent.MatchTargetsPreferences(area_id=scope.prefer_area_id),
    )
    if not result.is_match or len(result.states) != 1:
        return None
    entity_id = result.states[0].entity_id
    return entity_id if scope.allowed(entity_id) else None


def _suggest(
    hass: HomeAssistant,
    registries: Registries,
    value: str,
    scope: _Scope,
    *,
    limit: int,
) -> list[JsonObjectType]:
    """Fuzzy-rank the exposed candidates so the model can ask, without resolving anything."""
    result = intent.async_match_targets(
        hass,
        intent.MatchTargetsConstraints(
            allow_duplicate_names=True,
            assistant=scope.assistant,
            device_classes=scope.device_classes,
            domains=scope.domains,
        ),
    )
    if not result.is_match:
        return []
    states = [state for state in result.states if scope.allowed(state.entity_id)]
    if not states:
        return []

    # An id-shaped value scores badly as-is ("light.office_lamp" against "Office Lamp"), so
    # offer the de-slugged form as an alternative and keep whichever scores better.
    queries = [value]
    if valid_entity_id(value):
        queries.append(split_entity_id(value)[1].replace("_", " "))

    resolution = resolve_name_over_states(
        hass,
        registries,
        states,
        queries,
        prefer_area_id=scope.prefer_area_id,
        limit=limit,
    )
    scored = (
        [resolution.match] if resolution.match is not None else resolution.candidates
    )
    return [
        entity_result(hass, registries, candidate.key, score=candidate.score)
        for candidate in scored
    ]


def _failure(
    field: str,
    value: str,
    candidates: list[JsonObjectType],
    strings: ConversationStrings,
) -> JsonObjectType:
    """Build the tool_result for an argument that did not resolve to one entity."""
    ambiguous = bool(candidates)
    template = (
        strings.action_targets_ambiguous
        if ambiguous
        else strings.action_targets_not_found
    )
    result: JsonObjectType = {
        "argument": field,
        "error": "ambiguous_entity_argument"
        if ambiguous
        else "unresolved_entity_argument",
        "error_text": template.format(field=field, value=value),
        "success": False,
    }
    if ambiguous:
        result["candidates"] = candidates
    return result


class AnnotatedTool(llm.Tool):
    """A tool whose entity fields advertise that a spoken name is accepted.

    Prompt-side only. `async_call` delegates to the tool it wraps, so nothing about
    execution changes and the roster the proxy exposes stays interchangeable with the
    inner one.
    """

    def __init__(self, inner: llm.Tool, parameters: vol.Schema) -> None:
        """Wrap ``inner``, exposing ``parameters`` in place of its own schema."""
        self._inner = inner
        self.name = inner.name
        self.description = inner.description
        self.parameters = parameters

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Delegate to the wrapped tool; the annotation never reaches execution."""
        return await self._inner.async_call(hass, tool_input, llm_context)


def annotate_entity_arguments(tool: llm.Tool, strings: ConversationStrings) -> llm.Tool:
    """Return ``tool`` with its entity fields documenting that a name is accepted.

    Only meaningful where `resolve_entity_arguments` runs, and the caller is responsible
    for pairing them: advertising a name on a call that will not resolve one sends the
    model down a path that silently targets nothing, which is the failure this whole
    consumer exists to prevent.

    Without this, the affordance is invisible. Core serializes an `EntitySelector` to
    `{"type": "string", "format": "entity_id"}` (`helpers/llm.py:816`) and the field's own
    description is the script author's, written before any of this existed. A model reading
    that schema has no way to know a name would be accepted, so it spends a `find_entities`
    turn recovering an id the resolver would have found anyway. The hint is what turns
    Consumer 3 from a repair into a shortcut.

    Returns ``tool`` unchanged when it declares no entity fields, or when their schema keys
    carry no description to extend (a bare string key has nowhere to put one).
    """
    fields = _entity_fields(tool)
    if not fields:
        return tool
    schema = tool.parameters.schema
    if not isinstance(schema, dict):
        return tool

    annotated: dict[Any, Any] = {}
    changed = False
    for key, validator in schema.items():
        if str(key) in fields and isinstance(key, vol.Marker):
            # Copy the marker so the inner tool's schema is untouched; the copy keeps its
            # class (Required vs Optional) and any default.
            marker = copy.copy(key)
            marker.description = _extended_description(
                key.description, strings.action_targets_accepts_name
            )
            annotated[marker] = validator
            changed = True
        else:
            annotated[key] = validator
    if not changed:
        return tool
    return AnnotatedTool(tool, vol.Schema(annotated))


def _extended_description(existing: str | None, hint: str) -> str:
    """Append the hint to the author's own field description, if there is one."""
    if not existing:
        return hint
    separator = " " if existing.rstrip().endswith((".", "!", "?")) else ". "
    return f"{existing.rstrip()}{separator}{hint}"


def _entity_fields(tool: llm.Tool) -> dict[str, selector.EntitySelector]:
    """Return the tool's top-level `EntitySelector` parameters, keyed by field name.

    Top-level only, matching the depth at which core converts area and floor fields. A
    nested `TargetSelector` carries entity ids too and is deliberately out of scope
    (find-entities.md "Scope: EntitySelector now, TargetSelector as its own slice").
    """
    schema = getattr(tool.parameters, "schema", None)
    if not isinstance(schema, dict):
        return {}
    return {
        str(key): validator
        for key, validator in schema.items()
        if isinstance(validator, selector.EntitySelector)
    }


def _selector_filters(
    entity_selector: selector.EntitySelector,
) -> tuple[list[str] | None, list[str] | None]:
    """Read the author's own narrowing off the selector config, as structured filters.

    The field already declares what it accepts; resolving outside that would hand the
    service an entity it will reject.
    """
    config = entity_selector.config
    return _as_list(config.get("domain")), _as_list(config.get("device_class"))


def _allowed_predicate(
    entity_selector: selector.EntitySelector,
) -> Callable[[str], bool]:
    """Build the selector's explicit allow/deny check, which has no matcher equivalent.

    `domain` and `device_class` become `async_match_targets` constraints;
    `include_entities` and `exclude_entities` name entities outright, so they are applied
    to the result. Same authority as `EntitySelector.__call__` enforces
    (`helpers/selector.py:1018`), applied where resolution can respect it.
    """
    include = entity_selector.config.get("include_entities")
    exclude = entity_selector.config.get("exclude_entities")
    if not include and not exclude:
        return lambda entity_id: True

    def allowed(entity_id: str) -> bool:
        """Return whether the selector's explicit lists admit this entity."""
        if include and entity_id not in include:
            return False
        return not (exclude and entity_id in exclude)

    return allowed


def _as_list(value: str | list[str] | None) -> list[str] | None:
    """Normalize an optional string-or-list selector config value to a list, or None."""
    if value is None:
        return None
    return [value] if isinstance(value, str) else list(value)


__all__ = ["ArgumentResolution", "resolve_entity_arguments"]
