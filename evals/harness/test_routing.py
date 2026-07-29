"""HASSIL routing measurement: verify the corpus ``routing_truth`` labels.

Keyless and model-free. Runs each utterance through core's local agent with the
fixture home exposed. Two assertions survive the minimal harness (no per-domain
platforms, so most intents recognize but cannot execute; see ``routing.py``):

- every ``local`` case is recognized by HASSIL (a template matched), and
- no ``llm`` case resolves locally (the LLM path is genuinely needed).

Turning the label check into a live measurement is the point: it was grounded only
against the sentence templates before this ran.
"""

import pytest

from evals.harness import load_corpus
from evals.harness.corpus import ROUTING_LLM, ROUTING_LOCAL, Case
from evals.harness.routing import probe_local
from evals.harness.world import async_setup_local_agent, build_world
from homeassistant.core import HomeAssistant

_CORPUS = load_corpus()
_LOCAL = [c for c in _CORPUS.cases if c.routing_truth == ROUTING_LOCAL]
_LLM = [c for c in _CORPUS.cases if c.routing_truth == ROUTING_LLM]


@pytest.fixture
async def local_home(hass: HomeAssistant) -> None:
    """Stand up the local agent and expose the fixture home."""
    await async_setup_local_agent(hass)
    await build_world(hass, _CORPUS.world)


@pytest.mark.usefixtures("local_home")
@pytest.mark.parametrize("case", _LOCAL, ids=lambda c: c.id)
async def test_local_cases_are_recognized(hass: HomeAssistant, case: Case) -> None:
    """Every ``local``-labelled utterance matches a HASSIL sentence template."""
    outcome = await probe_local(hass, case.utterance)
    assert outcome.recognized, (
        f"{case.id}: {case.utterance!r} labelled local but HASSIL did not match "
        f"(response={outcome.response_type}, error={outcome.error_code})"
    )


@pytest.mark.usefixtures("local_home")
@pytest.mark.parametrize("case", _LLM, ids=lambda c: c.id)
async def test_llm_cases_do_not_resolve_locally(
    hass: HomeAssistant, case: Case
) -> None:
    """No ``llm``-labelled utterance produces a useful outcome on the local path."""
    outcome = await probe_local(hass, case.utterance)
    assert not outcome.resolved, (
        f"{case.id}: {case.utterance!r} labelled llm but the local agent resolved it "
        f"(speech={outcome.speech!r})"
    )
