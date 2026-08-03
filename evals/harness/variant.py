"""Measure the Tier-2 name-injection variant: summary-only vs summary+names.

The one comparison that isolates request-conditioned name injection (prompt-context
Tier 2). Both arms drive the **testbed** agent with the entity summary on and the
voice satellite placed in one room, so the only thing that differs between them is
whether the request-conditioned name block is injected. Room placement, model, and tool
config are held equal, so they cancel in the delta and what is left is Tier 2's effect:
does pre-loading in-room names shave the `find_entities` round-trip (fewer generations)
without changing which action is taken (task-success held).

Like the baseline, this needs a live key and cannot be faked: scoring a mocked response
would be scoring fabricated output. The delta to trust is **generations** and
**output_tokens** (docs/evaluation.md Part D); input-token deltas are cache-regime
dependent and reported for completeness only. The default run pairs both arms per case and
alternates which arm goes first. Use ``--trials 3`` with a targeted case set only when a
small difference would affect a decision.

    .venv/bin/python -m evals.harness.variant                    # full corpus, both arms
    .venv/bin/python -m evals.harness.variant --case turn-off-living-room-lamp
    .venv/bin/python -m evals.harness.variant --case implicit-cold --trials 3
    .venv/bin/python -m evals.harness.variant --area kitchen     # place satellite there
    .venv/bin/python -m evals.harness.variant --list             # show selection, no key
"""

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

import custom_components
from homeassistant import loader
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from .backing import (
    ExecutableWorld,
    Satellite,
    build_executable_world,
    register_satellite,
)
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
from .corpus import WAVE0_GOLDEN_SET, Case, Corpus, load_corpus
from .runner import run_case
from .scoring import CaseResult, Scorecard, build_scorecard
from .world import async_setup_local_agent

# Running as a plain script bypasses the repo-root conftest, so graft this repo's
# `custom_components/` onto the package search path (baseline.py does the same). Must
# precede the magic_mic imports below, hence the E402 waiver.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.const import DOMAIN  # noqa: E402
from custom_components.magic_mic.internal.claude.const import (  # noqa: E402
    CONF_CHAT_MODEL,
    DEFAULT,
)

VARIANT_ARTIFACT = RESULTS_DIR / "wave1_name_injection.json"

# The testbed proxy's unique_id suffix, from `conversation.async_setup_entry`.
_TESTBED_UNIQUE_SUFFIX = "_testbed"
# Where the satellite sits for the run. Injection is room-scoped, so it does nothing from
# an area-less turn; the room must hold named devices the cases reference. The corpus
# living room does (lamp, blinds), so it is the default.
DEFAULT_AREA = "living_room"
# The module global the proxy reads for the Tier-2 gate; patched off for the control arm.
_NAME_INJECTION_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_NAME_INJECTION"
)
_METRIC_KEYS = (
    "generations",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


@dataclass(frozen=True)
class PairedTrial:
    """One balanced pass over the selected cases, with both variant arms."""

    number: int
    off: Scorecard
    on: Scorecard
    orders: tuple[str, ...]


def _testbed_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the testbed proxy agent."""
    unique_id = f"{entry.entry_id}{_TESTBED_UNIQUE_SUFFIX}"
    for entity in er.async_get(hass).entities.values():
        if entity.platform == DOMAIN and entity.unique_id == unique_id:
            return entity.entity_id
    raise BaselineError(f"testbed agent {unique_id!r} not registered")


async def stand_up_testbed(
    hass: HomeAssistant, corpus: Corpus, api_key: str, *, area: str
) -> tuple[str, ExecutableWorld, Satellite, MockConfigEntry]:
    """Set up the core, the live integration, and the fixture world, satellite in ``area``.

    Mirrors `baseline.stand_up_agent`, but returns the **testbed** agent and config entry and
    places the satellite in a room (the baseline runs area-less). Placement is what gives
    injection a room to prefer, so both arms of this run carry it and it cancels in the
    delta.
    """
    # Force HA to re-scan for custom integrations so it discovers the grafted
    # `custom_components/` path, exactly as the baseline does for a script run.
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)

    await async_setup_local_agent(hass)

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: api_key})
    entry.add_to_hass(hass)
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise BaselineError("integration failed to set up (check the key is live)")
    await hass.async_block_till_done()

    world = await build_executable_world(hass, corpus.world)
    # build_executable_world already created the area (by spoken name); get_or_create returns
    # that same entry, so the satellite lands in the room the corpus entities sit in.
    area_id = ar.async_get(hass).async_get_or_create(area.replace("_", " ")).id
    satellite = register_satellite(hass, area_id=area_id)
    return _testbed_agent_id(hass, entry), world, satellite, entry


async def _run_variant_case(
    hass: HomeAssistant,
    agent_id: str,
    world: ExecutableWorld,
    satellite: Satellite,
    case: Case,
    *,
    entry: MockConfigEntry,
    names_on: bool,
) -> CaseResult:
    """Run one reset case under one arm of the variant."""
    gate = nullcontext() if names_on else patch(_NAME_INJECTION_FLAG, False)
    await apply_provider_options(hass, entry, case.provider_options)
    await world.reset(hass)
    with gate:
        return await run_case(
            hass,
            agent_id,
            case,
            llm=True,
            device_id=satellite.device_id,
        )


async def run_paired_trial(
    hass: HomeAssistant,
    agent_id: str,
    world: ExecutableWorld,
    satellite: Satellite,
    cases: Sequence[Case],
    *,
    entry: MockConfigEntry,
    trial_index: int = 0,
) -> PairedTrial:
    """Run case pairs, alternating which arm executes first.

    The order also flips between trials, so a targeted three-trial run gives each case both
    orderings without adding any extra work to the default one-trial smoke test.
    """
    off_results = []
    on_results = []
    orders = []
    for case_index, case in enumerate(cases):
        off_first = (case_index + trial_index) % 2 == 0
        order = (False, True) if off_first else (True, False)
        order_label = "off→on" if off_first else "on→off"
        orders.append(order_label)
        print(
            f"  [trial {trial_index + 1}] [{case_index + 1:>2}/{len(cases)}] "
            f"[{order_label}] {case.id} ...",
            flush=True,
        )
        for names_on in order:
            result = await _run_variant_case(
                hass,
                agent_id,
                world,
                satellite,
                case,
                entry=entry,
                names_on=names_on,
            )
            (on_results if names_on else off_results).append(result)
    return PairedTrial(
        number=trial_index + 1,
        off=build_scorecard(off_results),
        on=build_scorecard(on_results),
        orders=tuple(orders),
    )


def _arm_dict(
    scorecard: Scorecard, *, trial_numbers: Sequence[int] | None = None
) -> dict:
    """Reduce one arm's scorecard to a JSON-serializable record."""
    cases = [_result_to_dict(result) for result in scorecard.results]
    if trial_numbers is not None:
        for case, trial_number in zip(cases, trial_numbers, strict=True):
            case["trial"] = trial_number
    return {
        "buckets": {bucket.value: count for bucket, count in scorecard.buckets.items()},
        "cost_totals": scorecard.totals,
        "cases": cases,
    }


def _delta(off: dict[str, int], on: dict[str, int]) -> dict[str, int]:
    """names-on minus names-off, per cost metric. Negative means Tier 2 saved work."""
    return {key: on[key] - off[key] for key in on}


def _case_metrics(result: CaseResult) -> dict[str, int]:
    """Return the cost metrics used for one paired case delta."""
    return {key: getattr(result.observed, key) for key in _METRIC_KEYS}


def _paired_cases(trials: Sequence[PairedTrial]) -> list[dict]:
    """Serialize each paired observation and its metric deltas."""
    pairs = []
    for trial in trials:
        for order, off, on in zip(
            trial.orders, trial.off.results, trial.on.results, strict=True
        ):
            pairs.append(
                {
                    "trial": trial.number,
                    "id": off.case.id,
                    "order": order,
                    "summary_only": {
                        "bucket": off.bucket.value,
                        "correct": off.correct,
                    },
                    "summary_names": {
                        "bucket": on.bucket.value,
                        "correct": on.correct,
                    },
                    "delta": _delta(_case_metrics(off), _case_metrics(on)),
                }
            )
    return pairs


def _combine_trials(trials: Sequence[PairedTrial], *, names_on: bool) -> Scorecard:
    """Combine one arm across trials for aggregate reporting."""
    results = [
        result
        for trial in trials
        for result in (trial.on.results if names_on else trial.off.results)
    ]
    return build_scorecard(results)


def build_variant_artifact(
    model: str,
    area: str,
    corpus_name: str,
    trials: Sequence[PairedTrial],
) -> dict:
    """Assemble the two-arm artifact: metadata, each arm, and the isolated delta."""
    off = _combine_trials(trials, names_on=False)
    on = _combine_trials(trials, names_on=True)
    trial_numbers = tuple(
        trial.number for trial in trials for _result in trial.on.results
    )
    return {
        "run": {
            "kind": "wave1-name-injection",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "area": area,
            "prefer_local": False,
            "provider_options": "per-case",
            "effect_telemetry": True,
            "corpus": corpus_name,
            "cases": trials[0].on.total,
            "trials": len(trials),
            "pair_order": "alternating",
        },
        "arms": {
            "summary_only": _arm_dict(off, trial_numbers=trial_numbers),
            "summary_names": _arm_dict(on, trial_numbers=trial_numbers),
        },
        "delta": _delta(off.totals, on.totals),
        "trial_deltas": [
            {
                "trial": trial.number,
                "delta": _delta(trial.off.totals, trial.on.totals),
            }
            for trial in trials
        ],
        "pairs": _paired_cases(trials),
    }


def _render_delta(off: Scorecard, on: Scorecard, trials: Sequence[PairedTrial]) -> str:
    """Render the two arms' cost totals and the isolated delta as a plain-text block."""
    delta = _delta(off.totals, on.totals)
    lines = ["Name-injection delta (names-on minus names-off)", ""]
    lines.extend(
        f"  {key:>22}: {off.totals[key]:>8} -> {on.totals[key]:>8} ({delta[key]:+})"
        for key in on.totals
    )
    # correct is tri-state (True/False/None where a case is not correctness-scored), so
    # count truthy rather than sum, which would choke on None.
    off_correct = sum(1 for result in off.results if result.correct)
    on_correct = sum(1 for result in on.results if result.correct)
    lines += [
        "",
        f"  correct (task-success): {off_correct} -> {on_correct} (of {on.total})",
        "",
        "Per-case paired deltas (names-on minus names-off)",
    ]
    for pair in _paired_cases(trials):
        delta = pair["delta"]
        off_outcome = pair["summary_only"]
        on_outcome = pair["summary_names"]
        lines.append(
            f"  t{pair['trial']} {pair['id']:<32} {pair['order']:<6} "
            f"correct {off_outcome['correct']}→{on_outcome['correct']}  "
            f"generations {delta['generations']:+}  output {delta['output_tokens']:+}"
        )
    if len(trials) > 1:
        lines += ["", "Trial-total directions"]
        for trial in trials:
            delta = _delta(trial.off.totals, trial.on.totals)
            lines.append(
                f"  t{trial.number}: generations {delta['generations']:+}, "
                f"output {delta['output_tokens']:+}"
            )
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.variant",
        description="Measure the Tier-2 name-injection variant (summary-only vs +names).",
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
        "--area",
        default=DEFAULT_AREA,
        help=f"area to place the satellite in (default {DEFAULT_AREA})",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="paired trials to run (default 1; use 3 for a targeted decision check)",
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
    """Run both arms of the name-injection variant and persist the two-arm artifact."""
    args = _parse_args(argv)
    if args.trials < 1:
        raise BaselineError("--trials must be at least 1")
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
        f"Running name-injection variant: {len(cases)}/{len(corpus.cases)} cases, "
        f"model {model}, satellite in {args.area}, {args.trials} paired trial(s)\n"
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            agent_id, world, satellite, entry = await stand_up_testbed(
                hass, corpus, api_key, area=args.area
            )
            trials = [
                await run_paired_trial(
                    hass,
                    agent_id,
                    world,
                    satellite,
                    cases,
                    entry=entry,
                    trial_index=trial_index,
                )
                for trial_index in range(args.trials)
            ]

    off = _combine_trials(trials, names_on=False)
    on = _combine_trials(trials, names_on=True)

    print("\nsummary-only\n" + off.render())
    print("\nsummary+names\n" + on.render())
    print("\n" + _render_delta(off, on, trials))

    if subset and not args.out:
        print("\n(subset run: full artifact left untouched; pass --out to save)")
        return
    out_path = args.out or VARIANT_ARTIFACT
    written = write_artifact(
        build_variant_artifact(model, args.area, WAVE0_GOLDEN_SET.name, trials),
        out_path,
    )
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
