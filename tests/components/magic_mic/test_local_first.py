"""Local-first driver tests: drive the prefer-local decision and score the turn.

The local (HASSIL) path is keyless, so a local win needs no model; only the miss and
deferred cases reach the mocked stream. These exercise the four routing outcomes (local
win, miss, deferred, arg-unverifiable) and the honest UNJUDGED downgrade.
"""

import json
from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.internal.claude.const import CONF_CHAT_MODEL, DEFAULT
from evals.harness import (
    Bucket,
    Case,
    Expected,
    ExpectedAnswer,
    ExpectedEffect,
    ExpectedTool,
)
from evals.harness.backing import (
    ExecutableWorld,
    build_executable_world,
    register_satellite,
)
from evals.harness.corpus import Entity, StateChange, World
from evals.harness.local_first import (
    LOCAL_FALLBACK_INTENTS,
    LocalFirstReport,
    LocalFirstResult,
    LocalRouting,
    build_artifact,
    probe_local,
    run_case_prefer_local,
)
from evals.harness.scoring import CaseResult, ObservedTurn, ToolCall
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block


def _testbed_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the testbed proxy agent."""
    ent_reg = er.async_get(hass)
    return next(
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.platform == "magic_mic"
        and entity.unique_id == f"{entry.entry_id}_testbed"
    )


async def _build_light(hass: HomeAssistant) -> ExecutableWorld:
    """Stand up a single dimmable light, exposed and executable, on."""
    return await build_executable_world(
        hass,
        World(
            areas=("living_room",),
            entities=(
                Entity(
                    entity_id="light.lamp",
                    name="Lamp",
                    area="living_room",
                    state="on",
                ),
            ),
        ),
    )


async def _build_blind(hass: HomeAssistant) -> ExecutableWorld:
    """Stand up a single positionable blind, exposed and executable, open at 100."""
    return await build_executable_world(
        hass,
        World(
            areas=("living_room",),
            entities=(
                Entity(
                    entity_id="cover.blinds",
                    name="Blinds",
                    area="living_room",
                    device_class="blind",
                    state="open",
                ),
            ),
        ),
    )


def _turn_off_light_case() -> Case:
    """A state-scored case: the lamp must end off, by whatever tool."""
    return Case(
        id="turn-off-lamp",
        utterance="turn off the lamp",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        expect_changes={"light.lamp": StateChange(state="off")},
    )


async def test_local_win_scores_resolved_locally(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A device command HASSIL handles resolves locally, no model call, RESOLVED_LOCALLY."""
    await _build_light(hass)
    satellite = register_satellite(hass, area_id=None)
    agent_id = _testbed_agent_id(hass, setup_integration)
    # No stream is primed: a local win must never reach the model.

    lfr = await run_case_prefer_local(
        hass, agent_id, _turn_off_light_case(), device_id=satellite.device_id
    )

    assert lfr.routing.routed_locally is True
    assert lfr.routing.matched is True
    assert lfr.result.observed.routed_locally is True
    assert lfr.result.observed.generations == 0
    assert lfr.result.correct is True
    assert lfr.result.bucket is Bucket.RESOLVED_LOCALLY
    assert hass.states.get("light.lamp").state == "off"
    assert mock_create_stream.call_count == 0


async def test_local_miss_falls_through_to_the_llm(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """An utterance with no local intent goes to the model and is scored as an LLM turn."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    case = Case(
        id="capital",
        utterance="what's the capital of France?",
        category="knowledge",
        routing_truth="llm",
        resolves_at_wave0=True,
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )
    mock_create_stream.return_value = [
        create_content_block(0, ["The capital is ", "Paris."])
    ]

    lfr = await run_case_prefer_local(hass, agent_id, case)

    assert lfr.routing.matched is False
    assert lfr.routing.routed_locally is False
    assert lfr.result.observed.routed_locally is False
    assert lfr.result.observed.generations >= 1
    assert lfr.result.correct is True
    assert lfr.result.bucket is Bucket.LLM_CORRECT


async def test_arg_bearing_local_win_is_unjudged(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A locally-routed, non-state case whose args cannot be verified is UNJUDGED, not wrong.

    ``close the blinds`` resolves locally, but scored as a tool case expecting
    ``HassSetPosition`` at position 0 the local path exposes no args, so the harness declines
    to judge it rather than count it wrong.
    """
    await _build_blind(hass)
    satellite = register_satellite(hass, area_id=None)
    agent_id = _testbed_agent_id(hass, setup_integration)
    case = Case(
        id="close-blinds-tool",
        utterance="close the blinds",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        expected=Expected(tools=(ExpectedTool("HassSetPosition", {"position": 0}),)),
    )

    lfr = await run_case_prefer_local(
        hass, agent_id, case, device_id=satellite.device_id
    )

    assert lfr.routing.routed_locally is True
    assert lfr.result.correct is None
    assert lfr.result.bucket is Bucket.UNJUDGED
    assert mock_create_stream.call_count == 0


def _timer_case(seconds: int) -> Case:
    """A ten-minute timer, scored by the duration its effect records."""
    return Case(
        id="start-timer",
        utterance="set a timer for 10 minutes",
        category="timer",
        routing_truth="local",
        resolves_at_wave0=True,
        expected=Expected(
            tools=(ExpectedTool("HassStartTimer", {"minutes": 10}),),
            effects=(ExpectedEffect("timer.started", {"seconds": seconds}),),
        ),
    )


async def test_a_declared_effect_verifies_an_arg_bearing_local_win(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """An arg-bearing local win is judged when the corpus declares an observable effect.

    The local path exposes no tool args, but `timer.started` carries the seconds that a
    misread duration would change, so the case resolves instead of falling to UNJUDGED.
    """
    satellite = register_satellite(hass, area_id=None)
    agent_id = _testbed_agent_id(hass, setup_integration)

    lfr = await run_case_prefer_local(
        hass, agent_id, _timer_case(600), device_id=satellite.device_id
    )

    assert lfr.routing.routed_locally is True
    assert lfr.result.correct is True
    assert lfr.result.bucket is Bucket.RESOLVED_LOCALLY
    assert mock_create_stream.call_count == 0


async def test_a_contradicted_effect_is_a_wrong_action_not_unjudged(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A local win whose declared effect does not match is wrong, and says so.

    This is the regression class the effect check exists for: HASSIL starting a timer of the
    wrong length used to pass as UNJUDGED because the duration lived only in the args.
    """
    satellite = register_satellite(hass, area_id=None)
    agent_id = _testbed_agent_id(hass, setup_integration)

    lfr = await run_case_prefer_local(
        hass, agent_id, _timer_case(300), device_id=satellite.device_id
    )

    assert lfr.routing.routed_locally is True
    assert lfr.result.correct is False
    assert lfr.result.bucket is Bucket.WRONG_ACTION


async def test_local_win_records_resolved_slots_without_scoring_them(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Slots ride along as diagnostics: post-resolution values, and no effect on the verdict.

    The case declares an expectation the slots would contradict if anything scored them
    (10 minutes bound, 5 expected), and the verdict still comes from the effect.
    """
    satellite = register_satellite(hass, area_id=None)
    agent_id = _testbed_agent_id(hass, setup_integration)

    lfr = await run_case_prefer_local(
        hass, agent_id, _timer_case(600), device_id=satellite.device_id
    )

    assert lfr.routing.slots["minutes"] == 10
    assert lfr.result.correct is True


async def test_a_local_miss_records_no_slots(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Only a local win binds slots; a miss reaches the model with nothing to record."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [create_content_block(0, ["Paris."])]
    case = Case(
        id="capital",
        utterance="what's the capital of France?",
        category="knowledge",
        routing_truth="llm",
        resolves_at_wave0=True,
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )

    lfr = await run_case_prefer_local(hass, agent_id, case)

    assert lfr.routing.slots == {}


async def test_recognized_intent_without_a_handler_falls_through(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A sentence matching an intent this home cannot handle routes to the LLM, not crash.

    ``what's the weather`` recognizes ``HassGetWeather``, but no weather integration is set
    up, so execution raises and the turn must fall through to the model.
    """
    agent_id = _testbed_agent_id(hass, setup_integration)
    case = Case(
        id="weather",
        utterance="what's the weather?",
        category="query",
        routing_truth="local",
        resolves_at_wave0=True,
        expected=Expected(answer=ExpectedAnswer(contains=("sunny",))),
    )
    mock_create_stream.return_value = [create_content_block(0, ["It's sunny."])]

    lfr = await run_case_prefer_local(hass, agent_id, case)

    assert lfr.routing.matched is True
    assert lfr.routing.routed_locally is False
    assert lfr.result.observed.routed_locally is False
    assert lfr.result.observed.generations >= 1


def test_control_filter_defers_state_and_media() -> None:
    """The CONTROL fallback set is exactly the two intents HA holds back for the LLM."""
    assert (
        frozenset({"HassGetState", "HassMediaSearchAndPlay"}) == LOCAL_FALLBACK_INTENTS
    )


async def test_probe_reports_a_local_miss_without_touching_the_world(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """A miss returns unmatched with no executed response, so nothing was actuated."""
    routing, response = await probe_local(
        hass, "what's the capital of France?", device_id=None
    )

    assert routing.matched is False
    assert routing.intent is None
    assert routing.routed_locally is False
    assert response is None


def _report(*results: LocalFirstResult) -> LocalFirstReport:
    return LocalFirstReport(tuple(results))


def _lfr(routing: LocalRouting, bucket: Bucket) -> LocalFirstResult:
    """A hand-built result carrying only what the report aggregates."""
    case = Case(
        id="x",
        utterance="u",
        category="c",
        routing_truth="local",
        resolves_at_wave0=True,
    )
    observed = ObservedTurn(
        speech="", tools=(ToolCall("HassX"),), routed_locally=routing.routed_locally
    )
    return LocalFirstResult(
        result=CaseResult(case=case, observed=observed, bucket=bucket, correct=None),
        routing=routing,
    )


def test_report_counts_the_routing_outcomes() -> None:
    """The report tallies off-cloud, deferred, miss, and unverifiable-local turns."""
    report = _report(
        _lfr(LocalRouting(True, "HassTurnOn", False, True), Bucket.RESOLVED_LOCALLY),
        _lfr(LocalRouting(True, "HassSetPosition", False, True), Bucket.UNJUDGED),
        _lfr(LocalRouting(True, "HassGetState", True, False), Bucket.LLM_CORRECT),
        _lfr(LocalRouting(False, None, False, False), Bucket.LLM_CORRECT),
    )

    assert report.off_cloud == 2
    assert report.deferred == 1
    assert report.missed == 1
    assert report.unjudged_local == 1
    assert "off-cloud (local win): 2/4" in report.render()


def test_artifact_carries_routing_and_control_filter() -> None:
    """The artifact records the routing tally and the CONTROL filter it applied."""
    report = _report(
        _lfr(
            LocalRouting(True, "HassTurnOn", False, True, {"area": "living_room"}),
            Bucket.RESOLVED_LOCALLY,
        ),
        _lfr(LocalRouting(False, None, False, False), Bucket.LLM_CORRECT),
    )

    artifact = build_artifact(report, DEFAULT[CONF_CHAT_MODEL])

    assert artifact["routing"]["off_cloud"] == 1
    assert artifact["routing"]["missed"] == 1
    assert artifact["run"]["prefer_local"] is True
    assert artifact["run"]["control_filter"] == [
        "HassGetState",
        "HassMediaSearchAndPlay",
    ]
    assert {case["id"] for case in artifact["cases"]} == {"x"}
    # Slots ride into the artifact so a surprising route is diagnosable without a repro.
    assert [case["routing"]["slots"] for case in artifact["cases"]] == [
        {"area": "living_room"},
        {},
    ]
    json.dumps(artifact)  # the diagnostics must not make the artifact unwritable
