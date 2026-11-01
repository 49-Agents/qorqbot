"""Internal repositories over the assembly's table catalog, with scoped keys and revision CAS."""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_bytes, utc_now

UNSET = object()


def decode_row(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: json.loads(value) if key.endswith("_json") and value is not None else value
            for key, value in dict(row).items() if key != "scope_key"}


class Repository:
    def __init__(self, uow, table: str, business_id=UNSET):
        tables = uow.database.assembly.tables
        if table not in tables:
            raise ValueError("Unknown repository")
        self.uow, self.table, self.spec = uow, table, tables[table]
        self.columns = uow.database.columns(table)
        inherited = uow.business_id
        if business_id is UNSET:
            business_id = inherited
        if self.spec.scope_column and inherited is not UNSET and business_id != inherited:
            raise DomainError("forbidden", "Repository scope cannot differ from invocation scope")
        if self.spec.scope_column and business_id is UNSET:
            raise DomainError("scope_required", "A scoped repository requires an explicit business namespace")
        self.business_id = business_id

    def _values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(values)
        if set(values) - self.columns:
            raise ValueError(f"Unknown fields for {self.table}: {sorted(set(values) - self.columns)}")
        for key, value in values.items():
            if key.endswith("_json") and value is not None:
                values[key] = canonical_bytes(json.loads(value) if isinstance(value, str) else value).decode("utf-8")
        return values

    def _scope(self, filters=None) -> dict[str, Any]:
        result = dict(filters or {})
        if set(result) - self.columns:
            raise ValueError("Unknown filter field")
        if self.spec.scope_column:
            key = self.spec.scope_column
            if key in result and result[key] != self.business_id:
                raise DomainError("forbidden", "Resource is outside the repository scope")
            result[key] = self.business_id
        return result

    @staticmethod
    def _where(values):
        parts, parameters = [], []
        for key, value in values.items():
            if value is None:
                parts.append(f'"{key}" IS NULL')
            else:
                parts.append(f'"{key}" = ?')
                parameters.append(value)
        return " AND ".join(parts) or "1", parameters

    def _key(self, key) -> dict[str, Any]:
        keys = tuple(k for k in self.spec.keys if k != self.spec.scope_column)
        if isinstance(key, Mapping):
            result = dict(key)
            if set(result) != set(keys):
                raise ValueError(f"Key requires exactly {keys}")
        elif len(keys) == 1:
            result = {keys[0]: key}
        elif isinstance(key, tuple) and len(key) == len(keys):
            result = dict(zip(keys, key))
        else:
            raise ValueError(f"Composite key requires {keys}")
        return self._scope(result)

    def get(self, key, *, required: bool = False):
        where, params = self._where(self._key(key))
        row = decode_row(self.uow.connection.execute(f'SELECT * FROM "{self.table}" WHERE {where}', params).fetchone())
        if row is None and required:
            raise DomainError("not_found", "Resource does not exist")
        return row

    def list(self, *, filters=None, limit: int = 50, order_by=(), descending: bool = False):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("validation_error", "List limit must be between 1 and 200")
        order_by = tuple(order_by) or tuple(k for k in ("created_at", *self.spec.keys) if k in self.columns)
        if any(key not in self.columns for key in order_by):
            raise ValueError("Unknown ordering column")
        where, params = self._where(self._scope(filters))
        direction = " DESC" if descending else " ASC"
        order = ", ".join(f'"{key}"{direction}' for key in order_by)
        rows = self.uow.connection.execute(
            f'SELECT * FROM "{self.table}" WHERE {where} ORDER BY {order} LIMIT ?', [*params, limit])
        return [decode_row(row) for row in rows]

    def insert(self, values: Mapping[str, Any]):
        self.uow.require_write()
        values = self._scope(values)
        now = utc_now()
        for key in ("created_at", "updated_at"):
            if key in self.columns:
                values.setdefault(key, now)
        if self.spec.revision_column:
            values.setdefault(self.spec.revision_column, 1)
        encoded = self._values(values)
        columns = ", ".join(f'"{key}"' for key in encoded)
        marks = ", ".join("?" for _ in encoded)
        self.uow.connection.execute(f'INSERT INTO "{self.table}" ({columns}) VALUES ({marks})', list(encoded.values()))
        key = {k: values[k] for k in self.spec.keys if k != self.spec.scope_column}
        return self.get(key, required=True)

    def update(self, key, changes: Mapping[str, Any], *, expected_revision: int | None = None, force_revision=False):
        self.uow.require_write()
        if not self.spec.mutable:
            raise DomainError("immutable_record", "This record is append-only")
        blocked = (set(self.spec.keys) | {"created_at", "created_by", self.spec.scope_column, self.spec.revision_column}
                   | self.uow.database.assembly.immutable_fields.get(self.table, frozenset()))
        if set(changes) & blocked:
            raise DomainError("validation_error", "Immutable identity, scope, or revision fields cannot be updated")
        changes = self._values(changes)
        current = self.get(key, required=True)
        revision = self.spec.revision_column
        if revision and (type(expected_revision) is not int or expected_revision < 1):
            raise DomainError("validation_error", "expected_revision is required")
        if revision and current[revision] != expected_revision:
            raise DomainError("revision_conflict", "Resource changed", current_revision=current[revision])
        normalized = self._values({name: current[name] for name in changes})
        if normalized == changes and not (force_revision and revision):
            return current
        if "updated_at" in self.columns:
            changes["updated_at"] = utc_now()
        if revision:
            changes[revision] = expected_revision + 1
        filters = self._key(key)
        if revision:
            filters[revision] = expected_revision
        where, params = self._where(filters)
        sets = ", ".join(f'"{name}" = ?' for name in changes)
        cursor = self.uow.connection.execute(f'UPDATE "{self.table}" SET {sets} WHERE {where}', [*changes.values(), *params])
        if cursor.rowcount != 1:
            raise DomainError("revision_conflict", "Resource changed")
        return self.get(key, required=True)

    def delete(self, key, *, expected_revision: int | None = None):
        self.uow.require_write()
        if not self.spec.mutable:
            raise DomainError("immutable_record", "This record is append-only")
        current = self.get(key, required=True)
        filters = self._key(key)
        revision = self.spec.revision_column
        if revision:
            if type(expected_revision) is not int or expected_revision < 1:
                raise DomainError("validation_error", "expected_revision is required")
            if current[revision] != expected_revision:
                raise DomainError("revision_conflict", "Resource changed", current_revision=current[revision])
            filters[revision] = expected_revision
        where, params = self._where(filters)
        if self.uow.connection.execute(f'DELETE FROM "{self.table}" WHERE {where}', params).rowcount != 1:
            raise DomainError("revision_conflict", "Resource changed")
        return current
