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

# Default and hard cap on how many candidates find_entities returns per lookup.
FIND_ENTITIES_DEFAULT_LIMIT = 5
FIND_ENTITIES_MAX_LIMIT = 25
