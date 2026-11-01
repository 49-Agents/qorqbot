"""The expected schema-object inventory of a composed target, and its comparison with a real store.

The expected inventory is not parsed from the DDL: the same trusted composed statements (plus the identity table) are
executed in a disposable in-memory SQLite database and SQLite's own stored definitions are read back from its schema
catalog. A store is acceptable only when its catalog holds exactly those tables, indexes and triggers with the same
owner table and the same stored definition. DDL is never run against an existing store, rows are never read, and a
difference is reported, never repaired. SQLite's internal objects (``sqlite_*``) are ignored on both sides."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from types import MappingProxyType
from typing import Mapping

from .assembly import Assembly, AssemblyError
from .sqlfunctions import install_sql_functions

IDENTITY_TABLE = "target_identity"
IDENTITY_DDL = (f'CREATE TABLE "{IDENTITY_TABLE}" (id TEXT PRIMARY KEY CHECK (id = \'target\'), '
                'name TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL)')

# (type, name) -> (owner table, stored definition)
Objects = Mapping[tuple[str, str], tuple[str, str]]
MAXIMUM_REPORTED = 8


@dataclass(frozen=True)
class CompiledTarget:
    objects: Objects
    columns: Mapping[str, tuple[str, ...]]


def read_objects(connection) -> dict[tuple[str, str], tuple[str, str]]:
    return {(kind, name): (owner, sql) for kind, name, owner, sql in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "AND sql IS NOT NULL")}


def compile_target(assembly: Assembly) -> CompiledTarget:
    """Execute the composed DDL in memory and check that every table spec names columns that really exist."""
    connection = sqlite3.connect(":memory:")
    try:
        install_sql_functions(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute(IDENTITY_DDL)
            for _owner, statements in assembly.schema:
                for statement in statements:
                    connection.execute(statement)
        except sqlite3.Error as exc:
            raise AssemblyError(f"The composed schema does not compile: {exc}") from exc
        objects = read_objects(connection)
        columns = {table: tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
                   for table in sorted({owner for owner, _ in objects.values()})}
    finally:
        connection.close()
    for table, spec in assembly.tables.items():
        if table not in columns:
            raise AssemblyError(f"The composed schema does not create table {table}")
        required = {*spec.keys, *(name for name in (spec.scope_column, spec.revision_column) if name),
                    *assembly.immutable_fields.get(table, ())}
        if required - set(columns[table]):
            raise AssemblyError(f"Table {table} lacks declared columns: {', '.join(sorted(required - set(columns[table])))}")
    return CompiledTarget(MappingProxyType(objects), MappingProxyType(columns))


def differences(expected: Objects, actual: Objects) -> list[str]:
    """Names only (never definitions or data): what is missing, unexpected or changed in ``actual``."""
    found = [f"missing {kind} {name}" for (kind, name) in sorted(expected.keys() - actual.keys())]
    found += [f"unexpected {kind} {name}" for (kind, name) in sorted(actual.keys() - expected.keys())]
    found += [f"changed {kind} {name}" for (kind, name) in sorted(expected.keys() & actual.keys())
              if expected[(kind, name)] != actual[(kind, name)]]
    return found


def describe(problems: list[str]) -> str:
    shown = "; ".join(problems[:MAXIMUM_REPORTED])
    return shown + (f"; and {len(problems) - MAXIMUM_REPORTED} more" if len(problems) > MAXIMUM_REPORTED else "")
