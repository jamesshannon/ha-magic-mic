"""Fuzzy name scoring and the top-1/top-2 ambiguity guard.

The shared resolution primitive from `docs/find-entities.md`: one scorer plus one
guard, with several consumers (the `find_entities` tool today; prompt-context Tier-2
name injection and the match-layer fuzzy fallback later). It lives at the top level,
alongside `identity.py` and `store.py`, because it is a cross-capability seam rather
than a capability of its own.

rapidfuzz `token_set_ratio` is the scorer: order- and duplicate-insensitive, and it
rewards shared tokens ("reading light" ↔ "Reading Lamp") without punishing word order
("kitchen ceiling" ↔ "Ceiling Light Kitchen"). A stdlib `difflib` token-set
approximation stands in behind the same `score()` interface when rapidfuzz is absent,
so the eventual core PR can treat the dependency as optional (find-entities.md
"Dependency: rapidfuzz"). Depends on neither `hass` nor the conversation shell.
"""

from dataclasses import dataclass, field
import re

from .const import FUZZY_ACCEPT_SCORE, FUZZY_FLOOR_SCORE, FUZZY_MARGIN_SCORE

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


def score(query: str, name: str) -> float:
    """Return the fuzzy similarity of ``query`` to a single ``name`` in ``[0, 100]``."""
    if _HAVE_RAPIDFUZZ:
        return fuzz.token_set_ratio(query, name, processor=utils.default_process)
    return _difflib_token_set_ratio(query, name)


def score_candidates(query: str, candidates: dict[str, list[str]]) -> list[Scored]:
    """Score each candidate by its best-matching name, ranked high to low.

    ``candidates`` maps an opaque key (an ``entity_id`` for the entity consumers) to
    that candidate's names and aliases. Candidates with no names are dropped.
    """
    ranked = [
        Scored(key, max(score(query, name) for name in names))
        for key, names in candidates.items()
        if names
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


def _difflib_token_set_ratio(query: str, name: str) -> float:
    """Approximate rapidfuzz ``token_set_ratio`` with the stdlib ``difflib``.

    Follows the same shape: compare the shared-token core against each side's full
    sorted token string and take the best ratio, so a query that is a token subset of
    the name scores 100. Only reached when rapidfuzz is not installed.
    """
    from difflib import SequenceMatcher  # noqa: PLC0415

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
