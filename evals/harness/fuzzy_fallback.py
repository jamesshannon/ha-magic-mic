"""Measure the match-layer fuzzy fallback live (find_entities Consumer 1).

Drives the fuzzy-fallback corpus (`evals/corpus/wave1_fuzzy_fallback.yaml`) through the
testbed agent with the entity summary ON and name injection OFF. That pairing is the point:
the summary retires the exact roster and exposes `find_entities`, and with names not
injected the model never sees the exact entity names. So to act on "the floor lamp" it must
either look the name up (find_entities, Consumer 2) or name the device the way the user did
and let the match-layer fuzzy fallback recover it (Consumer 1, docs/find-entities.md).

Every case is state-scored, so correctness is the world the turn leaves behind: the right
device changed and no wrong one did. On top of the scorecard, the run classifies which
resolution path each case took, which is the question Consumer 1's evaluation gate asks:
how often does the model resolve a spoken name directly (via the fallback) versus reaching
for the find_entities lookup, and does either land the wrong target.

The fallback's retry rides the tool_use that was already happening; it emits no second
TOOL_CALL, so the trace shows the model's original (approximate) name. A successful actuator
call whose name is not an exact registered name therefore went through the fallback, since an
exact match is what missed. That is how a "direct" resolution is told apart from a lookup.

Run it with a live key (every case reaches the model; there is no local shortcut here):

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.fuzzy_fallback
"""

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import unicodedata
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_test_home_assistant

import custom_components
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"
CORPUS_DIR = REPO_ROOT / "evals" / "corpus"
FUZZY_CORPUS = CORPUS_DIR / "wave1_fuzzy_fallback.yaml"
FUZZY_ARTIFACT = RESULTS_DIR / "wave1_fuzzy_fallback.json"

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
from .runner import run_case  # noqa: E402
from .scoring import CaseResult, Scorecard, ToolCall, build_scorecard  # noqa: E402
from .variant import stand_up_testbed  # noqa: E402

# Default satellite placement for a case that does not set `satellite_area`: a neutral room
# with no devices of its own, so a bare name resolves house-wide rather than to a local device.
DEFAULT_AREA = "hallway"

# The module global the proxy reads for the Tier-2 name-injection gate. Patched off for the
# whole run so the model never receives the exact entity names and must resolve a spoken name.
_NAME_INJECTION_FLAG = (
    "custom_components.magic_mic.testbed.entity.DEFAULT_NAME_INJECTION"
)

# Intents that actuate a device (as opposed to a read or a lookup). A call to one of these
# carries the name the model chose to target, which is what the path classifier inspects.
_ACTUATOR_TOOLS = frozenset(
    {"HassTurnOff", "HassTurnOn", "HassLightSet", "HassSetPosition"}
)
_FIND_ENTITIES = "find_entities"


class ResolutionPath:
    """How a case reached (or failed to reach) its target. Values are artifact strings."""

    # The match-layer fuzzy fallback recovered the target from a spoken name, decisively or
    # by handing back candidates the model then chose (docs/find-entities.md Consumer 1).
    CONSUMER1_FALLBACK = "consumer1_fallback"
    # The model looked the name up with the find_entities tool (Consumer 2).
    CONSUMER2_FIND = "consumer2_find_entities"
    # Resolved without a name at all, by area/device-class slots; the fuzzy layer never ran.
    STRUCTURED_SLOTS = "structured_slots"
    # No device changed: the fallback could not decide, so the model asked (or did nothing).
    ASKED = "asked_no_action"


def _normalize(value: str) -> str:
    """Casefold and collapse whitespace, matching the scorer's name comparison."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class FuzzyResult:
    """One scored case plus the resolution path it took."""

    result: CaseResult
    path: str
    used_find_entities: bool
    actuated_names: tuple[str, ...]
    changed: tuple[str, ...]
    satellite_room: str | None
    passed_areas: tuple[str, ...]
    echoed_own_room: bool


def _passed_areas(tools: tuple[ToolCall, ...]) -> tuple[str, ...]:
    """The area values the model passed on device or lookup calls, flattened to strings."""
    areas: list[str] = []
    for tool in tools:
        if tool.name not in _ACTUATOR_TOOLS and tool.name != _FIND_ENTITIES:
            continue
        area = tool.args.get("area")
        if isinstance(area, str):
            areas.append(area)
        elif isinstance(area, list):
            areas.extend(str(item) for item in area)
    return tuple(areas)


def classify_path(
    result: CaseResult,
    exact_names: frozenset[str],
    changed: frozenset[str],
    satellite_room: str | None,
) -> FuzzyResult:
    """Reduce an observed turn to the resolution path it took.

    ``changed`` is the set of entities whose state actually moved, which is what tells a
    resolution apart from a failed attempt: a turn that named a device and moved it went
    through a resolver, while a turn that named a device and moved nothing had its name come
    back not-found (so the model asked). The find_entities tool is Consumer 2; a device that
    moved with no name at all was resolved by structured slots (area/device-class), where the
    fuzzy layer never runs; a device that moved after a spoken name went through the
    match-layer fallback (Consumer 1), since an exact name is what would have matched without
    it.

    ``satellite_room`` is where the request came from; ``echoed_own_room`` flags a turn that
    passed that room as an ``area`` on a named call, the prompt-adherence miss the fix relies
    on the model not making (find-entities.md "Room is a preference"). It is recorded whether
    or not the case still passed, so a harmless echo in a same-room case is visible too.
    """
    observed = result.observed
    used_find = any(tool.name == _FIND_ENTITIES for tool in observed.tools)
    actuated_names = tuple(
        str(tool.args["name"])
        for tool in observed.tools
        if tool.name in _ACTUATOR_TOOLS and tool.args.get("name")
    )
    named = bool(actuated_names)

    if not changed:
        path = ResolutionPath.ASKED
    elif used_find:
        path = ResolutionPath.CONSUMER2_FIND
    elif named:
        path = ResolutionPath.CONSUMER1_FALLBACK
    else:
        path = ResolutionPath.STRUCTURED_SLOTS

    passed_areas = _passed_areas(observed.tools)
    echoed_own_room = satellite_room is not None and any(
        _normalize(area) == _normalize(satellite_room) for area in passed_areas
    )
    return FuzzyResult(
        result=result,
        path=path,
        used_find_entities=used_find,
        actuated_names=actuated_names,
        changed=tuple(sorted(changed)),
        satellite_room=satellite_room,
        passed_areas=passed_areas,
        echoed_own_room=echoed_own_room,
    )


@dataclass(frozen=True)
class FuzzyReport:
    """Aggregate the fuzzy-fallback run: correctness plus resolution-path breakdown."""

    results: tuple[FuzzyResult, ...]

    @property
    def scorecard(self) -> Scorecard:
        """The standard scorecard over the scored outcomes."""
        return build_scorecard([fr.result for fr in self.results])

    @property
    def path_counts(self) -> dict[str, int]:
        """Count of cases by resolution path."""
        counts: dict[str, int] = {}
        for fr in self.results:
            counts[fr.path] = counts.get(fr.path, 0) + 1
        return counts

    @property
    def consumer1_correct(self) -> int:
        """Cases the match-layer fuzzy fallback resolved correctly (Consumer 1)."""
        return sum(
            1
            for fr in self.results
            if fr.path == ResolutionPath.CONSUMER1_FALLBACK and fr.result.correct
        )

    @property
    def echoed_own_room(self) -> int:
        """Turns that passed the requesting room as an area (a prompt-adherence miss)."""
        return sum(1 for fr in self.results if fr.echoed_own_room)

    def render(self) -> str:
        """Render the path breakdown above the standard scorecard."""
        total = len(self.results)
        lines = [f"Fuzzy fallback ({total} cases)", ""]
        for path, count in sorted(self.path_counts.items()):
            lines.append(f"  {count:>3}  {path}")
        lines += [
            "",
            f"  match-layer fallback (Consumer 1), correct: {self.consumer1_correct}",
            f"  echoed own room as area (prompt miss): {self.echoed_own_room}",
            "",
            self.scorecard.render(),
        ]
        return "\n".join(lines)


def _result_to_dict(fr: FuzzyResult) -> dict[str, object]:
    """Reduce one scored, classified case to a JSON record."""
    case = fr.result.case
    observed = fr.result.observed
    return {
        "actuated_names": list(fr.actuated_names),
        "bucket": fr.result.bucket.value,
        "category": case.category,
        "changed": list(fr.changed),
        "correct": fr.result.correct,
        "echoed_own_room": fr.echoed_own_room,
        "id": case.id,
        "passed_areas": list(fr.passed_areas),
        "path": fr.path,
        "satellite_room": fr.satellite_room,
        "speech": observed.speech,
        "tools": [{"args": t.args, "name": t.name} for t in observed.tools],
        "unexpected_changes": observed.unexpected_changes,
        "used_find_entities": fr.used_find_entities,
        "utterance": case.utterance,
    }


def build_artifact(report: FuzzyReport, model: str) -> dict:
    """Assemble the artifact: metadata, path aggregates, and per-case detail."""
    scorecard = report.scorecard
    return {
        "buckets": {b.value: c for b, c in scorecard.buckets.items()},
        "cases": [_result_to_dict(fr) for fr in report.results],
        "paths": report.path_counts,
        "run": {
            "kind": "wave1-fuzzy-fallback",
            "consumer1_correct": report.consumer1_correct,
            "corpus": FUZZY_CORPUS.name,
            "cases": len(report.results),
            "echoed_own_room": report.echoed_own_room,
            "entity_summary": True,
            "model": model,
            "name_injection": False,
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


async def run_fuzzy_fallback(
    hass: HomeAssistant,
    corpus: Corpus,
    api_key: str,
    cases: Sequence[Case],
    *,
    area: str,
) -> FuzzyReport:
    """Drive ``cases`` through the testbed with names off, and classify each resolution.

    The full corpus world is stood up so targets resolve; the world is reset before each case
    for order independence. Name injection is patched off for the whole run so the model
    never receives the exact names and must resolve a spoken one.
    """
    exact_names = frozenset(_normalize(entity.name) for entity in corpus.world.entities)
    entity_ids = tuple(entity.entity_id for entity in corpus.world.entities)
    agent_id, world, satellite, _entry = await stand_up_testbed(
        hass, corpus, api_key, area=area
    )
    results: list[FuzzyResult] = []
    with patch(_NAME_INJECTION_FLAG, False):
        for index, case in enumerate(cases, start=1):
            await world.reset(hass)
            # Place the satellite for this case: same-room vs different-room is what decides
            # where a bare name resolves and whether an echoed area scopes away the target.
            satellite.move_to(hass, _case_area_id(hass, case.satellite_area or area))
            satellite_room = satellite.area_name(hass)
            print(
                f"  [{index:>2}/{len(cases)}] {case.id} (from {satellite_room}) ...",
                flush=True,
            )
            before = _entity_states(hass, entity_ids)
            result = await run_case(
                hass, agent_id, case, llm=True, device_id=satellite.device_id
            )
            after = _entity_states(hass, entity_ids)
            changed = frozenset(
                entity_id
                for entity_id, state in after.items()
                if before.get(entity_id) != state
            )
            results.append(classify_path(result, exact_names, changed, satellite_room))
    return FuzzyReport(tuple(results))


def _case_area_id(hass: HomeAssistant, area_key: str) -> str:
    """Resolve a corpus area key (e.g. "den") to its area id, creating it if needed."""
    return ar.async_get(hass).async_get_or_create(area_key.replace("_", " ")).id


def _entity_states(
    hass: HomeAssistant, entity_ids: Sequence[str]
) -> dict[str, str | None]:
    """Snapshot the current state string of each entity, for the pre/post turn diff."""
    return {
        entity_id: state.state if (state := hass.states.get(entity_id)) else None
        for entity_id in entity_ids
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.fuzzy_fallback",
        description="Run the live match-layer fuzzy-fallback measurement (Consumer 1).",
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
        "--out",
        type=Path,
        help=f"write the artifact here (default {FUZZY_ARTIFACT.name})",
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


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the live fuzzy-fallback measurement and persist the artifact."""
    args = _parse_args(argv)
    corpus = load_corpus(FUZZY_CORPUS)
    cases = _select_cases(corpus, args.cases)
    if not cases:
        raise BaselineError("no cases matched the given filters")

    if args.list:
        for case in cases:
            print(f"{case.id}\t{case.category}\t{case.routing_truth}")
        return

    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    print(
        f"Running fuzzy-fallback: {len(cases)}/{len(corpus.cases)} cases, "
        f"model {model}, summary on / names off, area {args.area}\n"
    )

    async with async_test_home_assistant() as hass:
        # `pin_pre_magic_roster` keeps the LLM roster on the locked pre-magic set, matching
        # the baseline; `stand_up_testbed` sets up the core, the integration, and the world.
        with pin_pre_magic_roster():
            report = await run_fuzzy_fallback(
                hass, corpus, api_key, cases, area=args.area
            )

    print("\n" + report.render())
    out_path = args.out or FUZZY_ARTIFACT
    written = write_artifact(build_artifact(report, model), out_path)
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
