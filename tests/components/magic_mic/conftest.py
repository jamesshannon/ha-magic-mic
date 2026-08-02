"""Fixtures for the Magic Mic integration tests."""

from collections.abc import AsyncGenerator, Generator, Iterable
from unittest.mock import DEFAULT, AsyncMock, patch

from anthropic.pagination import AsyncPage
from anthropic.types import (
    Container,
    Message,
    MessageDeltaUsage,
    RawContentBlockStartEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RawMessageStreamEvent,
    ToolUseBlock,
    Usage,
)
from anthropic.types.raw_message_delta_event import Delta
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.capabilities.localization import (
    ConversationStrings,
    async_get_conversation_strings,
)
from custom_components.magic_mic.const import DOMAIN
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .streaming import model_list


@pytest.fixture
async def conversation_strings(hass: HomeAssistant) -> ConversationStrings:
    """Return Magic Mic's English model-facing strings through HA's loader."""
    return await async_get_conversation_strings(hass, "en")


@pytest.fixture(autouse=True, scope="package")
def build_anthropic_pydantic_schemas() -> None:
    """Build the Container pydantic schema so streamed Message events construct."""
    Container.model_rebuild(force=True)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a Magic Mic config entry."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_API_KEY: "test-key"})


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration with a mocked model list (no key/network)."""
    # The conversation dependency's default agent needs the homeassistant core
    # component (it provides the exposed-entities store).
    assert await async_setup_component(hass, "homeassistant", {})

    mock_config_entry.add_to_hass(hass)

    with patch(
        "anthropic.resources.models.AsyncModels.list",
        new_callable=AsyncMock,
        return_value=AsyncPage(data=model_list),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        yield mock_config_entry


@pytest.fixture
def mock_create_stream() -> Generator[AsyncMock]:
    """Mock the Claude streaming `messages.create` call (adapted from HA core).

    Set `.return_value` to a list of event-lists, one per expected model call; each
    is wrapped with the message start/delta/stop framing the provider loop expects.
    """

    async def mock_generator(
        events: Iterable[RawMessageStreamEvent], **kwargs
    ) -> AsyncGenerator[RawMessageStreamEvent]:
        stop_reason = "end_turn"
        yield RawMessageStartEvent(
            message=Message(
                type="message",
                id="msg_test",
                content=[],
                role="assistant",
                model=kwargs["model"],
                usage=Usage(input_tokens=0, output_tokens=0),
            ),
            type="message_start",
        )
        for event in events:
            if isinstance(event, RawContentBlockStartEvent) and isinstance(
                event.content_block, ToolUseBlock
            ):
                stop_reason = "tool_use"
            yield event
        yield RawMessageDeltaEvent(
            type="message_delta",
            delta=Delta(stop_reason=stop_reason, stop_sequence="", container=None),
            usage=MessageDeltaUsage(output_tokens=0),
        )
        yield RawMessageStopEvent(type="message_stop")

    with patch(
        "anthropic.resources.messages.AsyncMessages.create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.side_effect = lambda **kwargs: (
            mock_generator(mock_create.return_value.pop(0), **kwargs)
            if isinstance(mock_create.return_value, list)
            else DEFAULT
        )
        yield mock_create
