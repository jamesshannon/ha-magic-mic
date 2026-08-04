"""Shadow-mode measurement for capability selection (docs/capability-selection.md).

Before capability selection may remove a real tool, it runs *beside* the full-roster
baseline: compute the `SelectionPlan` without applying it, then check whether the tool the
full-roster model actually used would have been exposed (docs "Shadow mode first"). This
is the offline half of that: it needs no live model and no key, because the baseline
artifact already records, per case, the utterance and the tools the model called. For each
case it recomputes the plan at a sweep of tool budgets and asks one question, "would the
used tool have survived selection?", producing recall@budget and the tool-count saving.

The recall metric is exact: a case is covered at a budget when every tool it used is in
that budget's plan. The *saving* is catalog-relative: the baseline artifact does not record
the full per-turn roster HA exposed, so the denominator here is the demo catalog's tool
count, which understates the real saving on a home full of scripts. Measuring the true
exposed roster needs a live shadow run that records `inner.tools` per turn; that is the
follow-up, and it does not change the recall numbers this produces.

    .venv/bin/python -m evals.harness.selection_shadow
    .venv/bin/python -m evals.harness.selection_shadow --budgets 6,8,10,24
    .venv/bin/python -m evals.harness.selection_shadow --artifact evals/results/other.json
"""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

from custom_components.magic_mic.capabilities.capability_selection import (
    Catalog,
    action_descriptor,
    default_catalog,
    extend_catalog,
    select_capabilities,
)

from .baseline import REPO_ROOT, RESULTS_DIR, write_artifact
from .corpus import Corpus, World, load_corpus

SHADOW_ARTIFACT = RESULTS_DIR / "wave1_capability_selection_shadow.json"
DEFAULT_SOURCE = RESULTS_DIR / "wave0_baseline.json"
# The tool budgets to sweep. The demo catalog is 24 tools, so 24 is the full-roster
# sanity point (recall must be 1.0); the tighter budgets are where selection has to choose
# and recall becomes informative.
DEFAULT_BUDGETS = (6, 8, 10, 12, 24)


class ShadowError(Exception):
    """A shadow run could not be assembled from the given inputs."""


@dataclass(frozen=True)
class CaseTools:
    """One case reduced to what shadow scoring needs: its request and the tools it used."""

    id: str
    utterance: str
    used: tuple[str, ...]


@dataclass(frozen=True)
class CaseCoverage:
    """Whether one case's used tools survived selection at one budget."""

    budget: int
    covered: bool
    exposed_count: int
    missed: tuple[str, ...]


@dataclass(frozen=True)
class CaseMiss:
    """A case not fully covered at a budget, with the used tools that were not exposed."""

    id: str
    missed: tuple[str, ...]


@dataclass(frozen=True)
class BudgetRecall:
    """Aggregate recall and saving at one budget across the scorable cases."""

    budget: int
    cases_total: int
    cases_covered: int
    tools_total: int
    tools_covered: int
    avg_exposed: float
    misses: tuple[CaseMiss, ...]

    @property
    def case_recall(self) -> float:
        """Fraction of cases whose every used tool was exposed."""
        return self.cases_covered / self.cases_total if self.cases_total else 1.0

    @property
    def tool_recall(self) -> float:
        """Fraction of (case, used-tool) pairs that were exposed."""
        return self.tools_covered / self.tools_total if self.tools_total else 1.0


def load_case_tools(artifact: dict) -> list[CaseTools]:
    """Reduce a scored artifact's cases to (id, utterance, used-tools).

    Reads the ``tools`` the model called per case; a case that called none (a pure
    text answer) contributes no tools and is not scorable for retrieval.
    """
    cases = artifact.get("cases")
    if not isinstance(cases, list):
        raise ShadowError("artifact has no 'cases' list (is it a scored baseline run?)")
    reduced: list[CaseTools] = []
    for case in cases:
        # Dedup while preserving first-seen order, so a tool used twice counts once.
        used: list[str] = []
        for call in case.get("tools") or []:
            name = call.get("name")
            if name and name not in used:
                used.append(name)
        reduced.append(
            CaseTools(id=case["id"], utterance=case["utterance"], used=tuple(used))
        )
    return reduced


def load_case_tools_from_corpus(corpus: Corpus) -> list[CaseTools]:
    """Reduce a corpus's cases to (id, utterance, expected-tools).

    The measurement basis differs from a scored artifact: here the ground-truth tool is
    the one the case *declares* it should call (its LLM-scope `expected`), not a tool a
    live model happened to pick. That is what lets the script-heavy home be measured
    without a key. The first acceptable outcome's tools are used; an answer-only case
    contributes none and is not scorable.
    """
    reduced: list[CaseTools] = []
    for case in corpus.cases:
        outcomes = case.expected_for(llm=True)
        used = (
            tuple(dict.fromkeys(tool.name for tool in outcomes[0].tools))
            if outcomes
            else ()
        )
        reduced.append(CaseTools(id=case.id, utterance=case.utterance, used=used))
    return reduced


def catalog_for_world(world: World) -> Catalog:
    """Build the demo catalog plus one action descriptor per script in ``world``.

    Home Assistant exposes each script as its own tool named by object id, so every
    ``script.*`` entity becomes an individually-retrieved single-tool descriptor whose
    document is its name and area. Non-script entities are already served by the demo
    bundles, so they add no descriptor here.
    """
    scripts = tuple(
        action_descriptor(
            entity.entity_id.split(".", 1)[1],
            entity.name,
            aliases=entity.aliases,
            description=entity.description,
            area=entity.area,
        )
        for entity in world.entities
        if entity.entity_id.startswith("script.")
    )
    return extend_catalog(default_catalog(), scripts)


def uncatalogued_tools(cases: Sequence[CaseTools], catalog: Catalog) -> set[str]:
    """Return used tools no descriptor declares (a catalog gap, never coverable)."""
    known = catalog.tool_names()
    return {tool for case in cases for tool in case.used if tool not in known}


def score_case(
    case: CaseTools, catalog: Catalog, budgets: Sequence[int]
) -> dict[int, CaseCoverage]:
    """Compute per-budget coverage for one case against the demo catalog.

    Availability (Stage 1) is grounded in the catalog itself: the demo catalog is treated
    as the installed roster, so every catalogued tool is available and an uncatalogued used
    tool is a permanent miss surfaced separately.
    """
    roster = catalog.tool_names()
    coverage: dict[int, CaseCoverage] = {}
    for budget in budgets:
        plan = select_capabilities(
            case.utterance, roster, catalog=catalog, budget=budget
        )
        missed = tuple(tool for tool in case.used if not plan.exposes(tool))
        coverage[budget] = CaseCoverage(
            budget=budget,
            covered=not missed,
            exposed_count=plan.tool_count,
            missed=missed,
        )
    return coverage


def shadow_recall(
    cases: Sequence[CaseTools], catalog: Catalog, budgets: Sequence[int]
) -> dict[int, BudgetRecall]:
    """Aggregate case- and tool-level recall at each budget over the scorable cases."""
    scorable = [case for case in cases if case.used]
    per_case = {case.id: score_case(case, catalog, budgets) for case in scorable}
    recall: dict[int, BudgetRecall] = {}
    for budget in budgets:
        covered = [case for case in scorable if per_case[case.id][budget].covered]
        tools_total = sum(len(case.used) for case in scorable)
        tools_covered = sum(
            len(case.used) - len(per_case[case.id][budget].missed) for case in scorable
        )
        avg_exposed = (
            sum(per_case[case.id][budget].exposed_count for case in scorable)
            / len(scorable)
            if scorable
            else 0.0
        )
        recall[budget] = BudgetRecall(
            budget=budget,
            cases_total=len(scorable),
            cases_covered=len(covered),
            tools_total=tools_total,
            tools_covered=tools_covered,
            avg_exposed=avg_exposed,
            misses=tuple(
                CaseMiss(case.id, per_case[case.id][budget].missed)
                for case in scorable
                if not per_case[case.id][budget].covered
            ),
        )
    return recall


def build_shadow_artifact(
    source: Path,
    catalog: Catalog,
    cases: Sequence[CaseTools],
    budgets: Sequence[int],
    *,
    basis: str = "actual-tool-use",
) -> dict:
    """Assemble the shadow artifact: metadata, per-budget recall, and per-case detail.

    ``basis`` records what recall is measured against: ``actual-tool-use`` (the tool a
    live model called, from a scored baseline) or ``expected-tool`` (the tool a corpus
    case declares it should call).
    """
    recall = shadow_recall(cases, catalog, budgets)
    gaps = sorted(uncatalogued_tools(cases, catalog))
    scorable = [case for case in cases if case.used]
    return {
        "run": {
            "kind": "wave1-capability-selection-shadow",
            "timestamp": datetime.now(UTC).isoformat(),
            "source_artifact": source.name,
            "basis": basis,
            "catalog_tools": len(catalog.tool_names()),
            "catalog_bundles": len(catalog.descriptors),
            "budgets": list(budgets),
            "cases_total": len(cases),
            "cases_scorable": len(scorable),
            "enforced": False,
        },
        "uncatalogued_tools": gaps,
        "recall": {
            str(budget): {
                "case_recall": round(result.case_recall, 4),
                "tool_recall": round(result.tool_recall, 4),
                "cases_covered": result.cases_covered,
                "cases_total": result.cases_total,
                "tools_covered": result.tools_covered,
                "tools_total": result.tools_total,
                "avg_exposed_tools": round(result.avg_exposed, 2),
                "avg_saved_tools": round(
                    len(catalog.tool_names()) - result.avg_exposed, 2
                ),
                "misses": [
                    {"id": miss.id, "missed": list(miss.missed)}
                    for miss in result.misses
                ],
            }
            for budget, result in recall.items()
        },
        "cases": [
            {
                "id": case.id,
                "utterance": case.utterance,
                "used": list(case.used),
                "coverage": {
                    str(budget): coverage.covered
                    for budget, coverage in score_case(case, catalog, budgets).items()
                },
            }
            for case in scorable
        ],
    }


def render_report(artifact: dict) -> str:
    """Render the shadow artifact's recall sweep as a plain-text block."""
    run = artifact["run"]
    lines = [
        f"Capability-selection shadow: {run['source_artifact']} ({run['basis']})",
        (
            f"  catalog {run['catalog_bundles']} bundles / {run['catalog_tools']} tools, "
            f"{run['cases_scorable']}/{run['cases_total']} cases used a tool"
        ),
        "",
        (
            f"  {'budget':>6}  {'case recall':>12}  {'tool recall':>12}  "
            f"{'avg exposed':>12}  {'avg saved':>10}"
        ),
    ]
    for budget in run["budgets"]:
        record = artifact["recall"][str(budget)]
        lines.append(
            f"  {budget:>6}  "
            f"{record['case_recall']:>11.0%}  "
            f"{record['tool_recall']:>11.0%}  "
            f"{record['avg_exposed_tools']:>12}  "
            f"{record['avg_saved_tools']:>10}"
        )
    if artifact["uncatalogued_tools"]:
        lines += [
            "",
            "  WARNING: used tools with no catalog descriptor (uncoverable):",
            "    " + ", ".join(artifact["uncatalogued_tools"]),
        ]
    misses = artifact["recall"][str(run["budgets"][0])]["misses"]
    if misses:
        lines += ["", f"  misses at the tightest budget ({run['budgets'][0]}):"]
        lines.extend(
            f"    {miss['id']}: {', '.join(miss['missed'])}" for miss in misses
        )
    return "\n".join(lines)


def _parse_budgets(value: str) -> tuple[int, ...]:
    """Parse a comma-separated budget list, rejecting non-positive values."""
    try:
        budgets = tuple(int(part) for part in value.split(","))
    except ValueError as err:
        raise ShadowError(f"invalid --budgets {value!r}") from err
    if not budgets or any(budget < 1 for budget in budgets):
        raise ShadowError("--budgets must be positive integers")
    return budgets


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.selection_shadow",
        description="Shadow-measure capability selection against a baseline or a corpus.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--artifact",
        type=Path,
        help=f"scored baseline artifact, recall vs actual tool use (default {DEFAULT_SOURCE.name})",
    )
    source.add_argument(
        "--corpus",
        type=Path,
        help="corpus file, recall vs each case's declared expected tool (builds the "
        "catalog from the corpus world, including its scripts)",
    )
    parser.add_argument(
        "--budgets",
        type=_parse_budgets,
        default=DEFAULT_BUDGETS,
        help="comma-separated tool budgets to sweep; the catalog size is always added",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the shadow artifact here (default derived from the source name)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the offline shadow sweep and persist the artifact."""
    args = _parse_args(argv)
    if args.corpus is not None:
        corpus = load_corpus(args.corpus)
        catalog = catalog_for_world(corpus.world)
        cases = load_case_tools_from_corpus(corpus)
        source = args.corpus
        basis = "expected-tool"
        default_out = RESULTS_DIR / f"{args.corpus.stem}_selection_shadow.json"
    else:
        source = args.artifact or DEFAULT_SOURCE
        if not source.exists():
            raise ShadowError(f"artifact not found: {source}")
        cases = load_case_tools(json.loads(source.read_text(encoding="utf-8")))
        catalog = default_catalog()
        basis = "actual-tool-use"
        default_out = SHADOW_ARTIFACT

    # Always measure the full-roster ceiling, whatever budgets were asked for.
    budgets = tuple(sorted(set(args.budgets) | {len(catalog.tool_names())}))
    artifact = build_shadow_artifact(source, catalog, cases, budgets, basis=basis)

    print(render_report(artifact))

    written = write_artifact(artifact, args.out or default_out)
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
