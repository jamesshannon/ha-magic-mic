"""Localized, model-facing strings owned by Magic Mic capabilities."""

from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations

from ..const import DOMAIN

_PREFIX = f"component.{DOMAIN}.conversation."


@dataclass(frozen=True, slots=True)
class ConversationStrings:
    """Request-language strings used in Magic Mic prompts and tool schemas."""

    area_usage_instruction: str
    entity_summary_header: str
    find_entities_description: str
    find_entities_error_invalid_area: str
    find_entities_error_invalid_floor: str
    find_entities_error_no_assistant: str
    find_entities_field_area: str
    find_entities_field_device_class: str
    find_entities_field_domain: str
    find_entities_field_floor: str
    find_entities_field_limit: str
    find_entities_field_name: str
    find_entities_field_state: str
    match_fallback_ambiguous: str
    match_fallback_not_found: str
    name_injection_header: str


async def async_get_conversation_strings(
    hass: HomeAssistant, language: str | None
) -> ConversationStrings:
    """Load Magic Mic capability strings with HA's English fallback."""
    translations = await async_get_translations(
        hass,
        language or hass.config.language,
        "conversation",
        integrations={DOMAIN},
    )

    def translated(key: str) -> str:
        """Return one required translation from the flattened HA resource."""
        return translations[f"{_PREFIX}{key}"]

    return ConversationStrings(
        area_usage_instruction=translated("area_usage.instruction"),
        entity_summary_header=translated("entity_summary.header"),
        find_entities_description=translated("find_entities.description"),
        find_entities_error_invalid_area=translated(
            "find_entities.errors.invalid_area"
        ),
        find_entities_error_invalid_floor=translated(
            "find_entities.errors.invalid_floor"
        ),
        find_entities_error_no_assistant=translated(
            "find_entities.errors.no_assistant"
        ),
        find_entities_field_area=translated("find_entities.fields.area"),
        find_entities_field_device_class=translated(
            "find_entities.fields.device_class"
        ),
        find_entities_field_domain=translated("find_entities.fields.domain"),
        find_entities_field_floor=translated("find_entities.fields.floor"),
        find_entities_field_limit=translated("find_entities.fields.limit"),
        find_entities_field_name=translated("find_entities.fields.name"),
        find_entities_field_state=translated("find_entities.fields.state"),
        match_fallback_ambiguous=translated("match_fallback.ambiguous"),
        match_fallback_not_found=translated("match_fallback.not_found"),
        name_injection_header=translated("name_injection.header"),
    )


__all__ = ["ConversationStrings", "async_get_conversation_strings"]
