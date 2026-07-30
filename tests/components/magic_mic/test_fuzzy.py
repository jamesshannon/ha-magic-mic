"""Tests for the fuzzy scorer and the top-1/top-2 ambiguity guard (find-entities.md).

The guard tests build ``Scored`` lists directly so they pin the decision logic against
the thresholds, independent of whatever the underlying scorer emits.
"""

import pytest

from custom_components.magic_mic import fuzzy
from custom_components.magic_mic.const import (
    FUZZY_ACCEPT_SCORE,
    FUZZY_FLOOR_SCORE,
    FUZZY_IDF_MIN_CANDIDATES,
    FUZZY_MARGIN_SCORE,
)
from custom_components.magic_mic.fuzzy import (
    Candidate,
    Scored,
    resolve,
    resolve_candidates,
    score,
    score_candidates,
)

# A candidate set where union ties the target with a distractor on a shared token
# ("living room"), but IDF separates them: only the blind matches the rare "blind".
_SHARED_WORD_HOME = {
    "cover.blind": Candidate(("Living Room Blind",)),
    "media_player.tv": Candidate(("Living Room TV", "TV")),
    "light.bed": Candidate(("Bedroom Light",)),
    "light.kitchen": Candidate(("Kitchen Light",)),
    "fan.bath": Candidate(("Bathroom Fan",)),
}


def test_score_rewards_shared_tokens_over_distractors() -> None:
    """The canonical hit clears the accept bar; a distractor stays below the floor."""
    hit = score("reading light", "Reading Lamp")
    distractor = score("reading light", "Front Door Lock")

    assert hit >= FUZZY_ACCEPT_SCORE
    assert distractor < FUZZY_FLOOR_SCORE
    assert score("Reading Lamp", "Reading Lamp") == pytest.approx(100.0)


def test_score_is_order_insensitive() -> None:
    """token_set semantics: word order does not penalize a full-token overlap."""
    assert score("kitchen ceiling", "Ceiling Light Kitchen") >= FUZZY_ACCEPT_SCORE


def test_score_candidates_ranks_by_best_name_and_drops_nameless() -> None:
    """Each candidate scores by its best alias; nameless candidates are dropped."""
    ranked = score_candidates(
        "reading light",
        {
            "light.lr": Candidate(("Overhead", "Reading Lamp")),
            "light.kitchen": Candidate(("Kitchen Light",)),
            "light.nameless": Candidate(()),
        },
    )

    assert [scored.key for scored in ranked] == ["light.lr", "light.kitchen"]
    assert ranked[0].score >= ranked[1].score


def test_aliases_score_as_separate_documents() -> None:
    """A query cannot combine one token from one alias with another from a second alias.

    The joined blob "Reading Light Desk Lamp" would be a full subset match for "reading
    lamp" (score 100); scored per-alias, neither alias is more than a partial match.
    """
    combo = {"light.combo": Candidate(("Reading Light", "Desk Lamp"))}

    [scored] = score_candidates("reading lamp", combo)

    assert scored.score < 100


def test_location_context_still_completes_a_name() -> None:
    """Per-alias scoring keeps location on each document, so the area completes the name."""
    ranked = score_candidates(
        "kitchen light",
        {"light.ceiling": Candidate(("Ceiling Light",), context=("Kitchen",))},
    )

    assert ranked[0].score == pytest.approx(100.0)


def test_resolve_decisive_single_winner() -> None:
    """A leader past accept, clear of the runner-up by the margin, resolves."""
    resolution = resolve([Scored("a", 92.0), Scored("b", 55.0)], limit=5)

    assert resolution.match == Scored("a", 92.0)
    assert resolution.ambiguous is False


def test_resolve_lone_candidate_needs_accept_bar() -> None:
    """A single above-floor candidate still must clear accept, or it is ambiguous."""
    below = FUZZY_ACCEPT_SCORE - 1
    resolution = resolve([Scored("a", below)], limit=5)

    assert resolution.match is None
    assert resolution.ambiguous is True
    assert resolution.candidates == [Scored("a", below)]


def test_resolve_close_cluster_is_ambiguous() -> None:
    """Two above-floor candidates within the margin return as an ambiguous shortlist."""
    resolution = resolve([Scored("a", 90.0), Scored("b", 90.0 - 1)], limit=5)

    assert resolution.match is None
    assert resolution.ambiguous is True
    assert [scored.key for scored in resolution.candidates] == ["a", "b"]


def test_resolve_drops_below_floor_and_caps_to_limit() -> None:
    """Sub-floor candidates never appear; the shortlist is capped to the limit."""
    ranked = [Scored(str(i), FUZZY_ACCEPT_SCORE - 2) for i in range(5)]
    ranked.append(Scored("under", FUZZY_FLOOR_SCORE - 1))

    resolution = resolve(ranked, limit=3)

    assert resolution.ambiguous is True
    assert len(resolution.candidates) == 3
    assert "under" not in {scored.key for scored in resolution.candidates}


def test_resolve_empty_when_all_below_floor() -> None:
    """Nothing above the floor yields no match and no candidates."""
    resolution = resolve([Scored("a", FUZZY_FLOOR_SCORE - 1)], limit=5)

    assert resolution.match is None
    assert resolution.candidates == []
    assert resolution.ambiguous is False


def test_margin_boundary_is_inclusive() -> None:
    """A lead exactly equal to the margin (and past accept) is decisive."""
    resolution = resolve(
        [
            Scored("a", FUZZY_ACCEPT_SCORE),
            Scored("b", FUZZY_ACCEPT_SCORE - FUZZY_MARGIN_SCORE),
        ],
        limit=5,
    )

    assert resolution.match == Scored("a", FUZZY_ACCEPT_SCORE)


def test_union_alone_leaves_shared_word_cluster_ambiguous() -> None:
    """Baseline: the union scorer cannot separate the target from the shared-token TV."""
    resolution = resolve(score_candidates("living room blind", _SHARED_WORD_HOME), 5)

    assert resolution.match is None
    assert resolution.ambiguous is True
    assert {"cover.blind", "media_player.tv"} <= {s.key for s in resolution.candidates}


def test_idf_tiebreak_resolves_shared_word_cluster() -> None:
    """The IDF tie-break down-weights the shared 'living room' so the blind wins."""
    resolution = resolve_candidates("living room blind", _SHARED_WORD_HOME, 5)

    assert resolution.match is not None
    assert resolution.match.key == "cover.blind"


def test_idf_tiebreak_skipped_below_min_candidates() -> None:
    """With too few candidates to estimate rarity, the union ambiguity is left alone."""
    two = {
        "cover.blind": Candidate(("Living Room Blind",)),
        "media_player.tv": Candidate(("Living Room TV",)),
    }
    assert len(two) < FUZZY_IDF_MIN_CANDIDATES

    resolution = resolve_candidates("living room blind", two, 5)

    assert resolution.match is None
    assert resolution.ambiguous is True


def test_resolve_candidates_keeps_a_decisive_union_result() -> None:
    """When union already resolves, the tie-break is not consulted and does not interfere."""
    resolution = resolve_candidates(
        "reading lamp", _SHARED_WORD_HOME | {"light.r": Candidate(("Reading Lamp",))}, 5
    )

    assert resolution.match is not None
    assert resolution.match.key == "light.r"


def test_idf_tiebreak_works_without_rapidfuzz(monkeypatch: pytest.MonkeyPatch) -> None:
    """The difflib fallback drives the same tie-break when rapidfuzz is unavailable."""
    monkeypatch.setattr(fuzzy, "_HAVE_RAPIDFUZZ", False)

    resolution = resolve_candidates("living room blind", _SHARED_WORD_HOME, 5)

    assert resolution.match is not None
    assert resolution.match.key == "cover.blind"
