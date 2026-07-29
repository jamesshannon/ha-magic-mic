"""Runner tests: drive the testbed agent with a mocked stream and score the turn."""

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from evals.harness import Bucket, run_case
from evals.harness.corpus import Case, Expected, ExpectedAnswer, ExpectedTool
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .streaming import create_content_block, create_tool_use_block


def _testbed_agent_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the entity id of the testbed proxy agent."""
    ent_reg = er.async_get(hass)
    return next(
        entity.entity_id
        for entity in ent_reg.entities.values()
        if entity.platform == "magic_mic"
        and entity.unique_id == f"{entry.entry_id}_testbed"
    )


def _knowledge_case() -> Case:
    """A text-only case: no tools, answer predicate only."""
    return Case(
        id="k",
        utterance="what's the capital of France?",
        category="knowledge",
        routing_truth="llm",
        resolves_at_wave0=True,
        expected=Expected(answer=ExpectedAnswer(contains=("Paris",))),
    )


async def test_run_knowledge_case_scores_llm_correct(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A correct text answer is captured, scored correct, and bucketed LLM_CORRECT."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_content_block(0, ["The capital is ", "Paris."])
    ]

    result = await run_case(hass, agent_id, _knowledge_case(), llm=True)

    assert result.bucket is Bucket.LLM_CORRECT
    assert result.correct is True
    assert result.observed.tools == ()
    assert result.observed.generations == 1
    assert "Paris" in result.observed.speech


async def test_run_knowledge_case_wrong_answer_scores_wrong_action(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A resolved turn with the wrong answer buckets as WRONG_ACTION."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [create_content_block(0, ["It is Lyon."])]

    result = await run_case(hass, agent_id, _knowledge_case(), llm=True)

    assert result.correct is False
    assert result.bucket is Bucket.WRONG_ACTION


def _device_case(expected_name: str = "Kitchen Light") -> Case:
    """A device-control case expecting a HassTurnOff on the given entity name."""
    return Case(
        id="d",
        utterance="turn off the kitchen light",
        category="device-control",
        routing_truth="local",
        resolves_at_wave0=True,
        expected=Expected(
            tools=(ExpectedTool("HassTurnOff", {"name": expected_name}),)
        ),
    )


async def test_run_device_case_captures_tool_and_counts_generations(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """A tool_use round is captured from the trace; two rounds count as two generations."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassTurnOff", ['{"name": "Kitchen Light"}']
        ),
        create_content_block(0, ["Done."]),
    ]

    result = await run_case(hass, agent_id, _device_case(), llm=True)

    assert [tool.name for tool in result.observed.tools] == ["HassTurnOff"]
    assert result.observed.tools[0].args == {"name": "Kitchen Light"}
    assert result.observed.generations == 2
    assert result.correct is True
    assert result.bucket is Bucket.LLM_CORRECT


async def test_run_device_case_wrong_tool_scores_wrong_action(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_create_stream: AsyncMock,
) -> None:
    """Calling the wrong tool buckets as WRONG_ACTION even though the turn resolves."""
    agent_id = _testbed_agent_id(hass, setup_integration)
    mock_create_stream.return_value = [
        create_tool_use_block(
            0, "toolu_1", "HassTurnOn", ['{"name": "Kitchen Light"}']
        ),
        create_content_block(0, ["Done."]),
    ]

    result = await run_case(hass, agent_id, _device_case(), llm=True)

    assert [tool.name for tool in result.observed.tools] == ["HassTurnOn"]
    assert result.correct is False
    assert result.bucket is Bucket.WRONG_ACTION
