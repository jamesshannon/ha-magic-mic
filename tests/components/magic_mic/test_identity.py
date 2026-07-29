"""Tests for the identity resolution seam."""

from pytest_homeassistant_custom_component.common import MockUser

from custom_components.magic_mic.identity import DEFAULT_USER_ID, get_resolved_user
from homeassistant.core import Context, HomeAssistant


async def test_returns_default_without_user(hass: HomeAssistant) -> None:
    """No user on the context falls back to the household bucket, never None."""
    assert await get_resolved_user(hass, Context(user_id=None)) == DEFAULT_USER_ID


async def test_returns_real_user(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """A context whose user maps to a real, active user resolves to that user."""
    context = Context(user_id=hass_admin_user.id)
    assert await get_resolved_user(hass, context) == hass_admin_user.id


async def test_ignores_system_generated_user(hass: HomeAssistant) -> None:
    """A system-generated user is not a real Person; falls back to default."""
    user = MockUser(system_generated=True)
    user.add_to_hass(hass)
    assert await get_resolved_user(hass, Context(user_id=user.id)) == DEFAULT_USER_ID


async def test_unknown_user_id(hass: HomeAssistant) -> None:
    """A user_id that maps to no user falls back to default."""
    assert (
        await get_resolved_user(hass, Context(user_id="does-not-exist"))
        == DEFAULT_USER_ID
    )
