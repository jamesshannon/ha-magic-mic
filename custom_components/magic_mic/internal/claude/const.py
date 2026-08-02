"""Constants for the Claude provider adapter.

Lifted near-upstream from HA's `anthropic` component. `DOMAIN` and `LOGGER` come
from the Magic Mic root so errors and devices register under the one integration
domain; the config constants below stay provider-local.
"""

from enum import StrEnum

from ...const import DOMAIN, LOGGER  # noqa: F401  (re-exported for adapter modules)

CONF_RECOMMENDED = "recommended"
CONF_CHAT_MODEL = "chat_model"
CONF_CODE_EXECUTION = "code_execution"
CONF_MAX_TOKENS = "max_tokens"
CONF_PROMPT_CACHING = "prompt_caching"
CONF_THINKING_BUDGET = "thinking_budget"
CONF_THINKING_EFFORT = "thinking_effort"
CONF_TOOL_SEARCH = "tool_search"
CONF_WEB_FETCH = "web_fetch"
CONF_WEB_FETCH_MAX_USES = "web_fetch_max_uses"
CONF_WEB_SEARCH = "web_search"
CONF_WEB_SEARCH_USER_LOCATION = "user_location"
CONF_WEB_SEARCH_MAX_USES = "web_search_max_uses"
CONF_WEB_SEARCH_CITY = "city"
CONF_WEB_SEARCH_REGION = "region"
CONF_WEB_SEARCH_COUNTRY = "country"
CONF_WEB_SEARCH_TIMEZONE = "timezone"


class PromptCaching(StrEnum):
    """Prompt caching options."""

    OFF = "off"
    PROMPT = "prompt"
    AUTOMATIC = "automatic"


MIN_THINKING_BUDGET = 1024

DEFAULT = {
    CONF_CHAT_MODEL: "claude-haiku-4-5",
    CONF_CODE_EXECUTION: False,
    CONF_MAX_TOKENS: 3000,
    CONF_PROMPT_CACHING: PromptCaching.PROMPT.value,
    CONF_THINKING_BUDGET: MIN_THINKING_BUDGET,
    CONF_THINKING_EFFORT: "low",
    CONF_TOOL_SEARCH: False,
    CONF_WEB_FETCH: False,
    CONF_WEB_FETCH_MAX_USES: 5,
    CONF_WEB_SEARCH: False,
    CONF_WEB_SEARCH_USER_LOCATION: False,
    CONF_WEB_SEARCH_MAX_USES: 5,
}

TOOL_SEARCH_UNSUPPORTED_MODELS = [
    "claude-haiku",
]
