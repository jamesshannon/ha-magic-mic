"""Contract tests for the retained internal provider adapter."""

import ast
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROVIDER_ROOT = PROJECT_ROOT / "custom_components" / "magic_mic" / "internal" / "claude"
STRINGS_PATH = PROJECT_ROOT / "custom_components" / "magic_mic" / "strings.json"


def _provider_translation_keys() -> set[str]:
    """Extract statically declared exception translation keys from the provider."""
    keys: set[str] = set()
    for path in PROVIDER_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "translation_key":
                    continue
                if not (
                    isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    raise TypeError(
                        f"{path.name} contains a dynamic provider translation key"
                    )
                keys.add(keyword.value.value)
    return keys


def test_provider_translation_keys_exist_in_magic_mic_catalog() -> None:
    """Every retained provider exception can use Magic Mic's English fallback."""
    provider_keys = _provider_translation_keys()
    catalog = json.loads(STRINGS_PATH.read_text())["exceptions"]

    assert provider_keys
    assert provider_keys <= catalog.keys()
