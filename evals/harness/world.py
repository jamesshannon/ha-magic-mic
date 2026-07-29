"""Build the fixture home a corpus runs against, inside a test HA instance.

Keyless: sets up only core's ``homeassistant`` and ``conversation`` components (the
local HASSIL agent), then creates, areas, states, and exposes the corpus's entities so
slot-bearing utterances have something to resolve against.
"""

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component

from .corpus import World


async def async_setup_local_agent(hass: HomeAssistant) -> None:
    """Set up the core components the local (HASSIL) agent needs."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


async def build_world(hass: HomeAssistant, world: World) -> dict[str, str]:
    """Create, place, state, and expose the fixture entities.

    Returns a map from the corpus entity id to the entity id actually registered
    (they match unless the registry had to disambiguate).
    """
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)

    # Corpus area keys are ids like "living_room"; areas are matched on their spoken
    # name, so create them with the spaced display name and key them back to the id.
    area_ids = {
        key: area_reg.async_get_or_create(key.replace("_", " ")).id
        for key in world.areas
    }

    resolved: dict[str, str] = {}
    for entity in world.entities:
        domain, _, object_id = entity.entity_id.partition(".")
        entry = ent_reg.async_get_or_create(
            domain, "eval", entity.entity_id, suggested_object_id=object_id
        )
        # A registry entry with empty aliases yields no matchable name (the friendly-
        # name fallback only fires when there is no entry at all), so register the
        # name as an alias. Area assignment needs the entry, so both go here.
        updates: dict[str, object] = {"aliases": {entity.name}}
        if entity.area:
            updates["area_id"] = (
                area_ids.get(entity.area)
                or area_reg.async_get_or_create(entity.area.replace("_", " ")).id
            )
        ent_reg.async_update_entity(entry.entity_id, **updates)

        attributes = {ATTR_FRIENDLY_NAME: entity.name, **entity.attributes}
        if entity.device_class:
            attributes[ATTR_DEVICE_CLASS] = entity.device_class
        hass.states.async_set(entry.entity_id, entity.state or "on", attributes)
        async_expose_entity(hass, conversation.DOMAIN, entry.entity_id, True)
        resolved[entity.entity_id] = entry.entity_id

    await hass.async_block_till_done()
    return resolved
