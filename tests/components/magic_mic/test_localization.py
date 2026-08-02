"""Tests for Magic Mic-owned model-facing translations."""

from custom_components.magic_mic.capabilities.entities import FindEntitiesTool
from custom_components.magic_mic.capabilities.localization import (
    async_get_conversation_strings,
)
from custom_components.magic_mic.capabilities.prompt_context import (
    async_build_entity_summary,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, llm
from homeassistant.setup import async_setup_component


async def test_spanish_prompt_and_tool_schema(hass: HomeAssistant) -> None:
    """A non-English request builds translated Magic Mic prompts and tool metadata."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})
    kitchen = ar.async_get(hass).async_create("Cocina").id
    entry = er.async_get(hass).async_get_or_create(
        "light",
        "test",
        "ceiling",
        original_name="Luz del techo",
    )
    er.async_get(hass).async_update_entity(entry.entity_id, area_id=kitchen)
    hass.states.async_set(
        entry.entity_id,
        "on",
        {ATTR_FRIENDLY_NAME: "Luz del techo"},
    )
    async_expose_entity(hass, conversation.DOMAIN, entry.entity_id, True)

    strings = await async_get_conversation_strings(hass, "es")
    summary = async_build_entity_summary(hass, conversation.DOMAIN, strings)
    tool = FindEntitiesTool(strings)
    field_descriptions = {
        marker.schema: marker.description for marker in tool.parameters.schema
    }
    error = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="find_entities", tool_args={}),
        llm.LLMContext(
            platform="magic_mic",
            context=None,
            language="es",
            assistant=None,
            device_id=None,
        ),
    )

    assert summary.startswith("La estructura de la casa")
    assert strings.name_injection_header.startswith("Entidades relevantes")
    assert tool.description.startswith("Busca entidades de Home Assistant")
    assert field_descriptions["area"].startswith("Limitar a un área")
    assert error == {
        "success": False,
        "error": "assistant_not_configured",
        "error_text": "No hay ningún asistente configurado.",
    }


async def test_missing_locale_falls_back_to_english(hass: HomeAssistant) -> None:
    """HA's translation loader supplies English when a locale file is absent."""
    strings = await async_get_conversation_strings(hass, "eo")

    assert strings.entity_summary_header.startswith("Home structure below")
