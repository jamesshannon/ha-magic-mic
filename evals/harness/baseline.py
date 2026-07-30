"""Run the Wave 0 live baseline: the number every later change is measured against.

This is the one eval step that needs a real key and cannot be faked (scoring a mocked
response would be scoring fabricated output). It stands up a headless Home Assistant,
loads the integration with a live key, builds the fixture world, and drives the whole
golden-set corpus through the **baseline** agent (`Claude (baseline)`, the stock
provider agent) with `prefer_local` OFF, meaning every utterance is driven straight at
the LLM with no HASSIL preemption. It reuses the same `run_case` scorer the mocked
Tier-B tests use; only the key and the real network differ.

Run it explicitly, never from the CI suite:

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.baseline

The key is read from the environment, falling back to a project-root `.env`. Results
render to stdout and land as a JSON artifact under `evals/results/` so Wave 1 can report
Δtokens / Δturns / Δhassil-rate against it.
"""

import argparse
import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import os
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
from homeassistant.helpers import entity_registry as er

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"
BASELINE_ARTIFACT = RESULTS_DIR / "wave0_baseline.json"

# Running as a plain script bypasses the repo-root conftest, so graft this repo's
# `custom_components/` onto the package search path exactly as it does; otherwise HA's
# loader reports "Integration not found". Must precede the magic_mic imports below.
_REPO_CC = str(REPO_ROOT / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

from custom_components.magic_mic.const import DOMAIN  # noqa: E402
from custom_components.magic_mic.internal.claude.const import (  # noqa: E402
    CONF_CHAT_MODEL,
    CONF_WEB_FETCH,
    CONF_WEB_SEARCH,
    CONF_WEB_SEARCH_USER_LOCATION,
    DEFAULT,
)

from .backing import (  # noqa: E402
    ExecutableWorld,
    Satellite,
    build_executable_world,
    register_satellite,
)
from .corpus import WAVE0_GOLDEN_SET, Case, Corpus, load_corpus  # noqa: E402
from .runner import run_case  # noqa: E402
from .scoring import CaseResult, Scorecard, build_scorecard  # noqa: E402
from .world import async_setup_local_agent  # noqa: E402

# The baseline is the stock provider agent, not the testbed proxy (which is pass-through
# at Wave 0 but is the thing later waves change). Its unique_id suffix, from
# `conversation.async_setup_entry`.
_BASELINE_UNIQUE_SUFFIX = "_claude_baseline"


@contextmanager
def pin_pre_magic_roster() -> Iterator[None]:
    """Disable the server-side web tools for the baseline run, eval-only.

    The shipped agent enables `web_search`/`web_fetch` (the banked "free magic"), but the
    locked baseline is the pre-magic reference: `build-sequence.md` captures it before the
    magic is banked. Patching the provider `DEFAULT` for the run keeps
    `python -m evals.harness.baseline` reproducing that reference regardless of what ships,
    since the agent merges `DEFAULT` into its options at each turn.
    """
    with patch.dict(
        DEFAULT,
        {
            CONF_WEB_FETCH: False,
            CONF_WEB_SEARCH: False,
            CONF_WEB_SEARCH_USER_LOCATION: False,
        },
    ):
        yield


class BaselineError(RuntimeError):
    """Raised when the baseline cannot run (no key, no agent, bad corpus)."""


def load_api_key() -> str:
    """Return the Anthropic key from the environment or a project-root `.env`.

    An exported ``ANTHROPIC_API_KEY`` wins; otherwise the `.env` file at the repo root
    is parsed for it (a minimal ``KEY=VALUE`` read, no dependency on python-dotenv).
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()

    env_path = REPO_ROOT / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "ANTHROPIC_API_KEY":
                return value.strip().strip("'\"")

    raise BaselineError(
        "ANTHROPIC_API_KEY not found in the environment or a project-root .env"
    )


def _baseline_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the stock baseline conversation agent."""
    ent_reg = er.async_get(hass)
    unique_id = f"{entry.entry_id}{_BASELINE_UNIQUE_SUFFIX}"
    for entity in ent_reg.entities.values():
        if entity.platform == DOMAIN and entity.unique_id == unique_id:
            return entity.entity_id
    raise BaselineError(f"baseline agent {unique_id!r} not registered")


async def stand_up_agent(
    hass: HomeAssistant, corpus: Corpus, api_key: str
) -> tuple[str, ExecutableWorld, Satellite]:
    """Set up the local core, the live integration, and the fixture world.

    Returns the baseline agent's entity id, the world handle (for per-case reset), and the
    voice satellite the turns are issued from (area-less here, so the baseline scores as it
    did before locations existed). The config-entry setup makes a real ``models.list`` call,
    so a bad key fails loudly here before any turn runs.
    """
    # Force HA to re-scan for custom integrations so it discovers the grafted
    # `custom_components/` path (the `enable_custom_integrations` fixture's job).
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)

    await async_setup_local_agent(hass)

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: api_key})
    entry.add_to_hass(hass)
    if not await hass.config_entries.async_setup(entry.entry_id):
        raise BaselineError("integration failed to set up (check the key is live)")
    await hass.async_block_till_done()

    world = await build_executable_world(hass, corpus.world)
    satellite = register_satellite(hass)
    return _baseline_agent_id(hass, entry), world, satellite


async def run_baseline(
    hass: HomeAssistant,
    corpus: Corpus,
    api_key: str,
    cases: Sequence[Case],
) -> Scorecard:
    """Drive ``cases`` through the live baseline agent and score the run.

    The full corpus world is always stood up (so slot targets resolve), but only
    ``cases`` are driven. The fixture world is reset before each case, so a case runs
    against a clean, order-independent world rather than one a prior case mutated. Every
    case runs at the LLM scope (`prefer_local` OFF), scored against its
    ``expected_for(llm=True)`` expectation.
    """
    agent_id, world, satellite = await stand_up_agent(hass, corpus, api_key)
    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        await world.reset(hass)
        print(f"  [{index:>2}/{len(cases)}] {case.id} ...", flush=True)
        results.append(
            await run_case(
                hass, agent_id, case, llm=True, device_id=satellite.device_id
            )
        )
    return build_scorecard(results)


def _result_to_dict(result: CaseResult) -> dict:
    """Reduce a scored case to a JSON-serializable record for the artifact."""
    case = result.case
    observed = result.observed
    return {
        "id": case.id,
        "utterance": case.utterance,
        "category": case.category,
        "routing_truth": case.routing_truth,
        "resolves_at_wave0": case.resolves_at_wave0,
        "bucket": result.bucket.value,
        "correct": result.correct,
        "resolved": observed.resolved,
        "routed_locally": observed.routed_locally,
        "speech": observed.speech,
        "tools": [{"name": tool.name, "args": tool.args} for tool in observed.tools],
        "generations": observed.generations,
        "input_tokens": observed.input_tokens,
        "output_tokens": observed.output_tokens,
        "cache_read_tokens": observed.cache_read_tokens,
        "cache_creation_tokens": observed.cache_creation_tokens,
    }


def build_artifact(scorecard: Scorecard, model: str, *, subset: bool) -> dict:
    """Assemble the artifact: metadata, aggregates, and per-case detail."""
    agree, total = scorecard.routing_agreement
    return {
        "run": {
            "kind": "wave0-subset" if subset else "wave0-live-baseline",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "prefer_local": False,
            "web_tools": False,
            "corpus": WAVE0_GOLDEN_SET.name,
            "cases": scorecard.total,
            "subset": subset,
        },
        "buckets": {bucket.value: count for bucket, count in scorecard.buckets.items()},
        "routing_agreement": {"agree": agree, "total": total},
        "cost_totals": scorecard.totals,
        "cases": [_result_to_dict(result) for result in scorecard.results],
    }


def write_artifact(artifact: dict, path: Path) -> Path:
    """Write the artifact to ``path`` (creating parents) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def select_cases(corpus: Corpus, args: argparse.Namespace) -> list[Case]:
    """Filter the corpus by the CLI selectors, validating unknown ids.

    With no selector every case is returned. ``--case`` / ``--category`` / ``--routing``
    intersect (a case must satisfy all supplied filters).
    """
    if args.cases:
        known = {case.id for case in corpus.cases}
        unknown = sorted(set(args.cases) - known)
        if unknown:
            raise BaselineError(f"unknown case id(s): {', '.join(unknown)}")

    cases = list(corpus.cases)
    if args.cases:
        wanted = set(args.cases)
        cases = [case for case in cases if case.id in wanted]
    if args.categories:
        categories = set(args.categories)
        cases = [case for case in cases if case.category in categories]
    if args.routing:
        cases = [case for case in cases if case.routing_truth == args.routing]
    return cases


def _resolve_out_path(args: argparse.Namespace, *, subset: bool) -> Path | None:
    """Decide where (if anywhere) to write the artifact.

    A subset run never overwrites the locked baseline: it writes only when ``--out`` is
    given. A full run writes the baseline unless ``--out`` redirects it.
    """
    if args.out:
        return args.out
    return None if subset else BASELINE_ARTIFACT


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.baseline",
        description="Run the live Wave 0 baseline, or a subset of it.",
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
        "--out",
        type=Path,
        help="write the artifact here (a subset never overwrites the baseline)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the selected case ids and exit (no key, no run)",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> None:
    """Run the live baseline (or a subset) and persist the artifact."""
    args = _parse_args(argv)
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
    label = "subset" if subset else "live baseline"
    print(
        f"Running Wave 0 {label}: {len(cases)}/{len(corpus.cases)} cases, "
        f"model {model}, prefer_local OFF\n"
    )

    async with async_test_home_assistant() as hass:
        with pin_pre_magic_roster():
            scorecard = await run_baseline(hass, corpus, api_key, cases)

    print("\n" + scorecard.render())
    out_path = _resolve_out_path(args, subset=subset)
    if out_path is None:
        print("\n(subset run: baseline artifact left untouched; pass --out to save)")
    else:
        written = write_artifact(
            build_artifact(scorecard, model, subset=subset), out_path
        )
        print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
