"""Constants for the Magic Mic integration."""

import logging

DOMAIN = "magic_mic"
LOGGER = logging.getLogger(__package__)

# Base instructions for the agent. The Assist LLM API appends the exposed-entity
# context (the "api_prompt") to this at request time.
DEFAULT_PROMPT = "You are a voice assistant for Home Assistant."

# Prompt-context (docs/prompt-context.md, PRODUCT_PLAN §5.6). When enabled, the
# testbed proxy replaces HA's full exposed-entity roster ("Static Context") with the
# bounded taxonomy skeleton. On by default: the testbed is the product-in-progress
# surface, and the baseline agent keeps the roster for the measured comparison.
CONF_TAXONOMY_SKELETON = "taxonomy_skeleton"
DEFAULT_TAXONOMY_SKELETON = True
