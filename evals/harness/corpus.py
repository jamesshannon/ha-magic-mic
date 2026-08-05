"""Load and validate the golden-set corpus.

The corpus is declarative YAML (see ``evals/README.md``). This turns it into typed,
immutable objects the runner and scorer consume, and checks internal consistency so a
malformed case fails loudly at load time rather than mid-run.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"
WAVE0_GOLDEN_SET = CORPUS_DIR / "wave0_golden_set.yaml"

ROUTING_LOCAL = "local"
ROUTING_LLM = "llm"
_ROUTING_VALUES = frozenset({ROUTING_LOCAL, ROUTING_LLM})

# Correlation regime for capability-selection measurement: how well a case's phrasing
# aligns with the target's configured metadata. `in_vocabulary` is the configuring user's
# own words (name/alias overlap is realistic and legitimate); `out_of_vocabulary` is a
# different speaker, a guest, or drift, whose words the configured triggers may not
# anticipate. Untagged cases (plain device control, reads) sit in neither.
PHRASING_IN_VOCAB = "in_vocabulary"
PHRASING_OUT_OF_VOCAB = "out_of_vocabulary"
_PHRASING_VALUES = frozenset({PHRASING_IN_VOCAB, PHRASING_OUT_OF_VOCAB})
_PROVIDER_OPTION_KEYS = frozenset({"web_fetch", "web_search"})
_READ_ONLY_SUPPORTING_TOOLS = frozenset({"GetDateTime", "GetLiveContext"})


class CorpusError(ValueError):
    """Raised when a corpus file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Entity:
    """One exposed entity in the fixture home a case runs against.

    ``aliases`` are the entity's alternate names / voice triggers (as an HA entity carries
    in its registry entry), and ``description`` its configured description. Both are
    retrieval signal for capability selection, not used by the live runner.
    """

    entity_id: str
    name: str
    area: str | None = None
    device_class: str | None = None
    state: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    description: str | None = None


@dataclass(frozen=True)
class World:
    """The fixture home: the areas and entities exposed before a run."""

    areas: tuple[str, ...]
    entities: tuple[Entity, ...]

    def entity_ids(self) -> set[str]:
        """Return the set of entity ids present in the world."""
        return {entity.entity_id for entity in self.entities}


@dataclass(frozen=True)
class StateChange:
    """A declared entity state and/or attributes, for ``setup`` or ``expect_changes``.

    ``state`` absent (``None``) means "leave the state as it was"; only the named
    attributes are asserted. Used both to stage a pre-turn starting point and to declare
    the post-turn outcome a device-control case is scored against.
    """

    state: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedTool:
    """A tool call pattern. Named args require exact normalized values."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedEffect:
    """A durable or external effect the correct outcome must create."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedAnswer:
    """A predicate over the spoken response. Empty predicate matches anything."""

    contains: tuple[str, ...] = ()
    regex: str | None = None


@dataclass(frozen=True)
class Expected:
    """The correct outcome for a case: some tools, an answer predicate, or both."""

    tools: tuple[ExpectedTool, ...] = ()
    supporting_tools: tuple[ExpectedTool, ...] = ()
    effects: tuple[ExpectedEffect, ...] = ()
    answer: ExpectedAnswer | None = None


@dataclass(frozen=True)
class ProviderOptions:
    """Provider-native capabilities enabled while a case runs."""

    web_fetch: bool = False
    web_search: bool = False

    def as_dict(self) -> dict[str, bool]:
        """Return config-entry options for this provider setup."""
        return {
            "web_fetch": self.web_fetch,
            "web_search": self.web_search,
        }


@dataclass(frozen=True)
class Case:
    """One single-turn golden-set case.

    ``expected`` is the default, correct outcome, and the one the local (HASSIL) path is
    scored against. ``expected_llm`` overrides it for the LLM scope where the tool set
    genuinely differs: core's Assist API drops some intents (``GetCurrentTime``,
    ``GetState``, ``GetWeather``, …) in favor of the general ``GetDateTime`` /
    ``GetLiveContext`` tools, so on the LLM path the model reaches the same answer
    through a different call. Use ``expected_for(llm=...)`` to pick the right one.

    Each is a tuple of acceptable outcomes (``any_of`` in the corpus, one-element for the
    single-outcome shorthand). A turn is correct when it matches any of them, so genuine
    ties (close a cover by ``HassTurnOff`` or by ``HassSetPosition: 0``) do not flip-flop
    pass/fail on model non-determinism. ``supporting_tools`` declares optional calls that
    may accompany an outcome; every other extra call fails it.

    ``expect_changes`` scores the case by the state it leaves the world in, rather than by
    the tool the model called (see ``statediff.py``). It is scope-invariant: the world ends
    the same whether HASSIL or the LLM acted, and whichever equally-valid tool got there, so
    a case scored this way needs no ``expected_llm`` or ``any_of`` tool gymnastics.
    ``setup`` stages a known pre-turn state; ``ignore_changes`` suppresses a per-entity state
    check (list ``state``) where the outcome is genuinely non-deterministic.
    ``permitted_tools`` is the corresponding allowlist for a state-scored case: state proves
    success, while the allowlist prevents an unrelated call from riding along.
    """

    id: str
    utterance: str
    category: str
    routing_truth: str
    resolves_at_wave0: bool
    phrasing: str | None = None
    # The area the requesting satellite sits in for this case, as an area key from the world
    # (e.g. "den"). Lets one corpus pair a same-room and a different-room variant of a request:
    # the room is context, so it changes where a bare name resolves and whether an echoed area
    # scopes away the target. ``None`` leaves the satellite at the run's default placement.
    satellite_area: str | None = None
    requires: tuple[str, ...] = ()
    provider_options: ProviderOptions = field(default_factory=ProviderOptions)
    expected: Expected | tuple[Expected, ...] = ()
    expected_llm: Expected | tuple[Expected, ...] | None = None
    permitted_tools: tuple[ExpectedTool, ...] = ()
    setup: dict[str, StateChange] = field(default_factory=dict)
    expect_changes: dict[str, StateChange] = field(default_factory=dict)
    ignore_changes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    template: str | None = None
    note: str | None = None

    def expected_for(self, *, llm: bool) -> tuple[Expected, ...]:
        """Return the acceptable outcomes to score against for the given scope."""
        if llm and self.expected_llm is not None:
            return as_alternatives(self.expected_llm)
        return as_alternatives(self.expected)

    @property
    def state_scored(self) -> bool:
        """Whether this case is scored by its resulting state (``expect_changes``)."""
        return bool(self.expect_changes)


@dataclass(frozen=True)
class Corpus:
    """A parsed corpus: the fixture world plus the cases that run against it."""

    world: World
    cases: tuple[Case, ...]


def _parse_entity(raw: dict[str, Any]) -> Entity:
    return Entity(
        entity_id=raw["entity_id"],
        name=raw["name"],
        area=raw.get("area"),
        device_class=raw.get("device_class"),
        state=raw.get("state"),
        attributes=dict(raw.get("attributes") or {}),
        aliases=tuple(raw.get("aliases") or ()),
        description=raw.get("description"),
    )


def parse_world(raw: dict[str, Any]) -> World:
    """Parse a corpus ``world`` block into a `World`.

    Public because the trajectory corpus (`trajectory.py`) declares the same world block.
    """
    return World(
        areas=tuple(raw.get("areas") or ()),
        entities=tuple(_parse_entity(entity) for entity in raw.get("entities") or ()),
    )


def as_alternatives(
    value: Expected | tuple[Expected, ...] | None,
) -> tuple[Expected, ...]:
    """Normalize an expectation into a tuple of acceptable outcomes.

    A single ``Expected`` (as the tests construct) becomes a one-element tuple; ``None``
    becomes empty. The scorer treats the tuple as a disjunction: a case is correct when
    any alternative matches.
    """
    if value is None:
        return ()
    if isinstance(value, Expected):
        return (value,)
    return tuple(value)


def _parse_tools(raw: list[dict[str, Any]] | None) -> tuple[ExpectedTool, ...]:
    """Parse tool call patterns from an expectation or state-scored allowlist."""
    return tuple(
        ExpectedTool(name=tool["name"], args=dict(tool.get("args") or {}))
        for tool in raw or ()
    )


def _parse_effects(raw: list[dict[str, Any]] | None) -> tuple[ExpectedEffect, ...]:
    """Parse durable-effect predicates from an outcome."""
    return tuple(
        ExpectedEffect(kind=effect["kind"], data=dict(effect.get("data") or {}))
        for effect in raw or ()
    )


def _parse_one(raw: dict[str, Any]) -> Expected:
    """Parse one acceptable outcome (tools and/or an answer predicate)."""
    answer_raw = raw.get("answer")
    answer = (
        ExpectedAnswer(
            contains=tuple(answer_raw.get("contains") or ()),
            regex=answer_raw.get("regex"),
        )
        if answer_raw
        else None
    )
    return Expected(
        tools=_parse_tools(raw.get("tools")),
        supporting_tools=_parse_tools(raw.get("supporting_tools")),
        effects=_parse_effects(raw.get("effects")),
        answer=answer,
    )


def _parse_expectation(raw: dict[str, Any] | None) -> tuple[Expected, ...]:
    """Parse an expectation block into its acceptable outcomes.

    ``{any_of: [<outcome>, ...]}`` lists alternatives (LLM non-determinism: a case may
    have several equally-valid resolutions). A plain ``{tools, answer}`` block is the
    single-outcome shorthand for a one-element ``any_of``.
    """
    if not raw:
        return ()
    if "any_of" in raw:
        alternatives = raw["any_of"]
        if not isinstance(alternatives, list) or not alternatives:
            raise CorpusError("'any_of' must be a non-empty list of outcomes")
        return tuple(_parse_one(alternative) for alternative in alternatives)
    return (_parse_one(raw),)


def _parse_state_change(raw: dict[str, Any]) -> StateChange:
    """Parse one entity's declared state and/or attributes.

    State is coerced to a string so it compares cleanly against ``hass.states`` values
    (which are always strings), letting the corpus write ``state: closed`` or ``state: 0``.
    """
    state = raw.get("state")
    return StateChange(
        state=None if state is None else str(state),
        attributes=dict(raw.get("attributes") or {}),
    )


def _parse_state_map(raw: dict[str, Any] | None) -> dict[str, StateChange]:
    """Parse an ``entity_id -> {state, attributes}`` mapping (setup / expect_changes)."""
    return {
        entity_id: _parse_state_change(change or {})
        for entity_id, change in (raw or {}).items()
    }


def _parse_ignore_changes(raw: dict[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """Parse an ``entity_id -> [attribute, ...]`` mapping of checks to suppress."""
    return {entity_id: tuple(attrs or ()) for entity_id, attrs in (raw or {}).items()}


def _parse_provider_options(raw: Any) -> ProviderOptions:
    """Parse the provider-specific setup for one case."""
    if raw is None:
        return ProviderOptions()
    if not isinstance(raw, dict):
        raise CorpusError("'provider_options' must be a mapping")
    unknown = sorted(set(raw) - _PROVIDER_OPTION_KEYS, key=str)
    if unknown:
        raise CorpusError(
            "unknown provider option(s): " + ", ".join(str(key) for key in unknown)
        )
    non_boolean = sorted(
        key for key, value in raw.items() if not isinstance(value, bool)
    )
    if non_boolean:
        raise CorpusError(
            "provider option(s) must be boolean: " + ", ".join(non_boolean)
        )
    return ProviderOptions(
        web_fetch=raw.get("web_fetch", False),
        web_search=raw.get("web_search", False),
    )


def _parse_case(raw: dict[str, Any]) -> Case:
    resolves_at_wave0 = raw.get("resolves_at_wave0")
    if not isinstance(resolves_at_wave0, bool):
        raise CorpusError(
            f"{raw.get('id', '<unknown>')}: resolves_at_wave0 must be a boolean"
        )
    return Case(
        id=raw["id"],
        utterance=raw["utterance"],
        category=raw["category"],
        routing_truth=raw["routing_truth"],
        resolves_at_wave0=resolves_at_wave0,
        phrasing=raw.get("phrasing"),
        satellite_area=raw.get("satellite_area"),
        requires=tuple(raw.get("requires") or ()),
        provider_options=_parse_provider_options(raw.get("provider_options")),
        expected=_parse_expectation(raw.get("expected")),
        expected_llm=(
            _parse_expectation(raw["expected_llm"]) if "expected_llm" in raw else None
        ),
        permitted_tools=_parse_tools(raw.get("permitted_tools")),
        setup=_parse_state_map(raw.get("setup")),
        expect_changes=_parse_state_map(raw.get("expect_changes")),
        ignore_changes=_parse_ignore_changes(raw.get("ignore_changes")),
        template=raw.get("template"),
        note=raw.get("note"),
    )


def load_corpus(path: Path = WAVE0_GOLDEN_SET) -> Corpus:
    """Load and validate a corpus file. Raises ``CorpusError`` on any inconsistency."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise CorpusError(f"cannot read corpus {path}: {err}") from err
    if not isinstance(raw, dict) or "world" not in raw or "cases" not in raw:
        raise CorpusError(f"corpus {path} must have 'world' and 'cases' keys")

    corpus = Corpus(
        world=parse_world(raw["world"]),
        cases=tuple(_parse_case(case) for case in raw["cases"]),
    )
    validate_corpus(corpus)
    return corpus


def validate_corpus(corpus: Corpus) -> None:
    """Check a corpus for the invariants the runner relies on.

    Verifies unique ids, known ``routing_truth`` values, and that every ``requires``
    entity exists in the fixture world. Raises ``CorpusError`` listing all problems.
    """
    problems: list[str] = []

    seen: set[str] = set()
    for case in corpus.cases:
        if case.id in seen:
            problems.append(f"duplicate case id: {case.id}")
        seen.add(case.id)
        if case.routing_truth not in _ROUTING_VALUES:
            problems.append(
                f"{case.id}: routing_truth must be one of "
                f"{sorted(_ROUTING_VALUES)}, got {case.routing_truth!r}"
            )
        if case.phrasing is not None and case.phrasing not in _PHRASING_VALUES:
            problems.append(
                f"{case.id}: phrasing must be one of "
                f"{sorted(_PHRASING_VALUES)}, got {case.phrasing!r}"
            )
        if not case.resolves_at_wave0 and (
            as_alternatives(case.expected)
            or case.expected_llm is not None
            or case.state_scored
            or case.permitted_tools
        ):
            problems.append(
                f"{case.id}: a Wave 0 unsupported case cannot declare a success "
                "predicate"
            )
        if case.state_scored and not case.permitted_tools:
            problems.append(
                f"{case.id}: a state-scored case must declare permitted_tools"
            )
        if not case.state_scored and case.permitted_tools:
            problems.append(
                f"{case.id}: permitted_tools is only valid for a state-scored case"
            )
        for outcome in (
            *as_alternatives(case.expected),
            *as_alternatives(case.expected_llm),
        ):
            problems.extend(
                f"{case.id}: supporting tool {tool.name!r} is not classified read-only"
                for tool in outcome.supporting_tools
                if tool.name not in _READ_ONLY_SUPPORTING_TOOLS
            )

    world_ids = corpus.world.entity_ids()
    problems.extend(
        f"{case.id}: requires {required!r} absent from the fixture world"
        for case in corpus.cases
        for required in case.requires
        if required not in world_ids
    )

    world_areas = set(corpus.world.areas)
    problems.extend(
        f"{case.id}: satellite_area {case.satellite_area!r} absent from the fixture world"
        for case in corpus.cases
        if case.satellite_area is not None and case.satellite_area not in world_areas
    )

    # Every entity a case stages, expects, or ignores must exist in the fixture world;
    # a diff against an absent entity would silently never fire.
    for case in corpus.cases:
        referenced = (
            set(case.setup) | set(case.expect_changes) | set(case.ignore_changes)
        )
        problems.extend(
            f"{case.id}: state-scored entity {entity_id!r} absent from the fixture world"
            for entity_id in sorted(referenced)
            if entity_id not in world_ids
        )

    if problems:
        raise CorpusError("; ".join(problems))
