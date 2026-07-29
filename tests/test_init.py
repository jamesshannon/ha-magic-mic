"""Load test for the Magic Mic Wave 0 skeleton.

Proves the integration sets up and registers both conversation agents (baseline
and testbed) against a mocked Claude client, so no API key or network is needed.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _mock_client() -> MagicMock:
    """A Claude client whose model list contains the default model."""
    model = anthropic.types.ModelInfo(
        type="model",
        id="claude-haiku-4-5",
        created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        display_name="Claude Haiku 4.5",
    )
    client = MagicMock()
    client.models.list = AsyncMock(return_value=MagicMock(data=[model]))
    return client


async def test_setup_registers_baseline_and_testbed(hass: HomeAssistant) -> None:
    """The integration loads and registers the baseline and testbed agents."""
    # The conversation dependency's default agent needs the homeassistant core
    # component (it provides the exposed-entities store).
    assert await async_setup_component(hass, "homeassistant", {})

    entry = MockConfigEntry(domain="magic_mic", data={CONF_API_KEY: "test-key"})
    entry.add_to_hass(hass)

    # Patch the SDK client constructor (a normal module attribute) rather than our
    # own dotted path, which mock cannot attribute-walk through the
    # `custom_components` namespace package before it is imported.
    with patch("anthropic.AsyncAnthropic", return_value=_mock_client()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entities = [
        entity for entity in ent_reg.entities.values() if entity.platform == "magic_mic"
    ]
    assert sorted(entity.unique_id for entity in entities) == sorted(
        [f"{entry.entry_id}_claude_baseline", f"{entry.entry_id}_testbed"]
    )
