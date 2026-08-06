"""Deterministic tests for the corpus loader and validator (keyless, model-free)."""

from dataclasses import replace

import pytest

from evals.harness import (
    Case,
    Corpus,
    CorpusError,
    Entity,
    Expected,
    ExpectedAnswer,
    ExpectedEffect,
    ExpectedTool,
    ProviderOptions,
    StateChange,
    World,
    load_corpus,
    validate_corpus,
)
from evals.harness.corpus import UNPLACED, WAVE0_GOLDEN_SET, as_alternatives


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
    reasoning = next(
        case for case in corpus.cases if case.id == "reasoning-did-i-leave-garage"
    )
    assert reasoning.expected_for(llm=True)[0].supporting_tools == (
        ExpectedTool("GetLiveContext"),
    )
    state_case = next(
        case for case in corpus.cases if case.id == "turn-on-kitchen-light"
    )
    assert ExpectedTool("HassTurnOn") in state_case.permitted_tools
    timer_case = next(case for case in corpus.cases if case.id == "start-timer")
    assert timer_case.expected_for(llm=True)[0].effects == (
        ExpectedEffect("timer.started", {"seconds": 600}),
    )


def test_load_corpus_default_path() -> None:
    """``load_corpus`` with no argument loads the Wave 0 golden set."""
    assert load_corpus().cases == load_corpus(WAVE0_GOLDEN_SET).cases


def test_provider_options_are_per_case_and_default_off(tmp_path) -> None:
    """Cases independently configure provider-native web capabilities."""
    corpus_path = tmp_path / "provider-options.yaml"
    corpus_path.write_text(
        """
world:
  areas: []
  entities: []
cases:
  - id: default
    utterance: hello
    category: small-talk
    routing_truth: llm
    resolves_at_wave0: true
  - id: search
    utterance: what happened today?
    category: knowledge
    routing_truth: llm
    resolves_at_wave0: true
    provider_options:
      web_search: true
      web_fetch: false
""",
        encoding="utf-8",
    )

    corpus = load_corpus(corpus_path)

    assert corpus.cases[0].provider_options == ProviderOptions()
    assert corpus.cases[1].provider_options == ProviderOptions(web_search=True)


@pytest.mark.parametrize(
    "provider_options",
    ["true", "{unknown: true}", "{web_search: yes-please}"],
)
def test_invalid_provider_options_are_rejected(tmp_path, provider_options: str) -> None:
    """Provider options reject unknown shapes, keys, and non-boolean values."""
    corpus_path = tmp_path / "invalid-provider-options.yaml"
    corpus_path.write_text(
        f"""
world:
  areas: []
  entities: []
cases:
  - id: invalid
    utterance: hello
    category: small-talk
    routing_truth: llm
    resolves_at_wave0: true
    provider_options: {provider_options}
""",
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match=r"provider.?option"):
        load_corpus(corpus_path)


def test_resolves_at_wave0_must_be_boolean(tmp_path) -> None:
    """The baseline outcome contract rejects truthy strings."""
    corpus_path = tmp_path / "invalid-outcome.yaml"
    corpus_path.write_text(
        """
world:
  areas: []
  entities: []
cases:
  - id: invalid
    utterance: hello
    category: small-talk
    routing_truth: llm
    resolves_at_wave0: "false"
""",
        encoding="utf-8",
    )

    with pytest.raises(CorpusError, match="resolves_at_wave0 must be a boolean"):
        load_corpus(corpus_path)


def test_unsupported_case_cannot_declare_success_predicate() -> None:
    """An unbuilt Wave 0 feature cannot pass through a loose answer match."""
    corpus = Corpus(
        world=World(areas=(), entities=()),
        cases=(
            _case(
                resolves_at_wave0=False,
                expected=Expected(answer=ExpectedAnswer(contains=("done",))),
            ),
        ),
    )

    with pytest.raises(CorpusError, match="cannot declare a success predicate"):
        validate_corpus(corpus)


def test_state_scored_case_requires_permitted_tool_roster() -> None:
    """State correctness must not leave observed calls unconstrained."""
    corpus = Corpus(
        world=World(
            areas=(), entities=(Entity(entity_id="light.kitchen", name="Light"),)
        ),
        cases=(
            _case(
                expect_changes={"light.kitchen": StateChange(state="off")},
            ),
        ),
    )

    with pytest.raises(CorpusError, match="must declare permitted_tools"):
        validate_corpus(corpus)


def test_mutating_tool_cannot_be_declared_as_supporting() -> None:
    """Supporting calls are restricted to the scorer's read-only inventory."""
    corpus = Corpus(
        world=World(areas=(), entities=()),
        cases=(
            _case(
                expected=Expected(
                    tools=(ExpectedTool("GetLiveContext"),),
                    supporting_tools=(ExpectedTool("HassTurnOff"),),
                )
            ),
        ),
    )

    with pytest.raises(CorpusError, match="not classified read-only"):
        validate_corpus(corpus)


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
    """Every ``local`` case carries a checkable expectation: tools, answer, or state."""
    corpus = load_corpus(WAVE0_GOLDEN_SET)

    for case in corpus.cases:
        if case.routing_truth != "local":
            continue
        if case.state_scored:
            # A state-scored case is judged by expect_changes, not by tools/answer.
            continue
        outcomes = as_alternatives(case.expected)
        assert outcomes, f"{case.id}: local case needs expected or expect_changes"
        for outcome in outcomes:
            assert outcome.tools or outcome.answer, case.id
        # A replace round-trip proves the case is a well-formed frozen dataclass.
        assert replace(case) == case


def test_golden_set_keeps_the_hassil_covered_basics() -> None:
    """The corpus keeps enough ``local`` cases to exercise the prefer-local-off path.

    Every driver except ``local_first`` addresses the agent by id, which skips the pipeline,
    so these cases are what proves the model still handles the commands HASSIL would have
    taken (see ``docs/evaluation.md``, "Both routing configurations stay covered"). A corpus
    that drifted toward exotic utterances could break "turn on the kitchen light" on the LLM
    path with nothing going red, so the population is pinned rather than assumed.
    """
    corpus = load_corpus(WAVE0_GOLDEN_SET)
    local = [case for case in corpus.cases if case.routing_truth == "local"]

    assert len(local) >= len(corpus.cases) // 2, (
        "fewer than half the cases are HASSIL-covered; the prefer-local-off path is "
        "losing the basics it exists to check"
    )
    # The everyday device-control shapes, the ones a regression would be most embarrassing on.
    assert sum(1 for case in local if case.category == "device-control") >= 5


def test_unplaced_is_accepted_without_being_a_world_area() -> None:
    """The sentinel validates on its own; it names no room, so no world declares it."""
    world = World(
        areas=("kitchen",),
        entities=(Entity(entity_id="light.kitchen", name="Kitchen Light"),),
    )
    corpus = Corpus(world=world, cases=(_case(satellite_area=UNPLACED),))

    validate_corpus(corpus)  # does not raise


def test_a_world_area_colliding_with_the_sentinel_is_rejected() -> None:
    """A fixture room literally named "unplaced" would make the sentinel ambiguous."""
    world = World(
        areas=(UNPLACED,),
        entities=(Entity(entity_id="light.kitchen", name="Kitchen Light"),),
    )
    corpus = Corpus(world=world, cases=(_case(),))

    with pytest.raises(CorpusError, match="reserved satellite_area sentinel"):
        validate_corpus(corpus)


def test_an_unknown_satellite_area_is_still_rejected() -> None:
    """Adding the sentinel must not weaken the check on a real area key."""
    world = World(
        areas=("kitchen",),
        entities=(Entity(entity_id="light.kitchen", name="Kitchen Light"),),
    )
    corpus = Corpus(world=world, cases=(_case(satellite_area="den"),))

    with pytest.raises(CorpusError, match="absent from the fixture world"):
        validate_corpus(corpus)


def test_the_bare_lights_cases_declare_their_placement() -> None:
    """Both "turn off the lights" cases pin the satellite, since the outcome depends on it.

    Omitting the field would leave each case meaning one thing under the area-less baseline
    and another under the local-first driver's room-bound satellite, which is the failure
    that split them in two.
    """
    corpus = load_corpus(WAVE0_GOLDEN_SET)
    by_id = {case.id: case for case in corpus.cases}

    room, unplaced = (
        by_id["turn-off-lights-from-room"],
        by_id["turn-off-lights-unplaced"],
    )
    assert room.utterance == unplaced.utterance
    assert room.satellite_area == "living_room"
    assert unplaced.satellite_area == UNPLACED
    # And they route differently: the HASSIL template needs an {area}, which only a
    # room-bound satellite supplies, so the unplaced reading is the model's to handle.
    assert room.routing_truth == "local"
    assert unplaced.routing_truth == "llm"
    # Room-scoped expects only its own room; unplaced expects every lit light.
    assert set(room.expect_changes) == {"light.living_room"}
    assert set(unplaced.expect_changes) == {"light.kitchen", "light.living_room"}
