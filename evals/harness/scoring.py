"""Score an observed turn against a corpus case, and aggregate the scorecard.

The scorer is deterministic and model-free: it takes whatever a driver observed for a
case (which tools fired, the speech, whether the local path handled it) and maps it to
an outcome bucket (``docs/evaluation.md`` Part E). The mock driver and the live driver
share this scorer; only how the ``ObservedTurn`` is produced differs.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any
import unicodedata

from .corpus import (
    ROUTING_LOCAL,
    Case,
    Expected,
    ExpectedAnswer,
    ExpectedTool,
    as_alternatives,
)


class Bucket(Enum):
    """Outcome buckets of the value-dashboard scorecard, ordered best to worst."""

    RESOLVED_LOCALLY = "resolved locally (hassil)"
    LLM_CORRECT = "resolved by LLM, correct"
    RESOLVED_AFTER_CLARIFICATION = "resolved after clarification"
    WRONG_ACTION = "wrong action"
    UNRESOLVED = 'unresolved / "I don\'t understand"'
    UNJUDGED = "unjudged (no success predicate)"


@dataclass(frozen=True)
class ToolCall:
    """A tool the agent actually invoked during the turn."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ObservedTurn:
    """What a driver observed for one case. The scorer reads only this."""

    speech: str
    tools: tuple[ToolCall, ...] = ()
    # Did the deterministic local (HASSIL) path handle it, no LLM involved.
    routed_locally: bool = False
    # Did the turn produce a useful outcome (vs. "I don't understand" / error).
    resolved: bool = True
    # Did reaching the outcome cost a clarification turn (multi-turn only).
    clarified: bool = False
    # For a state-scored case, the entities whose end state did not match (empty = correct).
    # ``None`` on a tool/answer-scored case, where correctness comes from the tools/speech.
    unexpected_changes: dict[str, Any] | None = None
    generations: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class CaseResult:
    """The scored outcome for one case."""

    case: Case
    observed: ObservedTurn
    bucket: Bucket
    # True/False when the case carries a checkable predicate, None when it does not.
    correct: bool | None


def _arg_matches(expected: Any, actual: Any) -> bool:
    """Match one argument by exact value after conservative normalization."""
    if isinstance(expected, str) and isinstance(actual, str):
        return _normalize_string(expected) == _normalize_string(actual)
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(
            _arg_matches(expected_item, actual_item)
            for expected_item, actual_item in zip(expected, actual, strict=True)
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(
            _arg_matches(value, actual[key]) for key, value in expected.items()
        )
    return expected == actual


def _normalize_string(value: str) -> str:
    """Normalize Unicode, case, and whitespace without weakening value identity."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _tool_matches(expected: ExpectedTool, actual: ToolCall) -> bool:
    """A call matches when names are equal and every expected arg is present."""
    if expected.name != actual.name:
        return False
    return all(
        key in actual.args and _arg_matches(value, actual.args[key])
        for key, value in expected.args.items()
    )


def _tools_match(
    expected: tuple[ExpectedTool, ...],
    supporting: tuple[ExpectedTool, ...],
    actual: tuple[ToolCall, ...],
) -> bool:
    """Require expected calls in order and reject every undeclared extra call."""
    expected_index = 0
    for call in actual:
        if expected_index < len(expected) and _tool_matches(
            expected[expected_index], call
        ):
            expected_index += 1
            continue
        if not any(_tool_matches(allowed, call) for allowed in supporting):
            return False
    return expected_index == len(expected)


def _only_permitted_tools(
    permitted: tuple[ExpectedTool, ...], actual: tuple[ToolCall, ...]
) -> bool:
    """Return whether every state-scored call matches its explicit allowlist."""
    return all(
        any(_tool_matches(allowed, call) for allowed in permitted) for call in actual
    )


def _answer_matches(answer: ExpectedAnswer | None, speech: str) -> bool:
    if answer is None:
        return True
    folded = speech.casefold()
    if any(needle.casefold() not in folded for needle in answer.contains):
        return False
    return answer.regex is None or re.search(answer.regex, speech) is not None


def _outcome_matches(outcome: Expected, observed: ObservedTurn) -> bool:
    """Whether one acceptable outcome matches the observed turn."""
    return _tools_match(
        outcome.tools, outcome.supporting_tools, observed.tools
    ) and _answer_matches(outcome.answer, observed.speech)


def case_correct(
    case: Case,
    observed: ObservedTurn,
    *,
    expected: Expected | Sequence[Expected] | None = None,
) -> bool | None:
    """Judge correctness, or ``None`` when there is no checkable predicate.

    ``expected`` overrides the default `case.expected` so a caller can score the
    scope-specific expectation (e.g. `case.expected_for(llm=True)`). It may be a single
    outcome or a disjunction of acceptable outcomes; the turn is correct when any of them
    matches. An expectation with no judgeable outcome (all empty, e.g. `nevermind`) is
    ``None``, not a pass.

    A state-scored case (`case.expect_changes`) is judged by the world it left instead: the
    turn is correct when the runner observed no unexpected state change, whichever tool got
    there. This ignores ``expected`` entirely, so such a case needs no scope-specific tool
    expectation.
    """
    if case.state_scored:
        if observed.unexpected_changes is None:
            return None
        return not observed.unexpected_changes and _only_permitted_tools(
            case.permitted_tools, observed.tools
        )

    alternatives = (
        as_alternatives(case.expected)
        if expected is None
        else as_alternatives(expected)
    )
    judgeable = [
        outcome
        for outcome in alternatives
        if outcome.tools or outcome.answer is not None
    ]
    if not judgeable:
        return None
    return any(_outcome_matches(outcome, observed) for outcome in judgeable)


def classify(
    case: Case,
    observed: ObservedTurn,
    *,
    expected: Expected | Sequence[Expected] | None = None,
) -> Bucket:
    """Map a case's observed turn onto an outcome bucket."""
    if not observed.resolved:
        return Bucket.UNRESOLVED
    correct = case_correct(case, observed, expected=expected)
    if correct is None:
        return Bucket.UNJUDGED
    if correct is False:
        return Bucket.WRONG_ACTION
    if observed.clarified:
        return Bucket.RESOLVED_AFTER_CLARIFICATION
    if observed.routed_locally:
        return Bucket.RESOLVED_LOCALLY
    return Bucket.LLM_CORRECT


def score_case(
    case: Case,
    observed: ObservedTurn,
    *,
    expected: Expected | Sequence[Expected] | None = None,
) -> CaseResult:
    """Score one case against the given (or default) expectation."""
    return CaseResult(
        case=case,
        observed=observed,
        bucket=classify(case, observed, expected=expected),
        correct=case_correct(case, observed, expected=expected),
    )


@dataclass(frozen=True)
class Scorecard:
    """Aggregate outcome across the corpus: the value dashboard for one run."""

    results: tuple[CaseResult, ...]

    @property
    def total(self) -> int:
        """Number of cases scored."""
        return len(self.results)

    @property
    def buckets(self) -> dict[Bucket, int]:
        """Bucket distribution, every bucket present (zeros included)."""
        counts = Counter(result.bucket for result in self.results)
        return {bucket: counts.get(bucket, 0) for bucket in Bucket}

    @property
    def routing_agreement(self) -> tuple[int, int]:
        """(agreements, total) between labelled ``routing_truth`` and measured routing.

        The core finding of a run: how often a case routes where the corpus predicted.
        """
        agree = sum(
            (result.case.routing_truth == ROUTING_LOCAL)
            == result.observed.routed_locally
            for result in self.results
        )
        return agree, self.total

    @property
    def totals(self) -> dict[str, int]:
        """Summed cost metrics across the corpus (Part D: end-to-end, not per-prompt)."""
        return {
            "generations": sum(r.observed.generations for r in self.results),
            "input_tokens": sum(r.observed.input_tokens for r in self.results),
            "output_tokens": sum(r.observed.output_tokens for r in self.results),
            "cache_read_tokens": sum(
                r.observed.cache_read_tokens for r in self.results
            ),
            "cache_creation_tokens": sum(
                r.observed.cache_creation_tokens for r in self.results
            ),
        }

    def render(self) -> str:
        """Render the scorecard as a plain-text table."""
        lines = [f"Scorecard ({self.total} cases)", ""]
        for bucket, count in self.buckets.items():
            lines.append(f"  {count:>3}  {bucket.value}")
        agree, total = self.routing_agreement
        lines += [
            "",
            f"routing agreement (labelled vs measured): {agree}/{total}",
            "cost totals: "
            + ", ".join(f"{key}={value}" for key, value in self.totals.items()),
        ]
        return "\n".join(lines)


def build_scorecard(results: list[CaseResult]) -> Scorecard:
    """Aggregate case results into a scorecard."""
    return Scorecard(results=tuple(results))
