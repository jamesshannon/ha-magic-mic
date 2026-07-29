"""Back the fixture world with real, executable entities for the live baseline.

The keyless routing measurement only needs entities to *recognize* (HASSIL matching a
template), so `world.build_world` sets bare states. The live LLM baseline needs them to
*execute*: when the model calls ``HassTurnOn`` the service must exist, and capability
tools (set brightness, set volume, add a list item) are only exposed when a real entity
advertises the feature. Registering each corpus entity through its real domain platform
gets both at once, since ``async_setup_component(hass, <domain>, ...)`` registers the
domain's services *and* its Assist intents.

This mirrors HA core's own test pattern (`setup_test_component_platform` plus
`async_setup_component(domain, {domain: {"platform": "test"}})`). Domains without an
executable surface in the corpus (``weather``) stay state-only; ``timer`` is left out on
purpose (``HassStartTimer`` needs a satellite/device context a headless run lacks).
"""

from collections import defaultdict

from pytest_homeassistant_custom_component.common import setup_test_component_platform

from homeassistant.components import conversation
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.setup import async_setup_component

from .corpus import Entity as CorpusEntity, World


class _BackedEntity(Entity):
    """Shared setup for a corpus-backed entity: fixed id, name, and no polling."""

    _attr_should_poll = False

    def __init__(self, entity: CorpusEntity) -> None:
        """Pin the entity id and name to the corpus fixture."""
        self.entity_id = entity.entity_id
        self._attr_unique_id = entity.entity_id
        self._attr_name = entity.name


class _Light(_BackedEntity, LightEntity):
    """A brightness-capable light, so ``HassLightSet`` is exposed and executes."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_is_on = entity.state == "on"
        self._attr_brightness = entity.attributes.get("brightness")

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        if ATTR_BRIGHTNESS in kwargs:
            self._attr_brightness = kwargs[ATTR_BRIGHTNESS]
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class _Switch(_BackedEntity, SwitchEntity):
    """A plain on/off switch for the generic toggle intents."""

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_is_on = entity.state == "on"

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class _Fan(_BackedEntity, FanEntity):
    """A minimal on/off fan."""

    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_is_on = entity.state == "on"

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class _Cover(_BackedEntity, CoverEntity):
    """A positionable cover, so open/close and ``HassSetPosition`` execute."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        if entity.device_class:
            self._attr_device_class = CoverDeviceClass(entity.device_class)
        self._attr_current_cover_position = 0 if entity.state == "closed" else 100

    @property
    def is_closed(self) -> bool:
        return self._attr_current_cover_position == 0

    async def async_open_cover(self, **kwargs: object) -> None:
        self._attr_current_cover_position = 100
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: object) -> None:
        self._attr_current_cover_position = 0
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: object) -> None:
        self._attr_current_cover_position = int(kwargs[ATTR_POSITION])
        self.async_write_ha_state()


class _Climate(_BackedEntity, ClimateEntity):
    """A thermostat with a settable target temperature."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_hvac_mode = HVACMode(entity.state) if entity.state else HVACMode.OFF
        self._attr_current_temperature = entity.attributes.get("current_temperature")
        self._attr_target_temperature = entity.attributes.get("temperature")

    async def async_set_temperature(self, **kwargs: object) -> None:
        if ATTR_TEMPERATURE in kwargs:
            self._attr_target_temperature = float(kwargs[ATTR_TEMPERATURE])
        self.async_write_ha_state()


class _MediaPlayer(_BackedEntity, MediaPlayerEntity):
    """A media player that can pause and set volume."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_state = MediaPlayerState(entity.state) if entity.state else None
        self._attr_volume_level = entity.attributes.get("volume_level", 0.5)

    async def async_media_pause(self) -> None:
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_play(self) -> None:
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_set_volume_level(self, volume: float) -> None:
        self._attr_volume_level = volume
        self.async_write_ha_state()


class _TodoList(_BackedEntity, TodoListEntity):
    """A todo/shopping list that accepts new items."""

    _attr_supported_features = TodoListEntityFeature.CREATE_TODO_ITEM

    def __init__(self, entity: CorpusEntity) -> None:
        super().__init__(entity)
        self._attr_todo_items = []

    async def async_create_todo_item(self, item: TodoItem) -> None:
        item.status = item.status or TodoItemStatus.NEEDS_ACTION
        self._attr_todo_items.append(item)
        self.async_write_ha_state()


# Domains with an executable backing. Everything else (weather) stays state-only.
_ENTITY_TYPES: dict[str, type[_BackedEntity]] = {
    "climate": _Climate,
    "cover": _Cover,
    "fan": _Fan,
    "light": _Light,
    "media_player": _MediaPlayer,
    "switch": _Switch,
    "todo": _TodoList,
}


async def build_executable_world(hass: HomeAssistant, world: World) -> dict[str, str]:
    """Register the fixture entities on real platforms and expose them.

    Returns a map from the corpus entity id to the registered entity id. Domains in
    ``_ENTITY_TYPES`` come up as executable entities; any other entity falls back to a
    bare state so queries still have something to read.
    """
    area_reg = ar.async_get(hass)
    ent_reg = er.async_get(hass)

    # Corpus area keys are ids like "living_room"; areas match on their spoken name.
    area_ids = {
        key: area_reg.async_get_or_create(key.replace("_", " ")).id
        for key in world.areas
    }

    by_domain: dict[str, list[_BackedEntity]] = defaultdict(list)
    corpus_by_id: dict[str, CorpusEntity] = {}
    resolved: dict[str, str] = {}
    for entity in world.entities:
        domain = entity.entity_id.partition(".")[0]
        corpus_by_id[entity.entity_id] = entity
        entity_type = _ENTITY_TYPES.get(domain)
        if entity_type is None:
            # No executable surface needed (e.g. weather): a bare state suffices.
            hass.states.async_set(entity.entity_id, entity.state or "on")
            resolved[entity.entity_id] = entity.entity_id
            continue
        by_domain[domain].append(entity_type(entity))

    for domain, entities in by_domain.items():
        setup_test_component_platform(hass, domain, entities)
        assert await async_setup_component(hass, domain, {domain: {"platform": "test"}})
    await hass.async_block_till_done()

    # Assign areas and expose every registered entity to the conversation agent.
    for entity in world.entities:
        if entity.entity_id not in ent_reg.entities:
            resolved.setdefault(entity.entity_id, entity.entity_id)
            continue
        if entity.area:
            ent_reg.async_update_entity(
                entity.entity_id,
                area_id=area_ids.get(entity.area)
                or area_reg.async_get_or_create(entity.area.replace("_", " ")).id,
            )
        async_expose_entity(hass, conversation.DOMAIN, entity.entity_id, True)
        resolved[entity.entity_id] = entity.entity_id

    await hass.async_block_till_done()
    return resolved
