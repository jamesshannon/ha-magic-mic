"""Tests for setting up the Magic Mic integration."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_setup_registers_baseline_and_testbed(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The integration loads and registers the baseline and testbed agents."""
    entry = setup_integration
    ent_reg = er.async_get(hass)
    entities = [
        entity for entity in ent_reg.entities.values() if entity.platform == "magic_mic"
    ]
    assert sorted(entity.unique_id for entity in entities) == sorted(
        [f"{entry.entry_id}_claude_baseline", f"{entry.entry_id}_testbed"]
    )
