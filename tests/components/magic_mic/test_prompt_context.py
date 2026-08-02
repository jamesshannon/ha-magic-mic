"""Tests for the bounded entity summary (prompt-context Tier 1)."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.magic_mic.capabilities.prompt_context import (
    ENTITY_SUMMARY_HEADER,
    UNASSIGNED_LABEL,
    async_build_entity_summary,
)
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.setup import async_setup_component

ASSISTANT = conversation.DOMAIN


@pytest.fixture(autouse=True)
async def setup_core(hass: HomeAssistant) -> None:
    """Set up the core components the exposed-entity store needs."""
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {conversation.DOMAIN: {}})


def _register(
    hass: HomeAssistant,
    entity_id: str,
    *,
    area_id: str | None = None,
    device_id: str | None = None,
    device_class: str | None = None,
    expose: bool = True,
) -> None:
    """Register an entity, place it, state it, and expose it."""
    domain, _, object_id = entity_id.partition(".")
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get_or_create(
        domain, "test", entity_id, suggested_object_id=object_id, device_id=device_id
    )
    if area_id is not None:
        ent_reg.async_update_entity(entry.entity_id, area_id=area_id)
    attributes: dict[str, object] = {ATTR_FRIENDLY_NAME: entity_id}
    if device_class is not None:
        attributes[ATTR_DEVICE_CLASS] = device_class
    hass.states.async_set(entry.entity_id, "on", attributes)
    async_expose_entity(hass, ASSISTANT, entry.entity_id, expose)


async def test_empty_when_nothing_exposed(hass: HomeAssistant) -> None:
    """No exposed entities yields an empty summary (caller falls back)."""
    assert async_build_entity_summary(hass, ASSISTANT) == ""


async def test_groups_by_floor_area_domain_with_counts(hass: HomeAssistant) -> None:
    """Areas group under floors; domains carry counts; device classes break out."""
    floor_reg = fr.async_get(hass)
    area_reg = ar.async_get(hass)
    ground = floor_reg.async_create("Ground Floor")
    upstairs = floor_reg.async_create("Upstairs")
    living = area_reg.async_create("Living Room", floor_id=ground.floor_id)
    kitchen = area_reg.async_create("Kitchen", floor_id=ground.floor_id)
    bedroom = area_reg.async_create("Bedroom", floor_id=upstairs.floor_id)

    _register(hass, "light.lr_1", area_id=living.id)
    _register(hass, "light.lr_2", area_id=living.id)
    _register(hass, "cover.lr_blind", area_id=living.id, device_class="blind")
    _register(hass, "cover.lr_shade", area_id=living.id, device_class="shade")
    _register(hass, "media_player.lr_tv", area_id=living.id)
    _register(hass, "light.kitchen_1", area_id=kitchen.id)
    _register(hass, "switch.kitchen_kettle", area_id=kitchen.id)
    _register(hass, "light.bedroom_1", area_id=bedroom.id)

    summary = async_build_entity_summary(hass, ASSISTANT)

    assert summary.startswith(ENTITY_SUMMARY_HEADER)
    lines = summary.splitlines()[1:]
    # Ground Floor areas sort before Upstairs; areas alphabetical within a floor.
    assert lines == [
        "Ground Floor / Kitchen: light x1, switch x1",
        (
            "Ground Floor / Living Room: cover x2 (blind x1, shade x1), "
            "light x2, media_player x1"
        ),
        "Upstairs / Bedroom: light x1",
    ]


async def test_floorless_area_and_unassigned_bucket(hass: HomeAssistant) -> None:
    """Areas without a floor render bare; area-less entities land in Unassigned."""
    area_reg = ar.async_get(hass)
    garden = area_reg.async_create("Garden")

    _register(hass, "sensor.garden_temp", area_id=garden.id, device_class="temperature")
    _register(hass, "sensor.roaming_1")
    _register(hass, "sensor.roaming_2")

    lines = async_build_entity_summary(hass, ASSISTANT).splitlines()[1:]

    # Floorless areas sort after floored ones; Unassigned is always last.
    assert lines == [
        "Garden: sensor x1 (temperature x1)",
        f"{UNASSIGNED_LABEL}: sensor x2",
    ]


async def test_device_area_fallback_and_exposure_filter(hass: HomeAssistant) -> None:
    """Entity area falls back to its device; unexposed entities are excluded."""
    area_reg = ar.async_get(hass)
    dev_reg = dr.async_get(hass)
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    office = area_reg.async_create("Office")
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "dev1")},
    )
    dev_reg.async_update_device(device.id, area_id=office.id)

    # No entity area_id: it should inherit the device's Office area.
    _register(hass, "light.office_lamp", device_id=device.id)
    _register(hass, "light.hidden", area_id=office.id, expose=False)

    lines = async_build_entity_summary(hass, ASSISTANT).splitlines()[1:]

    assert lines == ["Office: light x1"]
