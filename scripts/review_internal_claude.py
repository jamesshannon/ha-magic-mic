"""Review Magic Mic's Claude adapter against the installed HA provider."""

# ruff: noqa: INP001

import argparse
import difflib
from pathlib import Path
import sys

import homeassistant
from homeassistant.const import __version__ as HA_VERSION

BASELINE_VERSION = "2026.7.4"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_ROOT = (
    Path(homeassistant.__file__).resolve().parent / "components" / "anthropic"
)
ADAPTER_ROOT = PROJECT_ROOT / "custom_components" / "magic_mic" / "internal" / "claude"

# A renamed file is still compared with the upstream module from which it derives.
MODULE_MAP = {
    "__init__.py": "__init__.py",
    "const.py": "const.py",
    "conversation.py": "agent.py",
    "coordinator.py": "coordinator.py",
    "entity.py": "entity.py",
}

# Magic Mic owns integration setup, configuration, and packaging at its root.
OMITTED_UPSTREAM_MODULES = {
    "ai_task.py",
    "config_flow.py",
    "diagnostics.py",
    "repairs.py",
}


def _python_modules(root: Path) -> set[str]:
    return {path.name for path in root.glob("*.py")}


def _validate_inventory() -> list[str]:
    errors: list[str] = []
    if HA_VERSION != BASELINE_VERSION:
        errors.append(
            f"installed Home Assistant is {HA_VERSION}; reviewed baseline is "
            f"{BASELINE_VERSION}"
        )

    expected_upstream = set(MODULE_MAP) | OMITTED_UPSTREAM_MODULES
    if unexpected := _python_modules(UPSTREAM_ROOT) - expected_upstream:
        errors.append(f"unclassified upstream modules: {', '.join(sorted(unexpected))}")
    if missing := expected_upstream - _python_modules(UPSTREAM_ROOT):
        errors.append(
            f"missing expected upstream modules: {', '.join(sorted(missing))}"
        )

    expected_adapter = set(MODULE_MAP.values())
    if unexpected := _python_modules(ADAPTER_ROOT) - expected_adapter:
        errors.append(f"unclassified adapter modules: {', '.join(sorted(unexpected))}")
    if missing := expected_adapter - _python_modules(ADAPTER_ROOT):
        errors.append(f"missing expected adapter modules: {', '.join(sorted(missing))}")
    return errors


def _print_diffs() -> None:
    for upstream_name, adapter_name in MODULE_MAP.items():
        upstream_path = UPSTREAM_ROOT / upstream_name
        adapter_path = ADAPTER_ROOT / adapter_name
        upstream_lines = upstream_path.read_text().splitlines(keepends=True)
        adapter_lines = adapter_path.read_text().splitlines(keepends=True)
        sys.stdout.writelines(
            difflib.unified_diff(
                upstream_lines,
                adapter_lines,
                fromfile=f"homeassistant/components/anthropic/{upstream_name}",
                tofile=f"custom_components/magic_mic/internal/claude/{adapter_name}",
            )
        )


def main() -> int:
    """Validate the inventory and optionally print the source delta."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the baseline and module inventory without printing line diffs",
    )
    args = parser.parse_args()

    if errors := _validate_inventory():
        for error in errors:
            sys.stderr.write(f"error: {error}\n")
        return 1

    sys.stdout.write(f"Claude adapter inventory matches Home Assistant {HA_VERSION}.\n")
    if not args.check:
        _print_diffs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
