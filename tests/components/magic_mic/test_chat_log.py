"""Tests for MagicMicChatLog and the provider's generation-record adapter."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from anthropic.types import Message, MessageDeltaUsage, Usage
from anthropic.types.raw_message_delta_event import Delta
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.chat_log import (
    GenerationRecord,
    MagicMicChatLog,
    upgrade_chat_log,
)
from custom_components.magic_mic.identity import UNIDENTIFIED_PRINCIPAL
from custom_components.magic_mic.internal.claude import entity as claude_entity
from custom_components.magic_mic.internal.claude.entity import AnthropicDeltaStream
from custom_components.magic_mic.pending_operation import (
    ConsequenceClass,
    PendingOperation,
)
from custom_components.magic_mic.session_state import (
    DATA_SESSION_STATES,
    UNDO_JOURNAL_LIMIT,
)
from homeassistant.components import conversation
from homeassistant.components.conversation import ChatLog
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import chat_session, entity_registry as er

from .streaming import create_content_block


async def _converse(hass: HomeAssistant, agent_id: str) -> None:
    """Drive a single conversation turn through the given agent."""
    await conversation.async_converse(hass, "hello", None, Context(), agent_id=agent_id)


def _pending_operation() -> PendingOperation:
    """Return a typed pending record for session-lifetime tests."""
    return PendingOperation.create(
        "fixture.action",
        {"target": "fixture"},
        UNIDENTIFIED_PRINCIPAL,
        ConsequenceClass.ALWAYS_CONFIRM,
        lifetime=timedelta(seconds=30),
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )


def test_generation_record_as_dict() -> None:
    """The neutral record serializes with cache read and creation kept distinct."""
    record = GenerationRecord(
        input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_creation_tokens=4
    )

    assert record.as_dict() == {
        "cache_creation_tokens": 4,
        "cache_read_tokens": 3,
        "input_tokens": 1,
        "output_tokens": 2,
    }


async def test_upgrade_is_in_place_and_idempotent(hass: HomeAssistant) -> None:
    """The upgrade reclasses the same instance, shares content, and is idempotent."""
    base = ChatLog(hass, "conv-1")
    content_list = base.content

    upgraded = upgrade_chat_log(base)

    assert upgraded is base
    assert isinstance(upgraded, MagicMicChatLog)
    assert isinstance(upgraded, ChatLog)
    assert upgraded.content is content_list
    assert upgrade_chat_log(upgraded) is upgraded


async def test_generation_records_accumulate(hass: HomeAssistant) -> None:
    """Records append in order and the count tracks model rounds."""
    chat_log = upgrade_chat_log(ChatLog(hass, "conv-1"))

    assert chat_log.generation_count == 0
    assert chat_log.generations == ()

    chat_log.async_trace_generation(
        GenerationRecord(input_tokens=100, output_tokens=10)
    )
    chat_log.async_trace_generation(
        GenerationRecord(input_tokens=5, output_tokens=2, cache_read_tokens=90)
    )

    assert chat_log.generation_count == 2
    assert chat_log.generations[1].cache_read_tokens == 90


async def test_replace_preserves_subclass_and_resets_records(
    hass: HomeAssistant,
) -> None:
    """Core replaces the cached log each turn; the subclass survives, records reset.

    Guards the in-place-upgrade contract: MagicMicChatLog must add no dataclass field,
    or both ``__class__`` reassignment and this cross-turn ``replace`` would break.
    """
    chat_log = upgrade_chat_log(ChatLog(hass, "conv-1"))
    chat_log.async_trace_generation(GenerationRecord(input_tokens=5))

    next_turn = replace(chat_log, content=chat_log.content.copy())

    assert isinstance(next_turn, MagicMicChatLog)
    assert next_turn.generation_count == 0
    assert chat_log.generation_count == 1


async def test_replace_preserves_session_state(hass: HomeAssistant) -> None:
    """A cloned next-turn chat log reaches the same conversation sidecar."""
    chat_log = upgrade_chat_log(ChatLog(hass, "conv-1"))
    state = chat_log.session_state
    pending = _pending_operation()
    undo = object()
    state.pending_operation = pending
    state.async_append_undo(undo)

    next_turn = replace(chat_log, content=chat_log.content.copy())

    assert next_turn.session_state is state
    assert next_turn.session_state.pending_operation is pending
    assert next_turn.session_state.undo_journal == (undo,)


async def test_session_state_isolates_conversations(hass: HomeAssistant) -> None:
    """Different conversation IDs never share pending or journal state."""
    first = upgrade_chat_log(ChatLog(hass, "conv-1")).session_state
    second = upgrade_chat_log(ChatLog(hass, "conv-2")).session_state
    first.pending_operation = _pending_operation()
    first.async_append_undo(object())

    assert second is not first
    assert second.pending_operation is None
    assert second.undo_journal == ()


async def test_undo_journal_is_bounded(hass: HomeAssistant) -> None:
    """Appending beyond the fixed limit evicts the oldest journal entries."""
    state = upgrade_chat_log(ChatLog(hass, "conv-1")).session_state

    for entry in range(UNDO_JOURNAL_LIMIT + 2):
        state.async_append_undo(entry)

    assert state.undo_journal == tuple(range(2, UNDO_JOURNAL_LIMIT + 2))


async def test_begin_turn_replaces_only_turn_metadata(hass: HomeAssistant) -> None:
    """A new turn resets metadata while preserving conversation-level state."""
    state = upgrade_chat_log(ChatLog(hass, "conv-1")).session_state
    state.pending_operation = pending = _pending_operation()
    state.async_append_undo(undo := object())
    first = state.async_begin_turn("turn-1")
    first.provenance.add("wake_word")

    same = state.async_begin_turn("turn-1")
    second = state.async_begin_turn("turn-2")

    assert same is first
    assert second is not first
    assert second.provenance == set()
    assert state.pending_operation is pending
    assert state.undo_journal == (undo,)


async def test_session_state_expires_with_chat_session(hass: HomeAssistant) -> None:
    """HA chat-session cleanup removes the matching Magic Mic sidecar."""
    with (
        chat_session.async_get_chat_session(hass) as session,
        conversation.async_get_chat_log(hass, session) as base_chat_log,
    ):
        conversation_id = session.conversation_id
        chat_log = upgrade_chat_log(base_chat_log)
        state = chat_log.session_state
        state.pending_operation = pending = _pending_operation()
        state.async_append_undo(undo := object())
        chat_log.async_trace_generation(GenerationRecord(input_tokens=5))
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(agent_id="test-agent", content="Done")
        )

    with (
        chat_session.async_get_chat_session(hass, conversation_id) as next_session,
        conversation.async_get_chat_log(hass, next_session) as next_base_chat_log,
    ):
        next_chat_log = upgrade_chat_log(next_base_chat_log)
        assert next_chat_log.session_state is state
        assert next_chat_log.session_state.pending_operation is pending
        assert next_chat_log.session_state.undo_journal == (undo,)
        assert next_chat_log.generation_count == 0

    assert conversation_id in hass.data[DATA_SESSION_STATES]
    next_session.async_cleanup()

    assert conversation_id not in hass.data[DATA_SESSION_STATES]


async def test_provider_adapter_maps_usage(hass: HomeAssistant) -> None:
    """The Claude-bound adapter maps usage onto the neutral record via public events.

    Exercises the mislabel fix: cache creation and cache read are distinct fields.
    """
    chat_log = upgrade_chat_log(ChatLog(hass, "conv-1"))
    stream = AnthropicDeltaStream(chat_log, stream=None)

    stream.on_message_start_event(
        Message(
            type="message",
            id="msg_test",
            content=[],
            role="assistant",
            model="claude-haiku-4-5",
            usage=Usage(
                input_tokens=100,
                output_tokens=0,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=20,
            ),
        )
    )
    stream.on_message_delta_event(
        Delta(stop_reason="end_turn", stop_sequence="", container=None),
        MessageDeltaUsage(output_tokens=42),
    )

    assert chat_log.generation_count == 1
    assert chat_log.generations[0] == GenerationRecord(
        input_tokens=100,
        output_tokens=42,
        cache_read_tokens=80,
        cache_creation_tokens=20,
    )


async def test_turn_populates_generations(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A real turn upgrades the chat log at the chokepoint and records one round."""
    entry = setup_integration
    ent_reg = er.async_get(hass)
    testbed_id = next(
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.platform == "magic_mic"
        and entity.unique_id == f"{entry.entry_id}_testbed"
    )

    captured: dict[str, MagicMicChatLog] = {}

    def _spy(chat_log: ChatLog) -> MagicMicChatLog:
        captured["chat_log"] = upgrade_chat_log(chat_log)
        return captured["chat_log"]

    mock_create_stream.return_value = [create_content_block(0, ["Hello there."])]
    with patch.object(claude_entity, "upgrade_chat_log", _spy):
        await _converse(hass, testbed_id)

    assert captured["chat_log"].generation_count == 1
