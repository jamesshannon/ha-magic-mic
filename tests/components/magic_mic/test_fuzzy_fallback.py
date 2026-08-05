"""Fuzzy-fallback runner tests: the resolution-path classifier.

`classify_path` reduces one observed turn to how it reached its target. These pin the four
paths the live run reports, and the two distinctions the classifier exists to make: a named
call that moved a device (Consumer 1) versus one that moved nothing (asked), and a device
that moved with no name (structured slots) versus one that moved after a spoken name.
"""

from evals.harness import Bucket, Case
from evals.harness.fuzzy_fallback import FuzzyReport, ResolutionPath, classify_path
from evals.harness.scoring import CaseResult, ObservedTurn, ToolCall

_EXACT_NAMES = frozenset({"corner floor lamp", "bedside table lamp"})


def _case(case_id: str = "case") -> Case:
    """Build a minimal device-control case; the classifier reads only the observed turn."""
    return Case(
        id=case_id,
        utterance="turn on the floor lamp",
        category="device-control",
        routing_truth="llm",
        resolves_at_wave0=True,
    )


def _result(observed: ObservedTurn, *, correct: bool | None = True) -> CaseResult:
    return CaseResult(
        case=_case(),
        observed=observed,
        bucket=Bucket.RESOLVED_LOCALLY,
        correct=correct,
    )


def test_named_call_that_moved_a_device_is_consumer1() -> None:
    """A spoken name that actuated a device went through the match-layer fallback."""
    observed = ObservedTurn(
        speech="Done!",
        tools=(ToolCall("HassTurnOn", {"name": "floor lamp"}),),
    )
    classified = classify_path(
        _result(observed), _EXACT_NAMES, frozenset({"light.den_floor"})
    )
    assert classified.path == ResolutionPath.CONSUMER1_FALLBACK
    assert classified.actuated_names == ("floor lamp",)
    assert classified.changed == ("light.den_floor",)


def test_named_call_that_moved_nothing_is_asked() -> None:
    """A spoken name that came back not-found moves nothing, so the model asked."""
    observed = ObservedTurn(
        speech="I don't see a lamp there. Which room?",
        tools=(ToolCall("HassTurnOn", {"name": "lamp"}),),
    )
    classified = classify_path(_result(observed), _EXACT_NAMES, frozenset())
    assert classified.path == ResolutionPath.ASKED


def test_device_moved_with_no_name_is_structured_slots() -> None:
    """Acting by area/device-class (no name) never runs the fuzzy layer."""
    observed = ObservedTurn(
        speech="Done!",
        tools=(
            ToolCall("HassSetPosition", {"area": "office", "device_class": ["shade"]}),
        ),
    )
    classified = classify_path(
        _result(observed), _EXACT_NAMES, frozenset({"cover.office_shade"})
    )
    assert classified.path == ResolutionPath.STRUCTURED_SLOTS
    assert classified.actuated_names == ()


def test_find_entities_is_consumer2() -> None:
    """A device moved after a find_entities lookup is the Consumer 2 path."""
    observed = ObservedTurn(
        speech="Done!",
        tools=(
            ToolCall("find_entities", {"name": "under cabinet lights"}),
            ToolCall("HassTurnOn", {"name": "Under Cabinet Lighting"}),
        ),
    )
    classified = classify_path(
        _result(observed), _EXACT_NAMES, frozenset({"light.kitchen_strip"})
    )
    assert classified.path == ResolutionPath.CONSUMER2_FIND
    assert classified.used_find_entities is True


def test_report_counts_paths_and_consumer1_wins() -> None:
    """The report tallies paths and counts only correct Consumer 1 resolutions."""
    win = classify_path(
        _result(
            ObservedTurn(speech="", tools=(ToolCall("HassTurnOn", {"name": "x"}),))
        ),
        _EXACT_NAMES,
        frozenset({"light.a"}),
    )
    wrong = classify_path(
        _result(
            ObservedTurn(speech="", tools=(ToolCall("HassTurnOn", {"name": "y"}),)),
            correct=False,
        ),
        _EXACT_NAMES,
        frozenset({"light.b"}),
    )
    asked = classify_path(
        _result(ObservedTurn(speech="which one?")), _EXACT_NAMES, frozenset()
    )
    report = FuzzyReport((win, wrong, asked))
    assert report.path_counts == {
        ResolutionPath.CONSUMER1_FALLBACK: 2,
        ResolutionPath.ASKED: 1,
    }
    assert report.consumer1_correct == 1
