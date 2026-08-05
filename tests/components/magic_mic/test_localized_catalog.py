"""Tests for the localized retrieval-document builder (evals/harness/localized_catalog.py).

The measured comparison lives in the artifact; what is worth protecting here is the
machinery that makes the number mean anything. Three things can silently invalidate it:
harvesting stems instead of surface forms (the exact-token scorer cannot match a stem),
deriving a document for a bundle that has no upstream sentences (which would quietly
replace an authored document with nothing), and a held-out split that is not actually held
out (which would score a document against itself).
"""

import pytest

from custom_components.magic_mic.capabilities.capability_selection import (
    action_descriptor,
    default_catalog,
    extend_catalog,
)
from evals.harness.localized_catalog import (
    LocalizedCatalogError,
    held_out_cases,
    held_out_novelty,
    intent_vocabulary,
    load_language,
    localized_catalog,
    render_template,
    score_held_out,
)

# Bundles whose tools are Magic Mic's own, not HASSIL intents: their localization runs
# through strings.json, so the builder must leave their authored document alone.
AUTHORED_BUNDLES = {"datetime", "find_entities", "live_context"}


def test_unknown_language_is_a_named_failure() -> None:
    """A locale Home Assistant ships no sentences for cannot be derived, and says so."""
    with pytest.raises(LocalizedCatalogError):
        load_language("zz")


def test_vocabulary_is_surface_forms_not_stems() -> None:
    """German inflection is an optional suffix; rendering must yield the whole word.

    `dreh[e]` harvested as text chunks gives "dreh" and "e", neither of which an
    exact-token scorer can match against a spoken "drehe". Rendering the template gives
    both real surface forms.
    """
    german = load_language("de")
    vocabulary = intent_vocabulary(german, "HassTurnOn")
    assert "drehe" in vocabulary
    assert "e" not in vocabulary


def test_derived_documents_are_in_the_requested_language() -> None:
    """A derived bundle carries the language's own words, not the authored English."""
    german = load_language("de")
    catalog, authored = localized_catalog(default_catalog(), german)
    climate = catalog.by_id["climate"]
    assert "temperatur" in climate.selection_text
    assert "thermostat temperature" not in climate.selection_text
    assert set(authored) == AUTHORED_BUNDLES


def test_bundles_without_upstream_sentences_keep_their_document() -> None:
    """No upstream sentences means the authored document survives untouched."""
    german = load_language("de")
    catalog, authored = localized_catalog(default_catalog(), german)
    for bundle_id in authored:
        assert (
            catalog.by_id[bundle_id].selection_text
            == default_catalog().by_id[bundle_id].selection_text
        )


def test_script_descriptors_pass_through_unchanged() -> None:
    """A script's document is already the household's own language, so leave it alone."""
    german = load_language("de")
    script = action_descriptor(
        "filmabend",
        "Filmabend",
        aliases=("kinoabend",),
        description="Licht dimmen und den Projektor starten.",
    )
    catalog, _ = localized_catalog(extend_catalog(default_catalog(), (script,)), german)
    assert catalog.by_id["script:filmabend"] == script


def test_held_out_cases_come_from_templates_the_document_excluded() -> None:
    """The split is by template: no held-out utterance may be one the build half rendered."""
    german = load_language("de")
    build_half_renderings = {
        " ".join(rendering.split())
        for index, template in enumerate(german.sentences["HassCancelTimer"])
        if index % 2 == 0
        for rendering in render_template(german, template)
    }
    cases = held_out_cases(german, ["HassCancelTimer"], per_intent=4)
    assert cases
    assert {case.expected for case in cases} == {"HassCancelTimer"}
    assert not {case.utterance for case in cases} & build_half_renderings


def test_novelty_bounds_the_held_out_claim() -> None:
    """Template-disjoint is not vocabulary-disjoint, and the artifact must say so.

    An intent's phrasings reuse the same words, so the build half has usually seen most
    held-out tokens already. That is not a defect in the split, it is the ceiling on what
    the held-out recall can claim, and it is reported rather than left implicit.
    """
    german = load_language("de")
    cases = held_out_cases(german, ["HassCancelTimer", "HassTurnOn"], per_intent=4)
    novelty = held_out_novelty(cases, german, keep=lambda index: index % 2 == 0)
    assert novelty["tokens_total"] > 0
    assert 0.0 <= novelty["novel_token_share"] < 0.5
    # Whatever the value, the honesty fields must be present for the report to bound it.
    assert "cases_with_a_novel_token" in novelty


def test_score_held_out_reports_what_carried_each_hit() -> None:
    """A cross-language hit must be attributable, so a loanword cannot pass as coverage."""
    german = load_language("de")
    cases = held_out_cases(german, ["HassCancelTimer"], per_intent=2)
    scored = score_held_out(cases, default_catalog(), budget=8)
    # "Timer" is a loanword: the English catalog reaches these cases through it alone.
    assert scored["carrying_tokens"].get("timer")
    assert scored["cases"] == len(cases)
