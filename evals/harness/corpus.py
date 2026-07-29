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


class CorpusError(ValueError):
    """Raised when a corpus file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Entity:
    """One exposed entity in the fixture home a case runs against."""

    entity_id: str
    name: str
    area: str | None = None
    device_class: str | None = None
    state: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class World:
    """The fixture home: the areas and entities exposed before a run."""

    areas: tuple[str, ...]
    entities: tuple[Entity, ...]

    def entity_ids(self) -> set[str]:
        """Return the set of entity ids present in the world."""
        return {entity.entity_id for entity in self.entities}


@dataclass(frozen=True)
class ExpectedTool:
    """An action the correct outcome invokes. ``args`` are partial hints, not exact."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedAnswer:
    """A predicate over the spoken response. Empty predicate matches anything."""

    contains: tuple[str, ...] = ()
    regex: str | None = None


@dataclass(frozen=True)
class Expected:
    """The correct outcome for a case: some tools, an answer predicate, or both."""

    tools: tuple[ExpectedTool, ...] = ()
    answer: ExpectedAnswer | None = None


@dataclass(frozen=True)
class Case:
    """One single-turn golden-set case.

    ``expected`` is the default, correct outcome, and the one the local (HASSIL) path is
    scored against. ``expected_llm`` overrides it for the LLM scope where the tool set
    genuinely differs: core's Assist API drops some intents (``GetCurrentTime``,
    ``GetState``, ``GetWeather``, …) in favor of the general ``GetDateTime`` /
    ``GetLiveContext`` tools, so on the LLM path the model reaches the same answer
    through a different call. Use ``expected_for(llm=...)`` to pick the right one.
    """

    id: str
    utterance: str
    category: str
    routing_truth: str
    resolves_at_wave0: bool
    requires: tuple[str, ...] = ()
    expected: Expected | None = None
    expected_llm: Expected | None = None
    template: str | None = None
    note: str | None = None

    def expected_for(self, *, llm: bool) -> Expected | None:
        """Return the expectation to score against for the given scope."""
        if llm and self.expected_llm is not None:
            return self.expected_llm
        return self.expected


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
    )


def _parse_world(raw: dict[str, Any]) -> World:
    return World(
        areas=tuple(raw.get("areas") or ()),
        entities=tuple(_parse_entity(entity) for entity in raw.get("entities") or ()),
    )


def _parse_expected(raw: dict[str, Any] | None) -> Expected | None:
    if not raw:
        return None
    tools = tuple(
        ExpectedTool(name=tool["name"], args=dict(tool.get("args") or {}))
        for tool in raw.get("tools") or ()
    )
    answer_raw = raw.get("answer")
    answer = (
        ExpectedAnswer(
            contains=tuple(answer_raw.get("contains") or ()),
            regex=answer_raw.get("regex"),
        )
        if answer_raw
        else None
    )
    return Expected(tools=tools, answer=answer)


def _parse_case(raw: dict[str, Any]) -> Case:
    return Case(
        id=raw["id"],
        utterance=raw["utterance"],
        category=raw["category"],
        routing_truth=raw["routing_truth"],
        resolves_at_wave0=bool(raw["resolves_at_wave0"]),
        requires=tuple(raw.get("requires") or ()),
        expected=_parse_expected(raw.get("expected")),
        expected_llm=_parse_expected(raw.get("expected_llm")),
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
        world=_parse_world(raw["world"]),
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

    world_ids = corpus.world.entity_ids()
    problems.extend(
        f"{case.id}: requires {required!r} absent from the fixture world"
        for case in corpus.cases
        for required in case.requires
        if required not in world_ids
    )

    if problems:
        raise CorpusError("; ".join(problems))
