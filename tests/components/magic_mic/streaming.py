"""Streaming-response test helpers, adapted from HA core's anthropic tests.

Builds the Anthropic streaming events the provider loop consumes, so a turn can be
driven end to end without a real model.
"""

import datetime

from anthropic.types import (
    ModelInfo,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageStreamEvent,
    TextBlock,
    TextDelta,
)

# Minimal model list for the coordinator; matches the default chat model.
model_list = [
    ModelInfo(
        type="model",
        id="claude-haiku-4-5",
        created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        display_name="Claude Haiku 4.5",
    ),
]


def create_content_block(
    index: int, text_parts: list[str]
) -> list[RawMessageStreamEvent]:
    """Create the events for a text content block with the given deltas."""
    return [
        RawContentBlockStartEvent(
            type="content_block_start",
            content_block=TextBlock(text="", type="text", citations=None),
            index=index,
        ),
        *[
            RawContentBlockDeltaEvent(
                delta=TextDelta(text=text_part, type="text_delta"),
                index=index,
                type="content_block_delta",
            )
            for text_part in text_parts
        ],
        RawContentBlockStopEvent(index=index, type="content_block_stop"),
    ]
