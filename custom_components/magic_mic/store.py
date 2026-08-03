"""Scoped persistent storage for household and personal capability data.

A thin wrapper over HA's `Store` that derives its namespace from an explicit principal and
scope (§5.1). Household data retains the stable `"default"` key; personal data uses a real
HA user ID and has no unidentified fallback. Empty in Wave 0 (no capability consumes it
yet); it exists so later capabilities (memory, reminders, annotations) never need a
multi-user migration. The backing store is HA's private JSON `Store`, written with owner-only
filesystem permissions. A capability that needs full-text search may use its own backend
(for example, SQLite FTS for the memory notebook) and must make the same privacy decision.
"""

from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .identity import DataScope, ResolvedPrincipal

STORAGE_VERSION = 1


class UserKeyedStore:
    """Persist scoped data as `{scope_key: {...}}`."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        """Create a store backed by `.storage/<domain>.<name>`."""
        self._store = Store[dict[str, dict[str, Any]]](
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{name}",
            private=True,
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load persisted data into memory."""
        self._data = deepcopy(await self._store.async_load() or {})

    def get(self, principal: ResolvedPrincipal, scope: DataScope) -> dict[str, Any]:
        """Return scoped data, or an empty dictionary if none exists."""
        scope_key = self._scope_key(principal, scope)
        return deepcopy(self._data.get(scope_key, {}))

    async def async_set(
        self,
        principal: ResolvedPrincipal,
        scope: DataScope,
        data: dict[str, Any],
    ) -> None:
        """Replace and persist data under the principal's authorized scope."""
        scope_key = self._scope_key(principal, scope)
        self._data[scope_key] = deepcopy(data)
        await self._store.async_save(deepcopy(self._data))

    @staticmethod
    def _scope_key(principal: ResolvedPrincipal, scope: DataScope) -> str:
        """Validate the boundary types and derive the authorized storage key."""
        if not isinstance(principal, ResolvedPrincipal):
            raise TypeError("principal must be a ResolvedPrincipal")
        if not isinstance(scope, DataScope):
            raise TypeError("scope must be a DataScope")
        return principal.storage_key(scope)
