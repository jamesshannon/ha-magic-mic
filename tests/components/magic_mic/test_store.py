"""Tests for the user-keyed store."""

import asyncio
from datetime import UTC, datetime
import math
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.magic_mic.identity import (
    UNIDENTIFIED_PRINCIPAL,
    DataScope,
    ResolvedPrincipal,
)
from custom_components.magic_mic.store import (
    STORAGE_VERSION,
    InvalidStoreDataError,
    UserKeyedStore,
)
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
    assert store.get_record(alice, DataScope.PERSONAL, "preferences", "wifi") is None

    await store.async_put_record(
        alice,
        DataScope.PERSONAL,
        "preferences",
        "wifi",
        {"password": "hunter2"},
    )
    assert store.get_record(alice, DataScope.PERSONAL, "preferences", "wifi") == {
        "password": "hunter2"
    }
    # Another user's scope is untouched.
    assert store.get_record(bob, DataScope.PERSONAL, "preferences", "wifi") is None

    # A fresh instance loads the persisted data.
    reloaded = UserKeyedStore(hass, "test")
    await reloaded.async_load()
    assert reloaded.get_record(alice, DataScope.PERSONAL, "preferences", "wifi") == {
        "password": "hunter2"
    }
    assert reloaded.get_record(bob, DataScope.PERSONAL, "preferences", "wifi") is None


async def test_household_scope_is_shared_by_every_principal(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Identified and unidentified callers share the one household bucket."""
    alice = ResolvedPrincipal(user_id="alice")
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    await store.async_put_record(
        alice,
        DataScope.HOUSEHOLD,
        "household_facts",
        "garage",
        {"door_code": "1234"},
    )

    assert store.get_record(
        UNIDENTIFIED_PRINCIPAL,
        DataScope.HOUSEHOLD,
        "household_facts",
        "garage",
    ) == {"door_code": "1234"}


async def test_store_file_is_private(hass: HomeAssistant) -> None:
    """Sensitive scoped data is written with owner-only filesystem access."""
    store = UserKeyedStore(hass, "private_test")
    await store.async_load()

    with patch.object(Store, "_async_write_data", _REAL_ASYNC_WRITE_DATA):
        await store.async_put_record(
            UNIDENTIFIED_PRINCIPAL,
            DataScope.HOUSEHOLD,
            "household_facts",
            "garage",
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
        store.get_record(
            UNIDENTIFIED_PRINCIPAL,
            DataScope.PERSONAL,
            "calendar",
            "private",
        )
    with pytest.raises(PermissionError):
        await store.async_put_record(
            UNIDENTIFIED_PRINCIPAL,
            DataScope.PERSONAL,
            "calendar",
            "private",
            {"summary": "private"},
        )


async def test_store_owns_nested_values_at_read_and_write_boundaries(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Caller mutation cannot change memory or disk without an explicit set."""
    alice = ResolvedPrincipal(user_id="alice")
    source = {"preferences": {"rooms": ["kitchen"]}}
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    await store.async_put_record(
        alice,
        DataScope.PERSONAL,
        "preferences",
        "rooms",
        source,
    )
    source["preferences"]["rooms"].append("garage")
    returned = store.get_record(alice, DataScope.PERSONAL, "preferences", "rooms")
    assert returned is not None
    returned["preferences"]["rooms"].append("bedroom")
    listed = store.list_records(alice, DataScope.PERSONAL, "preferences")
    listed["rooms"]["preferences"]["rooms"].append("office")

    assert store.get_record(alice, DataScope.PERSONAL, "preferences", "rooms") == {
        "preferences": {"rooms": ["kitchen"]}
    }
    reloaded = UserKeyedStore(hass, "test")
    await reloaded.async_load()
    assert reloaded.get_record(alice, DataScope.PERSONAL, "preferences", "rooms") == {
        "preferences": {"rooms": ["kitchen"]}
    }


async def test_store_rejects_raw_keys_and_scope_strings(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """Runtime validation prevents callers from bypassing the scoped API."""
    store = UserKeyedStore(hass, "test")
    await store.async_load()

    with pytest.raises(TypeError, match="ResolvedPrincipal"):
        store.get_record(  # type: ignore[arg-type]
            "alice", DataScope.PERSONAL, "preferences", "rooms"
        )
    with pytest.raises(TypeError, match="DataScope"):
        store.get_record(  # type: ignore[arg-type]
            ResolvedPrincipal(user_id="alice"),
            "personal",
            "preferences",
            "rooms",
        )
    with pytest.raises(ValueError, match="namespace"):
        store.list_records(ResolvedPrincipal(user_id="alice"), DataScope.PERSONAL, "")
    with pytest.raises(ValueError, match="record_id"):
        store.get_record(
            ResolvedPrincipal(user_id="alice"),
            DataScope.PERSONAL,
            "preferences",
            "",
        )


async def test_put_and_delete_return_prior_record(
    hass: HomeAssistant,
) -> None:
    """Row mutations expose an owned prior value for deterministic undo."""
    store = UserKeyedStore(hass, "prior")
    await store.async_load()
    principal = ResolvedPrincipal(user_id="alice")

    first = await store.async_put_record(
        principal,
        DataScope.PERSONAL,
        "preferences",
        "temperature",
        {"value": 70},
    )
    replaced = await store.async_put_record(
        principal,
        DataScope.PERSONAL,
        "preferences",
        "temperature",
        {"value": 72},
    )
    deleted = await store.async_delete_record(
        principal,
        DataScope.PERSONAL,
        "preferences",
        "temperature",
    )
    missing = await store.async_delete_record(
        principal,
        DataScope.PERSONAL,
        "preferences",
        "temperature",
    )

    assert first is None
    assert replaced == {"value": 70}
    assert deleted == {"value": 72}
    assert missing is None
    assert store.list_records(principal, DataScope.PERSONAL, "preferences") == {}


@pytest.mark.parametrize(
    ("second_namespace", "expected"),
    [
        (
            "memory",
            {
                "memory": {
                    "first": {"value": 1},
                    "second": {"value": 2},
                }
            },
        ),
        (
            "reminders",
            {
                "memory": {"first": {"value": 1}},
                "reminders": {"second": {"value": 2}},
            },
        ),
    ],
)
async def test_concurrent_record_writes_preserve_every_update(
    hass: HomeAssistant,
    second_namespace: str,
    expected: dict[str, dict[str, dict[str, int]]],
) -> None:
    """A paused save cannot let another row mutation replace its snapshot."""
    store = UserKeyedStore(hass, "concurrent")
    await store.async_load()
    principal = ResolvedPrincipal(user_id="alice")
    real_async_save = Store.async_save
    first_save_started = asyncio.Event()
    release_first_save = asyncio.Event()
    save_calls = 0

    async def blocking_save(
        ha_store: Store,
        data: dict[str, Any],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            first_save_started.set()
            await release_first_save.wait()
        await real_async_save(ha_store, data)

    with patch.object(Store, "async_save", blocking_save):
        first = asyncio.create_task(
            store.async_put_record(
                principal,
                DataScope.PERSONAL,
                "memory",
                "first",
                {"value": 1},
            )
        )
        await first_save_started.wait()
        second = asyncio.create_task(
            store.async_put_record(
                principal,
                DataScope.PERSONAL,
                second_namespace,
                "second",
                {"value": 2},
            )
        )
        await asyncio.sleep(0)

        assert save_calls == 1
        assert store.list_records(principal, DataScope.PERSONAL, "memory") == {}
        release_first_save.set()
        assert await first is None
        assert await second is None

    actual = {
        namespace: store.list_records(principal, DataScope.PERSONAL, namespace)
        for namespace in expected
    }
    assert actual == expected


async def test_concurrent_same_record_writes_are_serialized(
    hass: HomeAssistant,
) -> None:
    """A later upsert sees and replaces the first committed row."""
    store = UserKeyedStore(hass, "same_record")
    await store.async_load()
    principal = ResolvedPrincipal(user_id="alice")
    real_async_save = Store.async_save
    first_save_started = asyncio.Event()
    release_first_save = asyncio.Event()
    save_calls = 0

    async def blocking_save(
        ha_store: Store,
        data: dict[str, Any],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            first_save_started.set()
            await release_first_save.wait()
        await real_async_save(ha_store, data)

    with patch.object(Store, "async_save", blocking_save):
        first = asyncio.create_task(
            store.async_put_record(
                principal,
                DataScope.PERSONAL,
                "preferences",
                "temperature",
                {"value": 70},
            )
        )
        await first_save_started.wait()
        second = asyncio.create_task(
            store.async_put_record(
                principal,
                DataScope.PERSONAL,
                "preferences",
                "temperature",
                {"value": 72},
            )
        )
        await asyncio.sleep(0)
        release_first_save.set()

        assert await first is None
        assert await second == {"value": 70}

    assert store.get_record(
        principal,
        DataScope.PERSONAL,
        "preferences",
        "temperature",
    ) == {"value": 72}


@pytest.mark.parametrize(
    "invalid_record",
    [
        {"value": {"not", "json"}},
        {"value": datetime(2026, 8, 3, tzinfo=UTC)},
        {"value": math.nan},
        {"value": {1: "non-string key"}},
    ],
)
async def test_invalid_record_is_rejected_before_save_or_memory_change(
    hass: HomeAssistant,
    invalid_record: dict[str, object],
) -> None:
    """Invalid JSON cannot appear successful for the life of the process."""
    store = UserKeyedStore(hass, "invalid_write")
    await store.async_load()
    principal = ResolvedPrincipal(user_id="alice")
    save = AsyncMock()

    with (
        patch.object(Store, "async_save", save),
        pytest.raises((TypeError, ValueError)),
    ):
        await store.async_put_record(
            principal,
            DataScope.PERSONAL,
            "memory",
            "invalid",
            invalid_record,  # type: ignore[arg-type]
        )

    save.assert_not_awaited()
    assert store.get_record(principal, DataScope.PERSONAL, "memory", "invalid") is None


@pytest.mark.parametrize(
    "stored_data",
    [
        [],
        {"default": []},
        {"default": {"memory": []}},
        {"default": {"memory": {"record": "not an object"}}},
        {"default": {"memory": {"record": {"value": math.nan}}}},
        {"": {}},
    ],
)
async def test_corrupt_loaded_shape_is_rejected(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    stored_data: object,
) -> None:
    """Malformed persisted structure fails at the load boundary."""
    key = "magic_mic.corrupt"
    hass_storage[key] = {
        "data": stored_data,
        "key": key,
        "minor_version": 1,
        "version": STORAGE_VERSION,
    }

    with pytest.raises(InvalidStoreDataError):
        await UserKeyedStore(hass, "corrupt").async_load()


async def test_empty_version_one_store_migrates(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """The unused placeholder schema has an explicit empty migration."""
    key = "magic_mic.migrate"
    hass_storage[key] = {
        "data": {},
        "key": key,
        "minor_version": 1,
        "version": 1,
    }

    store = UserKeyedStore(hass, "migrate")
    await store.async_load()

    assert hass_storage[key]["version"] == STORAGE_VERSION
    assert (
        store.list_records(UNIDENTIFIED_PRINCIPAL, DataScope.HOUSEHOLD, "memory") == {}
    )


async def test_nonempty_version_one_store_is_rejected(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
) -> None:
    """Unknown experimental data is not silently discarded or reinterpreted."""
    key = "magic_mic.legacy_data"
    hass_storage[key] = {
        "data": {"default": {"wifi": "secret"}},
        "key": key,
        "minor_version": 1,
        "version": 1,
    }

    with pytest.raises(InvalidStoreDataError, match="no defined migration"):
        await UserKeyedStore(hass, "legacy_data").async_load()
