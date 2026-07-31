"""Resolve and expose request identity as explicit data-access scope.

Identity resolution is cheap, deterministic, and provider-neutral. It does not run
speaker identification. An upstream request adapter supplies the request source and,
later, any speaker identity established from audio. Resolution happens once at the
request boundary; tools and intents use ``get_resolved_user()`` as a source-agnostic
accessor for the established result.

Home Assistant's ``Context.user_id`` is authoritative only for authenticated text
requests. The same field can identify a voice pipeline owner rather than the speaker,
so unknown and voice requests fail closed to the unidentified household principal.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from homeassistant.core import Context, HomeAssistant

DATA_RESOLVED_PRINCIPALS = "magic_mic.resolved_principals"
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
    hass: HomeAssistant,
    context: Context,
    *,
    request_source: RequestSource,
) -> ResolvedPrincipal:
    """Resolve and establish the principal for one immediate request.

    Only an explicitly identified text source may derive a person from
    ``context.user_id``. Voice and unknown sources must receive a separately
    established speaker identity in a later phase.
    """
    principal = UNIDENTIFIED_PRINCIPAL
    if request_source is RequestSource.TEXT and context.user_id is not None:
        user = await hass.auth.async_get_user(context.user_id)
        if user is not None and user.is_active and not user.system_generated:
            principal = ResolvedPrincipal(user_id=user.id)

    _resolved_principals(hass)[context.id] = principal
    return principal


def get_resolved_user(hass: HomeAssistant, context: Context) -> ResolvedPrincipal:
    """Return the principal already established for this request.

    This is the uniform accessor for tools, intents, and capabilities. It does no
    resolution and requires no knowledge of the request source. A missing or cleared
    resolution fails closed to the unidentified household principal.
    """
    principals = hass.data.get(DATA_RESOLVED_PRINCIPALS)
    if principals is None:
        return UNIDENTIFIED_PRINCIPAL
    return cast(dict[str, ResolvedPrincipal], principals).get(
        context.id, UNIDENTIFIED_PRINCIPAL
    )


def clear_resolved_user(hass: HomeAssistant, context: Context) -> None:
    """Clear the established principal at the end of a request."""
    if principals := hass.data.get(DATA_RESOLVED_PRINCIPALS):
        cast(dict[str, ResolvedPrincipal], principals).pop(context.id, None)


def _resolved_principals(hass: HomeAssistant) -> dict[str, ResolvedPrincipal]:
    """Return the turn-scoped principal registry."""
    return hass.data.setdefault(DATA_RESOLVED_PRINCIPALS, {})


__all__ = [
    "HOUSEHOLD_STORAGE_KEY",
    "UNIDENTIFIED_PRINCIPAL",
    "DataScope",
    "RequestSource",
    "ResolvedPrincipal",
    "async_resolve_user",
    "clear_resolved_user",
    "get_resolved_user",
]
