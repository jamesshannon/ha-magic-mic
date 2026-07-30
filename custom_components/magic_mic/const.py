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
