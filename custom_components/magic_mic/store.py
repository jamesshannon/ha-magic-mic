"""Scoped persistent storage for household and personal capability data.

A thin wrapper over HA's `Store` that derives its namespace from an explicit principal and
scope (§5.1). Household data retains the stable `"default"` key; personal data uses a real
HA user ID and has no unidentified fallback. Empty in Wave 0 (no capability consumes it
yet); it exists so later capabilities (memory, reminders, annotations) never need a
multi-user migration. The backing store is HA's private JSON `Store`, written with owner-only
filesystem permissions. Records live under explicit capability namespaces and mutate through
one serialized row API instead of whole-scope replacement. A capability that needs full-text
search may use its own backend (for example, SQLite FTS for the memory notebook) and must
make the same privacy and concurrency decisions.
"""

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from typing import override

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.json import JsonObjectType, JsonValueType

from .const import DOMAIN
from .identity import DataScope, ResolvedPrincipal
from .immutable_json import FrozenJsonValue, freeze_json_mapping, thaw_json_mapping

STORAGE_VERSION = 2

type RecordMap = dict[str, JsonObjectType]
type NamespaceMap = dict[str, RecordMap]
type StoreData = dict[str, NamespaceMap]


class InvalidStoreDataError(ValueError):
    """Stored capability data does not match the owned persistence shape."""


class _UserKeyedHAStore(Store[StoreData]):
    """HA store with an explicit migration boundary for Magic Mic data."""

    @override
    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: object,
    ) -> StoreData:
        """Migrate the unused version-one placeholder or reject unknown data."""
        del old_minor_version
        if old_major_version == 1:
            if old_data != {}:
                raise InvalidStoreDataError(
                    "Version 1 Magic Mic storage contains data with no defined migration"
                )
            return {}
        raise NotImplementedError


class UserKeyedStore:
    """Persist validated records under scoped capability namespaces."""

    def __init__(self, hass: HomeAssistant, name: str) -> None:
        """Create a store backed by `.storage/<domain>.<name>`."""
        self._store = _UserKeyedHAStore(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{name}",
            private=True,
        )
        self._data: StoreData = {}
        self._mutation_lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load and validate persisted data before publishing it in memory."""
        loaded = await self._store.async_load()
        self._data = _validate_store_data({} if loaded is None else loaded)

    def get_record(
        self,
        principal: ResolvedPrincipal,
        scope: DataScope,
        namespace: str,
        record_id: str,
    ) -> JsonObjectType | None:
        """Return one owned record copy, or `None` when it does not exist."""
        scope_key = self._scope_key(principal, scope)
        _validate_key(namespace, "namespace")
        _validate_key(record_id, "record_id")
        record = self._data.get(scope_key, {}).get(namespace, {}).get(record_id)
        return deepcopy(record)

    def list_records(
        self,
        principal: ResolvedPrincipal,
        scope: DataScope,
        namespace: str,
    ) -> RecordMap:
        """Return owned copies of every record in one capability namespace."""
        scope_key = self._scope_key(principal, scope)
        _validate_key(namespace, "namespace")
        return deepcopy(self._data.get(scope_key, {}).get(namespace, {}))

    async def async_put_record(
        self,
        principal: ResolvedPrincipal,
        scope: DataScope,
        namespace: str,
        record_id: str,
        data: Mapping[str, JsonValueType | FrozenJsonValue],
    ) -> JsonObjectType | None:
        """Atomically upsert one record and return its prior value."""
        scope_key = self._scope_key(principal, scope)
        _validate_key(namespace, "namespace")
        _validate_key(record_id, "record_id")
        record = thaw_json_mapping(freeze_json_mapping(data))

        async with self._mutation_lock:
            previous = self._data.get(scope_key, {}).get(namespace, {}).get(record_id)
            candidate = deepcopy(self._data)
            records = candidate.setdefault(scope_key, {}).setdefault(namespace, {})
            records[record_id] = record
            await self._store.async_save(candidate)
            self._data = candidate
            return deepcopy(previous)

    async def async_delete_record(
        self,
        principal: ResolvedPrincipal,
        scope: DataScope,
        namespace: str,
        record_id: str,
    ) -> JsonObjectType | None:
        """Atomically delete one record and return its prior value."""
        scope_key = self._scope_key(principal, scope)
        _validate_key(namespace, "namespace")
        _validate_key(record_id, "record_id")

        async with self._mutation_lock:
            previous = self._data.get(scope_key, {}).get(namespace, {}).get(record_id)
            if previous is None:
                return None

            candidate = deepcopy(self._data)
            records = candidate[scope_key][namespace]
            del records[record_id]
            if not records:
                del candidate[scope_key][namespace]
            if not candidate[scope_key]:
                del candidate[scope_key]
            await self._store.async_save(candidate)
            self._data = candidate
            return deepcopy(previous)

    @staticmethod
    def _scope_key(principal: ResolvedPrincipal, scope: DataScope) -> str:
        """Validate the boundary types and derive the authorized storage key."""
        if not isinstance(principal, ResolvedPrincipal):
            raise TypeError("principal must be a ResolvedPrincipal")
        if not isinstance(scope, DataScope):
            raise TypeError("scope must be a DataScope")
        return principal.storage_key(scope)


def _validate_store_data(value: object) -> StoreData:
    """Validate and own the complete persisted scope/namespace/record shape."""
    if not isinstance(value, Mapping):
        raise InvalidStoreDataError("Store root must be a JSON object")

    validated: StoreData = {}
    for scope_key, namespaces in value.items():
        _validate_loaded_key(scope_key, "scope key")
        if not isinstance(namespaces, Mapping):
            raise InvalidStoreDataError(
                f"Scope {scope_key!r} must contain a namespace object"
            )
        validated_namespaces: NamespaceMap = {}
        for namespace, records in namespaces.items():
            _validate_loaded_key(namespace, f"namespace in scope {scope_key!r}")
            if not isinstance(records, Mapping):
                raise InvalidStoreDataError(
                    f"Namespace {namespace!r} in scope {scope_key!r} must contain "
                    "a record object"
                )
            validated_records: RecordMap = {}
            for record_id, record in records.items():
                _validate_loaded_key(
                    record_id,
                    f"record ID in namespace {namespace!r}",
                )
                if not isinstance(record, Mapping):
                    raise InvalidStoreDataError(
                        f"Record {record_id!r} in namespace {namespace!r} must be "
                        "a JSON object"
                    )
                try:
                    validated_records[record_id] = thaw_json_mapping(
                        freeze_json_mapping(record)
                    )
                except (TypeError, ValueError) as err:
                    raise InvalidStoreDataError(
                        f"Record {record_id!r} in namespace {namespace!r} is invalid: "
                        f"{err}"
                    ) from err
            validated_namespaces[namespace] = validated_records
        validated[scope_key] = validated_namespaces
    return validated


def _validate_key(value: object, label: str) -> str:
    """Require one nonempty caller-supplied storage key segment."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _validate_loaded_key(value: object, label: str) -> None:
    """Convert a public key validation failure into stored-data corruption."""
    try:
        _validate_key(value, label)
    except ValueError as err:
        raise InvalidStoreDataError(str(err)) from err


__all__ = ["InvalidStoreDataError", "UserKeyedStore"]
