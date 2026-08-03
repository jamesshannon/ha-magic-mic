"""Tests for the user-keyed store."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.magic_mic.identity import (
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    ResolvedPrincipal,
)
from custom_components.magic_mic.store import UserKeyedStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

# The standard fixture mocks disk writes; retain HA's writer for the mode test.
_REAL_ASYNC_WRITE_DATA = Store._async_write_data  # noqa: SLF001


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Give filesystem storage tests an isolated HA configuration directory."""
    return str(tmp_path)


async def test_per_user_isolation_and_persistence(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Data is namespaced per user, isolated, and persists across reloads."""
    alice = ResolvedPrincipal(user_id="alice")
    bob = ResolvedPrincipal(user_id="bob")
    store = UserKeyedStore(hass, "test")
    await store.async_load()
    assert store.get(alice, DataScope.PERSONAL) == {}

    await store.async_set(alice, DataScope.PERSONAL, {"wifi": "hunter2"})
    assert store.get(alice, DataScope.PERSONAL) == {"wifi": "hunter2"}
    # Another user's scope is untouched.
    assert store.get(bob, DataScope.PERSONAL) == {}

    # A fresh instance loads the persisted data.
    reloaded = UserKeyedStore(hass, "test")
    await reloaded.async_load()
    assert reloaded.get(alice, DataScope.PERSONAL) == {"wifi": "hunter2"}
    assert reloaded.get(bob, DataScope.PERSONAL) == {}


async def test_household_scope_is_shared_by_every_principal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Identified and unidentified callers share the one household bucket."""
    alice = ResolvedPrincipal(user_id="alice")
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    await store.async_set(alice, DataScope.HOUSEHOLD, {"door_code": "1234"})

    assert store.get(UNIDENTIFIED_PRINCIPAL, DataScope.HOUSEHOLD) == {
        "door_code": "1234"
    }


async def test_store_file_is_private(hass: HomeAssistant) -> None:
    """Sensitive scoped data is written with owner-only filesystem access."""
    store = UserKeyedStore(hass, "private_test")
    await store.async_load()

    with patch.object(Store, "_async_write_data", _REAL_ASYNC_WRITE_DATA):
        await store.async_set(
            UNIDENTIFIED_PRINCIPAL,
            DataScope.HOUSEHOLD,
            {"door_code": "1234"},
        )

    path = Path(hass.config.path(".storage", "magic_mic.private_test"))
    assert path.stat().st_mode & 0o777 == 0o600


async def test_unidentified_principal_cannot_access_personal_scope(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The storage boundary rejects unidentified personal reads and writes."""
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    with pytest.raises(PermissionError):
        store.get(UNIDENTIFIED_PRINCIPAL, DataScope.PERSONAL)
    with pytest.raises(PermissionError):
        await store.async_set(
            UNIDENTIFIED_PRINCIPAL,
            DataScope.PERSONAL,
            {"calendar": "private"},
        )


async def test_store_owns_nested_values_at_read_and_write_boundaries(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Caller mutation cannot change memory or disk without an explicit set."""
    alice = ResolvedPrincipal(user_id="alice")
    source = {"preferences": {"rooms": ["kitchen"]}}
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    await store.async_set(alice, DataScope.PERSONAL, source)
    source["preferences"]["rooms"].append("garage")
    returned = store.get(alice, DataScope.PERSONAL)
    returned["preferences"]["rooms"].append("bedroom")

    assert store.get(alice, DataScope.PERSONAL) == {
        "preferences": {"rooms": ["kitchen"]}
    }
    reloaded = UserKeyedStore(hass, "test")
    await reloaded.async_load()
    assert reloaded.get(alice, DataScope.PERSONAL) == {
        "preferences": {"rooms": ["kitchen"]}
    }


async def test_store_rejects_raw_keys_and_scope_strings(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Runtime validation prevents callers from bypassing the scoped API."""
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    with pytest.raises(TypeError, match="ResolvedPrincipal"):
        store.get("alice", DataScope.PERSONAL)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DataScope"):
        store.get(ResolvedPrincipal(user_id="alice"), "personal")  # type: ignore[arg-type]
