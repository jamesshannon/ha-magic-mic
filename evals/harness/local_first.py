"""Measure the live ``prefer_local_intents`` path over the corpus (Wave 1 gate C).

This is the scored, batch sibling of the interactive HASSIL probe in `console.py`. It
reproduces the pipeline's prefer-local decision (`assist_pipeline/pipeline.py`) faithfully
rather than approximating it: recognize the utterance against Home Assistant's local
intents first, and, unless the match is one HA deliberately holds back for a CONTROL-capable
LLM, resolve it locally without ever calling the model. Only a local miss, a held-back
intent, or a local execution error reaches the LLM.

Two faithfulness points the earlier `console.py` probe missed (see `docs/evaluation.md`
Part C):

- It recognizes through the **default agent's** ``async_recognize_intent(strict_intents_only)``
  and executes through ``async_handle_intents``, the same entry points the pipeline uses,
  not ``async_converse(agent_id=None)`` (the full default agent, which matches non-strict and
  returns conversational fallbacks).
- It applies the **CONTROL fallback filter**: with the LLM agent advertising CONTROL, HA
  defers ``HassGetState`` and ``HassMediaSearchAndPlay`` to the model even with prefer-local
  on, because the local template answer is lower quality there. Those turns route to the LLM
  here too, so the off-cloud rate is the real one, not an over-count.

What it can and cannot score. A locally-routed turn carries no LLM tool call, so a
tool-with-args expectation is never verified from the args themselves. Three predicates
cover it instead, in descending directness: world diff for a state-scored case, speech for
an answer case, and a **declared durable effect** for an action whose arguments land
somewhere observable (``timer.started`` carries the seconds, ``todo.item_created`` the
summary). Between them they catch the main regression class, a turn HA would have gotten
right that HASSIL grabbed and got wrong, including a wrong argument.

What is left is the action with arguments and no observable trace of them, such as
``HassLightSet`` scored as a tool call rather than by the brightness it leaves behind. That
is reported UNJUDGED rather than guessed, so a passing number is never inflated by a turn
this harness could not actually check; converting the case to state scoring is how to close
it. Slot values are recorded for diagnosis but deliberately not scored: see ``LocalRouting``
for why that check belongs upstream.

Run it with a live key (only the miss/deferred cases spend; local wins never call the LLM):

    ANTHROPIC_API_KEY=sk-... .venv/bin/python -m evals.harness.local_first
"""

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

from pytest_homeassistant_custom_component.common import async_test_home_assistant

import custom_components
from homeassistant.components import conversation, media_player
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import chat_session, intent

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "evals" / "results"
LOCAL_FIRST_ARTIFACT = RESULTS_DIR / "wave1_local_first.json"

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
from .corpus import (  # noqa: E402
    WAVE0_GOLDEN_SET,
    Case,
    Corpus,
    as_alternatives,
    load_corpus,
)
from .effects import effect_cursor, effects_since  # noqa: E402
from .runner import finalize_score, observe_turn, stage_case  # noqa: E402
from .scoring import (  # noqa: E402
    Bucket,
    CaseResult,
    ObservedTurn,
    Scorecard,
    _answer_matches,
    _effects_match,
    build_scorecard,
)

# The local-first outcome of a non-state case, by whether the local path could confirm it.
_LOCAL_BUCKET = {
    True: Bucket.RESOLVED_LOCALLY,
    False: Bucket.WRONG_ACTION,
    None: Bucket.UNJUDGED,
}
from .variant import DEFAULT_AREA, stand_up_testbed  # noqa: E402

# The intents HA defers to a CONTROL-capable LLM even with prefer-local on
# (`_async_local_fallback_intent_filter`): the local template answer is lower quality than
# the model's for state questions and media search.
LOCAL_FALLBACK_INTENTS = frozenset(
    {intent.INTENT_GET_STATE, media_player.INTENT_MEDIA_SEARCH_AND_PLAY}
)


@dataclass(frozen=True)
class LocalRouting:
    """How the local-first decision landed for one utterance.

    ``matched`` is whether a strict local intent recognized at all (before the CONTROL
    filter). ``deferred`` is a match HA holds back for the LLM. ``routed_locally`` is the
    faithful outcome: matched, not deferred, and it actually resolved without an error.

    ``slots`` records what the local path bound, for diagnosis only: **nothing scores it.**
    Home Assistant already tests that its sentence templates bind the right slots and that
    those slots reach the right service call, correlating both halves in one test
    (`tests/components/conversation/test_default_agent.py`), and per-language template
    binding is tested in the `home-assistant/intents` repo that owns the sentences. Scoring
    slots here would restate an upstream guarantee against a weaker fixture. Recording them
    still pays: when a case routes or resolves unexpectedly, the artifact says what the
    utterance bound to without needing a repro. The values are post-resolution (areas as
    ids, a percentage already converted), so they show what the handler acted on rather than
    what the sentence matched.
    """

    matched: bool
    intent: str | None
    deferred: bool
    routed_locally: bool
    slots: dict[str, Any] = field(default_factory=dict)


def _make_input(
    hass: HomeAssistant, utterance: str, device_id: str | None
) -> conversation.ConversationInput:
    """Build the local turn's input, addressed to the built-in home-assistant agent."""
    return conversation.ConversationInput(
        text=utterance,
        context=Context(),
        conversation_id=None,
        device_id=device_id,
        satellite_id=None,
        language=hass.config.language,
        agent_id=conversation.HOME_ASSISTANT_AGENT,
    )


def _speech(response: intent.IntentResponse) -> str:
    """Pull the plain spoken text out of an intent response."""
    return response.speech.get("plain", {}).get("speech", "")


def _resolved_slots(response: intent.IntentResponse) -> dict[str, Any]:
    """Reduce a handled intent's slots to artifact-safe ``{name: value}`` diagnostics.

    HA carries each slot as ``{"value": ..., "text": ...}``; only the value is kept, since
    the text is the utterance we already record. A value an intent defines as an arbitrary
    object is stringified rather than dropped, so writing the artifact cannot fail on a slot
    type this harness has never seen.
    """
    if response.intent is None:
        return {}
    slots: dict[str, Any] = {}
    for name, slot in response.intent.slots.items():
        value = slot.get("value")
        if isinstance(value, (bool, int, float, str, type(None))):
            slots[name] = value
        elif isinstance(value, (list, tuple)):
            slots[name] = [
                item
                if isinstance(item, (bool, int, float, str, type(None)))
                else str(item)
                for item in value
            ]
        else:
            slots[name] = str(value)
    return slots


async def probe_local(
    hass: HomeAssistant, utterance: str, *, device_id: str | None
) -> tuple[LocalRouting, intent.IntentResponse | None]:
    """Run the pipeline's prefer-local decision and, when it wins, execute locally.

    Recognizes strictly against the local intents, applies the CONTROL fallback filter, and
    only executes (mutating the world) when the turn genuinely routes local. A deferred or
    unmatched utterance returns without touching the world, so the caller can hand it to the
    LLM cleanly.
    """
    agent = conversation.get_agent_manager(hass).default_agent
    if agent is None:
        raise BaselineError("default conversation agent is not available")

    user_input = _make_input(hass, utterance, device_id)
    recognized = await agent.async_recognize_intent(
        user_input, strict_intents_only=True
    )
    name = recognized.intent.name if recognized is not None else None
    matched = name is not None
    deferred = name in LOCAL_FALLBACK_INTENTS
    if not matched or deferred:
        return LocalRouting(matched, name, deferred, False), None

    try:
        with (
            chat_session.async_get_chat_session(hass) as session,
            conversation.async_get_chat_log(hass, session, user_input) as chat_log,
        ):
            response = await agent.async_handle_intents(user_input, chat_log)
    except HomeAssistantError:
        # A sentence recognized against an intent this home cannot handle (no registered
        # handler, e.g. no weather integration) or a handler that raised is not a local win.
        # Let it fall to the LLM, exactly as a home missing that integration would.
        return LocalRouting(matched, name, deferred, False), None

    # A local match that errored on execution is not a local win; let it fall to the LLM.
    resolved = (
        response is not None
        and response.response_type is not intent.IntentResponseType.ERROR
    )
    slots = _resolved_slots(response) if resolved and response is not None else {}
    return LocalRouting(matched, name, deferred, resolved, slots), response


async def observe_prefer_local(
    hass: HomeAssistant,
    llm_agent_id: str,
    utterance: str,
    *,
    device_id: str | None = None,
) -> tuple[ObservedTurn, LocalRouting]:
    """Observe one turn through the prefer-local path: local when it wins, else the LLM.

    A local win is reduced to an `ObservedTurn` with its speech and any durable effects but
    **no** tool calls: the local path exposes no LLM-tool schema, and a synthetic tool would
    fail a state-scored case's permitted-tool gate. The executed intent name travels on the
    returned `LocalRouting` instead, where `run_case_prefer_local` judges it. Everything else
    is handed to ``llm_agent_id`` and observed exactly as the baseline runner would.
    """
    cursor = effect_cursor(hass)
    routing, response = await probe_local(hass, utterance, device_id=device_id)
    if routing.routed_locally and response is not None:
        observed = ObservedTurn(
            speech=_speech(response),
            effects=effects_since(hass, cursor),
            routed_locally=True,
            resolved=True,
            generations=0,
        )
        return observed, routing

    observed = await observe_turn(
        hass, llm_agent_id, utterance, routed_locally=False, device_id=device_id
    )
    return observed, routing


@dataclass(frozen=True)
class LocalFirstResult:
    """One case scored through the prefer-local path, with how it routed."""

    result: CaseResult
    routing: LocalRouting


def _judge_local(
    case: Case, routing: LocalRouting, observed: ObservedTurn
) -> bool | None:
    """Judge a locally-routed, non-state turn from what the local path exposes.

    Three predicates, in the order they settle a case. A matching spoken answer is correct.
    Otherwise the executed intent has to be one the case expected, and then either its
    **declared effects** decide it, or, for an intent that takes no arguments, the name
    match is itself the full verification.

    Effects are what make an arg-bearing local win judgeable. The local path exposes no
    LLM-tool schema, so its slot values never become `ObservedTurn.tools`, but a durable
    effect records the same fact downstream of the arguments: `timer.started` carries the
    seconds a wrong duration would change, `todo.item_created` carries the summary text. A
    declared effect that does not match is a genuine wrong action, so this returns ``False``
    rather than declining.

    ``None`` (unjudged) remains for the case with arguments and no effect to observe, where
    calling it right or wrong would be a guess. Converting such a case to state scoring is
    the way to close it, as `set-volume` already did. A wrong action is still invisible on a
    non-state case with neither an answer nor an effect.
    """
    outcomes = as_alternatives(case.expected_for(llm=False))
    for outcome in outcomes:
        if outcome.answer is not None and _answer_matches(
            outcome.answer, observed.speech
        ):
            return True

    contradicted = False
    for outcome in outcomes:
        if not any(tool.name == routing.intent for tool in outcome.tools):
            continue
        if outcome.effects:
            if _effects_match(outcome.effects, observed.effects):
                return True
            contradicted = True
        elif all(
            not tool.args for tool in outcome.tools if tool.name == routing.intent
        ):
            return True
    return False if contradicted else None


async def run_case_prefer_local(
    hass: HomeAssistant,
    llm_agent_id: str,
    case: Case,
    *,
    device_id: str | None = None,
) -> LocalFirstResult:
    """Stage a case, drive it through the prefer-local path, and score it.

    An LLM-routed turn (local miss or CONTROL defer) is scored exactly as the baseline
    runner does, against the LLM expectation. A locally-routed turn is scored against the
    local expectation: a state-scored case by world diff (which catches a wrong local
    action), a non-state case by `_judge_local`, which marks anything it cannot verify
    UNJUDGED rather than crediting or faulting it.
    """
    before = stage_case(hass, case)
    observed, routing = await observe_prefer_local(
        hass, llm_agent_id, case.utterance, device_id=device_id
    )
    result = finalize_score(
        hass, case, observed, before, llm=not observed.routed_locally
    )
    if observed.routed_locally and not case.state_scored:
        verdict = _judge_local(case, routing, observed)
        result = replace(result, correct=verdict, bucket=_LOCAL_BUCKET[verdict])
    return LocalFirstResult(result=result, routing=routing)


@dataclass(frozen=True)
class LocalFirstReport:
    """Aggregate the prefer-local run: what moved off-cloud, and did anything regress."""

    results: tuple[LocalFirstResult, ...]

    @property
    def scorecard(self) -> Scorecard:
        """The standard scorecard over the scored outcomes (buckets, routing, cost)."""
        return build_scorecard([lfr.result for lfr in self.results])

    @property
    def off_cloud(self) -> int:
        """Turns the local path owned, so the LLM never ran."""
        return sum(1 for lfr in self.results if lfr.routing.routed_locally)

    @property
    def deferred(self) -> int:
        """Turns a local intent matched but the CONTROL filter sent to the LLM."""
        return sum(1 for lfr in self.results if lfr.routing.deferred)

    @property
    def missed(self) -> int:
        """Turns no local intent matched, so the LLM ran."""
        return sum(1 for lfr in self.results if not lfr.routing.matched)

    @property
    def unjudged_local(self) -> int:
        """Locally-routed turns whose action this harness could not verify (arg-bearing)."""
        return sum(
            1
            for lfr in self.results
            if lfr.routing.routed_locally and lfr.result.bucket is Bucket.UNJUDGED
        )

    def render(self) -> str:
        """Render the routing summary above the standard scorecard."""
        total = len(self.results)
        lines = [
            f"Local-first ({total} cases)",
            "",
            f"  off-cloud (local win): {self.off_cloud}/{total}",
            f"  deferred to LLM (CONTROL): {self.deferred}",
            f"  local miss -> LLM: {self.missed}",
            f"  locally-routed but unverifiable here: {self.unjudged_local}",
            "",
            self.scorecard.render(),
        ]
        return "\n".join(lines)


def _routing_to_dict(routing: LocalRouting) -> dict[str, object]:
    return {
        "matched": routing.matched,
        "intent": routing.intent,
        "deferred": routing.deferred,
        "routed_locally": routing.routed_locally,
        "slots": routing.slots,
    }


def _result_to_dict(lfr: LocalFirstResult) -> dict[str, object]:
    """Reduce one scored, routed case to a JSON record."""
    case = lfr.result.case
    observed = lfr.result.observed
    return {
        "id": case.id,
        "utterance": case.utterance,
        "category": case.category,
        "routing_truth": case.routing_truth,
        "routing": _routing_to_dict(lfr.routing),
        "bucket": lfr.result.bucket.value,
        "correct": lfr.result.correct,
        "routed_locally": observed.routed_locally,
        "speech": observed.speech,
        "tools": [{"name": t.name, "args": t.args} for t in observed.tools],
    }


def build_artifact(report: LocalFirstReport, model: str) -> dict:
    """Assemble the artifact: metadata, routing aggregates, and per-case detail."""
    scorecard = report.scorecard
    agree, total = scorecard.routing_agreement
    return {
        "run": {
            "kind": "wave1-local-first",
            "timestamp": datetime.now(UTC).isoformat(),
            "model": model,
            "prefer_local": True,
            "control_filter": sorted(LOCAL_FALLBACK_INTENTS),
            "corpus": WAVE0_GOLDEN_SET.name,
            "cases": len(report.results),
        },
        "routing": {
            "off_cloud": report.off_cloud,
            "deferred": report.deferred,
            "missed": report.missed,
            "unjudged_local": report.unjudged_local,
        },
        "routing_agreement": {"agree": agree, "total": total},
        "buckets": {b.value: c for b, c in scorecard.buckets.items()},
        "cases": [_result_to_dict(lfr) for lfr in report.results],
    }


def write_artifact(artifact: dict, path: Path) -> Path:
    """Write the artifact to ``path`` (creating parents) and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


async def run_local_first(
    hass: HomeAssistant,
    corpus: Corpus,
    api_key: str,
    cases: Sequence[Case],
    *,
    area: str,
) -> LocalFirstReport:
    """Drive ``cases`` through the prefer-local path against the testbed agent.

    The full corpus world is stood up (so targets resolve) but only ``cases`` run. The world
    is reset before each case so a case runs against a clean, order-independent world.
    """
    agent_id, world, satellite, _entry = await stand_up_testbed(
        hass, corpus, api_key, area=area
    )
    results: list[LocalFirstResult] = []
    for index, case in enumerate(cases, start=1):
        await world.reset(hass)
        print(f"  [{index:>2}/{len(cases)}] {case.id} ...", flush=True)
        results.append(
            await run_case_prefer_local(
                hass, agent_id, case, device_id=satellite.device_id
            )
        )
    return LocalFirstReport(tuple(results))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals.harness.local_first",
        description="Run the live prefer-local routing measurement over the corpus.",
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
        help=f"write the artifact here (default {LOCAL_FIRST_ARTIFACT.name})",
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
    """Run the live prefer-local measurement and persist the artifact."""
    args = _parse_args(argv)
    corpus = load_corpus()
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
        f"Running local-first: {len(cases)}/{len(corpus.cases)} cases, "
        f"model {model} (LLM runs only on miss/deferred), area {args.area}\n"
    )

    async with async_test_home_assistant() as hass:
        # `async_setup_local_agent` inside `stand_up_testbed` needs the core component; the
        # helper handles it, and `pin_pre_magic_roster` keeps the LLM roster on the locked
        # pre-magic set for the fallback turns.
        with pin_pre_magic_roster():
            report = await run_local_first(hass, corpus, api_key, cases, area=args.area)

    print("\n" + report.render())
    out_path = args.out or LOCAL_FIRST_ARTIFACT
    written = write_artifact(build_artifact(report, model), out_path)
    print(f"\nartifact: {written.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
