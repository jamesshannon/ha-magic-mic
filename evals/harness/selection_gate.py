"""The capability-selection enforcement gate: full roster vs enforced, live.

Shadow mode measures recall@budget offline (`selection_shadow.py`); this measures the
other half the design requires before enforcement may flip on: that applying the plan to a
live request does not regress task success (docs/capability-selection.md "Gates"). It runs
the golden set through the **testbed** agent twice per case, in one balanced pass with the
arm order alternating:

- **full**: capability selection off, the roster the model sees today;
- **enforced**: capability selection on at a *binding* tool budget, so the plan actually
  prunes (the default 8 sits well under the 24-tool demo catalog, the point at which the
  shadow finding shows selection has to choose).

The number that gates enforcement is task-success non-regression. A case where the full arm
succeeded and the enforced arm did not is a regression; the harness attributes it by
checking whether a tool the full arm actually used was in the enforced arm's dropped set,
which separates a selection-caused miss (the gate's concern) from ordinary model
nondeterminism. Unauthorized executable-tool exposure cannot arise here by construction:
enforcement only ever *removes* tools from the policy-exposed roster, so the security
property is a tool-policy concern, not this gate's.

Like the baseline and the name-injection variant, this needs a live key and cannot be
faked; scoring a mocked response would be scoring fabricated output.

    .venv/bin/python -m evals.harness.selection_gate                 # full corpus, budget 8
    .venv/bin/python -m evals.harness.selection_gate --budget 12
    .venv/bin/python -m evals.harness.selection_gate --case turn-off-living-room-lamp
    .venv/bin/python -m evals.harness.selection_gate --list          # selection, no key
"""

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

import custom_components
from homeassistant.core import HomeAssistant

from .backing import ExecutableWorld, Satellite
from .baseline import (
    REPO_ROOT,
    RESULTS_DIR,
    BaselineError,
    _result_to_dict,
    apply_provider_options,
    load_api_key,
    pin_pre_magic_roster,
    select_cases,
    write_artifact,
)
from .corpus import WAVE0_GOLDEN_SET, Case, load_corpus
from .runner import run_case
from .scoring import CaseResult, Scorecard, build_scorecard
from .variant import DEFAULT_AREA, stand_up_testbed

# Running as a plain script bypasses the repo-root conftest, so graft this repo's
# `custom_components/` onto the package search path (baseline.py does the same). Must
# precede the magic_mic imports below, hence the E402 waiver.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.capabilities.capability_selection import (  # noqa: E402
    EnforcedSelection,
    enforce_on_roster,
)
from custom_components.magic_mic.internal.claude.const import (  # noqa: E402
    CONF_CHAT_MODEL,
    DEFAULT,
)

GATE_ARTIFACT = RESULTS_DIR / "wave1_selection_gate.json"
# The module global the testbed entity falls back to for the enforcement gate (the config
# entry does not carry the key, so this default decides). Patched True for the enforced arm.
_SELECTION_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_CAPABILITY_SELECTION"
)
# The name the testbed entity calls to compute the plan. Patched for the enforced arm with
# a collector that pins the run's budget and records each EnforcedSelection.
_ENFORCE_FN = "custom_components.magic_mic.testbed.entity.enforce_on_roster"
# A budget that binds: the demo catalog is 24 tools, so 8 forces selection to choose (the
# shadow finding's informative range). The default is deliberately not the 24-tool ceiling,
# which would barely prune and make the gate a near no-op.
DEFAULT_BUDGET = 8


class _SelectionCollector:
    """Pins the run's budget onto the entity's selection call and records the outcomes.

    The testbed entity calls `enforce_on_roster(utterance, exposed_tools)` with no budget,
    so it would use the const default (the full 24-tool catalog). Patching this collector in
    for the enforced arm injects the gate's budget and captures each `EnforcedSelection`, so
    a regression can be attributed to a dropped tool. Cleared per case.
    """

    def __init__(self, budget: int) -> None:
        """Initialize with the budget to enforce and an empty capture list."""
        self.budget = budget
        self.calls: list[EnforcedSelection] = []

    def __call__(
        self, utterance: str, exposed_tools: frozenset[str] | set[str]
    ) -> EnforcedSelection:
        """Compute the plan at the pinned budget and record it."""
        outcome = enforce_on_roster(utterance, exposed_tools, budget=self.budget)
        self.calls.append(outcome)
        return outcome


@dataclass(frozen=True)
class GatePair:
    """One case scored under both arms, with the enforced arm's prune detail."""

    case_id: str
    order: str
    full: CaseResult
    enforced: CaseResult
    exposed_before: int
    exposed_after: int
    dropped: tuple[str, ...]
    used_tools: tuple[str, ...]

    @property
    def regressed(self) -> bool:
        """The full arm succeeded and the enforced arm did not."""
        return self.full.correct is True and self.enforced.correct is not True

    @property
    def selection_attributed(self) -> bool:
        """A regression where a tool the full arm used was pruned by selection."""
        return self.regressed and bool(set(self.used_tools) & set(self.dropped))


async def _run_arm(
    hass: HomeAssistant,
    agent_id: str,
    world: ExecutableWorld,
    satellite: Satellite,
    case: Case,
    *,
    entry: MockConfigEntry,
    collector: _SelectionCollector | None,
) -> CaseResult:
    """Run one reset case under one arm. ``collector`` present means the enforced arm."""
    await apply_provider_options(hass, entry, case.provider_options)
    await world.reset(hass)
    if collector is None:
        return await run_case(
            hass, agent_id, case, llm=True, device_id=satellite.device_id
        )
    collector.calls.clear()
    with patch(_SELECTION_FLAG, True), patch(_ENFORCE_FN, collector):
        return await run_case(
            hass, agent_id, case, llm=True, device_id=satellite.device_id
        )


async def run_gate(
    hass: HomeAssistant,
    agent_id: str,
    world: ExecutableWorld,
    satellite: Satellite,
    cases: Sequence[Case],
    *,
    entry: MockConfigEntry,
    budget: int,
) -> list[GatePair]:
    """Run every case under both arms, alternating which arm goes first."""
    collector = _SelectionCollector(budget)
    pairs: list[GatePair] = []
    for index, case in enumerate(cases):
        full_first = index % 2 == 0
        order = "full→enforced" if full_first else "enforced→full"
        print(
            f"  [{index + 1:>2}/{len(cases)}] [{order}] {case.id} ...",
            flush=True,
        )
        results: dict[bool, CaseResult] = {}
        for enforced in (False, True) if full_first else (True, False):
            results[enforced] = await _run_arm(
                hass,
                agent_id,
                world,
                satellite,
                case,
                entry=entry,
                collector=collector if enforced else None,
            )
        # Every user turn re-wraps the seam once, so union the drops across turns; the
        # exposed counts come from the first (the case's opening) roster.
        first = collector.calls[0] if collector.calls else None
        dropped = sorted({tool for call in collector.calls for tool in call.dropped})
        pairs.append(
            GatePair(
                case_id=case.id,
                order=order,
                full=results[False],
                enforced=results[True],
                exposed_before=(len(first.kept) + len(first.dropped) if first else 0),
                exposed_after=len(first.kept) if first else 0,
                dropped=tuple(dropped),
                used_tools=tuple(
                    dict.fromkeys(tool.name for tool in results[False].observed.tools)
                ),
            )
        )
    return pairs


def _correct(scorecard: Scorecard) -> int:
    """Count cases the arm got right (``correct`` is tri-state; count only True)."""
    return sum(1 for result in scorecard.results if result.correct is True)


def _verdict(pairs: Sequence[GatePair]) -> str:
    """PASS when no task-success regression is attributable to selection."""
    attributed = [pair for pair in pairs if pair.selection_attributed]
    if attributed:
        return "REVIEW"
    # A regression not attributable to a dropped tool is model noise, not the gate's
    # concern, but it still warrants a look before the flag flips.
    return "PASS" if not any(pair.regressed for pair in pairs) else "PASS (noise only)"


def build_artifact(
    model: str, budget: int, corpus_name: str, pairs: Sequence[GatePair]
) -> dict:
    """Assemble the gate artifact: metadata, both arms, and per-case prune/regression."""
    full = build_scorecard([pair.full for pair in pairs])
    enforced = build_scorecard([pair.enforced for pair in pairs])
    return {
        "run": {
            "kind": "wave1-capability-selection-gate",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "budget": budget,
            "prefer_local": False,
            "provider_options": "per-case",
            "corpus": corpus_name,
            "cases": len(pairs),
            "pair_order": "alternating",
            "enforced": True,
        },
        "task_success": {
            "full": _correct(full),
            "enforced": _correct(enforced),
            "total": len(pairs),
            "regressions": sum(1 for pair in pairs if pair.regressed),
            "selection_attributed_regressions": sum(
                1 for pair in pairs if pair.selection_attributed
            ),
            "verdict": _verdict(pairs),
        },
        "exposure": {
            "avg_exposed_before": round(
                sum(pair.exposed_before for pair in pairs) / len(pairs), 2
            )
            if pairs
            else 0.0,
            "avg_exposed_after": round(
                sum(pair.exposed_after for pair in pairs) / len(pairs), 2
            )
            if pairs
            else 0.0,
        },
        "arms": {
            "full": {
                "buckets": {b.value: c for b, c in full.buckets.items()},
                "cost_totals": full.totals,
                "cases": [_result_to_dict(pair.full) for pair in pairs],
            },
            "enforced": {
                "buckets": {b.value: c for b, c in enforced.buckets.items()},
                "cost_totals": enforced.totals,
                "cases": [_result_to_dict(pair.enforced) for pair in pairs],
            },
        },
        "pairs": [
            {
                "id": pair.case_id,
                "order": pair.order,
                "full_correct": pair.full.correct,
                "enforced_correct": pair.enforced.correct,
                "regressed": pair.regressed,
                "selection_attributed": pair.selection_attributed,
                "exposed_before": pair.exposed_before,
                "exposed_after": pair.exposed_after,
                "used_tools": list(pair.used_tools),
                "dropped": list(pair.dropped),
            }
            for pair in pairs
        ],
    }


def render_report(pairs: Sequence[GatePair], budget: int) -> str:
    """Render the gate result: task-success delta, prune, and per-case detail."""
    full = build_scorecard([pair.full for pair in pairs])
    enforced = build_scorecard([pair.enforced for pair in pairs])
    total = len(pairs)
    avg_before = sum(pair.exposed_before for pair in pairs) / total if total else 0.0
    avg_after = sum(pair.exposed_after for pair in pairs) / total if total else 0.0
    lines = [
        f"Capability-selection gate (budget {budget}, {total} cases)",
        "",
        (
            f"  task success:  full {_correct(full)}/{total}"
            f"  ->  enforced {_correct(enforced)}/{total}"
        ),
        f"  avg tools exposed:  full {avg_before:.1f}  ->  enforced {avg_after:.1f}",
        f"  verdict: {_verdict(pairs)}",
        "",
        "  per-case (correct full->enforced, tools exposed, drops used):",
    ]
    for pair in pairs:
        flag = ""
        if pair.selection_attributed:
            flag = "  <- SELECTION-ATTRIBUTED REGRESSION"
        elif pair.regressed:
            flag = "  <- regression (model noise)"
        lines.append(
            f"    {pair.case_id:<34} {pair.full.correct!s:>5}->"
            f"{pair.enforced.correct!s:<5} "
            f"{pair.exposed_before:>2}->{pair.exposed_after:<2}"
            f"{flag}"
        )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.selection_gate",
        description="Live capability-selection enforcement gate (full vs enforced).",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        metavar="ID",
        help="run only this case id (repeatable)",
    )
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        metavar="CAT",
        help="run only cases in this category (repeatable)",
    )
    parser.add_argument(
        "--routing",
        choices=["local", "llm"],
        help="run only cases with this routing_truth",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=DEFAULT_BUDGET,
        help=f"tool budget the enforced arm assembles under (default {DEFAULT_BUDGET})",
    )
    parser.add_argument(
        "--area",
        default=DEFAULT_AREA,
        help=f"area to place the satellite in (default {DEFAULT_AREA})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the artifact here (a subset never overwrites the full artifact)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the selected case ids and exit (no key, no run)",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the enforcement gate and persist the two-arm artifact."""
    args = _parse_args(argv)
    if args.budget < 1:
        raise BaselineError("--budget must be a positive integer")
    corpus = load_corpus()
    cases = select_cases(corpus, args)
    if not cases:
        raise BaselineError("no cases matched the given filters")

    if args.list:
        for case in cases:
            print(f"{case.id}\t{case.category}\t{case.routing_truth}")
        return

    subset = len(cases) != len(corpus.cases)
    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    print(
        f"Running capability-selection gate: {len(cases)}/{len(corpus.cases)} cases, "
        f"model {model}, budget {args.budget}, satellite in {args.area}\n"
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            agent_id, world, satellite, entry = await stand_up_testbed(
                hass, corpus, api_key, area=args.area
            )
            pairs = await run_gate(
                hass,
                agent_id,
                world,
                satellite,
                cases,
                entry=entry,
                budget=args.budget,
            )

    print("\n" + render_report(pairs, args.budget))

    if subset and not args.out:
        print("\n(subset run: full artifact left untouched; pass --out to save)")
        return
    out_path = args.out or GATE_ARTIFACT
    written = write_artifact(
        build_artifact(model, args.budget, WAVE0_GOLDEN_SET.name, pairs), out_path
    )
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
