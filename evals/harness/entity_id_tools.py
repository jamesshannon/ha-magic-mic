"""Measure entity_id-typed tool arguments live (find_entities Consumer 3, core-deltas CD1).

Drives `evals/corpus/wave1_entity_id_tools.yaml`, whose targets are exposed **scripts** with
an entity-selector parameter. HA serializes that parameter to
`{"type": "string", "format": "entity_id"}` and puts no entity_id in the prompt, so the model
is asked for an identifier it was never given. This is the corpus gap `evaluation.md` Part E
has carried since the first name-injection run: until now nothing in any corpus forced an
`entity_id` to exist.

The run is **paired per case**, across the arms in `ARMS`, rotating which one goes first.
Cases are state-scored, so a call against an id that names nothing is visible as a world that
did not move.

The first run (2026-08-06) answered the original question and raised a better one. With the
entity summary on, the prompt carries no entity names at all, so the model called
`find_entities` on all 12 turns, always passed back a live id, and the `off` and `resolve`
arms scored identically. Resolution never fired because nothing was ever broken. What that
leaves untested is the cost of getting there: every one of those turns spent a generation on a
lookup. The `advertise` arm says so in the entity field's own description, which is the only
way the model can know a name would be accepted, so it is scored on **generations** as much as
correctness.

On top of correctness the run classifies **how the model supplied the argument**: a spoken
name means it never tried to produce an id (resolution is doing the work), an id-shaped value
naming nothing is CD1's bug caught in the act, and a live id means it got there on its own.

Run it with a live key:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.entity_id_tools
    ... --arms off,resolve,advertise    # all three
    ... --roster                        # full name roster, no find_entities (CD1's setup)
"""

import argparse
import asyncio
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
RESULTS_DIR = REPO_ROOT / "evals" / "results"
CORPUS_DIR = REPO_ROOT / "evals" / "corpus"
ENTITY_ID_CORPUS = CORPUS_DIR / "wave1_entity_id_tools.yaml"
ENTITY_ID_ARTIFACT = RESULTS_DIR / "wave1_entity_id_tools.json"

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
from .corpus import Case, Corpus, Script, load_corpus  # noqa: E402
from .runner import place_satellite, run_case  # noqa: E402
from .scoring import CaseResult, Scorecard, build_scorecard  # noqa: E402
from .variant import stand_up_testbed  # noqa: E402

# Default satellite placement for a case that does not set `satellite_area`.
DEFAULT_AREA = "living_room"

# The module globals the proxy reads for the gates this run controls. Every arm patches
# rather than inherits, so an arm cannot silently stop testing what it names when a shipped
# default changes.
_ENTITY_ARGUMENTS_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_ENTITY_ARGUMENTS"
)
_ENTITY_ARGUMENT_HINTS_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_ENTITY_ARGUMENT_HINTS"
)
_ENTITY_SUMMARY_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_ENTITY_SUMMARY"
)
_NAME_INJECTION_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_NAME_INJECTION"
)

_FIND_ENTITIES = "find_entities"


@dataclass(frozen=True)
class Arm:
    """One configuration of the two Consumer 3 gates, run against every case."""

    label: str
    entity_arguments: bool
    entity_argument_hints: bool
    summary: str


# The arms, in the order the questions were asked. `off` is stock Home Assistant: an invented
# id reaches the service and targets nothing (CD1). `resolve` repairs that argument silently.
# `advertise` also tells the model resolution exists, which is the only arm where the model
# can choose to skip the lookup, so it is the one measured on generations rather than
# correctness alone.
ARMS: dict[str, Arm] = {
    "advertise": Arm(
        label="advertise",
        entity_arguments=True,
        entity_argument_hints=True,
        summary="resolution on, and the entity field says a name is accepted",
    ),
    "off": Arm(
        label="off",
        entity_arguments=False,
        entity_argument_hints=False,
        summary="stock Home Assistant: the model's id reaches the service unchanged",
    ),
    "resolve": Arm(
        label="resolve",
        entity_arguments=True,
        entity_argument_hints=False,
        summary="resolution on, but nothing tells the model it exists",
    ),
}

# Default pairing. `off` measured zero on 2026-08-06 because the model looked every id up
# before calling, so the open question is no longer whether a bad argument gets repaired but
# whether advertising the repair saves the lookup turn. Run `--arms off,resolve,advertise`
# for the full picture.
DEFAULT_ARMS = ("resolve", "advertise")


class ArgumentSource:
    """What the model put in the entity_id-typed argument. Values are artifact strings."""

    # A live entity id: the model produced a real target on its own (a lookup, or the prompt
    # happened to carry it). Nothing for Consumer 3 to do.
    LIVE_ID = "live_entity_id"
    # Id-shaped, but no such entity exists. This is CD1's bug in the act: stock HA passes it
    # to the service and the call silently targets nothing.
    INVENTED_ID = "invented_entity_id"
    # A friendly name or paraphrase where an id was asked for. The model did not try to
    # produce an id at all, so resolution is the only thing that can land the call.
    NAME = "spoken_name"
    # The script tool was never called: the model asked, looked up and gave up, or did
    # something else entirely.
    ABSENT = "no_script_call"


@dataclass(frozen=True)
class ArmResult:
    """One case under one arm: the scored outcome plus how the argument was supplied."""

    result: CaseResult
    arm: str
    source: str
    supplied: tuple[str, ...]
    used_find_entities: bool
    satellite_room: str | None


def classify_source(
    result: CaseResult,
    script_tools: frozenset[str],
    live_ids: frozenset[str],
) -> tuple[str, tuple[str, ...]]:
    """Reduce a turn to how it filled the entity_id argument, and the values it passed.

    ``script_tools`` are the corpus's exposed script tool names; ``live_ids`` every entity id
    that exists in the fixture world. A value that parses as ``domain.object_id`` and names a
    live entity is a real id; one that parses but names nothing is invented; anything else is
    a spoken name.

    `ToolCall.args` holds the arguments **as executed**, so a resolved call reads as a live
    id no matter what the model typed. `ObservedTurn.entity_arguments` is the proxy's record
    of the value it overwrote, and it wins here: the whole question is what the model
    produced, not what ran.
    """
    rewritten = _rewritten_arguments(result)
    supplied = tuple(
        str(rewritten.get((tool.name, field), value))
        for tool in result.observed.tools
        if tool.name in script_tools
        for field, value in _argument_fields(tool.args)
    )
    if not supplied:
        return ArgumentSource.ABSENT, ()
    # A call carrying several arguments is judged by its weakest one: if any value could not
    # name a real entity, the call could not have done what it claimed.
    if any(not _is_entity_id(value) for value in supplied):
        return ArgumentSource.NAME, supplied
    if any(value not in live_ids for value in supplied):
        return ArgumentSource.INVENTED_ID, supplied
    return ArgumentSource.LIVE_ID, supplied


def _argument_fields(args: dict[str, object]) -> list[tuple[str, object]]:
    """Flatten a script call's arguments to (field, value), list-valued fields included."""
    fields: list[tuple[str, object]] = []
    for field, value in args.items():
        if isinstance(value, list):
            fields.extend((field, item) for item in value)
        else:
            fields.append((field, value))
    return fields


def _rewritten_arguments(result: CaseResult) -> dict[tuple[str, str], object]:
    """Map (tool_name, field) to the value the model supplied before resolution.

    Multi-valued fields collapse to the whole supplied list rather than per-item values,
    which reads as a spoken name and so classifies the call by its weakest member. That is
    the same judgment `classify_source` makes elsewhere, and the corpus has no multi-valued
    case, so nothing currently depends on the finer distinction.
    """
    return {
        (record["tool_name"], field): value
        for record in result.observed.entity_arguments
        for field, value in record["supplied"].items()
    }


def _is_entity_id(value: str) -> bool:
    """Whether a supplied value is shaped like an entity id (not whether it exists)."""
    domain, separator, object_id = value.partition(".")
    return bool(separator) and bool(domain) and bool(object_id) and " " not in value


@dataclass(frozen=True)
class EntityIdReport:
    """Every arm of the paired run, with the breakdown that explains the differences."""

    arms: tuple[str, ...]
    results: dict[str, tuple[ArmResult, ...]]
    orders: tuple[str, ...]

    @property
    def cases(self) -> int:
        """Cases run per arm."""
        return len(self.results[self.arms[0]])

    def scorecard(self, arm: str) -> Scorecard:
        """The standard scorecard for one arm."""
        return build_scorecard([item.result for item in self.results[arm]])

    def source_counts(self, arm: str) -> dict[str, int]:
        """Count of cases by argument source within one arm."""
        counts: dict[str, int] = {}
        for item in self.results[arm]:
            counts[item.source] = counts.get(item.source, 0) + 1
        return counts

    def find_entities_calls(self, arm: str) -> int:
        """Cases in one arm that reached for the lookup tool."""
        return sum(1 for item in self.results[arm] if item.used_find_entities)

    def generations(self, arm: str) -> int:
        """Model round-trips the arm spent in total.

        The metric the advertised-naming arm lives or dies on: a case that resolves from
        the utterance costs one generation to call and one to speak, where a case that
        looks the id up first costs three.
        """
        return sum(item.result.observed.generations for item in self.results[arm])

    def render(self) -> str:
        """Render the arms side by side, correctness first, then how each got there."""
        width = max(len(arm) for arm in self.arms) + 2
        lines = [
            f"Entity-id tool arguments ({self.cases} cases, {len(self.arms)} arms)",
            "",
            *(f"  {arm:<{width}} {ARMS[arm].summary}" for arm in self.arms),
            "",
            _row("correct", width, [_correct(self.scorecard(a)) for a in self.arms]),
            _row("generations", width, [self.generations(a) for a in self.arms]),
            _row(
                "find_entities",
                width,
                [self.find_entities_calls(a) for a in self.arms],
            ),
            "",
            "  argument source:",
        ]
        counts = {arm: self.source_counts(arm) for arm in self.arms}
        sources = sorted(set().union(*(c.keys() for c in counts.values())))
        lines.extend(
            _row(
                f"  {source}", width, [counts[arm].get(source, 0) for arm in self.arms]
            )
            for source in sources
        )
        for arm in self.arms:
            lines += ["", f"{arm}:", self.scorecard(arm).render()]
        return "\n".join(lines)


def _row(label: str, width: int, values: list[int]) -> str:
    """One aligned metric row across the arms."""
    cells = "".join(f"{value:<{width}}" for value in values)
    return f"  {label:<18}{cells}"


def _correct(scorecard: Scorecard) -> int:
    """Count cases the arm got right (``correct`` is tri-state; count only True)."""
    return sum(1 for result in scorecard.results if result.correct is True)


def _arm_case_dict(item: ArmResult) -> dict[str, object]:
    """Reduce one arm's scored, classified case to a JSON record."""
    observed = item.result.observed
    return {
        "arm": item.arm,
        "bucket": item.result.bucket.value,
        "correct": item.result.correct,
        "generations": observed.generations,
        "id": item.result.case.id,
        "satellite_room": item.satellite_room,
        "source": item.source,
        "speech": observed.speech,
        "supplied": list(item.supplied),
        "tools": [{"args": tool.args, "name": tool.name} for tool in observed.tools],
        "unexpected_changes": observed.unexpected_changes,
        "used_find_entities": item.used_find_entities,
    }


def build_artifact(
    report: EntityIdReport, model: str, *, names_on: bool, summary_on: bool
) -> dict:
    """Assemble the artifact: metadata, per-arm aggregates, and per-case detail."""
    return {
        "arms": {
            arm: {
                "buckets": {
                    bucket.value: count
                    for bucket, count in report.scorecard(arm).buckets.items()
                },
                "correct": _correct(report.scorecard(arm)),
                "entity_argument_hints": ARMS[arm].entity_argument_hints,
                "entity_arguments": ARMS[arm].entity_arguments,
                "find_entities_calls": report.find_entities_calls(arm),
                "generations": report.generations(arm),
                "sources": report.source_counts(arm),
            }
            for arm in report.arms
        },
        "cases": [
            _arm_case_dict(item) for arm in report.arms for item in report.results[arm]
        ],
        "run": {
            "kind": "wave1-entity-id-tools",
            "arms": list(report.arms),
            "cases": report.cases,
            "corpus": ENTITY_ID_CORPUS.name,
            "entity_summary": summary_on,
            "model": model,
            "name_injection": names_on,
            "orders": list(report.orders),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    }


def write_artifact(artifact: dict, path: Path) -> Path:
    """Write the artifact to ``path`` (creating parents) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


async def run_entity_id_tools(
    hass: HomeAssistant,
    corpus: Corpus,
    api_key: str,
    cases: Sequence[Case],
    *,
    area: str,
    arms: Sequence[str],
    names_on: bool,
    summary_on: bool,
) -> EntityIdReport:
    """Run each case under every arm, rotating which arm goes first."""
    script_tools = frozenset(script.tool_name for script in corpus.world.scripts)
    live_ids = frozenset(corpus.world.entity_ids())
    agent_id, world, satellite, _entry = await stand_up_testbed(
        hass, corpus, api_key, area=area
    )

    collected: dict[str, list[ArmResult]] = {arm: [] for arm in arms}
    orders: list[str] = []
    for index, case in enumerate(cases):
        # Rotate rather than alternate: with more than two arms a straight flip would leave
        # one of them always running against a warm cache and a settled world.
        offset = index % len(arms)
        order = [*arms[offset:], *arms[:offset]]
        orders.append("→".join(order))
        for label in order:
            arm = ARMS[label]
            await world.reset(hass)
            satellite_room = place_satellite(hass, satellite, case, default=area)
            print(
                f"  [{index + 1:>2}/{len(cases)}] {case.id} "
                f"(from {satellite_room}, arm {label}) ...",
                flush=True,
            )
            with (
                patch(_ENTITY_ARGUMENTS_FLAG, arm.entity_arguments),
                patch(_ENTITY_ARGUMENT_HINTS_FLAG, arm.entity_argument_hints),
                patch(_ENTITY_SUMMARY_FLAG, summary_on),
                patch(_NAME_INJECTION_FLAG, names_on),
            ):
                result = await run_case(
                    hass, agent_id, case, llm=True, device_id=satellite.device_id
                )
            source, supplied = classify_source(result, script_tools, live_ids)
            collected[label].append(
                ArmResult(
                    result=result,
                    arm=label,
                    source=source,
                    supplied=supplied,
                    used_find_entities=any(
                        tool.name == _FIND_ENTITIES for tool in result.observed.tools
                    ),
                    satellite_room=satellite_room,
                )
            )
    return EntityIdReport(
        arms=tuple(arms),
        results={arm: tuple(items) for arm, items in collected.items()},
        orders=tuple(orders),
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.entity_id_tools",
        description=(
            "Run the live entity_id-argument measurement (find_entities Consumer 3)."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        metavar="ID",
        help="run only this case id (repeatable)",
    )
    parser.add_argument(
        "--area",
        default=DEFAULT_AREA,
        help=f"area to place the satellite in (default {DEFAULT_AREA})",
    )
    parser.add_argument(
        "--arms",
        default=",".join(DEFAULT_ARMS),
        help=(
            "comma-separated arms to run, from "
            f"{', '.join(sorted(ARMS))} (default {','.join(DEFAULT_ARMS)})"
        ),
    )
    parser.add_argument(
        "--names",
        action="store_true",
        help="run every arm with Tier-2 name injection on (default off)",
    )
    parser.add_argument(
        "--roster",
        action="store_true",
        help=(
            "turn the entity summary off, restoring HA's full name roster. This also "
            "withholds find_entities, which ships with the summary, so it reproduces the "
            "environment CD1 was reported from: names in the prompt, no ids, no lookup"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=f"write the artifact here (default {ENTITY_ID_ARTIFACT.name})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the selected case ids and exit (no key, no run)",
    )
    return parser.parse_args(argv)


def _select_cases(corpus: Corpus, wanted: Sequence[str] | None) -> list[Case]:
    """Return the selected cases, validating any explicit ids."""
    if not wanted:
        return list(corpus.cases)
    known = {case.id for case in corpus.cases}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise BaselineError(f"unknown case id(s): {', '.join(unknown)}")
    return [case for case in corpus.cases if case.id in set(wanted)]


def _describe_scripts(scripts: Sequence[Script]) -> str:
    """One line naming the exposed script tools a run will expose."""
    return ", ".join(script.tool_name for script in scripts) or "(none)"


def _select_arms(requested: str) -> list[str]:
    """Return the requested arms in the order given, validating each name."""
    arms = [label.strip() for label in requested.split(",") if label.strip()]
    if not arms:
        raise BaselineError("no arms selected")
    if unknown := [label for label in arms if label not in ARMS]:
        raise BaselineError(
            f"unknown arm(s): {', '.join(unknown)}; choose from {', '.join(sorted(ARMS))}"
        )
    if len(set(arms)) != len(arms):
        raise BaselineError("an arm was requested more than once")
    return arms


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the live paired entity-argument measurement and persist the artifact."""
    args = _parse_args(argv)
    corpus = load_corpus(ENTITY_ID_CORPUS)
    cases = _select_cases(corpus, args.cases)
    arms = _select_arms(args.arms)
    if not cases:
        raise BaselineError("no cases matched the given filters")

    if args.list:
        for case in cases:
            print(f"{case.id}\t{case.category}\t{case.phrasing or '-'}")
        return

    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    summary_on = not args.roster
    print(
        f"Running entity-id tool arguments: {len(cases)}/{len(corpus.cases)} cases "
        f"x {len(arms)} arms ({len(cases) * len(arms)} turns), model {model}, names "
        f"{'on' if args.names else 'off'}, prompt "
        f"{'entity summary' if summary_on else 'full roster, no find_entities'}, "
        f"area {args.area}\n"
        f"  scripts: {_describe_scripts(corpus.world.scripts)}\n"
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            report = await run_entity_id_tools(
                hass,
                corpus,
                api_key,
                cases,
                area=args.area,
                arms=arms,
                names_on=args.names,
                summary_on=summary_on,
            )

    print("\n" + report.render())
    out_path = args.out or ENTITY_ID_ARTIFACT
    written = write_artifact(
        build_artifact(report, model, names_on=args.names, summary_on=summary_on),
        out_path,
    )
    print(f"\nartifact: {_display_path(written)}")


def _display_path(path: Path) -> str:
    """Show an artifact path relative to the repo when it is inside it, else in full."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        return str(resolved.relative_to(REPO_ROOT))
    return str(resolved)


if __name__ == "__main__":
    asyncio.run(main())
