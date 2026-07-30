"""Tests for the resolver micro-benchmark: the CI safety gate and the harness logic.

The gate (`test_seed_set_*`) runs the real scorer over the seed corpus and asserts the
invariants that must always hold; the rest drive the classifier and metrics with stub
resolvers so the harness is verified independent of the scorer under test.
"""

from pathlib import Path

import pytest

from custom_components.magic_mic.fuzzy import Candidate, Resolution, Scored
from evals.harness.resolution import (
    Expectation,
    Outcome,
    ResCase,
    ResCorpus,
    ResEntity,
    ResolutionCorpusError,
    Resolver,
    Scorecard,
    evaluate_case,
    load_corpus,
    run,
)

# Current measured floor for the seed set (30 cases across regimes). Union-document plus
# the IDF tie-break resolves location, medium-home, and most shared-word/area-filtered
# cases; the two survivors are a synonym (reading light -> Reading Lamp, out of scope for
# weighting) and one 3-shared-token area case, both recall-safe. Raise this as the scorer
# improves; a drop with no new cases is a regression. Never lower the false-resolve gate -
# decisively acting on the wrong entity is the one failure the guard exists to prevent.
_SEED_MIN_DECISIVE_ACCURACY = 0.92

# Regimes that must stay perfect regardless of scorer changes. Small homes are too sparse
# for IDF to estimate term rarity, so a weighting change must never make them cautious;
# the decisive/none/ambiguous guardrails pin the behavior the guard exists to guarantee.
# The shared-word and area-filtered regimes are deliberately absent: those are allowed to
# be imperfect (they are what a scorer change is trying to lift).
_GUARDRAIL_REGIMES = ("decisive", "small-home", "none", "ambiguous")


def _corpus(*cases: ResCase) -> ResCorpus:
    """Build a one-home corpus over a fixed candidate set for the given cases."""
    return ResCorpus(
        homes={
            "h": (
                ResEntity("light.a", ("Alpha Lamp",)),
                ResEntity("light.b", ("Beta Lamp",)),
            )
        },
        cases=cases,
    )


def _fixed(resolution: Resolution) -> Resolver:
    """A resolver that ignores its input and always returns the given resolution."""
    return lambda query, candidates, limit: resolution


def _case(expectation: Expectation, **kw: object) -> ResCase:
    return ResCase(id="c", home="h", query="alpha", expectation=expectation, **kw)


def test_seed_set_never_false_resolves(hass_free_seed: Scorecard) -> None:
    """The safety invariant: the guard must never decisively pick the wrong entity."""
    assert hass_free_seed.false_resolve_rate == 0.0
    assert hass_free_seed.false_resolves == 0


def test_seed_set_target_always_retrievable(hass_free_seed: Scorecard) -> None:
    """Every should-resolve case at least returns its target (decisive or shortlisted)."""
    assert hass_free_seed.resolve_recall == 1.0


def test_seed_set_meets_baseline_accuracy(hass_free_seed: Scorecard) -> None:
    """Decisive accuracy holds at or above the recorded floor (no regression)."""
    assert hass_free_seed.decisive_accuracy >= _SEED_MIN_DECISIVE_ACCURACY
    assert hass_free_seed.ambiguous_accuracy == 1.0
    assert hass_free_seed.none_accuracy == 1.0


def test_seed_set_guardrail_regimes_stay_perfect(hass_free_seed: Scorecard) -> None:
    """The regimes a scorer change must never regress are all-passing, false-resolve free.

    A weighting change (IDF) may lift the shared-word/area-filtered regimes, but small
    homes and the decisive/none/ambiguous guardrails must remain intact.
    """
    breakdown = hass_free_seed.tag_breakdown()
    for regime in _GUARDRAIL_REGIMES:
        passed, total, false_resolves = breakdown[regime]
        assert total > 0, f"guardrail regime {regime!r} has no cases"
        assert passed == total, (
            f"guardrail regime {regime!r} regressed: {passed}/{total}"
        )
        assert false_resolves == 0, f"guardrail regime {regime!r} false-resolved"


@pytest.fixture
def hass_free_seed() -> Scorecard:
    """Score the seed corpus with the production scorer (no HA world involved)."""
    return run(load_corpus())


def test_seed_carries_entity_location() -> None:
    """The corpus can express an entity's area/floor, even though the scorer ignores it.

    Location is the discriminating signal a name-only scorer misses; the corpus records
    it so those cases are expressible and measurable now, ahead of location-aware scoring.
    """
    corpus = load_corpus()
    by_id = {entity.entity_id: entity for entity in corpus.homes["named_by_room"]}
    assert by_id["light.kitchen_ceiling"].area == "Kitchen"
    assert by_id["light.bedroom_ceiling"].area == "Bedroom"


def test_tag_breakdown_counts_each_regime() -> None:
    """A case counts under each of its tags; overlapping tags aggregate independently."""
    scorecard = run(
        ResCorpus(
            homes={"h": (ResEntity("light.a", ("Alpha",)),)},
            cases=(
                ResCase(
                    "r",
                    "h",
                    "alpha",
                    Expectation.RESOLVES_TO,
                    resolves_to="light.a",
                    tags=("decisive", "shared"),
                ),
                ResCase("n", "h", "zzz", Expectation.NONE, tags=("shared",)),
            ),
        ),
        _fixed(Resolution(Scored("light.a", 95.0))),
    )
    breakdown = scorecard.tag_breakdown()
    assert breakdown["decisive"] == (1, 1, 0)
    # The none case is WRONG_NONE under this always-resolve stub, so 'shared' is 1 of 2.
    assert breakdown["shared"] == (1, 2, 0)


def test_correct_resolve() -> None:
    """A decisive match on the expected entity passes."""
    result = evaluate_case(
        _case(Expectation.RESOLVES_TO, resolves_to="light.a"),
        {"light.a": Candidate(("Alpha Lamp",))},
        _fixed(Resolution(Scored("light.a", 95.0))),
    )
    assert result.outcome is Outcome.CORRECT_RESOLVE
    assert result.outcome.passed


def test_false_resolve_is_unsafe_and_fails() -> None:
    """A decisive match on the wrong entity is the false-resolve outcome."""
    scorecard = run(
        _corpus(_case(Expectation.RESOLVES_TO, resolves_to="light.a")),
        _fixed(Resolution(Scored("light.b", 95.0))),
    )
    result = scorecard.results[0]
    assert result.outcome is Outcome.FALSE_RESOLVE
    assert not result.outcome.passed
    assert scorecard.false_resolve_rate == 1.0


def test_missed_in_shortlist_vs_entirely() -> None:
    """A non-decisive result splits on whether the target survived in the shortlist."""
    ambiguous = Resolution(
        None, [Scored("light.a", 66.0), Scored("light.b", 66.0)], ambiguous=True
    )
    in_shortlist = evaluate_case(
        _case(Expectation.RESOLVES_TO, resolves_to="light.a"),
        {},
        _fixed(ambiguous),
    )
    assert in_shortlist.outcome is Outcome.MISSED_IN_SHORTLIST

    dropped = evaluate_case(
        _case(Expectation.RESOLVES_TO, resolves_to="light.c"),
        {},
        _fixed(ambiguous),
    )
    assert dropped.outcome is Outcome.MISSED_ENTIRELY


def test_correct_ambiguous_requires_expected_subset() -> None:
    """An ambiguous case passes only when its expected members are all shortlisted."""
    resolver = _fixed(
        Resolution(
            None, [Scored("light.a", 80.0), Scored("light.b", 80.0)], ambiguous=True
        )
    )
    ok = evaluate_case(
        _case(Expectation.AMBIGUOUS, ambiguous=("light.a", "light.b")), {}, resolver
    )
    assert ok.outcome is Outcome.CORRECT_AMBIGUOUS

    missing = evaluate_case(
        _case(Expectation.AMBIGUOUS, ambiguous=("light.a", "light.c")), {}, resolver
    )
    assert missing.outcome is Outcome.WRONG_AMBIGUOUS


def test_none_expectation_over_reach_fails() -> None:
    """A none case fails if anything resolves or is flagged ambiguous."""
    empty = evaluate_case(_case(Expectation.NONE), {}, _fixed(Resolution(None)))
    assert empty.outcome is Outcome.CORRECT_NONE

    over = evaluate_case(
        _case(Expectation.NONE), {}, _fixed(Resolution(Scored("light.a", 90.0)))
    )
    assert over.outcome is Outcome.WRONG_NONE


def test_decisive_accuracy_denominator_is_resolve_cases_only() -> None:
    """Accuracy is over should-resolve cases; ambiguous/none cases do not dilute it."""
    scorecard = run(
        ResCorpus(
            homes={"h": (ResEntity("light.a", ("Alpha",)),)},
            cases=(
                ResCase(
                    "r", "h", "alpha", Expectation.RESOLVES_TO, resolves_to="light.a"
                ),
                ResCase("n", "h", "zzz", Expectation.NONE),
            ),
        ),
        _fixed(Resolution(Scored("light.a", 95.0))),
    )
    # The none case is scored WRONG_NONE here, but decisive accuracy ignores it entirely.
    assert scorecard.decisive_accuracy == 1.0


_ONE_HOME = "homes:\n  h:\n    - {entity_id: light.a, names: [Alpha]}\ncases:\n"


def test_loader_rejects_multiple_expectation_forms(tmp_path: Path) -> None:
    """A case must set exactly one of resolves_to / ambiguous / none."""
    corpus = _ONE_HOME + (
        "  - {id: bad, home: h, query: x, expect: {resolves_to: light.a, none: true}}\n"
    )
    with pytest.raises(ResolutionCorpusError, match="exactly one"):
        load_corpus(_write(tmp_path, corpus))


def test_loader_rejects_unknown_home(tmp_path: Path) -> None:
    """Validation flags a case pointing at a home that is not defined."""
    corpus = _ONE_HOME + "  - {id: n, home: ghost, query: x, expect: {none: true}}\n"
    with pytest.raises(ResolutionCorpusError, match="unknown home"):
        load_corpus(_write(tmp_path, corpus))


def test_loader_rejects_absent_target_entity(tmp_path: Path) -> None:
    """Validation flags a resolves_to that names an entity absent from the home."""
    corpus = _ONE_HOME + (
        "  - {id: n, home: h, query: x, expect: {resolves_to: light.missing}}\n"
    )
    with pytest.raises(ResolutionCorpusError, match="absent from home"):
        load_corpus(_write(tmp_path, corpus))


def test_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    """Validation flags two cases sharing an id."""
    corpus = _ONE_HOME + (
        "  - {id: dup, home: h, query: x, expect: {none: true}}\n"
        "  - {id: dup, home: h, query: y, expect: {none: true}}\n"
    )
    with pytest.raises(ResolutionCorpusError, match="duplicate case id"):
        load_corpus(_write(tmp_path, corpus))


def _write(tmp_path: Path, text: str) -> Path:
    """Write a full corpus document to a temp file and return its path."""
    path = tmp_path / "corpus.yaml"
    path.write_text(text, encoding="utf-8")
    return path
