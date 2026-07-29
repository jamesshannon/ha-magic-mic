"""Deterministic tests for the corpus loader and validator (keyless, model-free)."""

from dataclasses import replace

import pytest

from evals.harness import (
    Case,
    Corpus,
    CorpusError,
    Entity,
    World,
    load_corpus,
    validate_corpus,
)
from evals.harness.corpus import WAVE0_GOLDEN_SET, as_alternatives


def _case(**overrides: object) -> Case:
    """Build a minimal valid case, overriding the given fields."""
    base = {
        "id": "c1",
        "utterance": "turn on the light",
        "category": "device-control",
        "routing_truth": "local",
        "resolves_at_wave0": True,
    }
    base.update(overrides)
    return Case(**base)  # type: ignore[arg-type]


def test_wave0_golden_set_loads_and_validates() -> None:
    """The shipped corpus parses and satisfies every invariant."""
    corpus = load_corpus(WAVE0_GOLDEN_SET)

    assert corpus.cases, "corpus should not be empty"
    assert corpus.world.areas
    assert {"light.kitchen", "cover.garage_door"} <= corpus.world.entity_ids()


def test_load_corpus_default_path() -> None:
    """``load_corpus`` with no argument loads the Wave 0 golden set."""
    assert load_corpus().cases == load_corpus(WAVE0_GOLDEN_SET).cases


def test_duplicate_ids_rejected() -> None:
    """Two cases sharing an id fail validation."""
    world = World(areas=(), entities=())
    corpus = Corpus(world=world, cases=(_case(id="dup"), _case(id="dup")))

    with pytest.raises(CorpusError, match="duplicate case id: dup"):
        validate_corpus(corpus)


def test_unknown_routing_truth_rejected() -> None:
    """A ``routing_truth`` outside {local, llm} fails validation."""
    corpus = Corpus(
        world=World(areas=(), entities=()),
        cases=(_case(routing_truth="cloud"),),
    )

    with pytest.raises(CorpusError, match="routing_truth"):
        validate_corpus(corpus)


def test_requires_must_exist_in_world() -> None:
    """A ``requires`` entity absent from the world fails validation."""
    world = World(
        areas=("kitchen",),
        entities=(Entity(entity_id="light.kitchen", name="Kitchen Light"),),
    )
    corpus = Corpus(
        world=world,
        cases=(_case(requires=("light.kitchen", "light.ghost")),),
    )

    with pytest.raises(CorpusError, match="light.ghost"):
        validate_corpus(corpus)


def test_valid_requires_passes() -> None:
    """A ``requires`` entity present in the world validates cleanly."""
    world = World(
        areas=("kitchen",),
        entities=(Entity(entity_id="light.kitchen", name="Kitchen Light"),),
    )
    corpus = Corpus(world=world, cases=(_case(requires=("light.kitchen",)),))

    validate_corpus(corpus)  # does not raise


def test_missing_top_level_keys_rejected(tmp_path) -> None:
    """A file without both ``world`` and ``cases`` is rejected at load."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("cases: []\n", encoding="utf-8")

    with pytest.raises(CorpusError, match="world"):
        load_corpus(bad)


def test_expected_for_scope_falls_back_and_overrides() -> None:
    """``expected_for`` returns the LLM override when present, else the default."""
    corpus = load_corpus(WAVE0_GOLDEN_SET)
    by_id = {case.id: case for case in corpus.cases}

    # A device-control case has no override: both scopes see the same expectation.
    kitchen = by_id["turn-on-kitchen-light"]
    assert kitchen.expected_llm is None
    assert kitchen.expected_for(llm=True) == kitchen.expected_for(llm=False)

    # An Assist-dropped intent diverges: local intent tool vs the general LLM tool.
    # Each scope has one acceptable outcome here.
    time_case = by_id["current-time"]
    assert [t.name for t in time_case.expected_for(llm=False)[0].tools] == [
        "HassGetCurrentTime"
    ]
    assert [t.name for t in time_case.expected_for(llm=True)[0].tools] == [
        "GetDateTime"
    ]

    # nevermind's LLM override is a single empty, non-None outcome (unjudgeable, not a
    # fallback to the HassNevermind intent the model cannot call).
    nevermind = by_id["nevermind"]
    assert nevermind.expected_llm is not None
    (outcome,) = nevermind.expected_for(llm=True)
    assert outcome.tools == ()


def test_local_labels_have_expectations() -> None:
    """Every ``local`` case carries a checkable expectation (tools or answer)."""
    corpus = load_corpus(WAVE0_GOLDEN_SET)

    for case in corpus.cases:
        if case.routing_truth == "local":
            outcomes = as_alternatives(case.expected)
            assert outcomes, f"{case.id}: local case needs expected"
            for outcome in outcomes:
                assert outcome.tools or outcome.answer, case.id
        # A replace round-trip proves the case is a well-formed frozen dataclass.
        assert replace(case) == case
