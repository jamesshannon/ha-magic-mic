"""Pytest configuration for Magic Mic.

`pytest-homeassistant-custom-component` puts its own `testing_config/custom_components`
on the import path, which shadows this repo's `custom_components`. Graft our directory
onto the package search path so HA's loader discovers the integration.
"""

from pathlib import Path

import pytest

import custom_components

_REPO_CC = str(Path(__file__).parent / "custom_components")
if _REPO_CC not in custom_components.__path__:
    custom_components.__path__.insert(0, _REPO_CC)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in every test."""
    return
