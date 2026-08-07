"""Measure run-to-run consistency: one case, one configuration, many trials.

Every other driver here reports a mean. Six cases run once, scored, aggregated. That
instrument cannot see the failure mode this one exists for: **the same utterance, in the same
home, at the same settings, doing two different things.**

It is not hypothetical. `paraphrased-target-couch-lamp` ("run the evening dim on the lamp by
the couch", against a living room holding a Reading Lamp and a Corner Floor Lamp) landed on a
different device, or asked instead of acting, in six of six observations across the 2026-08-06
runs. Those runs differed in configuration, so none of them isolates variance from
configuration. This driver holds the configuration fixed and varies nothing at all.

What it reports is a **distribution over outcomes**, not a score. An outcome is what the model
did, reduced to the parts a user would notice: did it act, on what, and was it right. One
distinct outcome across N trials is a consistent case. Several is the defect
`find-entities.md` "Path consistency" is about, quantified.

Deliberately not scored pass/fail. A case can be consistently wrong (bad, but predictable, and
a corpus or resolution problem) or inconsistently right (worse in a way a mean hides, because
the user cannot learn what the assistant does). Both are worth seeing; neither is a threshold
this driver should be asserting.

Run it with a live key. N trials is N live turns, so start small:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.consistency
        --case paraphrased-target-couch-lamp --trials 20
"""

import argparse
import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_test_home_assistant

import custom_components
from homeassistant.core import HomeAssistant

REPO_ROOT = Path(__file__).resolve().parents[2]

# Graft this repo's `custom_components/` onto the search path for a plain script run, exactly
# as `baseline.py` does; must precede the magic_mic-dependent imports below.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.internal.claude.const import (  # noqa: E402
    CONF_CHAT_MODEL,
    DEFAULT,
)

from .baseline import BaselineError, load_api_key, pin_pre_magic_roster  # noqa: E402
from .corpus import Case, Corpus, load_corpus  # noqa: E402
from .entity_id_tools import (  # noqa: E402
    _ENTITY_ARGUMENT_HINTS_FLAG,
    _ENTITY_ARGUMENTS_FLAG,
    _ENTITY_SUMMARY_FLAG,
    _NAME_INJECTION_FLAG,
    ARMS,
    DEFAULT_AREA,
    ENTITY_ID_CORPUS,
    ArgumentSource,
    _display_path,
    classify_source,
)
from .runner import place_satellite, run_case  # noqa: E402
from .scoring import CaseResult  # noqa: E402
from .variant import stand_up_testbed  # noqa: E402

CONSISTENCY_ARTIFACT = REPO_ROOT / "evals" / "results" / "wave1_consistency.json"

DEFAULT_TRIALS = 20
DEFAULT_ARM = "advertise"


@dataclass(frozen=True)
class Outcome:
    """One trial reduced to what a user would notice, and nothing else.

    Deliberately coarse. Token counts, latency, and which internal rung resolved the
    argument all vary run to run without the user seeing any difference; folding them in
    would report variance that does not exist at the only level that matters. What is here
    is: did it act, on what, and was the result right.
    """

    acted_on: tuple[str, ...]
    correct: bool | None
    source: str

    @property
    def asked(self) -> bool:
        """Whether the turn ended without acting, which reads to the user as a question."""
        return self.source == ArgumentSource.ABSENT

    def describe(self) -> str:
        """One human-readable line for the report."""
        if self.asked:
            return "asked instead of acting"
        target = ", ".join(self.acted_on) or "(nothing)"
        verdict = {True: "correct", False: "wrong", None: "unjudged"}[self.correct]
        return f"acted on {target} ({verdict}, {self.source})"


def reduce_outcome(
    result: CaseResult, script_tools: frozenset[str], live_ids: frozenset[str]
) -> Outcome:
    """Reduce one scored trial to its user-visible outcome."""
    source, supplied = classify_source(result, script_tools, live_ids)
    return Outcome(acted_on=supplied, correct=result.correct, source=source)


@dataclass(frozen=True)
class ConsistencyReport:
    """The distribution of outcomes over N trials of one case at one configuration."""

    case_id: str
    arm: str
    outcomes: tuple[Outcome, ...]
    summary_on: bool
    names_on: bool

    @property
    def trials(self) -> int:
        """Trials run."""
        return len(self.outcomes)

    @property
    def counts(self) -> list[tuple[Outcome, int]]:
        """Distinct outcomes with their frequencies, most common first."""
        return Counter(self.outcomes).most_common()

    @property
    def distinct(self) -> int:
        """How many different things the same utterance did."""
        return len(set(self.outcomes))

    @property
    def correct(self) -> int:
        """Trials that produced the expected world."""
        return sum(1 for outcome in self.outcomes if outcome.correct is True)

    @property
    def distinct_targets(self) -> int:
        """How many different devices were acted on across the trials.

        The sharpest number in the report. More than one means the same words moved
        different hardware, which no amount of averaging makes acceptable.
        """
        return len({outcome.acted_on for outcome in self.outcomes if outcome.acted_on})

    def render(self) -> str:
        """Render the distribution, widest signal first."""
        lines = [
            f"Consistency: {self.case_id} x {self.trials} trials",
            "",
            f"  arm            {self.arm} ({ARMS[self.arm].summary})",
            (
                f"  prompt         "
                f"{'entity summary' if self.summary_on else 'full roster'}"
                f"{', names injected' if self.names_on else ''}"
            ),
            "",
            f"  distinct outcomes   {self.distinct}",
            f"  distinct targets    {self.distinct_targets}",
            f"  correct             {self.correct}/{self.trials}",
            "",
            "  outcomes:",
        ]
        lines.extend(
            f"    {count:>3}x  {outcome.describe()}" for outcome, count in self.counts
        )
        return "\n".join(lines)


def build_artifact(report: ConsistencyReport, model: str) -> dict:
    """Assemble the artifact: the configuration, the distribution, and every trial."""
    return {
        "distribution": [
            {
                "acted_on": list(outcome.acted_on),
                "correct": outcome.correct,
                "count": count,
                "description": outcome.describe(),
                "source": outcome.source,
            }
            for outcome, count in report.counts
        ],
        "run": {
            "arm": report.arm,
            "case": report.case_id,
            "correct": report.correct,
            "corpus": ENTITY_ID_CORPUS.name,
            "distinct_outcomes": report.distinct,
            "distinct_targets": report.distinct_targets,
            "entity_summary": report.summary_on,
            "kind": "wave1-consistency",
            "model": model,
            "name_injection": report.names_on,
            "timestamp": datetime.now(UTC).isoformat(),
            "trials": report.trials,
        },
    }


def write_artifact(artifact: dict, path: Path) -> Path:
    """Write the artifact to ``path`` (creating parents) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


async def run_consistency(
    hass: HomeAssistant,
    corpus: Corpus,
    api_key: str,
    case: Case,
    *,
    arm: str,
    area: str,
    names_on: bool,
    summary_on: bool,
    trials: int,
) -> ConsistencyReport:
    """Run one case ``trials`` times at one fixed configuration."""
    script_tools = frozenset(script.tool_name for script in corpus.world.scripts)
    live_ids = frozenset(corpus.world.entity_ids())
    configuration = ARMS[arm]
    agent_id, world, satellite, _entry = await stand_up_testbed(
        hass, corpus, api_key, area=area
    )

    outcomes: list[Outcome] = []
    for trial in range(trials):
        await world.reset(hass)
        satellite_room = place_satellite(hass, satellite, case, default=area)
        print(
            f"  [{trial + 1:>3}/{trials}] {case.id} (from {satellite_room}) ...",
            flush=True,
        )
        with (
            patch(_ENTITY_ARGUMENTS_FLAG, configuration.entity_arguments),
            patch(_ENTITY_ARGUMENT_HINTS_FLAG, configuration.entity_argument_hints),
            patch(_ENTITY_SUMMARY_FLAG, summary_on),
            patch(_NAME_INJECTION_FLAG, names_on),
        ):
            result = await run_case(
                hass, agent_id, case, llm=True, device_id=satellite.device_id
            )
        outcomes.append(reduce_outcome(result, script_tools, live_ids))
    return ConsistencyReport(
        case_id=case.id,
        arm=arm,
        outcomes=tuple(outcomes),
        summary_on=summary_on,
        names_on=names_on,
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.consistency",
        description=(
            "Run one case repeatedly at one configuration and report the distribution "
            "of outcomes (find-entities.md 'Path consistency')."
        ),
    )
    parser.add_argument(
        "--case",
        required=True,
        metavar="ID",
        help="the single case id to repeat",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_TRIALS,
        help=f"how many times to run it, one live turn each (default {DEFAULT_TRIALS})",
    )
    parser.add_argument(
        "--arm",
        choices=sorted(ARMS),
        default=DEFAULT_ARM,
        help=f"the fixed Consumer 3 configuration (default {DEFAULT_ARM})",
    )
    parser.add_argument(
        "--area",
        default=DEFAULT_AREA,
        help=f"area to place the satellite in (default {DEFAULT_AREA})",
    )
    parser.add_argument(
        "--names",
        action="store_true",
        help="run with Tier-2 name injection on (default off)",
    )
    parser.add_argument(
        "--roster",
        action="store_true",
        help="turn the entity summary off, restoring the full roster and withholding "
        "find_entities",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=f"write the artifact here (default {CONSISTENCY_ARTIFACT.name})",
    )
    return parser.parse_args(argv)


def select_case(corpus: Corpus, case_id: str) -> Case:
    """Return the named case, or fail before a key is read or a turn is spent."""
    for case in corpus.cases:
        if case.id == case_id:
            return case
    known = ", ".join(sorted(case.id for case in corpus.cases))
    raise BaselineError(f"unknown case id: {case_id}; choose from {known}")


def check_trials(trials: int) -> int:
    """Reject a trial count that cannot show a distribution, before spending anything."""
    if trials < 2:
        raise BaselineError("--trials must be at least 2 to observe any variance")
    return trials


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the repeated-trial consistency measurement and persist the artifact."""
    args = _parse_args(argv)
    corpus = load_corpus(ENTITY_ID_CORPUS)
    case = select_case(corpus, args.case)
    trials = check_trials(args.trials)

    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    summary_on = not args.roster
    print(
        f"Running consistency: {case.id} x {trials} trials ({trials} turns), "
        f"model {model}, arm {args.arm}, prompt "
        f"{'entity summary' if summary_on else 'full roster, no find_entities'}, "
        f"names {'on' if args.names else 'off'}, area {args.area}\n"
        f'  utterance: "{case.utterance}"\n'
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            report = await run_consistency(
                hass,
                corpus,
                api_key,
                case,
                arm=args.arm,
                area=args.area,
                names_on=args.names,
                summary_on=summary_on,
                trials=trials,
            )

    print("\n" + report.render())
    written = write_artifact(
        build_artifact(report, model), args.out or CONSISTENCY_ARTIFACT
    )
    print(f"\nartifact: {_display_path(written)}")


if __name__ == "__main__":
    asyncio.run(main())
