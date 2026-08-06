"""Constants for the Magic Mic integration."""

import logging

DOMAIN = "magic_mic"
LOGGER = logging.getLogger(__package__)

# No base-prompt constant: the agents leave CONF_PROMPT unset so chat_log falls back
# to core's llm.DEFAULT_INSTRUCTIONS_PROMPT, rather than re-typing (and drifting from)
# it here. The Assist LLM API appends the exposed-entity context at request time.

# Prompt-context (docs/prompt-context.md, PRODUCT_PLAN §5.6). When enabled, the
# testbed proxy replaces HA's full exposed-entity roster ("Static Context") with the
# bounded entity summary. On by default: the testbed is the product-in-progress
# surface, and the baseline agent keeps the roster for the measured comparison.
CONF_ENTITY_SUMMARY = "entity_summary"
DEFAULT_ENTITY_SUMMARY = True

# Tier-2 request-conditioned name injection (docs/prompt-context.md "Tier 2"). On top of
# the summary, inject exact names for a small, request-relevant subset so the common
# command stays zero-lookup. Gated separately so the eval harness can A/B summary-only
# vs summary+names.
#
# Off by default since 2026-08-05: measured at 1.45x total spend against summary-only with
# identical task success, because per-turn names sit inside the single cached system block
# and re-prefill it every turn. No tool in the Wave 1 roster consumes an entity_id, so the
# lookup this exists to skip is never taken. Keep the mechanism; re-measure when the first
# entity_id-consuming tool lands (the Wave 3 scheduling substrate), with the cache-isolated
# second block (prompt-context.md "Option 2") built at the same time.
CONF_NAME_INJECTION = "name_injection"
DEFAULT_NAME_INJECTION = False
# Top-N names injected per request: enough to cover a room's relevant devices, small
# enough to stay a bounded tail (the point of retiring the roster). Tunable on the eval.
NAME_INJECTION_LIMIT = 10
# Registry and integration values embedded in a system prompt are untrusted data. Keep
# each value and the complete JSON block bounded independently of ordinary home size.
PROMPT_CONTEXT_FIELD_LIMIT = 160
PROMPT_CONTEXT_BLOCK_LIMIT = 8192
PROMPT_CONTEXT_DEVICE_CLASS_LIMIT = 32
# Recall floor for what counts as relevant, rapidfuzz token_set_ratio 0-100. Deliberately
# at/below the resolution FLOOR (find_entities asks when unsure; injection just pre-loads
# candidates, so a spurious inclusion wastes a few tokens, not a wrong action). Tunable.
NAME_INJECTION_FLOOR = 55.0
# Room scope is a soft prior, not a hard gate (docs/prompt-context.md Tier 2, Refinement B):
# entities outside the requesting area still qualify, but only on a strong, explicit name
# match, so "turn off the kitchen ceiling light" from the living room still gets a fast path
# while a generic in-house match cannot leak cross-room noise. Room entities also get a small
# ranking bonus so they sort above equal-scoring house-wide ones. Both tunable.
NAME_INJECTION_HOUSE_FLOOR = 75.0
NAME_INJECTION_ROOM_BONUS = 10.0

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

# Capability selection (docs/capability-selection.md, PRODUCT_PLAN §5.6/§6.2). The
# prompt-time system that turns the full installed tool catalog into a small, relevant,
# authorized API for one turn. Wave 1 builds it in *shadow mode*: it computes the plan
# and records recall/savings against the full-roster baseline, but never removes a tool
# from a live request until the recall@budget and task-success gates pass.
#
# CONF_CAPABILITY_SELECTION is the enforcement switch. Off by default precisely because
# those gates have not passed (recall is "a direction, not a gate pass" and the demo
# catalog's retrieval documents are English, which docs "Localization" forbids from gating
# a live request). The testbed seam reads the flag; the eval harness turns it on to run
# the full-roster-vs-enforced task-success comparison that must pass before the default
# can flip. When off, the roster is unchanged from the tool-policy exposure filter.
CONF_CAPABILITY_SELECTION = "capability_selection"
DEFAULT_CAPABILITY_SELECTION = False
#
# SELECTION_TOOL_BUDGET is the tool-count ceiling the assembler fits under. The provider
# hard limit is 128 (docs "Goals"); this soft budget is the shadow target we measure
# recall against, deliberately far below the ceiling so savings are visible on real homes
# full of scripts. Tunable on the eval.
SELECTION_TOOL_BUDGET = 24
# Retrieval floor: a descriptor scoring below this is not admitted on relevance alone
# (dependencies and always-resident bundles bypass it). Deliberately near zero, so it only
# drops a bundle with no query overlap at all; budget and ranking do the real pruning, not
# an aggressive early cut (docs "Optimize for recall": a false positive costs a few tokens,
# a false negative breaks the task). Score is IDF-weighted coverage, 0-100. Tunable.
SELECTION_RELEVANCE_FLOOR = 1.0
