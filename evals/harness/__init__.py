"""The Tier-B eval harness: corpus loader, scorer, and scorecard.

Deterministic and model-free. A driver (mock or live) produces an ``ObservedTurn`` per
case; the scorer here buckets it and the scorecard aggregates the run.
"""

from .corpus import (
    WAVE0_GOLDEN_SET,
    Case,
    Corpus,
    CorpusError,
    Entity,
    Expected,
    ExpectedAnswer,
    ExpectedTool,
    ProviderOptions,
    World,
    load_corpus,
    validate_corpus,
)
from .runner import observe_turn, run_case
from .scoring import (
    Bucket,
    CaseResult,
    ObservedTurn,
    Scorecard,
    ToolCall,
    build_scorecard,
    case_correct,
    classify,
    score_case,
)

__all__ = [
    "WAVE0_GOLDEN_SET",
    "Bucket",
    "Case",
    "CaseResult",
    "Corpus",
    "CorpusError",
    "Entity",
    "Expected",
    "ExpectedAnswer",
    "ExpectedTool",
    "ObservedTurn",
    "ProviderOptions",
    "Scorecard",
    "ToolCall",
    "World",
    "build_scorecard",
    "case_correct",
    "classify",
    "load_corpus",
    "observe_turn",
    "run_case",
    "score_case",
    "validate_corpus",
]
