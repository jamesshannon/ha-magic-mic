"""Tests for the name-injection eval variant runner (evals/harness/variant.py).

The measured delta needs a live key, but the runner *wiring* is deterministic and worth
protecting: both arms run, and toggling names changes what the model is actually sent.
The mocked stream is fixed, so it cannot show the round-trip the real model would shave;
that is a live observation. What it proves here is that the names-on arm injects the
in-room name block and the names-off arm does not, over the same case.
"""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness.backing import build_executable_world, register_satellite
from evals.harness.corpus import (
    Case,
    Entity,
    Expected,
    ExpectedTool,
    ProviderOptions,
    World,
)
from evals.harness.variant import (
    _parse_args,
    _render_delta,
    _testbed_agent_id,
    build_variant_artifact,
    run_paired_trial,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .streaming import create_content_block, create_tool_use_block


def test_variant_trials_default_and_override() -> None:
    """One paired trial stays cheap unless a targeted rerun asks for three."""
    assert _parse_args([]).trials == 1
    assert _parse_args(["--trials", "3"]).trials == 3


def _living_room_world() -> World:
    """One exposed, executable light named Reading Lamp in the living room."""
    return World(
        areas=("living_room",),
        entities=(
            Entity(
                entity_id="light.reading_lamp",
                name="Reading Lamp",
                area="living_room",
                state="off",
            ),
        ),
    )


def _lamp_case() -> Case:
    """A device case whose utterance names the in-room lamp (so injection fires)."""
    return Case(
        id="lamp",
        utterance="turn on the reading lamp",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        provider_options=ProviderOptions(web_search=True),
        expected=Expected(
            tools=(ExpectedTool("HassTurnOn", {"name": "Reading Lamp"}),)
        ),
    )


def _lamp_turn_stream() -> list:
    """A gen1 tool_use + gen2 confirmation, the shape a device command produces."""
    return [
        create_tool_use_block(0, "toolu_1", "HassTurnOn", ['{"name": "Reading Lamp"}']),
        create_content_block(0, ["Done."]),
    ]


async def test_paired_trial_toggles_name_injection(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """The names-on arm injects the in-room name; the names-off arm does not.

    Both arms drive the same case through the same room-placed satellite, so the only
    difference in what the model is sent is the Tier-2 block.
    """
    world = await build_executable_world(hass, _living_room_world())
    area_id = ar.async_get(hass).async_get_or_create("living room").id
    satellite = register_satellite(hass, area_id=area_id)
    agent_id = _testbed_agent_id(hass, setup_integration)
    case = _lamp_case()

    mock_create_stream.return_value = _lamp_turn_stream() * 2
    trial = await run_paired_trial(
        hass,
        agent_id,
        world,
        satellite,
        [case],
        entry=setup_integration,
    )
    first_generation_calls = mock_create_stream.call_args_list[::2]
    off_system, on_system = (
        "\n".join(block["text"] for block in call.kwargs["system"])
        for call in first_generation_calls
    )
    off_tools, on_tools = (
        {tool["name"] for tool in call.kwargs["tools"]}
        for call in first_generation_calls
    )

    assert trial.off.total == trial.on.total == 1
    injected_record = 'name="Reading Lamp"; entity_id="light.reading_lamp"'
    assert injected_record in on_system
    assert injected_record not in off_system
    assert "web_search" in off_tools
    assert "web_search" in on_tools
    assert "web_fetch" not in off_tools
    assert "web_fetch" not in on_tools


async def test_paired_trial_alternates_arm_order_and_reports_case_deltas(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A default trial balances order without adding model calls."""
    world = await build_executable_world(hass, _living_room_world())
    area_id = ar.async_get(hass).async_get_or_create("living room").id
    satellite = register_satellite(hass, area_id=area_id)
    agent_id = _testbed_agent_id(hass, setup_integration)
    first = _lamp_case()
    second = Case(
        id="lamp-again",
        utterance=first.utterance,
        category=first.category,
        routing_truth=first.routing_truth,
        resolves_at_wave0=first.resolves_at_wave0,
        provider_options=first.provider_options,
        expected=first.expected,
    )
    mock_create_stream.return_value = _lamp_turn_stream() * 4

    trial = await run_paired_trial(
        hass,
        agent_id,
        world,
        satellite,
        [first, second],
        entry=setup_integration,
    )

    injected_record = 'name="Reading Lamp"; entity_id="light.reading_lamp"'
    prompts = [
        "\n".join(block["text"] for block in call.kwargs["system"])
        for call in mock_create_stream.call_args_list[::2]
    ]
    assert trial.orders == ("off→on", "on→off")
    assert [injected_record in prompt for prompt in prompts] == [
        False,
        True,
        True,
        False,
    ]
    assert trial.off.total == trial.on.total == 2

    artifact = build_variant_artifact(
        "test-model", "living_room", "corpus.yaml", [trial]
    )
    assert artifact["run"]["pair_order"] == "alternating"
    assert artifact["run"]["trials"] == 1
    assert [pair["order"] for pair in artifact["pairs"]] == ["off→on", "on→off"]
    assert all(pair["delta"]["generations"] == 0 for pair in artifact["pairs"])
    assert [case["trial"] for case in artifact["arms"]["summary_only"]["cases"]] == [
        1,
        1,
    ]
    rendered = _render_delta(trial.off, trial.on, [trial])
    assert "Per-case paired deltas" in rendered
    assert "lamp-again" in rendered
    assert "on→off" in rendered

    mock_create_stream.reset_mock()
    mock_create_stream.return_value = _lamp_turn_stream() * 2
    reversed_trial = await run_paired_trial(
        hass,
        agent_id,
        world,
        satellite,
        [first],
        entry=setup_integration,
        trial_index=1,
    )
    reversed_prompts = [
        "\n".join(block["text"] for block in call.kwargs["system"])
        for call in mock_create_stream.call_args_list[::2]
    ]
    assert reversed_trial.orders == ("on→off",)
    assert [injected_record in prompt for prompt in reversed_prompts] == [True, False]
