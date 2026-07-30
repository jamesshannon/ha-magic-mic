"""Constants for the Magic Mic integration."""

import logging

DOMAIN = "magic_mic"
LOGGER = logging.getLogger(__package__)

# No base-prompt constant: the agents leave CONF_PROMPT unset so chat_log falls back
# to core's llm.DEFAULT_INSTRUCTIONS_PROMPT, rather than re-typing (and drifting from)
# it here. The Assist LLM API appends the exposed-entity context at request time.

# Prompt-context (docs/prompt-context.md, PRODUCT_PLAN §5.6). When enabled, the
# testbed proxy replaces HA's full exposed-entity roster ("Static Context") with the
# bounded taxonomy skeleton. On by default: the testbed is the product-in-progress
# surface, and the baseline agent keeps the roster for the measured comparison.
CONF_TAXONOMY_SKELETON = "taxonomy_skeleton"
DEFAULT_TAXONOMY_SKELETON = True

# Tier-2 request-conditioned name injection (docs/prompt-context.md "Tier 2"). On top of
# the skeleton, inject exact names for a small, request-relevant subset so the common
# command stays zero-lookup. Gated separately from the skeleton so the eval harness can
# A/B skeleton-only vs skeleton+names.
CONF_NAME_INJECTION = "name_injection"
DEFAULT_NAME_INJECTION = True
# Top-N names injected per request: enough to cover a room's relevant devices, small
# enough to stay a bounded tail (the point of retiring the roster). Tunable on the eval.
NAME_INJECTION_LIMIT = 10
# Recall floor for what counts as relevant, rapidfuzz token_set_ratio 0-100. Deliberately
# at/below the resolution FLOOR (find_entities asks when unsure; injection just pre-loads
# candidates, so a spurious inclusion wastes a few tokens, not a wrong action). Tunable.
NAME_INJECTION_FLOOR = 55.0

# Fuzzy entity resolution (docs/find-entities.md, the scorer + ambiguity guard). Scores
# are rapidfuzz token_set_ratio, 0-100. These are starting values calibrated against the
# doc's examples ("reading light" -> "Reading Lamp" resolves; "couch lamp" does not), to
# be tuned on the eval harness:
#   FLOOR  - discard candidates below this (stops "the thermostat" matching a light).
#   ACCEPT - a single winner must clear this to auto-resolve without asking.
#   MARGIN - and must lead the runner-up by this, or the match is treated as ambiguous.
FUZZY_FLOOR_SCORE = 60.0
FUZZY_ACCEPT_SCORE = 70.0
FUZZY_MARGIN_SCORE = 15.0

# IDF tie-break (docs/find-entities.md term weighting). When the union scorer leaves an
# above-floor cluster ambiguous, re-rank just that cluster by IDF-weighted coverage,
# which down-weights tokens common across the candidate set (a shared "light", or the
# area token inside an area-filtered set) so the discriminating token decides. Gated so
# it never fires where it would misjudge:
#   TOKEN_MATCH  - two tokens count as the same only at/above this char similarity (0-100),
#                  so "light"~"lights" match but "lamp"/"light" do not.
#   IDF_MIN_CANDIDATES - below this many candidates, df cannot estimate term rarity (a
#                  common word looks rare just by being absent), so stay on union.
FUZZY_TOKEN_MATCH_SCORE = 82.0
FUZZY_IDF_MIN_CANDIDATES = 5

# Default and hard cap on how many candidates find_entities returns per lookup.
FIND_ENTITIES_DEFAULT_LIMIT = 5
FIND_ENTITIES_MAX_LIMIT = 25
