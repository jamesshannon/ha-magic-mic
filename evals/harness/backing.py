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
executable surface in the corpus (``weather``) stay state-only.

Timers are device-scoped rather than entity-scoped: HA strips the timer intents from the
roster unless the turn carries a timer-capable ``device_id`` (`helpers/llm.py`). A voice
satellite registers itself as that device; headless, ``register_satellite`` stands one up
(so ``HassStartTimer`` is exposed and runs) and, being registry-backed, lets it hold a room
for location context (see :class:`Satellite`).
"""

from collections import defaultdict
from dataclasses import dataclass, field

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

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
from homeassistant.components.intent import async_register_timer_handler
from homeassistant.components.intent.timers import TimerEventType, TimerInfo
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
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.entity import Entity
from homeassistant.setup import async_setup_component

from .corpus import Entity as CorpusEntity, World


class _BackedEntity(Entity):
    """Shared setup for a corpus-backed entity: fixed id, name, and no polling.

    Mutable state is assigned in ``reset()``, not ``__init__``, so a case can be returned to
    the fixture baseline between runs. ``__init__`` pins the identity and calls ``reset()``
    once; the runner calls ``reset()`` again before each case for a clean, order-independent
    starting world (the pytest-fixture model, not carry-over state).
    """

    _attr_should_poll = False

    def __init__(self, entity: CorpusEntity) -> None:
        """Pin the entity id and name to the corpus fixture, then set baseline state."""
        self._corpus = entity
        self.entity_id = entity.entity_id
        self._attr_unique_id = entity.entity_id
        self._attr_name = entity.name
        self.reset()

    def reset(self) -> None:
        """Restore mutable state to the corpus baseline (overridden per domain)."""


class _Light(_BackedEntity, LightEntity):
    """A brightness-capable light, so ``HassLightSet`` is exposed and executes."""

    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def reset(self) -> None:
        self._attr_is_on = self._corpus.state == "on"
        self._attr_brightness = self._corpus.attributes.get("brightness")

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

    def reset(self) -> None:
        self._attr_is_on = self._corpus.state == "on"

    async def async_turn_on(self, **kwargs: object) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class _Fan(_BackedEntity, FanEntity):
    """A minimal on/off fan."""

    _attr_supported_features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF

    def reset(self) -> None:
        self._attr_is_on = self._corpus.state == "on"

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

    def reset(self) -> None:
        if self._corpus.device_class:
            self._attr_device_class = CoverDeviceClass(self._corpus.device_class)
        self._attr_current_cover_position = 0 if self._corpus.state == "closed" else 100

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

    def reset(self) -> None:
        corpus = self._corpus
        self._attr_hvac_mode = HVACMode(corpus.state) if corpus.state else HVACMode.OFF
        self._attr_current_temperature = corpus.attributes.get("current_temperature")
        self._attr_target_temperature = corpus.attributes.get("temperature")

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

    def reset(self) -> None:
        corpus = self._corpus
        self._attr_state = MediaPlayerState(corpus.state) if corpus.state else None
        self._attr_volume_level = corpus.attributes.get("volume_level", 0.5)

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

    def reset(self) -> None:
        self._attr_todo_items = []

    async def async_create_todo_item(self, item: TodoItem) -> None:
        item.status = item.status or TodoItemStatus.NEEDS_ACTION
        self._attr_todo_items.append(item)
        self.async_write_ha_state()


# A stable identifier for the synthetic voice satellite that issues turns. Used as the
# device's registry identifier, not as its device_id (that is a generated registry id).
EVAL_DEVICE_ID = "magic_mic_eval_satellite"
_SATELLITE_NAME = "Voice Satellite"


@dataclass
class Satellite:
    """The voice satellite a turn is issued from: a real device, optionally in a room.

    A turn carries its ``device_id`` so both paths can resolve "here": HASSIL injects the
    device's area as ``preferred_area_id`` before matching, and the LLM's ``IntentTool`` fills
    the same slot at execution from ``llm_context.device_id`` (`helpers/llm.py`). ``move_to``
    relocates it, so a bare "turn on the lights" can be watched resolving to different rooms.
    """

    device_id: str

    def area_id(self, hass: HomeAssistant) -> str | None:
        """Return the id of the area the satellite currently sits in, if any."""
        device = dr.async_get(hass).async_get(self.device_id)
        return device.area_id if device else None

    def area_name(self, hass: HomeAssistant) -> str | None:
        """Return the spoken name of the satellite's area, or None when placed nowhere."""
        if not (area_id := self.area_id(hass)):
            return None
        area = ar.async_get(hass).async_get_area(area_id)
        return area.name if area else None

    def move_to(self, hass: HomeAssistant, area_id: str | None) -> None:
        """Place the satellite in ``area_id`` (``None`` clears its location).

        Also clears HASSIL's recognition cache: it keys on ``(text, language, satellite_id)``
        and not the device area (`default_agent.py`), so without this a repeated utterance
        would resolve against the room the satellite just left.
        """
        dr.async_get(hass).async_update_device(self.device_id, area_id=area_id)
        agent = conversation.async_get_agent(hass)
        if (cache := getattr(agent, "_intent_cache", None)) is not None:
            cache.clear()


def _assert_world_healthy(hass: HomeAssistant, entity_ids: list[str]) -> None:
    """Fail loudly if a fixture entity is missing or in a bad state.

    A tripwire adapted from home-assistant-datasets' ``get_state`` (which asserts no
    snapshotted entity is unavailable/unknown) and its ``validate_entities`` fixture (which
    asserts every inventory entity was created). Our sequential single-``hass`` runner has
    neither the per-test fixture that would rebuild a broken world nor a hard failure when a
    platform silently drops an entity, so a mis-built or mis-reset world would otherwise
    score as if it were fine. This turns that into an error at build and reset time.
    """
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        assert state is not None, f"Fixture entity was not created: {entity_id}"
        assert state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN), (
            f"Fixture entity {entity_id} is {state.state}; the world is not healthy"
        )


def register_satellite(hass: HomeAssistant, *, area_id: str | None = None) -> Satellite:
    """Create the voice-satellite device (with its timer handler) and return a handle.

    The device is registry-backed so it can hold an area for location context; it hangs off
    its own throwaway config entry rather than magic_mic's, since a satellite is its own
    integration's device. It doubles as the timer device: timer intents are device-scoped and
    HA strips them from the roster unless the turn carries a timer-capable ``device_id``
    (`helpers/llm.py`), which the registered handler satisfies. Area-less by default, matching
    a turn from an unknown location; pass ``area_id`` (or call ``move_to``) to place it.
    """
    entry = MockConfigEntry(domain=_SATELLITE_NAME.lower().replace(" ", "_"))
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(EVAL_DEVICE_ID, EVAL_DEVICE_ID)},
        name=_SATELLITE_NAME,
    )
    satellite = Satellite(device_id=device.id)
    if area_id:
        satellite.move_to(hass, area_id)

    @callback
    def _handle(event_type: TimerEventType, timer: TimerInfo) -> None:
        """Record nothing; the timer running is all the eval needs."""

    async_register_timer_handler(hass, device.id, _handle)
    return satellite


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


@dataclass
class ExecutableWorld:
    """A built fixture world, with a handle to reset it between cases.

    ``resolved`` maps each corpus entity id to the id it registered under. ``reset`` returns
    every entity to its fixture baseline so each case runs against a clean, order-independent
    world (a prior case's actuation does not carry over).
    """

    resolved: dict[str, str]
    _entities: list[_BackedEntity] = field(default_factory=list)
    _bare: dict[str, str] = field(default_factory=dict)

    async def reset(self, hass: HomeAssistant) -> None:
        """Restore every fixture entity to its baseline state."""
        for entity in self._entities:
            entity.reset()
            entity.async_write_ha_state()
        for entity_id, state in self._bare.items():
            hass.states.async_set(entity_id, state)
        await hass.async_block_till_done()
        _assert_world_healthy(
            hass,
            [entity.entity_id for entity in self._entities] + list(self._bare),
        )


async def build_executable_world(hass: HomeAssistant, world: World) -> ExecutableWorld:
    """Register the fixture entities on real platforms and expose them.

    Returns an :class:`ExecutableWorld` handle (id map plus a ``reset``). Domains in
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
    instances: list[_BackedEntity] = []
    bare: dict[str, str] = {}
    resolved: dict[str, str] = {}
    for entity in world.entities:
        domain = entity.entity_id.partition(".")[0]
        entity_type = _ENTITY_TYPES.get(domain)
        if entity_type is None:
            # No executable surface needed (e.g. weather): a bare state suffices.
            bare[entity.entity_id] = entity.state or "on"
            hass.states.async_set(entity.entity_id, bare[entity.entity_id])
            resolved[entity.entity_id] = entity.entity_id
            continue
        instance = entity_type(entity)
        by_domain[domain].append(instance)
        instances.append(instance)

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
    _assert_world_healthy(hass, [entity.entity_id for entity in instances] + list(bare))
    return ExecutableWorld(resolved=resolved, _entities=instances, _bare=bare)
