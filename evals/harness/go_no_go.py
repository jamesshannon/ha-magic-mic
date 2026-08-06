"""Assemble the Wave 1 go/no-go artifact from the reads that closed each gate.

This runs no model and spends nothing. It reads the locked artifacts the wave produced and
restates them as one record: the token verdict, the turn verdict, the local verdict, what
ships as a result, and what the wave did **not** measure. The build-sequence exit checklist
calls for the Wave 1 analogue of ``wave0_baseline.json``; this is it.

Why assemble rather than write it by hand. Every number here already exists in a keyed
artifact, and a hand-typed summary drifts the moment one of those is re-run. The verdicts
themselves are human decisions, recorded in the design docs and declared in the
``*_verdict`` builders below; only their evidence is pulled from disk, so a re-run that
moves a number moves this artifact with it. A missing artifact or a changed shape raises
rather than degrading to a partial record, because a go/no-go with a silently absent leg is
worse than no go/no-go.

Two honesty rules the wave's own checklist imposes on this file:

- **State the negative results as negatives.** Two of the three reads closed against the
  feature they were testing, and the shipped defaults are off. The artifact records the
  shipped default beside each verdict so the record cannot be read as three wins.
- **Name what is unmeasured.** A verdict on a lever that was never isolated is not a
  verdict. ``_UNMEASURED`` carries those, and they are part of the artifact, not a footnote.

Run it any time (no key, no network):

    .venv/bin/python -m evals.harness.go_no_go
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"
GO_NO_GO_ARTIFACT = RESULTS_DIR / "wave1_go_no_go.json"

# Token weights relative to one base input token, from Anthropic's published multipliers:
# a cache write costs 1.25x base, a cache read 0.1x. The output weight is the model's
# output-to-input price ratio (5x for the Haiku tier the wave measured on). They live here
# so a spend ratio in the artifact can be recomputed by hand from the arm token counts.
TOKEN_WEIGHTS = {
    "cache_creation_tokens": 1.25,
    "cache_read_tokens": 0.1,
    "input_tokens": 1.0,
    "output_tokens": 5.0,
}
PROMPT_FIELDS = ("input_tokens", "cache_creation_tokens", "cache_read_tokens")

# On 2026-08-06 two scoring changes re-based the Wave 0 golden set: durable effects judge an
# arg-bearing local win, and `set-bedroom-brightness` moved to state scoring. Staleness is
# corpus-conditional, not just a date. An artifact on another corpus is unaffected however
# old it is, and flagging it would cry wolf. A paired A/B on the golden set is also
# unaffected in its comparison, since both arms were scored the same way; only its absolute
# bucket counts are dated, which is what the flag means.
RESCORED_ON = datetime(2026, 8, 6, tzinfo=UTC)
RESCORED_CORPUS = "wave0_golden_set.yaml"


class GoNoGoError(RuntimeError):
    """A source artifact is missing, or does not carry the field a verdict cites."""


@dataclass(frozen=True)
class Verdict:
    """One gate: what was asked, how it landed, and what ships because of it.

    ``outcome`` is the answer to ``question``, not a grade. A read that closed against its
    feature is a completed gate, so ``NEGATIVE`` and ``PASS`` are both closed states; only
    ``OPEN`` blocks the wave. ``ships`` is the shipped default the outcome implies, stated
    even when it is "off", and ``reopens`` is the condition that would make the question
    live again.
    """

    gate: str
    question: str
    outcome: str
    ships: str
    evidence: dict[str, Any]
    sources: tuple[str, ...]
    reopens: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def load_artifact(name: str) -> dict[str, Any]:
    """Load one results artifact by stem, raising if the wave is missing a leg."""
    path = RESULTS_DIR / f"{name}.json"
    if not path.is_file():
        raise GoNoGoError(
            f"{path} is missing; the go/no-go cannot cite a read that was not run"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def dig(artifact: dict[str, Any], name: str, *path: str) -> Any:
    """Read a nested field, naming the artifact and path when the shape has changed."""
    cursor: Any = artifact
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            trail = ".".join(path)
            raise GoNoGoError(f"{name}.json has no {trail}; its shape changed")
        cursor = cursor[key]
    return cursor


def weighted_units(totals: dict[str, Any], fields: Sequence[str]) -> float:
    """Price-weight the given token counts into comparable units."""
    return sum(float(totals[key]) * TOKEN_WEIGHTS[key] for key in fields)


def spend_ratio(
    treatment: dict[str, Any], control: dict[str, Any], fields: Sequence[str]
) -> float:
    """Treatment spend over control spend, in weighted token units."""
    base = weighted_units(control, fields)
    if base == 0:
        raise GoNoGoError("control arm reports zero weighted tokens")
    return round(weighted_units(treatment, fields) / base, 4)


def _stale(run: dict[str, Any]) -> bool:
    """Whether a run's absolute scores predate the 2026-08-06 golden-set re-scoring.

    Both conditions have to hold: the run used the re-scored corpus, and it predates the
    change. An artifact on another corpus never goes stale from this.
    """
    timestamp = run.get("timestamp")
    if not timestamp or run.get("corpus") != RESCORED_CORPUS:
        return False
    return datetime.fromisoformat(timestamp) < RESCORED_ON


def token_verdicts() -> list[Verdict]:
    """The two token levers the wave tested: name injection, and capability selection."""
    injection = load_artifact("wave1_name_injection")
    names = dig(injection, "wave1_name_injection", "arms", "summary_names")
    only = dig(injection, "wave1_name_injection", "arms", "summary_only")
    names_cost = dig(
        injection, "wave1_name_injection", "arms", "summary_names", "cost_totals"
    )
    only_cost = dig(
        injection, "wave1_name_injection", "arms", "summary_only", "cost_totals"
    )
    injection_run = dig(injection, "wave1_name_injection", "run")

    gate = load_artifact("wave1_selection_gate")
    scripts = load_artifact("wave1_scripts_selection_shadow")
    localized = load_artifact("wave1_localized_catalog")

    tier2 = Verdict(
        gate="tokens.name_injection",
        question=(
            "Does request-conditioned name injection (prompt-context Tier 2) reduce spend?"
        ),
        outcome="NEGATIVE",
        ships="off (DEFAULT_NAME_INJECTION = False)",
        evidence={
            "prompt_spend_ratio": spend_ratio(names_cost, only_cost, PROMPT_FIELDS),
            "total_spend_ratio": spend_ratio(
                names_cost, only_cost, tuple(TOKEN_WEIGHTS)
            ),
            "token_weights": TOKEN_WEIGHTS,
            "arms": {"treatment": "summary_names", "control": "summary_only"},
            "cost_totals": {"summary_names": names_cost, "summary_only": only_cost},
            "task_success_identical": names["buckets"] == only["buckets"],
            "buckets": names["buckets"],
        },
        sources=("wave1_name_injection",),
        reopens=(
            "the first tool that consumes an entity_id, in Wave 3, measured against the "
            "cache-isolated second system block rather than the single cached block"
        ),
        notes=(
            (
                "Costs more, not less: per-turn names sit inside the single cached system "
                "block and re-prefill it every turn."
            ),
            (
                "No Wave 1 tool consumes an entity_id, so the find_entities round-trip the "
                "tier exists to skip is never taken."
            ),
            (
                "The selector keys on name overlap and domain keywords, so it goes silent "
                "on the oblique references the tier was justified by."
            ),
        )
        + (
            (
                (
                    "Scored before the 2026-08-06 re-scoring; both arms used the same "
                    "scorer, so the comparison holds and only the absolute buckets are dated."
                ),
            )
            if _stale(injection_run)
            else ()
        ),
    )

    selection = Verdict(
        gate="tokens.capability_selection",
        question="Can the tool roster be cut per request without losing task success?",
        outcome="NEGATIVE",
        ships="off (DEFAULT_CAPABILITY_SELECTION = False)",
        evidence={
            "task_success": dig(gate, "wave1_selection_gate", "task_success"),
            "exposure": dig(gate, "wave1_selection_gate", "exposure"),
            "budget": dig(gate, "wave1_selection_gate", "run", "budget"),
            "recall_at_8": {
                "overall": dig(
                    scripts,
                    "wave1_scripts_selection_shadow",
                    "recall",
                    "8",
                    "case_recall",
                ),
                "in_vocabulary": dig(
                    scripts,
                    "wave1_scripts_selection_shadow",
                    "recall_by_phrasing",
                    "in_vocabulary",
                    "8",
                    "case_recall",
                ),
                "out_of_vocabulary": dig(
                    scripts,
                    "wave1_scripts_selection_shadow",
                    "recall_by_phrasing",
                    "out_of_vocabulary",
                    "8",
                    "case_recall",
                ),
            },
            "localization": {
                "language": dig(
                    localized, "wave1_localized_catalog", "run", "language"
                ),
                "authored_english_catalog": dig(
                    localized,
                    "wave1_localized_catalog",
                    "localized_held_out",
                    "authored_english_catalog",
                    "covered",
                ),
                "derived_localized_catalog": dig(
                    localized,
                    "wave1_localized_catalog",
                    "localized_held_out",
                    "derived_localized_catalog",
                    "covered",
                ),
                "cases": dig(
                    localized,
                    "wave1_localized_catalog",
                    "localized_held_out",
                    "authored_english_catalog",
                    "cases",
                ),
            },
        },
        sources=(
            "wave1_selection_gate",
            "wave1_scripts_selection_shadow",
            "wave1_localized_catalog",
        ),
        reopens=(
            "the localized-document builder moving into the component, and a retrieval "
            "signal that reaches name-only scripts; both scoped into Wave 2"
        ),
        notes=(
            (
                "The task-success gate passes and exposure falls by nearly half, so the "
                "flag is not held by task success."
            ),
            (
                "Recall does not move with budget: the miss list is identical at 8, 12, 16, "
                "24, and 50, so it is a retrieval floor and a wider budget buys nothing."
            ),
            (
                "The independent blocker is localization: retrieval documents are English, "
                "and an English catalog scoring German utterances strands the corpus."
            ),
        ),
    )
    return [tier2, selection]


def turn_verdict() -> Verdict:
    """The turn lever: fuzzy resolution and the disambiguation recovery it enables."""
    live = load_artifact("wave1_disambiguation_live")
    fallback = load_artifact("wave1_fuzzy_fallback")
    scorecard = dig(live, "wave1_disambiguation_live", "scorecard")
    return Verdict(
        gate="turns.find_entities",
        question="Does deterministic fuzzy resolution complete a task in fewer turns?",
        outcome="PASS",
        ships="on (both consumers wired into the testbed roster)",
        evidence={
            "mean_turns_to_complete": round(scorecard["mean_turns_to_complete"], 4),
            "recovered": scorecard["recovered"],
            "misfired": scorecard["misfired"],
            "no_action": scorecard["no_action"],
            "passed": scorecard["passed"],
            "total": scorecard["total"],
            "consumer1_correct": dig(
                fallback, "wave1_fuzzy_fallback", "run", "consumer1_correct"
            ),
            "consumer1_cases": dig(fallback, "wave1_fuzzy_fallback", "run", "cases"),
            "echoed_own_room": dig(
                fallback, "wave1_fuzzy_fallback", "run", "echoed_own_room"
            ),
        },
        sources=("wave1_disambiguation_live", "wave1_fuzzy_fallback"),
        notes=(
            (
                "Every genuine ambiguity cost exactly one extra turn, and no case misfired "
                "by acting on a guess."
            ),
            "This is the one Wave 1 lever that ships on by default.",
        ),
    )


def local_verdict() -> Verdict:
    """The local lever: how much of the corpus leaves the cloud path entirely."""
    local = load_artifact("wave1_local_first")
    routing = dig(local, "wave1_local_first", "routing")
    agreement = dig(local, "wave1_local_first", "routing_agreement")
    return Verdict(
        gate="local.prefer_local_intents",
        question="How much of the corpus does the local matcher take off the cloud path?",
        outcome="RECOMMEND_ON",
        ships="not ours to ship: a Home Assistant pipeline setting, recommended in the README",
        evidence={
            "off_cloud": routing["off_cloud"],
            "deferred": routing["deferred"],
            "missed": routing["missed"],
            "unverifiable_local_wins": routing["unjudged_local"],
            "cases": dig(local, "wave1_local_first", "run", "cases"),
            "routing_agreement": agreement,
            "buckets": dig(local, "wave1_local_first", "buckets"),
        },
        sources=("wave1_local_first",),
        notes=(
            (
                "Nothing in the integration reads the setting, so the deliverable is a "
                "recorded verdict and a README step, not a code change."
            ),
            (
                "None of the three routing disagreements is the local matcher taking a turn "
                "it should not have, so the false-positive pre-emption the design warned "
                "about did not appear."
            ),
            (
                "The off-cloud count is a floor: `weather` matched locally and fell through "
                "only because the fixture home has no weather integration."
            ),
        ),
    )


# What the wave did not isolate. Each entry names the lever, why it stayed unmeasured, and
# what would measure it, so the artifact cannot be read as a complete accounting.
_UNMEASURED = (
    {
        "lever": "prompt-context Tier 1 (entity summary vs HA's Static Context roster dump)",
        "why": (
            "both name-injection arms ran with the summary applied, so the A/B never "
            "isolated the summary itself"
        ),
        "measures_it": "a summary-on against summary-off pass over the same corpus",
    },
    {
        "lever": "undo coverage of locally handled mutations",
        "why": (
            "locally routed turns never reach the proxy, so their mutations do not enter the "
            "journal; at a 14/25 off-cloud rate that is most of them"
        ),
        "measures_it": (
            "HassUndo as a local intent, plus a core intent chokepoint emitting the same "
            "outcome contract"
        ),
    },
    {
        "lever": "end-to-end voice latency (TTFT/TTLT, spoken duration)",
        "why": (
            "the wave clocks provider rounds only; no controlled pipeline driver exists yet"
        ),
        "measures_it": "the controlled voice pipeline layer, with a labelled real-engine profile",
    },
    {
        "lever": "argument correctness of a local action with no state or effect trace",
        "why": (
            "no Wave 0 case has that shape, so the residual is a shape rather than a gap"
        ),
        "measures_it": "an ExpectedEffect declared at the external boundary of such a capability",
    },
)


def build_artifact(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    """Assemble the record: verdicts, what ships, what is unmeasured, and the sources."""
    names = sorted({name for verdict in verdicts for name in verdict.sources})
    sources = {}
    for name in names:
        artifact = load_artifact(name)
        run = artifact.get("run", {})
        sources[name] = {
            "kind": run.get("kind"),
            "corpus": run.get("corpus"),
            "timestamp": run.get("timestamp"),
            "predates_rescoring": _stale(run),
        }
    return {
        "run": {
            "kind": "wave1-go-no-go",
            "timestamp": datetime.now(UTC).isoformat(),
            "wave": 1,
            "gates": len(verdicts),
            "open_gates": sum(1 for v in verdicts if v.outcome == "OPEN"),
        },
        "verdicts": {
            verdict.gate: {
                "question": verdict.question,
                "outcome": verdict.outcome,
                "ships": verdict.ships,
                "evidence": verdict.evidence,
                "reopens": verdict.reopens,
                "notes": list(verdict.notes),
                "sources": list(verdict.sources),
            }
            for verdict in verdicts
        },
        "unmeasured": [dict(entry) for entry in _UNMEASURED],
        "sources": sources,
    }


def render_report(artifact: dict[str, Any]) -> str:
    """Render the artifact as the plain-text summary a reader can scan."""
    lines = [f"Wave 1 go/no-go ({artifact['run']['timestamp']})", ""]
    for gate, verdict in artifact["verdicts"].items():
        lines.append(f"  {verdict['outcome']:<14} {gate}")
        lines.append(f"    {verdict['question']}")
        lines.append(f"    ships: {verdict['ships']}")
        if verdict["reopens"]:
            lines.append(f"    reopens: {verdict['reopens']}")
        lines.append("")
    lines.append(f"  unmeasured ({len(artifact['unmeasured'])}):")
    lines.extend(f"    - {entry['lever']}" for entry in artifact["unmeasured"])
    lines.append("")
    stale = [
        name for name, meta in artifact["sources"].items() if meta["predates_rescoring"]
    ]
    lines.append(
        f"  sources: {len(artifact['sources'])}, of which {len(stale)} predate re-scoring"
    )
    lines.extend(f"    - {name}" for name in stale)
    return "\n".join(lines)


def write_artifact(artifact: dict[str, Any], path: Path) -> Path:
    """Write the artifact to ``path`` (creating parents) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def collect_verdicts() -> list[Verdict]:
    """Every Wave 1 gate, in the order the exit checklist reads them."""
    return [*token_verdicts(), turn_verdict(), local_verdict()]


def main(argv: Sequence[str] | None = None) -> None:
    """Assemble the go/no-go artifact and print its summary."""
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.go_no_go",
        description="Assemble the Wave 1 go/no-go artifact from the wave's locked reads.",
    )
    parser.add_argument(
        "--out", type=Path, default=GO_NO_GO_ARTIFACT, help="write the artifact here"
    )
    args = parser.parse_args(argv)

    artifact = build_artifact(collect_verdicts())
    print(render_report(artifact))
    print(f"\nartifact: {write_artifact(artifact, args.out).relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
