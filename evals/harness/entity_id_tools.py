"""Measure entity_id-typed tool arguments live (find_entities Consumer 3, core-deltas CD1).

Drives `evals/corpus/wave1_entity_id_tools.yaml`, whose targets are exposed **scripts** with
an entity-selector parameter. HA serializes that parameter to
`{"type": "string", "format": "entity_id"}` and puts no entity_id in the prompt, so the model
is asked for an identifier it was never given. This is the corpus gap `evaluation.md` Part E
has carried since the first name-injection run: until now nothing in any corpus forced an
`entity_id` to exist.

The run is **paired per case**: the same case runs with entity-argument resolution off and on,
alternating which arm goes first. Off is stock Home Assistant, where the model's invented id
reaches `hass.services.async_call` and the script targets nothing. Cases are state-scored, so
that failure is visible as a world that did not move, and the arm delta is what Consumer 3 is
worth.

On top of correctness the run classifies **how the model supplied the argument**, which is the
question behind the feature: a spoken name means the model never even tried to produce an id
(so resolution is doing the work), an id-shaped value with no such entity is CD1's bug caught
in the act, and a live id means the model got there on its own (through `find_entities`, or
because the prompt happened to carry it).

Run it with a live key:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.entity_id_tools
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

# The module globals the proxy reads for the two gates this run controls. Both arms patch
# rather than inherit, so an arm cannot silently stop testing what it names when a shipped
# default changes.
_ENTITY_ARGUMENTS_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_ENTITY_ARGUMENTS"
)
_NAME_INJECTION_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_NAME_INJECTION"
)

_FIND_ENTITIES = "find_entities"


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
    entity_arguments: bool
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

    The trace records the arguments **as executed**, so in the resolution-on arm a resolved
    call reads as a live id. That is why the off arm is the one that shows what the model
    actually produced, and why the two arms are reported separately rather than pooled.
    """
    supplied = tuple(
        str(value)
        for tool in result.observed.tools
        if tool.name in script_tools
        for value in _argument_values(tool.args)
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


def _argument_values(args: dict[str, object]) -> list[object]:
    """Flatten a script call's argument values, list-valued fields included."""
    values: list[object] = []
    for value in args.values():
        if isinstance(value, list):
            values.extend(value)
        else:
            values.append(value)
    return values


def _is_entity_id(value: str) -> bool:
    """Whether a supplied value is shaped like an entity id (not whether it exists)."""
    domain, separator, object_id = value.partition(".")
    return bool(separator) and bool(domain) and bool(object_id) and " " not in value


@dataclass(frozen=True)
class EntityIdReport:
    """Both arms of the paired run, with the per-arm breakdown that explains the delta."""

    off: tuple[ArmResult, ...]
    on: tuple[ArmResult, ...]
    orders: tuple[str, ...]

    def scorecard(self, *, entity_arguments: bool) -> Scorecard:
        """The standard scorecard for one arm."""
        arm = self.on if entity_arguments else self.off
        return build_scorecard([item.result for item in arm])

    def source_counts(self, *, entity_arguments: bool) -> dict[str, int]:
        """Count of cases by argument source within one arm."""
        counts: dict[str, int] = {}
        for item in self.on if entity_arguments else self.off:
            counts[item.source] = counts.get(item.source, 0) + 1
        return counts

    def find_entities_calls(self, *, entity_arguments: bool) -> int:
        """Cases in one arm that reached for the lookup tool."""
        return sum(
            1
            for item in (self.on if entity_arguments else self.off)
            if item.used_find_entities
        )

    def render(self) -> str:
        """Render both arms side by side, correctness first, then how it got there."""
        off_card = self.scorecard(entity_arguments=False)
        on_card = self.scorecard(entity_arguments=True)
        lines = [
            f"Entity-id tool arguments ({len(self.off)} cases, paired)",
            "",
            (
                f"  correct  off={off_card.correct}  on={on_card.correct}  "
                f"delta={on_card.correct - off_card.correct:+d}"
            ),
            (
                "  find_entities calls  "
                f"off={self.find_entities_calls(entity_arguments=False)}  "
                f"on={self.find_entities_calls(entity_arguments=True)}"
            ),
            "",
            "  argument source:",
        ]
        sources = sorted(
            set(self.source_counts(entity_arguments=False))
            | set(self.source_counts(entity_arguments=True))
        )
        off_sources = self.source_counts(entity_arguments=False)
        on_sources = self.source_counts(entity_arguments=True)
        lines.extend(
            f"    {source:<20} off={off_sources.get(source, 0):>3}"
            f"  on={on_sources.get(source, 0):>3}"
            for source in sources
        )
        lines += [
            "",
            "resolution off:",
            off_card.render(),
            "",
            "resolution on:",
            on_card.render(),
        ]
        return "\n".join(lines)


def _arm_case_dict(item: ArmResult) -> dict[str, object]:
    """Reduce one arm's scored, classified case to a JSON record."""
    observed = item.result.observed
    return {
        "bucket": item.result.bucket.value,
        "correct": item.result.correct,
        "entity_arguments": item.entity_arguments,
        "id": item.result.case.id,
        "satellite_room": item.satellite_room,
        "source": item.source,
        "speech": observed.speech,
        "supplied": list(item.supplied),
        "tools": [{"args": tool.args, "name": tool.name} for tool in observed.tools],
        "unexpected_changes": observed.unexpected_changes,
        "used_find_entities": item.used_find_entities,
    }


def build_artifact(report: EntityIdReport, model: str, *, names_on: bool) -> dict:
    """Assemble the artifact: metadata, per-arm aggregates, and per-case detail."""
    off_card = report.scorecard(entity_arguments=False)
    on_card = report.scorecard(entity_arguments=True)
    return {
        "arms": {
            "off": {
                "buckets": {b.value: c for b, c in off_card.buckets.items()},
                "correct": off_card.correct,
                "find_entities_calls": report.find_entities_calls(
                    entity_arguments=False
                ),
                "sources": report.source_counts(entity_arguments=False),
            },
            "on": {
                "buckets": {b.value: c for b, c in on_card.buckets.items()},
                "correct": on_card.correct,
                "find_entities_calls": report.find_entities_calls(
                    entity_arguments=True
                ),
                "sources": report.source_counts(entity_arguments=True),
            },
        },
        "cases": [_arm_case_dict(item) for item in (*report.off, *report.on)],
        "run": {
            "kind": "wave1-entity-id-tools",
            "cases": len(report.off),
            "corpus": ENTITY_ID_CORPUS.name,
            "correct_delta": on_card.correct - off_card.correct,
            "entity_summary": True,
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
    names_on: bool,
) -> EntityIdReport:
    """Run each case under both resolution arms, alternating which arm goes first."""
    script_tools = frozenset(script.tool_name for script in corpus.world.scripts)
    live_ids = frozenset(corpus.world.entity_ids())
    agent_id, world, satellite, _entry = await stand_up_testbed(
        hass, corpus, api_key, area=area
    )

    off: list[ArmResult] = []
    on: list[ArmResult] = []
    orders: list[str] = []
    for index, case in enumerate(cases):
        off_first = index % 2 == 0
        orders.append("off→on" if off_first else "on→off")
        for entity_arguments in (False, True) if off_first else (True, False):
            await world.reset(hass)
            satellite_room = place_satellite(hass, satellite, case, default=area)
            print(
                f"  [{index + 1:>2}/{len(cases)}] {case.id} "
                f"(from {satellite_room}, resolution "
                f"{'on' if entity_arguments else 'off'}) ...",
                flush=True,
            )
            with (
                patch(_ENTITY_ARGUMENTS_FLAG, entity_arguments),
                patch(_NAME_INJECTION_FLAG, names_on),
            ):
                result = await run_case(
                    hass, agent_id, case, llm=True, device_id=satellite.device_id
                )
            source, supplied = classify_source(result, script_tools, live_ids)
            item = ArmResult(
                result=result,
                entity_arguments=entity_arguments,
                source=source,
                supplied=supplied,
                used_find_entities=any(
                    tool.name == _FIND_ENTITIES for tool in result.observed.tools
                ),
                satellite_room=satellite_room,
            )
            (on if entity_arguments else off).append(item)
    return EntityIdReport(tuple(off), tuple(on), tuple(orders))


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
        "--names",
        action="store_true",
        help="run both arms with Tier-2 name injection on (default off)",
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


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the live paired entity-argument measurement and persist the artifact."""
    args = _parse_args(argv)
    corpus = load_corpus(ENTITY_ID_CORPUS)
    cases = _select_cases(corpus, args.cases)
    if not cases:
        raise BaselineError("no cases matched the given filters")

    if args.list:
        for case in cases:
            print(f"{case.id}\t{case.category}\t{case.phrasing or '-'}")
        return

    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    print(
        f"Running entity-id tool arguments: {len(cases)}/{len(corpus.cases)} cases "
        f"x 2 arms, model {model}, names "
        f"{'on' if args.names else 'off'}, area {args.area}\n"
        f"  scripts: {_describe_scripts(corpus.world.scripts)}\n"
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            report = await run_entity_id_tools(
                hass, corpus, api_key, cases, area=args.area, names_on=args.names
            )

    print("\n" + report.render())
    out_path = args.out or ENTITY_ID_ARTIFACT
    written = write_artifact(
        build_artifact(report, model, names_on=args.names), out_path
    )
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
