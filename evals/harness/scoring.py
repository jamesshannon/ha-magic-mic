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
    ExpectedEffect,
    ExpectedTool,
    as_alternatives,
)
from .effects import ObservedEffect


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
    # Entity arguments the proxy rewrote before executing, as {"resolved", "supplied",
    # "tool_name"} records (find-entities.md Consumer 3). ``tools`` carries the arguments as
    # executed, so this is the only record of what the model itself passed. Empty when
    # nothing was rewritten, and on any artifact predating the instrumentation.
    entity_arguments: tuple[dict[str, Any], ...] = ()
    # ``None`` means a historical artifact predates effect instrumentation.
    effects: tuple[ObservedEffect, ...] | None = ()
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
    # Provider-round timing (docs/evaluation.md "Model TTFT / round duration"). ``ttft_ms``
    # is the first round's time to first content delta; ``round_duration_ms`` sums model
    # round durations across the turn. ``None`` TTFT means the run was not clocked (e.g. a
    # historical artifact predating the timing extension).
    ttft_ms: float | None = None
    round_duration_ms: float = 0.0


@dataclass(frozen=True)
class LatencySummary:
    """Latency percentiles over the model-driven turns of a run (all values in ms).

    Locally-routed turns never reach the model, so they carry no timing and are excluded;
    ``model_turns`` counts the turns that did. ``ttft_clocked`` can be lower than
    ``model_turns`` when a producer streamed without clocking (a historical artifact
    predating the timing extension), so the TTFT percentiles cover only the clocked turns.
    Every percentile is ``None`` when nothing contributed. TTFT is what the caller waits to
    hear (first content delta); round duration sums the model's per-round compute and
    excludes tool-execution time between rounds, so it is not wall-clock turn latency.
    """

    model_turns: int
    ttft_clocked: int
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    duration_p50_ms: float | None
    duration_p95_ms: float | None


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


def _effect_matches(expected: ExpectedEffect, actual: ObservedEffect) -> bool:
    """Match one effect by kind and exact named data values."""
    return expected.kind == actual.kind and all(
        key in actual.data and _arg_matches(value, actual.data[key])
        for key, value in expected.data.items()
    )


def _effects_match(
    expected: tuple[ExpectedEffect, ...],
    actual: tuple[ObservedEffect, ...] | None,
) -> bool | None:
    """Require an exact effect sequence, or return unknown for legacy telemetry."""
    if actual is None:
        return None if expected else True
    return len(expected) == len(actual) and all(
        _effect_matches(want, got) for want, got in zip(expected, actual, strict=True)
    )


def _outcome_matches(outcome: Expected, observed: ObservedTurn) -> bool | None:
    """Whether one acceptable outcome matches the observed turn."""
    ordinary_matches = _tools_match(
        outcome.tools, outcome.supporting_tools, observed.tools
    ) and _answer_matches(outcome.answer, observed.speech)
    if not ordinary_matches:
        return False
    return _effects_match(outcome.effects, observed.effects)


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
        return (
            not observed.unexpected_changes
            and _only_permitted_tools(case.permitted_tools, observed.tools)
            and not observed.effects
        )

    alternatives = (
        as_alternatives(case.expected)
        if expected is None
        else as_alternatives(expected)
    )
    judgeable = [
        outcome
        for outcome in alternatives
        if outcome.tools or outcome.effects or outcome.answer is not None
    ]
    if not judgeable:
        return None
    matches = [_outcome_matches(outcome, observed) for outcome in judgeable]
    if any(match is True for match in matches):
        return True
    if any(match is None for match in matches):
        return None
    return False


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


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """Linear-interpolated percentile of ``values``, or ``None`` when empty.

    Uses the inclusive method (`statistics.quantiles(..., method="inclusive")` and NumPy's
    default): rank ``pct/100 * (n - 1)``, interpolated between its neighbours. Over a small
    corpus p95 sits close to the maximum; that is a sample-size artifact, not a tail
    estimate to lean on. Rounded to 0.1 ms, since the underlying timing is network-inclusive
    and sub-millisecond precision is noise.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (pct / 100) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (rank - low), 1)


def _fmt_ms(value: float | None) -> str:
    """Render a millisecond percentile for the plain-text scorecard."""
    return "n/a" if value is None else f"{value:.1f}ms"


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

    @property
    def latency(self) -> LatencySummary:
        """Percentile latency over the turns that reached the model (Part D timing).

        A turn reached the model when it ran at least one generation; a locally-routed turn
        did not and contributes no timing. TTFT is summarised only over turns that were
        actually clocked (``ttft_ms is not None``); round duration over every model turn.
        """
        model_turns = [r.observed for r in self.results if r.observed.generations > 0]
        ttfts = [o.ttft_ms for o in model_turns if o.ttft_ms is not None]
        durations = [o.round_duration_ms for o in model_turns]
        return LatencySummary(
            model_turns=len(model_turns),
            ttft_clocked=len(ttfts),
            ttft_p50_ms=_percentile(ttfts, 50),
            ttft_p95_ms=_percentile(ttfts, 95),
            duration_p50_ms=_percentile(durations, 50),
            duration_p95_ms=_percentile(durations, 95),
        )

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
            self._render_latency(),
        ]
        return "\n".join(lines)

    def _render_latency(self) -> str:
        """Render the latency percentiles as one plain-text line."""
        latency = self.latency
        if latency.model_turns == 0:
            return "latency: no model-driven turns"
        return (
            f"latency ({latency.model_turns} model turns, "
            f"{latency.ttft_clocked} clocked): "
            f"TTFT p50={_fmt_ms(latency.ttft_p50_ms)} "
            f"p95={_fmt_ms(latency.ttft_p95_ms)} | "
            f"round p50={_fmt_ms(latency.duration_p50_ms)} "
            f"p95={_fmt_ms(latency.duration_p95_ms)}"
        )


def build_scorecard(results: list[CaseResult]) -> Scorecard:
    """Aggregate case results into a scorecard."""
    return Scorecard(results=tuple(results))
