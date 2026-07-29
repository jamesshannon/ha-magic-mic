"""Fixtures for the Magic Mic integration tests."""

import datetime
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.const import DOMAIN


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a Magic Mic config entry."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: "test-key"})


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a Claude client whose model list contains the default model."""
    model = anthropic.types.ModelInfo(
        type="model",
        id="claude-haiku-4-5",
        created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        display_name="Claude Haiku 4.5",
    )
    client = MagicMock()
    client.models.list = AsyncMock(return_value=MagicMock(data=[model]))
    return client


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: MagicMock,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration against a mocked Claude client (no key/network)."""
    # The conversation dependency's default agent needs the homeassistant core
    # component (it provides the exposed-entities store).
    assert await async_setup_component(hass, "homeassistant", {})

    mock_config_entry.add_to_hass(hass)

    # Patch the SDK client constructor (a normal module attribute) rather than our
    # own dotted path, which mock cannot attribute-walk through the
    # `custom_components` namespace package before it is imported.
    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        yield mock_config_entry
