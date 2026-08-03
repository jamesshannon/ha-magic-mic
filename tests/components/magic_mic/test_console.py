"""Tests for the live eval console configuration and rendering."""

from evals.harness.console import TurnResult, _parse_args, _Style, render_turn
from evals.harness.effects import ObservedEffect


def test_console_web_tools_default_off() -> None:
    """The console does not enable provider web tools implicitly."""
    args = _parse_args([])

    assert args.web_fetch is False
    assert args.web_search is False


def test_console_web_tools_can_be_enabled_independently() -> None:
    """CLI flags independently enable the provider-native web tools."""
    search = _parse_args(["--web-search"])
    fetch = _parse_args(["--web-fetch"])

    assert search.web_search is True
    assert search.web_fetch is False
    assert fetch.web_search is False
    assert fetch.web_fetch is True


def test_console_renders_durable_effects() -> None:
    """Effects outside HA state remain visible during interactive evaluation."""
    result = TurnResult(
        local=None,
        handled_locally=False,
        requests=(),
        tools=(),
        generations=[],
        speech="Timer started.",
        error=None,
        conversation_id="conversation-id",
        effects=(
            ObservedEffect(
                kind="timer.started",
                data={"name": "pasta", "seconds": 600},
            ),
        ),
    )

    rendered = render_turn(result, _Style(enabled=False), verbose=False)

    assert "DURABLE EFFECTS" in rendered
    assert "timer.started" in rendered
    assert '"seconds": 600' in rendered
