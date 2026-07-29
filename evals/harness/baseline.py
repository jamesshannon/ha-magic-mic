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

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path

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
    DEFAULT,
)

from .backing import build_executable_world  # noqa: E402
from .corpus import WAVE0_GOLDEN_SET, Corpus, load_corpus  # noqa: E402
from .runner import run_case  # noqa: E402
from .scoring import CaseResult, Scorecard, build_scorecard  # noqa: E402
from .world import async_setup_local_agent  # noqa: E402

# The baseline is the stock provider agent, not the testbed proxy (which is pass-through
# at Wave 0 but is the thing later waves change). Its unique_id suffix, from
# `conversation.async_setup_entry`.
_BASELINE_UNIQUE_SUFFIX = "_claude_baseline"


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


async def stand_up_agent(hass: HomeAssistant, corpus: Corpus, api_key: str) -> str:
    """Set up the local core, the live integration, and the fixture world.

    Returns the baseline agent's entity id. The config-entry setup makes a real
    ``models.list`` call, so a bad key fails loudly here before any turn runs.
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

    await build_executable_world(hass, corpus.world)
    return _baseline_agent_id(hass, entry)


async def run_baseline(hass: HomeAssistant, corpus: Corpus, api_key: str) -> Scorecard:
    """Drive every case through the live baseline agent and score the run.

    Every case runs at the LLM scope (`prefer_local` OFF), scored against its
    ``expected_for(llm=True)`` expectation.
    """
    agent_id = await stand_up_agent(hass, corpus, api_key)
    results: list[CaseResult] = []
    for index, case in enumerate(corpus.cases, start=1):
        print(f"  [{index:>2}/{len(corpus.cases)}] {case.id} ...", flush=True)
        results.append(await run_case(hass, agent_id, case, llm=True))
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


def build_artifact(scorecard: Scorecard, model: str) -> dict:
    """Assemble the full baseline artifact: metadata, aggregates, and per-case detail."""
    agree, total = scorecard.routing_agreement
    return {
        "run": {
            "kind": "wave0-live-baseline",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "prefer_local": False,
            "corpus": WAVE0_GOLDEN_SET.name,
            "cases": scorecard.total,
        },
        "buckets": {bucket.value: count for bucket, count in scorecard.buckets.items()},
        "routing_agreement": {"agree": agree, "total": total},
        "cost_totals": scorecard.totals,
        "cases": [_result_to_dict(result) for result in scorecard.results],
    }


def write_artifact(artifact: dict) -> Path:
    """Write the baseline artifact to ``evals/results/`` and return its path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE_ARTIFACT.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return BASELINE_ARTIFACT


async def main() -> None:
    """Run the live baseline end to end and persist the artifact."""
    api_key = load_api_key()
    corpus = load_corpus()
    model = DEFAULT[CONF_CHAT_MODEL]
    print(
        f"Running Wave 0 live baseline: {len(corpus.cases)} cases, model {model}, "
        "prefer_local OFF\n"
    )

    async with async_test_home_assistant() as hass:
        scorecard = await run_baseline(hass, corpus, api_key)

    print("\n" + scorecard.render())
    path = write_artifact(build_artifact(scorecard, model))
    print(f"\nartifact: {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
