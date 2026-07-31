"""Resolve request identity into explicit data-access scope.

Identity resolution is cheap, deterministic, and provider-neutral. It does not run
speaker identification. An upstream request adapter supplies the request source and,
later, any speaker identity established from audio.

Home Assistant's ``Context.user_id`` is authoritative only for authenticated text
requests. The same field can identify a voice pipeline owner rather than the speaker,
so unknown and voice requests fail closed to the unidentified household principal.
"""

from dataclasses import dataclass
from enum import StrEnum

from homeassistant.core import Context, HomeAssistant

HOUSEHOLD_STORAGE_KEY = "default"


class DataScope(StrEnum):
    """Data scopes understood by Magic Mic capabilities."""

    HOUSEHOLD = "household"
    PERSONAL = "personal"


class RequestSource(StrEnum):
    """Origin of an immediate conversation request."""

    TEXT = "text"
    UNKNOWN = "unknown"
    VOICE = "voice"


@dataclass(frozen=True, slots=True)
class ResolvedPrincipal:
    """An identified person or the unidentified household principal."""

    user_id: str | None

    @property
    def is_identified(self) -> bool:
        """Return whether the request belongs to a real HA user."""
        return self.user_id is not None

    @property
    def personal_owner_id(self) -> str | None:
        """Return the personal-data owner, if the caller is identified."""
        return self.user_id

    def can_access(self, scope: DataScope) -> bool:
        """Return whether this principal may use the requested data scope."""
        return scope is DataScope.HOUSEHOLD or self.is_identified

    def storage_key(self, scope: DataScope) -> str:
        """Return the stable storage key for an accessible scope.

        Household data always uses the existing ``"default"`` bucket. Personal data
        uses the identified HA user and has no unidentified fallback namespace.

        Raises:
            PermissionError: If an unidentified principal requests personal storage.
        """
        if scope is DataScope.HOUSEHOLD:
            return HOUSEHOLD_STORAGE_KEY
        if self.user_id is None:
            raise PermissionError("The unidentified principal has no personal scope")
        return self.user_id


UNIDENTIFIED_PRINCIPAL = ResolvedPrincipal(user_id=None)


async def async_resolve_user(
    hass: HomeAssistant, user_id: str | None
) -> ResolvedPrincipal:
    """Resolve a candidate HA user, falling back to the unidentified principal."""
    if user_id is None:
        return UNIDENTIFIED_PRINCIPAL

    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_active or user.system_generated:
        return UNIDENTIFIED_PRINCIPAL
    return ResolvedPrincipal(user_id=user.id)


async def get_resolved_user(
    hass: HomeAssistant,
    context: Context,
    *,
    request_source: RequestSource,
) -> ResolvedPrincipal:
    """Return the principal and scope available to an immediate request.

    Only an explicitly identified text source may derive a person from
    ``context.user_id``. Voice and unknown sources must receive a separately
    established speaker identity in a later phase.
    """
    if request_source is not RequestSource.TEXT:
        return UNIDENTIFIED_PRINCIPAL
    return await async_resolve_user(hass, context.user_id)


__all__ = [
    "HOUSEHOLD_STORAGE_KEY",
    "UNIDENTIFIED_PRINCIPAL",
    "DataScope",
    "RequestSource",
    "ResolvedPrincipal",
    "async_resolve_user",
    "get_resolved_user",
]
