"""Tests for Magic Mic configuration and provider reauthentication."""

from unittest.mock import AsyncMock, patch

import anthropic
from anthropic.pagination import AsyncPage
from httpx import Request, Response
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.const import DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from .streaming import model_list

_MODEL_LIST = "anthropic.resources.models.AsyncModels.list"


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up core before loading Magic Mic's conversation dependency."""
    assert await async_setup_component(hass, "homeassistant", {})


def _authentication_error() -> anthropic.AuthenticationError:
    """Return a realistic provider authentication failure."""
    request = Request("POST", "https://api.anthropic.com/v1/models")
    return anthropic.AuthenticationError(
        message="Invalid API key",
        response=Response(401, request=request),
        body=None,
    )


def _connection_error() -> anthropic.APIConnectionError:
    """Return a realistic provider connection failure."""
    return anthropic.APIConnectionError(
        request=Request("POST", "https://api.anthropic.com/v1/models")
    )


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Add an existing entry and open its replacement-key form."""
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    return result


@pytest.mark.parametrize(
    ("provider_error", "flow_error"),
    [
        (_authentication_error(), "invalid_auth"),
        (_connection_error(), "cannot_connect"),
    ],
)
async def test_reauth_rejects_invalid_or_unreachable_replacement(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    provider_error: anthropic.AnthropicError,
    flow_error: str,
) -> None:
    """A failed validation keeps the old key and replacement form open."""
    result = await _start_reauth(hass, mock_config_entry)

    with patch(_MODEL_LIST, new_callable=AsyncMock, side_effect=provider_error):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "replacement-key"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": flow_error}
    assert mock_config_entry.data[CONF_API_KEY] == "test-key"


async def test_reauth_updates_key_and_schedules_reload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A validated replacement updates the existing entry and reloads it."""
    result = await _start_reauth(hass, mock_config_entry)

    with (
        patch(
            _MODEL_LIST,
            new_callable=AsyncMock,
            return_value=AsyncPage(data=model_list),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_API_KEY: "replacement-key"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_KEY] == "replacement-key"
    schedule_reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_coordinator_auth_failure_starts_reauth_flow(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
) -> None:
    """A runtime provider credential failure opens the entry's reauth form."""
    entry = setup_integration

    with patch(
        _MODEL_LIST,
        new_callable=AsyncMock,
        side_effect=_authentication_error(),
    ):
        await entry.runtime_data.async_request_refresh()
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["step_id"] == "reauth_confirm"
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["context"]["entry_id"] == entry.entry_id
