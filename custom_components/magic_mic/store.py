"""User-keyed persistent storage: the keying convention for per-user data.

A thin wrapper over HA's `Store` that namespaces all data by resolved `user_id`
(§5.1). Empty in Wave 0 (no capability consumes it yet); it exists so later
capabilities (memory, reminders, annotations) never need a multi-user migration.
The backing store is HA's JSON `Store`; a capability that needs full-text search may
use its own backend (e.g. SQLite FTS for the memory notebook).
"""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


class UserKeyedStore:
    """Persist per-user data as `{user_id: {...}}`, namespaced by user_id."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        """Create a store backed by `.storage/<domain>.<name>`."""
        self._store = Store[dict[str, dict[str, Any]]](
            hass, STORAGE_VERSION, f"{DOMAIN}.{name}"
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load persisted data into memory."""
        self._data = await self._store.async_load() or {}

    def get(self, user_id: str) -> dict[str, Any]:
        """Return a user's data (empty dict if none)."""
        return self._data.get(user_id, {})

    async def async_set(self, user_id: str, data: dict[str, Any]) -> None:
        """Replace and persist a user's data."""
        self._data[user_id] = data
        await self._store.async_save(self._data)
