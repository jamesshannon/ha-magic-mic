"""Tests for the live eval console configuration."""

from evals.harness.console import _parse_args


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
