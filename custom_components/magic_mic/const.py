"""Constants for the Magic Mic integration."""

import logging

DOMAIN = "magic_mic"
LOGGER = logging.getLogger(__package__)

# Base instructions for the agent. The Assist LLM API appends the exposed-entity
# context (the "api_prompt") to this at request time.
DEFAULT_PROMPT = "You are a voice assistant for Home Assistant."
