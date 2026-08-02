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
from evals.harness.variant import _testbed_agent_id, run_arm
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from .streaming import create_content_block, create_tool_use_block


def _system_text(mock_create: AsyncMock) -> str:
    """Flatten the most recent model call's system prompt to one string."""
    system = mock_create.call_args.kwargs["system"]
    if isinstance(system, str):
        return system
    return "\n".join(block["text"] for block in system)


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


async def test_run_arm_toggles_name_injection(
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
    cases = [_lamp_case()]

    mock_create_stream.return_value = _lamp_turn_stream()
    off = await run_arm(
        hass,
        agent_id,
        world,
        satellite,
        cases,
        entry=setup_integration,
        names_on=False,
    )
    off_system = _system_text(mock_create_stream)
    off_tools = {tool["name"] for tool in mock_create_stream.call_args.kwargs["tools"]}

    mock_create_stream.return_value = _lamp_turn_stream()
    on = await run_arm(
        hass,
        agent_id,
        world,
        satellite,
        cases,
        entry=setup_integration,
        names_on=True,
    )
    on_system = _system_text(mock_create_stream)
    on_tools = {tool["name"] for tool in mock_create_stream.call_args.kwargs["tools"]}

    assert off.total == 1
    assert on.total == 1
    assert "Reading Lamp (light.reading_lamp)" in on_system
    assert "Reading Lamp (light.reading_lamp)" not in off_system
    assert "web_search" in off_tools
    assert "web_search" in on_tools
    assert "web_fetch" not in off_tools
    assert "web_fetch" not in on_tools
