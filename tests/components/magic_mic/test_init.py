"""Tests for setting up the Magic Mic integration."""

import json
from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

PROJECT_ROOT = Path(__file__).parents[3]


def test_hacs_minimum_tracks_exercised_ha_release_line() -> None:
    """HACS requires the .0 release of the Home Assistant line under test."""
    major, minor, _patch = HA_VERSION.split(".", maxsplit=2)
    hacs = json.loads((PROJECT_ROOT / "hacs.json").read_text())

    assert hacs["homeassistant"] == f"{major}.{minor}.0"


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
