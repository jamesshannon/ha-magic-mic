"""Tests for the offline capability-selection shadow harness."""

from pathlib import Path

from custom_components.magic_mic.capabilities.capability_selection import (
    CapabilityDescriptor,
    Catalog,
    default_catalog,
)

from .corpus import CORPUS_DIR, load_corpus
from .selection_shadow import (
    CaseTools,
    build_shadow_artifact,
    catalog_for_world,
    load_case_tools,
    load_case_tools_from_corpus,
    shadow_recall,
    uncatalogued_tools,
)

_SCRIPTS_CORPUS = CORPUS_DIR / "wave1_scripts.yaml"


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


def test_catalog_for_world_adds_a_tool_per_script() -> None:
    """Each script entity in the corpus world becomes its own catalog tool."""
    corpus = load_corpus(_SCRIPTS_CORPUS)

    catalog = catalog_for_world(corpus.world)

    assert "movie_night" in catalog.tool_names()
    assert catalog.by_tool["movie_night"] == "script:movie_night"
    # The base bundles are still present alongside the scripts.
    assert "HassTurnOn" in catalog.tool_names()


def test_corpus_case_tools_use_the_declared_expected_tool() -> None:
    """Corpus-driven recall measures against each case's expected tool, not a live run."""
    corpus = load_corpus(_SCRIPTS_CORPUS)

    cases = {case.id: case for case in load_case_tools_from_corpus(corpus)}

    assert cases["iv-movie-night"].used == ("movie_night",)
    assert cases["iv-movie-night"].phrasing == "in_vocabulary"
    assert cases["base-read-time"].used == ("GetDateTime",)


def test_configured_aliases_carry_an_out_of_vocab_request() -> None:
    """A configured trigger bridges a different-speaker phrasing end to end.

    "I'm going to sleep" shares no token with "Bedtime"; the script's configured "going to
    bed" trigger is what bridges it. This checks the alias field flows from the corpus
    through catalog_for_world into a covered case, at a budget far below the roster.
    """
    corpus = load_corpus(_SCRIPTS_CORPUS)
    catalog = catalog_for_world(corpus.world)
    cases = load_case_tools_from_corpus(corpus)

    recall = shadow_recall(cases, catalog, (8,))
    missed_ids = {miss.id for miss in recall[8].misses}

    assert "oob-bedtime" not in missed_ids
    # And selection is real: budget 8 exposes far fewer than the ~50-tool roster.
    assert recall[8].avg_exposed < 20


def test_recall_is_reported_by_phrasing_regime() -> None:
    """Tagged cases get a per-regime recall breakdown, not just an aggregate."""
    catalog = default_catalog()
    cases = [
        CaseTools(
            "a",
            "turn off the living room lamp",
            ("HassTurnOff",),
            phrasing="in_vocabulary",
        ),
        CaseTools(
            "b",
            "add milk to my shopping list",
            ("HassListAddItem",),
            phrasing="out_of_vocabulary",
        ),
        CaseTools("c", "what time is it", ("GetDateTime",)),
    ]

    artifact = build_shadow_artifact(
        Path("c.yaml"), catalog, cases, (24,), basis="expected-tool"
    )

    assert set(artifact["recall_by_phrasing"]) == {
        "in_vocabulary",
        "out_of_vocabulary",
    }
    assert artifact["recall_by_phrasing"]["in_vocabulary"]["24"]["cases_total"] == 1
    # The untagged case still counts in the aggregate.
    assert artifact["recall"]["24"]["cases_total"] == 3
    assert artifact["cases"][0]["phrasing"] == "in_vocabulary"


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
