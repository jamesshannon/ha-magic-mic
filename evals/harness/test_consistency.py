"""Tests for the consistency driver's reduction and reporting.

Model-free. The report path runs only after every live turn is paid for, and two earlier
drivers crashed there after a full run, so it is exercised here rather than discovered live.
"""

from pathlib import Path

import pytest

from evals.harness.baseline import BaselineError
from evals.harness.consistency import (
    CONSISTENCY_ARTIFACT,
    ConsistencyReport,
    Outcome,
    build_artifact,
    check_trials,
    reduce_outcome,
    select_case,
)
from evals.harness.corpus import load_corpus
from evals.harness.entity_id_tools import ENTITY_ID_CORPUS, ArgumentSource
from evals.harness.scoring import Bucket, CaseResult, ObservedTurn, ToolCall

from .test_corpus import _case  # reuse the minimal case builder

SCRIPT_TOOLS = frozenset({"evening_dim"})
LIVE_IDS = frozenset({"light.hue_00a3", "light.hue_00b7"})


def _result(tools: tuple[ToolCall, ...], *, correct: bool | None) -> CaseResult:
    """Wrap observed tool calls in a scored case result."""
    return CaseResult(
        case=_case(),
        observed=ObservedTurn(speech="ok", tools=tools),
        bucket=Bucket.LLM_CORRECT if correct else Bucket.WRONG_ACTION,
        correct=correct,
    )


def _outcome(target: str | None, *, correct: bool | None = True) -> Outcome:
    """One reduced trial: acted on ``target``, or asked when it is None."""
    if target is None:
        return Outcome(acted_on=(), correct=correct, source=ArgumentSource.ABSENT)
    return Outcome(acted_on=(target,), correct=correct, source=ArgumentSource.LIVE_ID)


def _report(outcomes: tuple[Outcome, ...]) -> ConsistencyReport:
    """A report over the given outcomes at a fixed configuration."""
    return ConsistencyReport(
        case_id="paraphrased-target-couch-lamp",
        arm="advertise",
        outcomes=outcomes,
        summary_on=True,
        names_on=False,
    )


def test_a_trial_reduces_to_what_it_acted_on() -> None:
    """The outcome is the device and the verdict, not the internals."""
    result = _result(
        (ToolCall(name="evening_dim", args={"light": "light.hue_00a3"}),), correct=True
    )

    outcome = reduce_outcome(result, SCRIPT_TOOLS, LIVE_IDS)

    assert outcome.acted_on == ("light.hue_00a3",)
    assert outcome.correct is True
    assert not outcome.asked


def test_a_turn_that_never_called_the_tool_reads_as_asking() -> None:
    """No script call is what a clarifying question looks like from the outside."""
    result = _result(
        (ToolCall(name="find_entities", args={"name": "couch lamp"}),), correct=False
    )

    outcome = reduce_outcome(result, SCRIPT_TOOLS, LIVE_IDS)

    assert outcome.asked
    assert outcome.describe() == "asked instead of acting"


def test_identical_trials_collapse_to_one_outcome() -> None:
    """A consistent case is the whole point of the measurement, so it must read as one."""
    report = _report((_outcome("light.hue_00b7"),) * 5)

    assert report.trials == 5
    assert report.distinct == 1
    assert report.distinct_targets == 1
    assert report.correct == 5


def test_different_devices_are_counted_as_different_targets() -> None:
    """The sharpest number: the same words moved different hardware."""
    report = _report(
        (
            _outcome("light.hue_00b7"),
            _outcome("light.hue_00a3", correct=False),
            _outcome("light.hue_00b7"),
            _outcome(None, correct=False),
        )
    )

    assert report.distinct == 3
    assert report.distinct_targets == 2
    assert report.correct == 2


def test_an_inconsistently_right_case_is_visible() -> None:
    """A mean would report 50%; the point is that the user cannot predict which."""
    report = _report(
        (_outcome("light.hue_00b7"), _outcome("light.hue_00a3", correct=False))
    )

    rendered = report.render()

    assert "distinct outcomes   2" in rendered
    assert "distinct targets    2" in rendered
    assert "correct             1/2" in rendered


def test_the_report_renders_every_outcome() -> None:
    """Rendering runs after N live turns are spent, so it is exercised model-free."""
    rendered = _report(
        (_outcome("light.hue_00b7"), _outcome(None, correct=False))
    ).render()

    assert "asked instead of acting" in rendered
    assert "light.hue_00b7" in rendered
    assert "entity summary" in rendered


def test_the_artifact_records_the_distribution_and_the_configuration() -> None:
    """The artifact has to say what was held fixed, or the number means nothing."""
    report = _report((_outcome("light.hue_00b7"), _outcome("light.hue_00b7")))

    artifact = build_artifact(report, "claude-haiku-4-5")

    assert artifact["run"]["trials"] == 2
    assert artifact["run"]["distinct_outcomes"] == 1
    assert artifact["run"]["arm"] == "advertise"
    assert artifact["run"]["entity_summary"] is True
    assert artifact["distribution"][0]["count"] == 2


def test_an_unknown_case_is_rejected_before_the_key_is_read() -> None:
    """A typo must not surface after the first live turn is spent."""
    corpus = load_corpus(ENTITY_ID_CORPUS)

    with pytest.raises(BaselineError, match="unknown case id"):
        select_case(corpus, "no-such-case")


def test_the_documented_flaky_case_exists() -> None:
    """The driver's docstring names this case; keep the two from drifting apart."""
    corpus = load_corpus(ENTITY_ID_CORPUS)

    case = select_case(corpus, "paraphrased-target-couch-lamp")

    assert case.phrasing == "out_of_vocabulary"


def test_a_single_trial_is_rejected() -> None:
    """One trial cannot show a distribution, so it is a mistake worth catching early."""
    with pytest.raises(BaselineError, match="at least 2"):
        check_trials(1)


def test_the_artifact_path_is_inside_the_repo() -> None:
    """Artifacts land with the others, not wherever the driver was invoked from."""
    assert CONSISTENCY_ARTIFACT.parent.name == "results"
    assert isinstance(CONSISTENCY_ARTIFACT, Path)
