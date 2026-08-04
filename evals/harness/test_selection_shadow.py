"""Tests for the offline capability-selection shadow harness."""

from pathlib import Path

from custom_components.magic_mic.capabilities.capability_selection import (
    CapabilityDescriptor,
    Catalog,
    default_catalog,
)

from .selection_shadow import (
    CaseTools,
    build_shadow_artifact,
    load_case_tools,
    shadow_recall,
    uncatalogued_tools,
)


def test_load_case_tools_dedups_and_skips_answers() -> None:
    """Used tools are read per case, deduped, and toolless cases keep an empty tuple."""
    artifact = {
        "cases": [
            {
                "id": "turn-off",
                "utterance": "turn off the lamp",
                "tools": [
                    {"name": "HassTurnOff", "args": {}},
                    {"name": "HassTurnOff", "args": {}},
                ],
            },
            {"id": "chat", "utterance": "hello", "tools": []},
        ]
    }

    cases = load_case_tools(artifact)

    assert cases[0].used == ("HassTurnOff",)
    assert cases[1].used == ()


def test_full_budget_recalls_every_catalogued_tool() -> None:
    """At a budget equal to the catalog, every used tool is exposed."""
    catalog = default_catalog()
    cases = [
        CaseTools("a", "turn off the living room lamp", ("HassTurnOff",)),
        CaseTools("b", "add milk to my shopping list", ("HassListAddItem",)),
    ]

    recall = shadow_recall(cases, catalog, (24,))

    assert recall[24].case_recall == 1.0
    assert recall[24].tool_recall == 1.0


def test_tight_budget_misses_a_displaced_bundle() -> None:
    """A budget too small to hold the needed bundle lowers recall and lists the miss.

    The model called ``ToolB``, but the utterance paraphrases bundle ``a``, so ``a``
    outranks ``b``. At a budget that fits only the resident plus one bundle, ``a`` takes
    the slot and ``b``'s tool is not exposed: a real retrieval miss, deterministic.
    """
    catalog = Catalog(
        (
            CapabilityDescriptor(
                id="resident",
                selection_text="current state of the home",
                tools=("GetLiveContext",),
                resident=True,
            ),
            CapabilityDescriptor(
                id="a",
                selection_text="turn the lights on",
                tools=("ToolA",),
                examples=("turn on the lights",),
            ),
            CapabilityDescriptor(
                id="b",
                selection_text="set the volume of a speaker",
                tools=("ToolB",),
                examples=("set the volume",),
            ),
        )
    )
    cases = [CaseTools("mismatch", "turn on the lights", ("ToolB",))]

    tight = shadow_recall(cases, catalog, (2,))
    full = shadow_recall(cases, catalog, (3,))

    assert full[3].case_recall == 1.0
    assert tight[2].case_recall == 0.0
    assert tight[2].misses[0].id == "mismatch"
    assert tight[2].misses[0].missed == ("ToolB",)


def test_uncatalogued_tool_is_surfaced_not_silently_missed() -> None:
    """A used tool no descriptor declares is reported as a catalog gap."""
    catalog = default_catalog()
    cases = [CaseTools("x", "do the thing", ("TotallyUnknownTool",))]

    assert uncatalogued_tools(cases, catalog) == {"TotallyUnknownTool"}
    # And it counts as a miss at every budget, since it can never be exposed.
    recall = shadow_recall(cases, catalog, (24,))
    assert recall[24].case_recall == 0.0


def test_shadow_artifact_shape() -> None:
    """The artifact carries run metadata, per-budget recall, and per-case coverage."""
    catalog = default_catalog()
    cases = [CaseTools("a", "turn off the living room lamp", ("HassTurnOff",))]

    artifact = build_shadow_artifact(Path("src.json"), catalog, cases, (6, 24))

    assert artifact["run"]["enforced"] is False
    assert artifact["run"]["source_artifact"] == "src.json"
    assert set(artifact["recall"]) == {"6", "24"}
    assert artifact["recall"]["24"]["case_recall"] == 1.0
    assert artifact["cases"][0]["id"] == "a"
    assert artifact["cases"][0]["coverage"]["24"] is True
