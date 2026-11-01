"""The frozen export catalog and its single read-only export over one existing Operations read unit.

``ExportCatalog.export(uow)`` reads inside the unit it is given and never opens, writes or commits anything:

1. The actual tables are enumerated from the schema catalog **for coverage only**. Reflection never selects values:
   a table that no contribution classifies appears in ``missing`` and makes ``coverage_complete`` false.
2. Every classified table and declared column is checked against the real schema (names only).
3. Each exported table is read once, through its trusted selector or a generic explicit quoted column list (never
   ``SELECT *``), every value is checked against its closed declared type, structured columns go through their
   projector and closed schema, and rows are sorted by their stable key.

The result is deterministic: no timestamp, path or process-dependent value enters the manifest or any row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import sqlite3
from types import MappingProxyType
from typing import Any

from qorqut_operations.contracts import canonical_bytes, freeze, plain

from .contributions import (EXPORTED, REPRESENTED, HistoryContribution, RowRequest, TableClassification,
                            TableExport, quoted)
from .errors import HistoryCompositionError, HistoryExportError
from .fields import FieldValueError

FORMAT = "qorqut-operations-history-export"
VERSION = 1


@dataclass(frozen=True)
class SchemaIdentity:
    name: str
    version: int
    digest: str


def shard_id(key: tuple[Any, ...], hex_digits: int) -> str | None:
    """The fixed hash-prefix shard of a stable key, or ``None`` for an unsharded table."""
    if hex_digits == 0:
        return None
    return hashlib.sha256(canonical_bytes(list(key))).hexdigest()[:hex_digits]


@dataclass(frozen=True)
class ExportedTable:
    """The deterministic rows of one exported table, sorted by its stable key."""

    table: str
    owner: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    shard_hex: int
    rows: tuple[Mapping[str, Any], ...]

    def shard_of(self, row: Mapping[str, Any]) -> str | None:
        return shard_id(tuple(row[name] for name in self.key), self.shard_hex)


@dataclass(frozen=True)
class HistoryExport:
    """One consistent export: sorted tables, the coverage manifest and whether every actual table is classified."""

    format: str
    version: int
    schema: SchemaIdentity
    tables: Mapping[str, ExportedTable]
    manifest: Mapping[str, Any]
    missing: tuple[str, ...]
    coverage_complete: bool

    def counts(self) -> dict[str, int]:
        return {name: len(table.rows) for name, table in self.tables.items()}

    def manifest_bytes(self) -> bytes:
        return canonical_bytes(self.manifest)


@dataclass(frozen=True)
class ExportCatalog:
    """All contributions of one trusted composition, validated once and immutable.

    Duplicate owners or tables, an invalid contribution value and a ``represented`` table whose canonical table is
    not exported are composition errors raised here, before any database is touched."""

    contributions: tuple[HistoryContribution, ...]
    exports: Mapping[str, tuple[str, TableExport]] = field(init=False, repr=False, compare=False)
    classifications: Mapping[str, tuple[str, TableClassification]] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        contributions = tuple(self.contributions)
        owners: set[str] = set()
        exports: dict[str, tuple[str, TableExport]] = {}
        classifications: dict[str, tuple[str, TableClassification]] = {}
        for contribution in contributions:
            if not isinstance(contribution, HistoryContribution):
                raise HistoryCompositionError("A catalog holds HistoryContribution values made by trusted assembly code")
            if contribution.owner in owners:
                raise HistoryCompositionError(f"Contribution owner defined twice: {contribution.owner}")
            owners.add(contribution.owner)
            for table in (item.table for item in (*contribution.exports, *contribution.classifications)):
                if table in exports or table in classifications:
                    raise HistoryCompositionError(f"Table defined by more than one contribution: {table}")
            exports.update({item.table: (contribution.owner, item) for item in contribution.exports})
            classifications.update({item.table: (contribution.owner, item) for item in contribution.classifications})
        for table, (_, classification) in classifications.items():
            if classification.kind == REPRESENTED and classification.canonical not in exports:
                raise HistoryCompositionError(
                    f"Table {table} is represented by {classification.canonical}, which is not an exported table")
        object.__setattr__(self, "contributions", tuple(sorted(contributions, key=lambda item: item.owner)))
        object.__setattr__(self, "exports", MappingProxyType(dict(sorted(exports.items()))))
        object.__setattr__(self, "classifications", MappingProxyType(dict(sorted(classifications.items()))))

    @property
    def tables(self) -> tuple[str, ...]:
        """Every table the composition names, exported or classified."""
        return tuple(sorted({*self.exports, *self.classifications}))

    # -- export ----------------------------------------------------------------------------------------------

    def export(self, uow: Any) -> HistoryExport:
        """Export every declared table from the one read-only ``uow`` and report coverage. Does no filesystem work."""
        if getattr(uow, "writable", None) is not False or not hasattr(uow, "connection"):
            raise HistoryExportError("history_export_read_only", "History export requires an existing read-only unit")
        target = _identity(uow)
        connection = uow.connection
        actual = _actual_tables(connection)
        unknown = sorted(name for name in self.tables if name not in actual)
        if unknown:
            raise HistoryCompositionError(
                "Classified tables do not exist in the schema: " + ", ".join(unknown[:8]))
        columns = {table: _actual_columns(connection, table) for table in self.exports}
        for table, (_, export) in self.exports.items():
            absent = sorted({*export.source_columns, *export.key} - set(columns[table]))
            if absent:
                raise HistoryCompositionError(
                    f"Export of {table} names columns the schema lacks: {', '.join(absent[:8])}")
        missing = tuple(name for name in actual if name not in self.exports and name not in self.classifications)

        tables = {table: self._table(uow, owner, export) for table, (owner, export) in self.exports.items()}
        manifest = self._manifest(target, tables, columns, missing)
        return HistoryExport(FORMAT, VERSION, target, MappingProxyType(tables), manifest, missing, not missing)

    def _table(self, uow: Any, owner: str, export: TableExport) -> ExportedTable:
        name = export.table
        request = RowRequest(name, export.source_columns)
        try:
            if export.selector is not None:
                source = [dict(row) if isinstance(row, Mapping) else row for row in export.selector(uow, request)]
            else:
                source = request.select(uow)
        except HistoryExportError:
            raise
        except sqlite3.Error as error:
            raise HistoryExportError("history_export_failed", "Reading the table failed inside the read-only unit",
                                     table=name) from error
        except Exception as error:
            raise HistoryExportError("history_export_failed", f"Reading the rows failed ({type(error).__name__})",
                                     table=name) from error
        rows = [self._row(export, row) for row in source]
        rows.sort(key=lambda row: tuple(row[column] for column in export.key))
        keys = [tuple(row[column] for column in export.key) for row in rows]
        if len(set(keys)) != len(keys):
            raise HistoryExportError("history_export_invalid", "The declared stable key is not unique", table=name)
        return ExportedTable(name, owner, tuple(sorted((*export.columns, *export.structured))), export.key,
                             export.shard_hex, tuple(freeze(row) for row in rows))

    @staticmethod
    def _row(export: TableExport, source: Any) -> dict[str, Any]:
        name = export.table
        if not isinstance(source, Mapping):
            raise HistoryExportError("history_export_invalid", "A selected row is not a mapping", table=name)
        expected = set(export.source_columns)
        extra = sorted(str(column) for column in source if column not in expected)
        if extra:
            raise HistoryExportError("history_export_invalid",
                                     f"Rows carry undeclared fields: {', '.join(extra[:5])}", table=name)
        absent = sorted(column for column in expected if column not in source)
        if absent:
            raise HistoryExportError("history_export_invalid",
                                     f"Rows lack declared fields: {', '.join(absent[:5])}", table=name)
        row: dict[str, Any] = {}
        for column, kind in export.columns.items():
            try:
                row[column] = kind.check(source[column], column)
            except FieldValueError as error:
                raise HistoryExportError("history_export_invalid", f"Value rejected: {error}",
                                         table=name, field=column) from None
        for column, projection in export.structured.items():
            try:
                projected = projection.project(source[column])
            except Exception as error:
                raise HistoryExportError("history_export_invalid",
                                         f"Projection failed ({type(error).__name__})", table=name, field=column) from None
            try:
                row[column] = projection.schema.check(projected, column)
            except FieldValueError as error:
                raise HistoryExportError("history_export_invalid", f"Projection rejected: {error}",
                                         table=name, field=column) from None
        return row

    def _manifest(self, target: SchemaIdentity, tables: Mapping[str, ExportedTable],
                  columns: Mapping[str, tuple[str, ...]], missing: tuple[str, ...]) -> Mapping[str, Any]:
        exported = {}
        for name, (owner, export) in self.exports.items():
            exported[name] = {
                "classification": EXPORTED,
                "owner": owner,
                "rows": len(tables[name].rows),
                "columns": {column: kind.describe() for column, kind in sorted(export.columns.items())},
                "structured": {column: projection.schema.describe()
                               for column, projection in sorted(export.structured.items())},
                "key": list(export.key),
                "shard_hex": export.shard_hex,
                "selector": export.selector is not None,
                "omitted_columns": sorted(set(columns[name]) - set(export.source_columns)),
            }
        excluded = {}
        for name, (owner, classification) in self.classifications.items():
            entry = {"classification": classification.kind, "owner": owner, "reason": classification.reason}
            if classification.canonical is not None:
                entry["canonical"] = classification.canonical
            excluded[name] = entry
        return freeze(plain({
            "format": FORMAT, "version": VERSION,
            "schema": {"name": target.name, "version": target.version, "digest": target.digest},
            "coverage_complete": not missing,
            "missing": list(missing),
            "tables": exported,
            "exclusions": excluded,
        }))


def _identity(uow: Any) -> SchemaIdentity:
    try:
        target = uow.database.assembly.target
        return SchemaIdentity(str(target.name), int(target.version), str(target.digest))
    except AttributeError:
        raise HistoryExportError("history_export_read_only",
                                 "History export requires an Operations unit with a composed schema identity") from None


def _actual_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Names of the real tables for coverage reflection only; SQLite-internal tables are not part of coverage."""
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                              "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name").fetchall()
    return tuple(row[0] for row in rows)


def _actual_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f"PRAGMA table_xinfo({quoted(table)})").fetchall())
