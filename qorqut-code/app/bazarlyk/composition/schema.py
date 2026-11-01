"""The retained host domains' storage contributions: source-derived DDL plus the v1 repository specifications.

The DDL is ``schema_ddl`` (generated from the deployed v1 migration source by ``tools/derive_host_schema.py``; no
migration runs here), the table specifications and immutable columns are ``tables``. Every table belongs to exactly
one owner and the engine's own tables are never redefined: the engine refuses a duplicate table, and this module
checks before it hands the contributions over that the generated ownership and the specifications agree."""

from __future__ import annotations

from qorqut_operations.storage.assembly import AssemblyError, StorageContribution

from . import schema_ddl, tables
from .documents import RESOURCE_TYPE as DOCUMENTS

# Document availability is tracked beside every file receipt (v1), so the documents owner requires its lifecycle.
REQUIRED_LIFECYCLES = {"documents": frozenset({DOCUMENTS})}


def storage_contributions() -> tuple[StorageContribution, ...]:
    owned = {}
    for owner, names in schema_ddl.OWNER_TABLES.items():
        for name in names:
            if name in owned:
                raise AssemblyError(f"Table {name} is assigned to two schema owners")
            owned[name] = owner
    if set(schema_ddl.OWNER_SCHEMA) != set(schema_ddl.OWNER_TABLES):
        raise AssemblyError("Generated schema and table ownership name different owners")
    unowned = sorted(set(tables.TABLE_SPECS) - set(owned))
    if unowned:
        raise AssemblyError(f"Repository specifications without a schema owner: {', '.join(unowned)}")
    stray = sorted(set(tables.IMMUTABLE_FIELDS) - set(tables.TABLE_SPECS))
    if stray:
        raise AssemblyError(f"Immutable columns for undeclared tables: {', '.join(stray)}")
    contributions = []
    for owner, names in schema_ddl.OWNER_TABLES.items():
        specs = {name: tables.TABLE_SPECS[name] for name in names if name in tables.TABLE_SPECS}
        contributions.append(StorageContribution(
            owner, specs, {name: fields for name, fields in tables.IMMUTABLE_FIELDS.items() if name in specs},
            schema_ddl.OWNER_SCHEMA[owner], required_lifecycles=REQUIRED_LIFECYCLES.get(owner, frozenset())))
    return tuple(contributions)
