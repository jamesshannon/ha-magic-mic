"""Tests for the identity resolution seam."""

import pytest
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.magic_mic.identity import (
    HOUSEHOLD_STORAGE_KEY,
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    RequestSource,
    ResolvedPrincipal,
    get_resolved_user,
)
from homeassistant.core import Context, HomeAssistant


async def test_unidentified_request_has_household_scope(hass: HomeAssistant) -> None:
    """An unidentified request has household access and no personal owner."""
    principal = await get_resolved_user(
        hass, Context(user_id=None), request_source=RequestSource.VOICE
    )

    assert principal is UNIDENTIFIED_PRINCIPAL
    assert principal.personal_owner_id is None
    assert principal.can_access(DataScope.HOUSEHOLD)
    assert not principal.can_access(DataScope.PERSONAL)
    assert principal.storage_key(DataScope.HOUSEHOLD) == HOUSEHOLD_STORAGE_KEY
    with pytest.raises(PermissionError):
        principal.storage_key(DataScope.PERSONAL)


async def test_authenticated_text_user_has_personal_scope(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """An authenticated text user receives household and personal access."""
    principal = await get_resolved_user(
        hass,
        Context(user_id=hass_admin_user.id),
        request_source=RequestSource.TEXT,
    )

    assert principal == ResolvedPrincipal(user_id=hass_admin_user.id)
    assert principal.personal_owner_id == hass_admin_user.id
    assert principal.can_access(DataScope.HOUSEHOLD)
    assert principal.can_access(DataScope.PERSONAL)
    assert principal.storage_key(DataScope.HOUSEHOLD) == HOUSEHOLD_STORAGE_KEY
    assert principal.storage_key(DataScope.PERSONAL) == hass_admin_user.id


async def test_voice_does_not_trust_pipeline_owner(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """A voice request never treats context.user_id as the current speaker."""
    principal = await get_resolved_user(
        hass,
        Context(user_id=hass_admin_user.id),
        request_source=RequestSource.VOICE,
    )

    assert principal is UNIDENTIFIED_PRINCIPAL


async def test_unknown_source_does_not_trust_context_user(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """A request with no explicit source fails closed."""
    principal = await get_resolved_user(
        hass,
        Context(user_id=hass_admin_user.id),
        request_source=RequestSource.UNKNOWN,
    )

    assert principal is UNIDENTIFIED_PRINCIPAL


@pytest.mark.parametrize(
    ("is_active", "system_generated"),
    [(False, False), (True, True)],
)
async def test_ineligible_text_user_is_unidentified(
    hass: HomeAssistant, is_active: bool, system_generated: bool
) -> None:
    """Inactive and system-generated text users have no personal scope."""
    user = MockUser(is_active=is_active, system_generated=system_generated)
    user.add_to_hass(hass)

    principal = await get_resolved_user(
        hass, Context(user_id=user.id), request_source=RequestSource.TEXT
    )

    assert principal is UNIDENTIFIED_PRINCIPAL


async def test_nonexistent_text_user_is_unidentified(hass: HomeAssistant) -> None:
    """A nonexistent context user has no personal scope."""
    principal = await get_resolved_user(
        hass,
        Context(user_id="does-not-exist"),
        request_source=RequestSource.TEXT,
    )

    assert principal is UNIDENTIFIED_PRINCIPAL


async def test_resolution_is_idempotent(
    hass: HomeAssistant, hass_admin_user: MockUser
) -> None:
    """Resolving the same inputs repeatedly returns the same value."""
    context = Context(user_id=hass_admin_user.id)

    first = await get_resolved_user(hass, context, request_source=RequestSource.TEXT)
    second = await get_resolved_user(hass, context, request_source=RequestSource.TEXT)

    assert first == second
