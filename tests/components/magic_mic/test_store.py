"""Tests for the user-keyed store."""

from typing import Any

from custom_components.magic_mic.store import UserKeyedStore
from homeassistant.core import HomeAssistant


async def test_per_user_isolation_and_persistence(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Data is namespaced per user, isolated, and persists across reloads."""
    store = UserKeyedStore(hass, "test")
    await store.async_load()
    assert store.get("alice") == {}

    await store.async_set("alice", {"wifi": "hunter2"})
    assert store.get("alice") == {"wifi": "hunter2"}
    # Another user's scope is untouched.
    assert store.get("bob") == {}

    # A fresh instance loads the persisted data.
    reloaded = UserKeyedStore(hass, "test")
    await reloaded.async_load()
    assert reloaded.get("alice") == {"wifi": "hunter2"}
    assert reloaded.get("bob") == {}
