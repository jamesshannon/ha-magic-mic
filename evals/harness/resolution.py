"""The resolver micro-benchmark: a pure, model-free scorer eval.

Distinct from the golden set (Parts D-E, LLM-in-the-loop) and from `test_find_entities`
(a handful of tool-level integration tests against a real `hass`). This is the
deterministic scorer benchmark evaluation.md Part G names: a large labeled
``(query, candidates) -> expected ranking + guard decision`` set run straight through
the pure scorer (`custom_components.magic_mic.fuzzy`), with no Home Assistant world at
all. Entity *state / exposure* and structured filtering are `async_match_targets`'
concern (HA's exact code, not what we tune); the scorer sees only each candidate's
descriptive phrases (names, aliases, area, floor), so a case is plain data and runs in
microseconds. That is what lets this scale to thousands of cases and A/B two scorers
cheaply.

Cases are grouped under a named ``home`` - the candidate set the scorer is handed, i.e.
what `async_match_targets` already returned. A home therefore stands in for a regime: a
whole small house, or the set left after an area filter (the corpus an IDF-weighted
scorer derives term weights from). Richer scorers slot in behind the same `Resolver`
seam without touching the corpus.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

import yaml

from custom_components.magic_mic.const import FIND_ENTITIES_DEFAULT_LIMIT
from custom_components.magic_mic.fuzzy import Resolution, resolve_candidates

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus" / "resolution"
SEED_SET = CORPUS_DIR / "seed.yaml"

# A resolver maps (query, {entity_id: phrases}, limit) to a guard Resolution, where
# phrases are a candidate's names/aliases plus location terms. The default is the
# production scorer; a candidate variant (IDF-weighted, phonetic) is a drop-in.
type Resolver = Callable[[str, dict[str, list[str]], int], Resolution]


class ResolutionCorpusError(ValueError):
    """Raised when a resolution corpus file is malformed or inconsistent."""


class Expectation(Enum):
    """Which kind of outcome a case asserts."""

    RESOLVES_TO = auto()
    AMBIGUOUS = auto()
    NONE = auto()


class Outcome(Enum):
    """How a case actually turned out, once scored.

    The split that matters for tuning is FALSE_RESOLVE (decisively acted on the *wrong*
    entity, the one genuinely dangerous failure) versus MISSED (should have resolved but
    asked instead, merely a lost turn). The two are aggregated into different metrics.
    """

    CORRECT_RESOLVE = auto()
    FALSE_RESOLVE = auto()
    MISSED_IN_SHORTLIST = auto()
    MISSED_ENTIRELY = auto()
    CORRECT_AMBIGUOUS = auto()
    WRONG_AMBIGUOUS = auto()
    CORRECT_NONE = auto()
    WRONG_NONE = auto()

    @property
    def passed(self) -> bool:
        """Whether this outcome matched the case's expectation."""
        return self in _PASSING_OUTCOMES

    @property
    def is_false_resolve(self) -> bool:
        """Whether the scorer decisively acted on the wrong entity (the unsafe case)."""
        return self is Outcome.FALSE_RESOLVE


_PASSING_OUTCOMES = frozenset(
    {Outcome.CORRECT_RESOLVE, Outcome.CORRECT_AMBIGUOUS, Outcome.CORRECT_NONE}
)


@dataclass(frozen=True)
class ResEntity:
    """One candidate entity: its id, its names/aliases, and where it lives.

    ``area`` / ``floor`` are the entity's location. The current name-only scorer does not
    read them (a location case therefore misses, quantifying the gap); they exist so the
    corpus can express the truth a location-aware scorer will match against - the
    discriminating token for "kitchen light" is often the area, not the name.
    """

    entity_id: str
    names: tuple[str, ...]
    area: str | None = None
    floor: str | None = None


@dataclass(frozen=True)
class ResCase:
    """One labeled resolution case: a query against a home, and the expected outcome.

    ``tags`` group cases into regimes (e.g. "small-home", "area-filtered",
    "shared-word") so the scorecard can report accuracy per regime, not just blended;
    that is what shows whether a scorer change helps one regime while regressing another.
    """

    id: str
    home: str
    query: str
    expectation: Expectation
    resolves_to: str | None = None
    ambiguous: tuple[str, ...] = ()
    note: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResCorpus:
    """A parsed resolution corpus: named homes plus the cases that run against them."""

    homes: dict[str, tuple[ResEntity, ...]]
    cases: tuple[ResCase, ...]

    def candidates(self, home: str) -> dict[str, list[str]]:
        """Return the ``{entity_id: phrases}`` candidate map for a home.

        Each candidate's phrases are its names plus its location terms (area, floor).
        The scorer joins them into one descriptive document, so the discriminating token
        can live in the registry (area "Kitchen") rather than the name ("Ceiling Light").
        This mirrors what the find_entities tool feeds the same scorer.
        """
        return {
            entity.entity_id: [
                *entity.names,
                *(term for term in (entity.area, entity.floor) if term),
            ]
            for entity in self.homes[home]
        }


@dataclass(frozen=True)
class CaseEval:
    """The scored result of one case: its outcome and what the scorer returned."""

    case: ResCase
    outcome: Outcome
    resolved_to: str | None
    shortlist: tuple[str, ...]


@dataclass(frozen=True)
class Scorecard:
    """Aggregate metrics over a run, plus the per-case results behind them."""

    results: tuple[CaseEval, ...]

    @property
    def total(self) -> int:
        """Total cases scored."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Cases whose outcome matched their expectation."""
        return sum(1 for result in self.results if result.outcome.passed)

    @property
    def false_resolves(self) -> int:
        """Cases where the scorer decisively acted on the wrong entity (unsafe)."""
        return sum(1 for result in self.results if result.outcome.is_false_resolve)

    @property
    def false_resolve_rate(self) -> float:
        """Fraction of all cases that were false resolves (the headline safety metric)."""
        return self.false_resolves / self.total if self.total else 0.0

    def _count(self, *outcomes: Outcome) -> int:
        wanted = frozenset(outcomes)
        return sum(1 for result in self.results if result.outcome in wanted)

    @property
    def decisive_accuracy(self) -> float:
        """Of cases that should resolve, the fraction decisively resolved correctly."""
        expecting = self._expecting(Expectation.RESOLVES_TO)
        if not expecting:
            return 0.0
        return self._count(Outcome.CORRECT_RESOLVE) / expecting

    @property
    def resolve_recall(self) -> float:
        """Of cases that should resolve, the fraction whose target was returned at all.

        Counts a correct decisive resolve or a shortlist that still contained the target
        (recoverable by one clarifying question), but not a false resolve or a total miss.
        """
        expecting = self._expecting(Expectation.RESOLVES_TO)
        if not expecting:
            return 0.0
        recovered = self._count(Outcome.CORRECT_RESOLVE, Outcome.MISSED_IN_SHORTLIST)
        return recovered / expecting

    @property
    def ambiguous_accuracy(self) -> float:
        """Of cases that should be ambiguous, the fraction flagged ambiguous correctly."""
        expecting = self._expecting(Expectation.AMBIGUOUS)
        if not expecting:
            return 0.0
        return self._count(Outcome.CORRECT_AMBIGUOUS) / expecting

    @property
    def none_accuracy(self) -> float:
        """Of cases that should match nothing, the fraction that returned nothing."""
        expecting = self._expecting(Expectation.NONE)
        if not expecting:
            return 0.0
        return self._count(Outcome.CORRECT_NONE) / expecting

    def _expecting(self, expectation: Expectation) -> int:
        return sum(
            1 for result in self.results if result.case.expectation is expectation
        )

    def failures(self) -> tuple[CaseEval, ...]:
        """Return the case evals that did not pass, for reporting."""
        return tuple(result for result in self.results if not result.outcome.passed)

    def tag_breakdown(self) -> dict[str, tuple[int, int, int]]:
        """Per-regime ``tag -> (passed, total, false_resolves)``, tags sorted.

        A case counts under each of its tags, so tags overlap by design. This is how a
        scorer change is judged: a lift in one regime must not hide a regression in
        another (e.g. shared-word accuracy up while small-home accuracy drops).
        """
        tags = sorted({tag for result in self.results for tag in result.case.tags})
        breakdown: dict[str, tuple[int, int, int]] = {}
        for tag in tags:
            tagged = [r for r in self.results if tag in r.case.tags]
            passed = sum(1 for r in tagged if r.outcome.passed)
            false_resolves = sum(1 for r in tagged if r.outcome.is_false_resolve)
            breakdown[tag] = (passed, len(tagged), false_resolves)
        return breakdown


def default_resolver(
    query: str, candidates: dict[str, list[str]], limit: int
) -> Resolution:
    """The production scorer: union `token_set_ratio` with the IDF tie-break on top."""
    return resolve_candidates(query, candidates, limit)


def run(
    corpus: ResCorpus,
    resolver: Resolver = default_resolver,
    limit: int = FIND_ENTITIES_DEFAULT_LIMIT,
) -> Scorecard:
    """Score every case with the given resolver and return the aggregate scorecard."""
    return Scorecard(
        results=tuple(
            evaluate_case(case, corpus.candidates(case.home), resolver, limit)
            for case in corpus.cases
        )
    )


def evaluate_case(
    case: ResCase,
    candidates: dict[str, list[str]],
    resolver: Resolver = default_resolver,
    limit: int = FIND_ENTITIES_DEFAULT_LIMIT,
) -> CaseEval:
    """Score one case: run the resolver and classify its result against the expectation."""
    resolution = resolver(case.query, candidates, limit)
    resolved_to = resolution.match.key if resolution.match is not None else None
    shortlist = tuple(scored.key for scored in resolution.candidates)
    outcome = _classify(case, resolution, resolved_to, shortlist)
    return CaseEval(case, outcome, resolved_to, shortlist)


def _classify(
    case: ResCase,
    resolution: Resolution,
    resolved_to: str | None,
    shortlist: tuple[str, ...],
) -> Outcome:
    """Map a resolver result onto an Outcome relative to the case's expectation."""
    if case.expectation is Expectation.RESOLVES_TO:
        if resolved_to is not None:
            if resolved_to == case.resolves_to:
                return Outcome.CORRECT_RESOLVE
            return Outcome.FALSE_RESOLVE
        if case.resolves_to in shortlist:
            return Outcome.MISSED_IN_SHORTLIST
        return Outcome.MISSED_ENTIRELY

    if case.expectation is Expectation.AMBIGUOUS:
        if resolution.ambiguous and set(case.ambiguous) <= set(shortlist):
            return Outcome.CORRECT_AMBIGUOUS
        return Outcome.WRONG_AMBIGUOUS

    # Expectation.NONE: nothing should clear the floor.
    if resolved_to is None and not resolution.ambiguous:
        return Outcome.CORRECT_NONE
    return Outcome.WRONG_NONE


def format_scorecard(scorecard: Scorecard) -> str:
    """Render the scorecard as a short, human-readable report."""
    lines = [
        f"cases:              {scorecard.total}",
        f"passed:             {scorecard.passed}/{scorecard.total}",
        (
            f"false-resolve rate: {scorecard.false_resolve_rate:.1%} "
            f"({scorecard.false_resolves})"
        ),
        f"decisive accuracy:  {scorecard.decisive_accuracy:.1%}",
        f"resolve recall:     {scorecard.resolve_recall:.1%}",
        f"ambiguous accuracy: {scorecard.ambiguous_accuracy:.1%}",
        f"none accuracy:      {scorecard.none_accuracy:.1%}",
    ]
    breakdown = scorecard.tag_breakdown()
    if breakdown:
        lines.append("by regime (passed/total, false-resolves):")
        lines.extend(
            f"  {tag:<16} {passed}/{total}" + (f"  [{fr} FALSE-RESOLVE]" if fr else "")
            for tag, (passed, total, fr) in breakdown.items()
        )
    failures = scorecard.failures()
    if failures:
        lines.append(f"failures ({len(failures)}):")
        lines.extend(
            f"  [{result.outcome.name}] {result.case.id}: {result.case.query!r} "
            f"-> {result.resolved_to or list(result.shortlist)}"
            for result in failures
        )
    return "\n".join(lines)


def _parse_case(raw: dict[str, Any]) -> ResCase:
    """Parse one case, requiring exactly one expectation form."""
    expect = raw.get("expect")
    if not isinstance(expect, dict) or len(expect) != 1:
        raise ResolutionCorpusError(
            f"{raw.get('id')}: 'expect' must set exactly one of "
            "resolves_to / ambiguous / none"
        )
    resolves_to = expect.get("resolves_to")
    ambiguous = expect.get("ambiguous")
    if resolves_to is not None:
        expectation = Expectation.RESOLVES_TO
    elif ambiguous is not None:
        expectation = Expectation.AMBIGUOUS
    elif expect.get("none"):
        expectation = Expectation.NONE
    else:
        raise ResolutionCorpusError(
            f"{raw.get('id')}: unrecognized 'expect' {expect!r}"
        )

    return ResCase(
        id=raw["id"],
        home=raw["home"],
        query=raw["query"],
        expectation=expectation,
        resolves_to=resolves_to,
        ambiguous=tuple(ambiguous or ()),
        note=raw.get("note"),
        tags=tuple(raw.get("tags") or ()),
    )


def load_corpus(path: Path = SEED_SET) -> ResCorpus:
    """Load and validate a resolution corpus file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ResolutionCorpusError(f"cannot read corpus {path}: {err}") from err
    if not isinstance(raw, dict) or "homes" not in raw or "cases" not in raw:
        raise ResolutionCorpusError(f"corpus {path} must have 'homes' and 'cases' keys")

    homes = {
        name: tuple(
            ResEntity(
                entity_id=entity["entity_id"],
                names=tuple(entity["names"]),
                area=entity.get("area"),
                floor=entity.get("floor"),
            )
            for entity in entities or ()
        )
        for name, entities in (raw["homes"] or {}).items()
    }
    corpus = ResCorpus(
        homes=homes,
        cases=tuple(_parse_case(case) for case in raw["cases"] or ()),
    )
    validate_corpus(corpus)
    return corpus


def validate_corpus(corpus: ResCorpus) -> None:
    """Check unique ids, known homes, and that every referenced entity id exists."""
    problems: list[str] = []
    seen: set[str] = set()
    for case in corpus.cases:
        if case.id in seen:
            problems.append(f"duplicate case id: {case.id}")
        seen.add(case.id)
        if case.home not in corpus.homes:
            problems.append(f"{case.id}: unknown home {case.home!r}")
            continue
        home_ids = {entity.entity_id for entity in corpus.homes[case.home]}
        referenced = tuple(case.ambiguous)
        if case.resolves_to is not None:
            referenced += (case.resolves_to,)
        problems.extend(
            f"{case.id}: expected entity {entity_id!r} absent from home {case.home!r}"
            for entity_id in referenced
            if entity_id not in home_ids
        )

    if problems:
        raise ResolutionCorpusError("; ".join(problems))


def main() -> None:
    """Load the seed corpus, run the default scorer, and print the scorecard."""
    print(format_scorecard(run(load_corpus())))


if __name__ == "__main__":
    main()
