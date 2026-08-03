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


def test_tool_and_args_partial_match() -> None:
    """Expected args are a subset check; extra observed args do not break a match."""
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


def test_compound_tools_matched_as_ordered_subsequence() -> None:
    """Expected tools match when present in order, interleaved calls allowed."""
    case = _case(
        expected=Expected(
            tools=(
                ExpectedTool("HassTurnOff", {"name": "Kitchen Light"}),
                ExpectedTool("HassTurnOff", {"name": "Bedroom Fan"}),
            )
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
