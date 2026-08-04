"""Run the disambiguation trajectory corpus against a live model for emergent Δturns.

The keyless `test_trajectory_corpus` scores the machinery: it scripts the provider, so a
`recovered: true` case passes only if the round-trip works end to end, but its turn count
is authored, not measured. This is the other half: it drives the *same* corpus worlds and
utterances through the real testbed proxy with a live key, letting the model generate its
own tool calls and speech. The question it answers is the one the script cannot: given an
ambiguous request, does the model actually ask before acting, and how many turns does the
task take?

Turn counts here are emergent. Each case is driven turn by turn and stops the moment the
action tool fires (`drive_trajectory(stop_on_action=...)`), so a model that guesses on the
first turn scores DIRECT (and fails a case that expected a clarifying round-trip) while one
that asks first and resolves on the follow-up scores RECOVERED. The `passed` count is
therefore "how often the live model matched the expected disambiguation shape", and the
mean-turns figure is the real Δturns input, not a replay of the script.

Each trajectory case gets a fresh headless Home Assistant: the cases have distinct worlds,
and `find_entities` searches every exposed entity, so a prior case's entities left in a
shared instance would leak into the next case's search. Standing each world up on its own
instance keeps the search scoped to the case under test.

Run it explicitly, never from the CI suite:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.trajectory_live
    .venv/bin/python -m evals.harness.trajectory_live --case ambiguous-two-bedroom-lights
    .venv/bin/python -m evals.harness.trajectory_live --list

The key is read from the environment, falling back to a project-root `.env`. Results render
to stdout and land as a JSON artifact under `evals/results/`.
"""

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from custom_components.magic_mic.const import DOMAIN
from custom_components.magic_mic.internal.claude.const import CONF_CHAT_MODEL, DEFAULT
from homeassistant import loader
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .backing import build_executable_world, register_satellite
from .baseline import (
    REPO_ROOT,
    RESULTS_DIR,
    BaselineError,
    load_api_key,
    pin_pre_magic_roster,
    write_artifact,
)
from .trajectory import (
    TrajectoryCase,
    TrajectoryEval,
    TurnObservation,
    build_scorecard,
    drive_trajectory,
    load_trajectory_corpus,
    render_scorecard,
    score_trajectory,
)
from .world import async_setup_local_agent

TRAJECTORY_LIVE_ARTIFACT = RESULTS_DIR / "wave1_disambiguation_live.json"

# The testbed proxy, not the stock baseline agent: disambiguation lives in the proxy
# (find_entities, tool policy). Its unique_id suffix, from `conversation.async_setup_entry`.
_TESTBED_UNIQUE_SUFFIX = "_testbed"


def _testbed_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the testbed proxy conversation agent."""
    unique_id = f"{entry.entry_id}{_TESTBED_UNIQUE_SUFFIX}"
    for entity in er.async_get(hass).entities.values():
        if entity.platform == DOMAIN and entity.unique_id == unique_id:
            return entity.entity_id
    raise BaselineError(f"testbed agent {unique_id!r} not registered")


async def run_case_live(
    hass: HomeAssistant, case: TrajectoryCase, api_key: str
) -> tuple[TrajectoryEval, list[TurnObservation]]:
    """Stand the case's world up on ``hass``, drive it live, and score it.

    Sets up the local core, the live integration, and the case's executable world, then
    drives the utterances straight at the testbed agent (bypassing HASSIL preemption, so
    the ambiguous request reaches the model). The drive stops when the action tool fires,
    and the end state is read from the world after the last turn.
    """
    # Force HA to re-scan for custom integrations so it discovers the grafted path.
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    await async_setup_local_agent(hass)

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: api_key})
    entry.add_to_hass(hass)
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise BaselineError("integration failed to set up (check the key is live)")
    await hass.async_block_till_done()

    world = await build_executable_world(hass, case.world)
    satellite = register_satellite(hass)
    agent_id = _testbed_agent_id(hass, entry)

    observations = await drive_trajectory(
        hass,
        agent_id,
        case.utterances,
        device_id=satellite.device_id,
        stop_on_action=case.action_tool,
    )
    final_state = {
        entity_id: hass.states.get(world.resolved.get(entity_id, entity_id)).state
        for entity_id in case.final_state
    }
    return score_trajectory(case, observations, final_state), observations


def select_cases(
    cases: Sequence[TrajectoryCase], wanted: Sequence[str]
) -> list[TrajectoryCase]:
    """Filter the corpus to ``wanted`` case ids, validating unknown ones."""
    if not wanted:
        return list(cases)
    known = {case.id for case in cases}
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise BaselineError(f"unknown case id(s): {', '.join(unknown)}")
    return [case for case in cases if case.id in wanted]


def _turn_to_dict(observation: TurnObservation) -> dict:
    """Reduce one driven turn to a JSON-serializable record."""
    return {
        "utterance": observation.utterance,
        "speech": observation.speech,
        "tools": [{"name": call.name, "args": call.args} for call in observation.tools],
        "resolved": observation.resolved,
        "continue_conversation": observation.continue_conversation,
    }


def build_artifact(
    results: Sequence[tuple[TrajectoryEval, list[TurnObservation]]],
    model: str,
    corpus: Path,
) -> dict:
    """Assemble the live-trajectory artifact: metadata, aggregates, and per-case turns."""
    scorecard = build_scorecard([result for result, _ in results])
    return {
        "run": {
            "kind": "wave1-disambiguation-live",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "corpus": corpus.name,
            "cases": scorecard.total,
        },
        "scorecard": {
            "total": scorecard.total,
            "passed": scorecard.passed,
            "recovered": scorecard.recovered,
            "misfired": scorecard.misfired,
            "no_action": scorecard.no_action,
            "mean_turns_to_complete": scorecard.mean_turns_to_complete,
        },
        "cases": [
            {
                "id": result.case.id,
                "tags": list(result.case.tags),
                "expected_shape": "recovered" if result.case.recovered else "direct",
                "outcome": result.outcome.name.lower(),
                "passed": result.passed,
                "turns_to_complete": result.turns_to_complete,
                "final_state": result.case.final_state,
                "turns": [_turn_to_dict(observation) for observation in observations],
            }
            for result, observations in results
        ],
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.trajectory_live",
        description="Drive the disambiguation trajectory corpus against a live model.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        help="trajectory corpus file (default: the shipped wave1 disambiguation set)",
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        metavar="ID",
        default=[],
        help="run only this case id (repeatable)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=f"write the artifact here (default {TRAJECTORY_LIVE_ARTIFACT.name})",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the selected case ids and exit (no key, no run)",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> None:
    """Drive the trajectory corpus against a live model and persist the artifact."""
    args = _parse_args(argv)
    corpus_path = args.corpus
    corpus = (
        load_trajectory_corpus(corpus_path)
        if corpus_path is not None
        else load_trajectory_corpus()
    )
    corpus_path = corpus_path or Path("wave1_disambiguation.yaml")
    cases = select_cases(corpus.cases, args.cases)
    if not cases:
        raise BaselineError("no cases matched the given filters")

    if args.list:
        for case in cases:
            shape = "recovered" if case.recovered else "direct"
            print(f"{case.id}\t{shape}\t{','.join(case.tags)}")
        return

    api_key = load_api_key()
    model = DEFAULT[CONF_CHAT_MODEL]
    print(
        f"Running live disambiguation trajectories: {len(cases)}/{len(corpus.cases)} "
        f"cases, model {model}\n"
    )

    results: list[tuple[TrajectoryEval, list[TurnObservation]]] = []
    for index, case in enumerate(cases, start=1):
        print(f"  [{index:>2}/{len(cases)}] {case.id} ...", flush=True)
        async with async_test_home_assistant() as hass:
            with pin_pre_magic_roster():
                results.append(await run_case_live(hass, case, api_key))

    scorecard = build_scorecard([result for result, _ in results])
    print("\n" + render_scorecard(scorecard))

    written = write_artifact(
        build_artifact(results, model, corpus_path),
        args.out or TRAJECTORY_LIVE_ARTIFACT,
    )
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
