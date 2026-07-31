"""Scoped persistent storage for household and personal capability data.

A thin wrapper over HA's `Store` that namespaces data by a key obtained from
`ResolvedPrincipal.storage_key()` (§5.1). Household data retains the stable `"default"`
key; personal data uses a real HA user ID and has no unidentified fallback. Empty in
Wave 0 (no capability consumes it yet); it exists so later capabilities (memory,
reminders, annotations) never need a multi-user migration. The backing store is HA's
JSON `Store`; a capability that needs full-text search may use its own backend (e.g.
SQLite FTS for the memory notebook).
"""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


class UserKeyedStore:
    """Persist scoped data as `{scope_key: {...}}`."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        """Create a store backed by `.storage/<domain>.<name>`."""
        self._store = Store[dict[str, dict[str, Any]]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{name}"
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load persisted data into memory."""
        self._data = await self._store.async_load() or {}

    def get(self, scope_key: str) -> dict[str, Any]:
        """Return scoped data, or an empty dictionary if none exists."""
        return self._data.get(scope_key, {})

    async def async_set(self, scope_key: str, data: dict[str, Any]) -> None:
        """Replace and persist data under an authorized scope key."""
        self._data[scope_key] = data
        await self._store.async_save(self._data)
