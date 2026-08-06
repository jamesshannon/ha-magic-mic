"""Go/no-go assembler tests: the record is derived, complete, and honest.

The assembler runs no model, so these are exact. What they guard is the property the
artifact exists for: every number traces to a keyed artifact, a missing or reshaped source
raises instead of degrading, and the negative results stay legible as negatives.
"""

import json

import pytest

from evals.harness.go_no_go import (
    PROMPT_FIELDS,
    TOKEN_WEIGHTS,
    GoNoGoError,
    Verdict,
    build_artifact,
    collect_verdicts,
    dig,
    load_artifact,
    render_report,
    spend_ratio,
    weighted_units,
)


def test_spend_ratio_reproduces_the_recorded_name_injection_figures() -> None:
    """The 1.73x prompt / 1.45x total figures re-derive from the raw arm token counts.

    These are the numbers the Tier-2 decision was taken on, so they must come from the
    artifact and the published price weights rather than from anyone's notes.
    """
    artifact = load_artifact("wave1_name_injection")
    names = dig(
        artifact, "wave1_name_injection", "arms", "summary_names", "cost_totals"
    )
    only = dig(artifact, "wave1_name_injection", "arms", "summary_only", "cost_totals")

    assert spend_ratio(names, only, PROMPT_FIELDS) == pytest.approx(1.73, abs=0.01)
    assert spend_ratio(names, only, tuple(TOKEN_WEIGHTS)) == pytest.approx(
        1.45, abs=0.01
    )


def test_weighted_units_applies_the_published_multipliers() -> None:
    """A cache write counts 1.25x, a cache read 0.1x, output 5x."""
    totals = {
        "cache_creation_tokens": 100,
        "cache_read_tokens": 100,
        "input_tokens": 100,
        "output_tokens": 100,
    }

    assert weighted_units(totals, PROMPT_FIELDS) == pytest.approx(100 + 125 + 10)
    assert weighted_units(totals, tuple(TOKEN_WEIGHTS)) == pytest.approx(
        100 + 125 + 10 + 500
    )


def test_a_missing_source_artifact_raises() -> None:
    """A go/no-go with a silently absent leg is worse than none, so this is fatal."""
    with pytest.raises(GoNoGoError, match="is missing"):
        load_artifact("wave1_no_such_read")


def test_a_reshaped_source_artifact_raises_and_names_the_path() -> None:
    """A renamed field fails loudly rather than dropping evidence from the record."""
    with pytest.raises(GoNoGoError, match="routing.off_cloud"):
        dig({"routing": {}}, "wave1_local_first", "routing", "off_cloud")


def test_every_gate_declares_what_ships() -> None:
    """A verdict without a shipped default lets a negative read be misread as a win."""
    for verdict in collect_verdicts():
        assert verdict.ships, verdict.gate
        assert verdict.question.endswith("?"), verdict.gate


def test_the_artifact_records_negatives_as_negatives() -> None:
    """Two of the four gates closed against their feature, and both ship off."""
    artifact = build_artifact(collect_verdicts())
    verdicts = artifact["verdicts"]

    assert verdicts["tokens.name_injection"]["outcome"] == "NEGATIVE"
    assert verdicts["tokens.name_injection"]["ships"].startswith("off")
    assert verdicts["tokens.capability_selection"]["outcome"] == "NEGATIVE"
    assert verdicts["tokens.capability_selection"]["ships"].startswith("off")
    # A negative gate has to say what would reopen it, or it reads as abandoned.
    assert verdicts["tokens.name_injection"]["reopens"]
    assert verdicts["tokens.capability_selection"]["reopens"]


def test_the_local_gate_says_the_setting_is_not_ours() -> None:
    """The one recommendation is not a shipped flag, and the record must not imply it is."""
    artifact = build_artifact(collect_verdicts())
    local = artifact["verdicts"]["local.prefer_local_intents"]

    assert local["outcome"] == "RECOMMEND_ON"
    assert "not ours to ship" in local["ships"]


def test_the_artifact_carries_the_unmeasured_levers() -> None:
    """The entity summary was never isolated; the record says so rather than implying a win."""
    artifact = build_artifact(collect_verdicts())
    levers = [entry["lever"] for entry in artifact["unmeasured"]]

    assert any("Tier 1" in lever for lever in levers)
    assert all(
        entry["why"] and entry["measures_it"] for entry in artifact["unmeasured"]
    )


def test_staleness_is_corpus_conditional_not_just_a_date() -> None:
    """Only golden-set runs predating the re-scoring are flagged; other corpora never are."""
    artifact = build_artifact(collect_verdicts())
    flagged = {
        name for name, meta in artifact["sources"].items() if meta["predates_rescoring"]
    }

    # Both are golden-set A/Bs recorded before 2026-08-06.
    assert flagged == {"wave1_name_injection", "wave1_selection_gate"}
    # The disambiguation run is older than some flagged artifacts but on its own corpus.
    assert (
        artifact["sources"]["wave1_disambiguation_live"]["predates_rescoring"] is False
    )


def test_the_artifact_is_serializable_and_has_no_open_gates() -> None:
    """Wave 1 closes only when every gate has an outcome; the record is the proof."""
    artifact = build_artifact(collect_verdicts())

    json.dumps(artifact)
    assert artifact["run"]["open_gates"] == 0
    assert artifact["run"]["gates"] == len(artifact["verdicts"])


def test_the_report_names_each_gate_and_its_outcome() -> None:
    """The text summary is what a reader scans, so it carries the verdict, not just a count."""
    artifact = build_artifact(collect_verdicts())

    report = render_report(artifact)

    for gate, verdict in artifact["verdicts"].items():
        assert gate in report
        assert verdict["outcome"] in report
    assert "unmeasured (4)" in report


def test_an_open_gate_is_counted() -> None:
    """An unfinished read must show up in open_gates rather than passing silently."""
    verdicts = [
        *collect_verdicts(),
        Verdict(
            gate="tokens.entity_summary",
            question="Does the entity summary beat HA's roster dump?",
            outcome="OPEN",
            ships="on, unmeasured",
            evidence={},
            sources=("wave1_local_first",),
        ),
    ]

    assert build_artifact(verdicts)["run"]["open_gates"] == 1
