"""Fuzzy name scoring and the top-1/top-2 ambiguity guard.

The shared resolution primitive from `docs/find-entities.md`: one scorer plus one
guard, with several consumers (the `find_entities` tool today; prompt-context Tier-2
name injection and the match-layer fuzzy fallback later). It lives at the top level,
alongside `identity.py` and `store.py`, because it is a cross-capability seam rather
than a capability of its own.

`resolve_candidates` is the entry point the consumers call; it runs a two-stage
pipeline. Stage one is rapidfuzz `token_set_ratio` over each candidate's *documents*:
one per name/alias, each enriched with the candidate's shared location context (area,
floor), scored separately and maxed. Per-alias (not one joined blob) so a query cannot
match "reading" from one alias and "lamp" from another; location on each so a query can
still span fields ("kitchen light" ↔ a "Ceiling Light" in the "Kitchen").
`token_set_ratio` is order- and duplicate-insensitive and rewards shared tokens without
punishing word order. Stage two runs only when stage one leaves an above-floor cluster
ambiguous and the candidate set is large enough to estimate term rarity: it re-ranks
just that cluster by IDF-weighted coverage, down-weighting tokens common across the set
(a shared "light", or the area token inside an area-filtered set) so the discriminating
token decides. Union stays the floor, so small homes and recall are never sacrificed.

A stdlib `difflib` approximation stands in behind both `token_set_ratio` and the
per-token ratio when rapidfuzz is absent, so the eventual core PR can treat the
dependency as optional (find-entities.md "Dependency: rapidfuzz"). Depends on neither
`hass` nor the conversation shell.
"""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import math
import re

from .const import (
    FUZZY_ACCEPT_SCORE,
    FUZZY_FLOOR_SCORE,
    FUZZY_IDF_MIN_CANDIDATES,
    FUZZY_MARGIN_SCORE,
    FUZZY_TOKEN_MATCH_SCORE,
)

try:
    from rapidfuzz import fuzz, utils

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - rapidfuzz is a manifest requirement
    _HAVE_RAPIDFUZZ = False

# Mirrors rapidfuzz.utils.default_process for the difflib fallback: lowercase, then
# collapse every non-alphanumeric run to a single space so tokenization matches.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


@dataclass(slots=True, frozen=True)
class Scored:
    """A candidate key with its best name-match score in ``[0, 100]``."""

    key: str
    score: float


@dataclass(slots=True, frozen=True)
class Resolution:
    """Outcome of the ambiguity guard over a ranked candidate list.

    ``match`` is the confident single winner, or ``None`` when the guard could not
    settle on one; ``candidates`` is the above-floor shortlist (truncated to the
    caller's limit) to offer for disambiguation; ``ambiguous`` is True when there are
    above-floor candidates but no decisive winner.
    """

    match: Scored | None
    candidates: list[Scored] = field(default_factory=list)
    ambiguous: bool = False


@dataclass(slots=True, frozen=True)
class Candidate:
    """One entity to score against: its names/aliases plus shared location context.

    ``names`` are the entity's name and aliases. Each is scored as its *own* document
    (see `documents`), so a query cannot combine tokens across two different aliases:
    "reading lamp" will not match an entity whose aliases are "Reading Light" and "Desk
    Lamp" just because the two together contain {reading, lamp}.

    This is a deliberate, conservative choice, and the trade-off runs both ways - keep it
    in mind before changing it:

    - Per-alias (here) can't manufacture a match no single alias supports, and a spurious
      cross-alias match is the dangerous kind: it can become a *false resolve* (a wrong
      physical action) instead of a clarifying question.
    - It gives up *legitimate* cross-alias matches. If the aliases were complementary
      fragments ("Reading Light" + "Nook Lamp"), "reading lamp" arguably should hit this
      entity, and now won't - the guard asks instead.

    We lean conservative (a wrong action costs more than a question, find-entities.md),
    but the seed corpus has neither case, so this is a reasoned default, not a measured
    one. To allow cross-alias matching, join ``names`` into one document below.

    ``context`` is location (area, floor) appended to *every* name document, so a query
    can match a name token and an area token together without a bare area token resolving
    anything on its own.
    """

    names: tuple[str, ...]
    context: tuple[str, ...] = ()

    def documents(self) -> list[str]:
        """Return one scorable document per name, each with the location appended."""
        suffix = f" {' '.join(self.context)}" if self.context else ""
        return [f"{name}{suffix}" for name in self.names if name]


def score(query: str, name: str) -> float:
    """Return the fuzzy similarity of ``query`` to a single ``name`` in ``[0, 100]``."""
    if _HAVE_RAPIDFUZZ:
        return fuzz.token_set_ratio(query, name, processor=utils.default_process)
    return _difflib_token_set_ratio(query, name)


def score_candidates(query: str, candidates: dict[str, Candidate]) -> list[Scored]:
    """Score each candidate by its best-matching document, ranked high to low.

    ``candidates`` maps an opaque key (an ``entity_id`` for the entity consumers) to a
    `Candidate`. Each candidate's documents (one per name/alias, with location appended)
    are scored separately and the best is kept: per-alias so a query cannot span two
    different aliases, location-on-each so it can span a name token and an area token.
    Candidates with no documents are dropped.
    """
    ranked = [
        Scored(key, max(score(query, document) for document in documents))
        for key, candidate in candidates.items()
        if (documents := candidate.documents())
    ]
    ranked.sort(key=lambda scored: scored.score, reverse=True)
    return ranked


def resolve(ranked: list[Scored], limit: int) -> Resolution:
    """Apply the ambiguity guard to a ranked list (as from `score_candidates`).

    Drops anything below the floor, then accepts a single winner only when it clears
    the accept threshold *and* leads the runner-up by the margin. Otherwise the
    above-floor cluster is returned as an ambiguous shortlist for the model to
    disambiguate. Conservative by design: a wrong physical action costs more than a
    clarifying question (find-entities.md "Why not optimistic best-match like music").
    """
    above = [scored for scored in ranked if scored.score >= FUZZY_FLOOR_SCORE]
    if not above:
        return Resolution(None)

    top = above[0]
    runner_up = above[1].score if len(above) > 1 else 0.0
    if top.score >= FUZZY_ACCEPT_SCORE and top.score - runner_up >= FUZZY_MARGIN_SCORE:
        return Resolution(top, above[:limit])
    return Resolution(None, above[:limit], ambiguous=True)


def resolve_candidates(
    query: str, candidates: dict[str, Candidate], limit: int
) -> Resolution:
    """Resolve a fuzzy ``query`` against ``candidates`` (the shared primitive entry point).

    Union `token_set_ratio` first; if that decides or finds nothing, return it. Only when
    it leaves an above-floor cluster ambiguous *and* there are enough candidates for IDF
    to estimate term rarity (`FUZZY_IDF_MIN_CANDIDATES`) do we re-rank that cluster by
    IDF-weighted coverage and see if the shared token was the only thing tying it. IDF
    can therefore break a tie but never demote the union result: small homes and recall
    are unaffected, and the cost is paid only on the ambiguous large-set path.
    """
    union = resolve(score_candidates(query, candidates), limit)
    if union.match is not None or not union.ambiguous:
        return union
    if len(candidates) < FUZZY_IDF_MIN_CANDIDATES:
        return union

    weighted = {
        scored.key: scored.score for scored in _score_candidates_idf(query, candidates)
    }
    cluster = sorted(
        (
            Scored(scored.key, weighted.get(scored.key, 0.0))
            for scored in union.candidates
        ),
        key=lambda scored: scored.score,
        reverse=True,
    )
    reranked = resolve(cluster, limit)
    return reranked if reranked.match is not None else union


def _score_candidates_idf(query: str, candidates: dict[str, Candidate]) -> list[Scored]:
    """Score each candidate by IDF-weighted token coverage over the candidate set.

    Each candidate contributes one token set per document (name/alias + location); a
    token's weight is its inverse document frequency across the *candidates*, counted
    once per candidate, so words common here (a shared "light", or the area token when
    the set is one area) count for little. A candidate's score is the best of its
    documents, each the higher of its query-side coverage (how much of the query's weight
    it matched) and its doc-side coverage (how much of its own weight the query matched):
    query-side keeps a subset of a longer name scoring high, doc-side stops an unmatched
    content word in the query from being ignored. Per-document (not one pooled bag) so a
    match cannot span two different aliases.
    """
    docs = {
        key: token_docs
        for key, candidate in candidates.items()
        if (token_docs := [t for d in candidate.documents() if (t := _tokenize(d))])
    }
    document_frequency: Counter[str] = Counter()
    for token_docs in docs.values():
        document_frequency.update(set().union(*token_docs))
    total = len(docs)

    def idf(token: str) -> float:
        # Unknown tokens (an out-of-vocabulary query word) get the rarest weight, so an
        # unmatched content word is penalized rather than ignored.
        return math.log(1 + total / document_frequency.get(token, 1))

    query_tokens = _tokenize(query)
    return [
        Scored(
            key, max(_idf_coverage(query_tokens, tokens, idf) for tokens in token_docs)
        )
        for key, token_docs in docs.items()
    ]


def _idf_coverage(
    query_tokens: set[str], doc_tokens: set[str], idf: Callable[[str], float]
) -> float:
    """Return the IDF-weighted ``max(query-side, doc-side)`` coverage in ``[0, 100]``."""
    matched_weight = 0.0
    unmatched_query = 0.0
    matched_doc: set[str] = set()
    for token in query_tokens:
        best, best_ratio = None, 0.0
        for candidate_token in doc_tokens:
            ratio = _char_ratio(token, candidate_token)
            if ratio > best_ratio:
                best, best_ratio = candidate_token, ratio
        if best is not None and best_ratio >= FUZZY_TOKEN_MATCH_SCORE:
            matched_weight += (best_ratio / 100.0) * idf(best)
            matched_doc.add(best)
        else:
            unmatched_query += idf(token)

    unmatched_doc = sum(idf(token) for token in doc_tokens - matched_doc)
    query_side = _fraction(matched_weight, unmatched_query)
    doc_side = _fraction(matched_weight, unmatched_doc)
    return 100.0 * max(query_side, doc_side)


def _fraction(matched: float, unmatched: float) -> float:
    """Return ``matched / (matched + unmatched)``, or 0 when both are zero."""
    total = matched + unmatched
    return matched / total if total else 0.0


def _char_ratio(left: str, right: str) -> float:
    """Character-level similarity of two tokens in ``[0, 100]`` (rapidfuzz or difflib)."""
    if _HAVE_RAPIDFUZZ:
        return fuzz.ratio(left, right)
    return 100.0 * SequenceMatcher(None, left, right).ratio()


def _difflib_token_set_ratio(query: str, name: str) -> float:
    """Approximate rapidfuzz ``token_set_ratio`` with the stdlib ``difflib``.

    Follows the same shape: compare the shared-token core against each side's full
    sorted token string and take the best ratio, so a query that is a token subset of
    the name scores 100. Only reached when rapidfuzz is not installed.
    """
    query_tokens = _tokenize(query)
    name_tokens = _tokenize(name)
    if not query_tokens or not name_tokens:
        return 0.0

    shared = query_tokens & name_tokens
    core = " ".join(sorted(shared))
    query_only = core + " " + " ".join(sorted(query_tokens - shared))
    name_only = core + " " + " ".join(sorted(name_tokens - shared))

    return 100.0 * max(
        SequenceMatcher(None, core, query_only).ratio(),
        SequenceMatcher(None, core, name_only).ratio(),
        SequenceMatcher(None, query_only, name_only).ratio(),
    )


def _tokenize(value: str) -> set[str]:
    """Lowercase and split on non-alphanumeric runs, as `default_process` would."""
    return {token for token in _NON_ALNUM.sub(" ", value.lower()).split() if token}
