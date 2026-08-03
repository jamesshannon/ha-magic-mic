"""Deterministic tests for the scorer and scorecard (keyless, model-free)."""

from evals.harness import (
    Bucket,
    Case,
    Expected,
    ExpectedAnswer,
    ExpectedTool,
    ObservedTurn,
    ToolCall,
    build_scorecard,
    case_correct,
    classify,
    score_case,
)
from evals.harness.corpus import StateChange


def _case(
    routing_truth: str = "local",
    expected: Expected | None = None,
    **overrides: object,
) -> Case:
    """Build a minimal resolvable case, overriding the given fields."""
    base = {
        "id": "c",
        "utterance": "turn off the kitchen light",
        "category": "device-control",
        "routing_truth": routing_truth,
        "resolves_at_wave0": True,
        "expected": expected,
    }
    base.update(overrides)
    return Case(**base)  # type: ignore[arg-type]


# --- correctness matching -----------------------------------------------------------


def test_expected_arg_keys_are_subset_but_values_are_exact() -> None:
    """Unspecified observed args are allowed without weakening named values."""
    case = _case(
        expected=Expected(
            tools=(ExpectedTool("HassTurnOff", {"name": "Kitchen Light"}),)
        )
    )
    observed = ObservedTurn(
        speech="Done",
        tools=(ToolCall("HassTurnOff", {"name": "Kitchen Light", "brightness": 0}),),
    )

    assert case_correct(case, observed) is True


def test_string_arg_does_not_match_longer_value() -> None:
    """A target name cannot pass by being a substring of another target."""
    case = _case(
        expected=Expected(tools=(ExpectedTool("HassTurnOff", {"name": "Lamp"}),))
    )
    observed = ObservedTurn(
        speech="Done",
        tools=(ToolCall("HassTurnOff", {"name": "Bedroom Lamp"}),),
    )

    assert case_correct(case, observed) is False


def test_string_args_normalize_unicode_case_and_whitespace() -> None:
    """Conservative text normalization does not create false mismatches."""
    case = _case(
        expected=Expected(
            tools=(ExpectedTool("HassTurnOff", {"name": " CAFÉ  Lamp "}),)
        )
    )
    observed = ObservedTurn(
        speech="Done",
        tools=(
            ToolCall("HassTurnOff", {"name": "Cafe\N{COMBINING ACUTE ACCENT} lamp"}),
        ),
    )

    assert case_correct(case, observed) is True


def test_any_of_accepts_either_valid_outcome() -> None:
    """A disjunction passes when any acceptable outcome matches, fails when none do."""
    case = _case(
        expected=(
            Expected(tools=(ExpectedTool("HassTurnOff", {"name": "Blinds"}),)),
            Expected(
                tools=(
                    ExpectedTool("HassSetPosition", {"name": "Blinds", "position": 0}),
                )
            ),
        )
    )
    via_off = ObservedTurn(
        speech="Done", tools=(ToolCall("HassTurnOff", {"name": "Blinds"}),)
    )
    via_position = ObservedTurn(
        speech="Done",
        tools=(ToolCall("HassSetPosition", {"name": "Blinds", "position": 0}),),
    )
    neither = ObservedTurn(
        speech="Done", tools=(ToolCall("HassTurnOn", {"name": "Blinds"}),)
    )

    assert case_correct(case, via_off) is True
    assert case_correct(case, via_position) is True
    assert case_correct(case, neither) is False


def test_arg_mismatch_is_wrong() -> None:
    """A differing arg value is incorrect."""
    case = _case(
        expected=Expected(
            tools=(ExpectedTool("HassTurnOff", {"name": "Bedroom Light"}),)
        )
    )
    observed = ObservedTurn(
        speech="Done", tools=(ToolCall("HassTurnOff", {"name": "Kitchen Light"}),)
    )

    assert case_correct(case, observed) is False


def test_missing_tool_is_wrong() -> None:
    """The wrong tool name is incorrect."""
    case = _case(expected=Expected(tools=(ExpectedTool("HassTurnOff"),)))
    observed = ObservedTurn(speech="I turned it on", tools=(ToolCall("HassTurnOn"),))

    assert case_correct(case, observed) is False


def test_declared_supporting_tool_can_interleave_expected_calls() -> None:
    """Only an explicitly declared supporting call may appear between required calls."""
    case = _case(
        expected=Expected(
            tools=(
                ExpectedTool("HassTurnOff", {"name": "Kitchen Light"}),
                ExpectedTool("HassTurnOff", {"name": "Bedroom Fan"}),
            ),
            supporting_tools=(ExpectedTool("GetLiveContext"),),
        )
    )
    observed = ObservedTurn(
        speech="Done",
        tools=(
            ToolCall("HassTurnOff", {"name": "Kitchen Light"}),
            ToolCall("GetLiveContext", {}),
            ToolCall("HassTurnOff", {"name": "Bedroom Fan"}),
        ),
    )

    assert case_correct(case, observed) is True


def test_undeclared_extra_tool_is_wrong() -> None:
    """An extra call fails even when all expected calls appear in order."""
    case = _case(expected=Expected(tools=(ExpectedTool("GetLiveContext"),)))
    observed = ObservedTurn(
        speech="The garage is open.",
        tools=(
            ToolCall("GetLiveContext", {"name": "Garage Door"}),
            ToolCall("HassTurnOff", {"name": "Kitchen Light"}),
        ),
    )

    assert case_correct(case, observed) is False


def test_state_scoring_rejects_tool_outside_permitted_roster() -> None:
    """Correct final state does not excuse an unrelated tool call."""
    case = _case(
        expected=None,
        expect_changes={"light.kitchen": StateChange(state="off")},
        permitted_tools=(ExpectedTool("HassTurnOff"),),
    )
    observed = ObservedTurn(
        speech="Done",
        tools=(
            ToolCall("HassTurnOff", {"name": "Kitchen Light"}),
            ToolCall("HassStartTimer", {"minutes": 10}),
        ),
        unexpected_changes={},
    )

    assert case_correct(case, observed) is False


def test_answer_contains_case_insensitive() -> None:
    """An answer predicate matches its substrings case-insensitively."""
    case = _case(
        routing_truth="llm",
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )

    assert case_correct(case, ObservedTurn(speech="The capital is paris.")) is True
    assert case_correct(case, ObservedTurn(speech="It is Lyon.")) is False


def test_no_predicate_is_unjudgeable() -> None:
    """A case with no tools and no answer predicate yields ``None`` correctness."""
    assert case_correct(_case(expected=None), ObservedTurn(speech="ok")) is None


# --- bucket classification ----------------------------------------------------------


def test_local_correct_bucket() -> None:
    """A correct, locally-routed turn lands in RESOLVED_LOCALLY."""
    case = _case(expected=Expected(tools=(ExpectedTool("HassTurnOff"),)))
    observed = ObservedTurn(
        speech="Done", tools=(ToolCall("HassTurnOff"),), routed_locally=True
    )

    assert classify(case, observed) is Bucket.RESOLVED_LOCALLY


def test_llm_correct_bucket() -> None:
    """A correct, LLM-routed turn lands in LLM_CORRECT."""
    case = _case(
        routing_truth="llm",
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )
    observed = ObservedTurn(speech="Paris.", routed_locally=False)

    assert classify(case, observed) is Bucket.LLM_CORRECT


def test_wrong_action_bucket_ignores_routing() -> None:
    """A wrong action is WRONG_ACTION regardless of where it routed."""
    case = _case(expected=Expected(tools=(ExpectedTool("HassTurnOff"),)))
    observed = ObservedTurn(
        speech="on", tools=(ToolCall("HassTurnOn"),), routed_locally=True
    )

    assert classify(case, observed) is Bucket.WRONG_ACTION


def test_unresolved_bucket() -> None:
    """An unresolved turn lands in UNRESOLVED even with no expectation."""
    case = _case(resolves_at_wave0=False, expected=None)
    observed = ObservedTurn(speech="Sorry, I don't understand", resolved=False)

    assert classify(case, observed) is Bucket.UNRESOLVED


def test_unjudgeable_response_cannot_land_in_success_bucket() -> None:
    """A resolved-looking response without a predicate is explicitly UNJUDGED."""
    case = _case(resolves_at_wave0=False, expected=None)
    observed = ObservedTurn(speech="Sure, I did that.")

    assert case_correct(case, observed) is None
    assert classify(case, observed) is Bucket.UNJUDGED


def test_clarification_bucket() -> None:
    """A correct turn that clarified lands in RESOLVED_AFTER_CLARIFICATION."""
    case = _case(expected=Expected(tools=(ExpectedTool("HassTurnOff"),)))
    observed = ObservedTurn(
        speech="Done",
        tools=(ToolCall("HassTurnOff"),),
        clarified=True,
        routed_locally=False,
    )

    assert classify(case, observed) is Bucket.RESOLVED_AFTER_CLARIFICATION


# --- scorecard aggregation ----------------------------------------------------------


def test_scorecard_distribution_and_routing_agreement() -> None:
    """The scorecard sums buckets, cost totals, and labelled-vs-measured routing."""
    local_case = _case(
        id="local", expected=Expected(tools=(ExpectedTool("HassTurnOff"),))
    )
    llm_case = _case(
        id="llm",
        routing_truth="llm",
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )

    results = [
        score_case(
            local_case,
            ObservedTurn(
                speech="Done",
                tools=(ToolCall("HassTurnOff"),),
                routed_locally=True,
                input_tokens=100,
                generations=1,
            ),
        ),
        # Labelled llm but measured as routing locally: a routing disagreement.
        score_case(
            llm_case,
            ObservedTurn(
                speech="Paris",
                routed_locally=True,
                input_tokens=250,
                generations=2,
            ),
        ),
    ]
    card = build_scorecard(results)

    assert card.total == 2
    assert card.buckets[Bucket.RESOLVED_LOCALLY] == 2
    assert card.buckets[Bucket.UNRESOLVED] == 0
    # local matches (local==local); llm disagrees (llm labelled, local measured).
    assert card.routing_agreement == (1, 2)
    assert card.totals["input_tokens"] == 350
    assert card.totals["generations"] == 3
    assert "Scorecard (2 cases)" in card.render()
