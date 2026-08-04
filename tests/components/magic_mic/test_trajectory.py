"""Driven multi-turn tests: the disambiguation round-trip through the real machinery.

These prove find-entities.md's "the round-trip" claim with a scripted provider: an
ambiguous `find_entities` result ends a turn as a question and does not act, then a
follow-up in the *same* conversation resolves and actuates. The provider's generations
are scripted; the tools, the session, and the history replay are real.
"""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness.backing import build_executable_world
from evals.harness.corpus import Entity, World
from evals.harness.trajectory import (
    disambiguation_recovered,
    drive_trajectory,
    drive_turn,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block, create_tool_use_block

_LAMP = "light.bedroom_lamp"
_CEILING = "light.bedroom_ceiling"


def _testbed_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the testbed conversation entity id."""
    for entity in er.async_get(hass).entities.values():
        if (
            entity.platform == "magic_mic"
            and entity.unique_id == f"{entry.entry_id}_testbed"
        ):
            return entity.entity_id
    raise AssertionError("testbed agent not registered")


async def _two_bedroom_lights(hass: HomeAssistant) -> None:
    """Two exposed, executable lights that both match 'bedroom', both off."""
    await build_executable_world(
        hass,
        World(
            areas=("bedroom",),
            entities=(
                Entity(
                    entity_id=_LAMP, name="Bedroom Lamp", area="bedroom", state="off"
                ),
                Entity(
                    entity_id=_CEILING,
                    name="Bedroom Light",
                    area="bedroom",
                    state="off",
                ),
            ),
        ),
    )


async def test_ambiguous_resolution_asks_then_a_follow_up_resolves(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Turn 1 finds two candidates and asks; turn 2 in the same conversation acts."""
    await _two_bedroom_lights(hass)
    testbed_id = _testbed_id(hass, setup_integration)

    # gen1 searches, gen2 asks (turn 1); gen3 acts on the disambiguated name, gen4
    # confirms (turn 2). One event-list per generation, consumed across both turns.
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_find", "find_entities", ['{"name": "bedroom"}']
        ),
        create_content_block(
            0, ["Did you mean the Bedroom Lamp or the Bedroom Light?"]
        ),
        create_tool_use_block(
            0, "toolu_on", "HassTurnOn", ['{"name": "Bedroom Light"}']
        ),
        create_content_block(0, ["Turned on the Bedroom Light."]),
    ]

    # Drive the two turns explicitly so the world can be inspected between them: the
    # point is that nothing actuates until after the clarification.
    ask = await drive_turn(hass, testbed_id, "turn on the bedroom light")

    # Turn 1 searched and asked; it did not actuate either light.
    assert ask.called("find_entities")
    assert not ask.called("HassTurnOn")
    assert hass.states.get(_LAMP).state == "off"
    assert hass.states.get(_CEILING).state == "off"
    assert "Bedroom Lamp or the Bedroom Light" in ask.speech

    resolve = await drive_turn(
        hass, testbed_id, "the ceiling one", conversation_id=ask.conversation_id
    )

    # Turn 2 continued the same conversation and actuated exactly the chosen light.
    assert resolve.conversation_id == ask.conversation_id
    assert resolve.called("HassTurnOn")
    assert hass.states.get(_CEILING).state == "on"
    assert hass.states.get(_LAMP).state == "off"

    assert disambiguation_recovered([ask, resolve], action_tool="HassTurnOn")


async def test_follow_up_generation_sees_the_prior_turn_history(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """The acting generation's request carries the first turn's exchange (history replay)."""
    await _two_bedroom_lights(hass)
    testbed_id = _testbed_id(hass, setup_integration)

    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_find", "find_entities", ['{"name": "bedroom"}']
        ),
        create_content_block(0, ["Which one, the lamp or the ceiling light?"]),
        create_tool_use_block(
            0, "toolu_on", "HassTurnOn", ['{"name": "Bedroom Light"}']
        ),
        create_content_block(0, ["Done."]),
    ]

    await drive_trajectory(
        hass, testbed_id, ["turn on the bedroom light", "the ceiling one"]
    )

    # The third generation (turn 2, gen1) is the follow-up. Its request messages must
    # replay turn 1: the find_entities result and the clarifying question the agent gave.
    follow_up_messages = repr(mock_create_stream.call_args_list[2].kwargs["messages"])
    assert "find_entities" in follow_up_messages
    assert "the lamp or the ceiling light" in follow_up_messages
    assert "the ceiling one" in follow_up_messages
