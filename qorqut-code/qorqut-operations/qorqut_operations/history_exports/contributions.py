"""Trusted export contributions: what one domain exports and how it classifies every other table it owns.

A contribution is a frozen dataclass of this module built by trusted assembly code. There is no dynamic plugin loading,
no registration point reachable from HTTP/MCP input and no way to name a column, key, projector or selector from agent
or configuration text. A table that is neither exported nor classified is *missing coverage*, never an automatic dump.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType
from typing import Any

from .errors import HistoryCompositionError
from .fields import NAME, SCALARS, Arr, FieldType, Obj, Text, Integer

EXPORTED = "exported"
REPRESENTED = "represented"
TECHNICAL_EXCLUDED = "technical_excluded"
PRIVATE_EXCLUDED = "private_excluded"
SEPARATE_HISTORY = "separate_history"
EXCLUDED_CLASSIFICATIONS = (REPRESENTED, TECHNICAL_EXCLUDED, PRIVATE_EXCLUDED, SEPARATE_HISTORY)
CLASSIFICATIONS = (EXPORTED, *EXCLUDED_CLASSIFICATIONS)

OWNER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
MECHANISM = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
MAXIMUM_REASON = 240
MAXIMUM_SHARD_HEX = 3


def quoted(name: str) -> str:
    """A validated identifier in double quotes. Validation, not escaping, keeps SQL text free of caller input."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise HistoryCompositionError("Identifiers are lowercase names")
    return f'"{name}"'


@dataclass(frozen=True)
class RowRequest:
    """What a trusted row selector is asked for: the table and the exact declared source columns.

    ``select`` runs one read through the supplied unit with an explicit quoted column list (never ``SELECT *``); the
    optional ``where`` text and parameters are trusted source code of the selector, not configuration or agent input."""

    table: str
    columns: tuple[str, ...]

    def select(self, uow: Any, where: str = "", parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        listed = ", ".join(f'{quoted(self.table)}.{quoted(name)}' for name in self.columns)
        sql = f"SELECT {listed} FROM {quoted(self.table)}" + (f" WHERE {where}" if where else "")
        cursor = uow.connection.execute(sql, tuple(parameters))
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


Selector = Callable[[Any, RowRequest], Iterable[Mapping[str, Any]]]
Projector = Callable[[Any], Any]


def declared_json(schema: FieldType) -> Projector:
    """A projector for a JSON text column that keeps exactly the properties ``schema`` declares.

    The stored text is parsed and every undeclared property, at any depth, is dropped before the closed schema checks
    the result (a declared property that is absent becomes ``None`` and is accepted only when nullable). It is a
    convenience for selective export of safe nested fields; a domain whose structure needs more than selection writes its
    own projector. Anything that is not JSON text for the schema's shape raises and fails the export."""
    if not isinstance(schema, (Obj, Arr)):
        raise HistoryCompositionError("declared_json needs an object or array schema")

    def select(kind: FieldType, value: Any) -> Any:
        if isinstance(kind, Obj) and isinstance(value, Mapping):
            return {name: select(child, value.get(name)) for name, child in kind.fields.items()}
        if isinstance(kind, Arr) and isinstance(value, list):
            return [select(kind.item, item) for item in value]
        return value

    def project(raw: Any) -> Any:
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise TypeError("A JSON column holds text")
        return select(schema, json.loads(raw))

    return project


@dataclass(frozen=True)
class StructuredField:
    """A selected JSON/container column: the closed schema that may leave the table, and the projector producing it.

    The projector receives the raw stored value (for a JSON column, its text) and returns only the declared schema;
    anything the schema does not declare is rejected at export, so nested secrets or opaque events cannot ride along.
    ``schema`` must be an ``Obj`` or ``Arr``."""

    schema: FieldType
    project: Projector

    def __post_init__(self):
        if not isinstance(self.schema, (Obj, Arr)):
            raise HistoryCompositionError("A structured field declares an object or array schema")
        if not callable(self.project):
            raise HistoryCompositionError("A structured field requires a projector")


@dataclass(frozen=True)
class TableExport:
    """One exported table: exact scalar columns, the stable key, structured projections and an optional selector.

    ``columns`` maps each exported scalar column to its closed type. ``key`` is the stable ordered key (non-null text
    or integer columns among ``columns``). ``selector`` is a trusted domain row selector required whenever rows mix
    exportable and private channels: it returns mappings holding exactly the declared source columns (scalars plus
    structured raw values) for the rows that may be exported. ``shard_hex`` is 0 for one file per table or the number of
    leading hex digits of the SHA-256 of the canonical key that name a shard file."""

    table: str
    columns: Mapping[str, FieldType]
    key: tuple[str, ...]
    structured: Mapping[str, StructuredField] = field(default_factory=dict)
    selector: Selector | None = None
    shard_hex: int = 0

    def __post_init__(self):
        if not isinstance(self.table, str) or not NAME.fullmatch(self.table):
            raise HistoryCompositionError("An exported table needs a lowercase table name")
        if not isinstance(self.columns, Mapping) or not self.columns:
            raise HistoryCompositionError(f"Table {self.table} declares exact scalar columns")
        columns = dict(self.columns)
        for name, kind in columns.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise HistoryCompositionError(f"Table {self.table} has an invalid column name")
            if not isinstance(kind, SCALARS):
                raise HistoryCompositionError(
                    f"Column {self.table}.{name} must be a scalar type; a structured column needs a projector")
        if not isinstance(self.structured, Mapping):
            raise HistoryCompositionError(f"Table {self.table} structured fields are a mapping")
        structured = dict(self.structured)
        for name, projection in structured.items():
            if not isinstance(name, str) or not NAME.fullmatch(name):
                raise HistoryCompositionError(f"Table {self.table} has an invalid structured column name")
            if name in columns:
                raise HistoryCompositionError(f"Column {self.table}.{name} is declared as scalar and structured")
            if not isinstance(projection, StructuredField):
                raise HistoryCompositionError(f"Structured column {self.table}.{name} requires a StructuredField")
        key = tuple(self.key) if not isinstance(self.key, str) else ()
        if not key or len(set(key)) != len(key):
            raise HistoryCompositionError(f"Table {self.table} needs a stable ordered key of distinct columns")
        for name in key:
            kind = columns.get(name)
            if not isinstance(kind, (Text, Integer)) or kind.nullable:
                raise HistoryCompositionError(
                    f"Key column {self.table}.{name} must be a declared non-null text or integer column")
        if self.selector is not None and not callable(self.selector):
            raise HistoryCompositionError(f"Table {self.table} selector must be callable")
        if type(self.shard_hex) is not int or not 0 <= self.shard_hex <= MAXIMUM_SHARD_HEX:
            raise HistoryCompositionError(f"Table {self.table} sharding is 0 to {MAXIMUM_SHARD_HEX} hex digits")
        object.__setattr__(self, "columns", MappingProxyType(columns))
        object.__setattr__(self, "structured", MappingProxyType(structured))
        object.__setattr__(self, "key", key)

    @property
    def source_columns(self) -> tuple[str, ...]:
        """Every column read from the table, sorted: the declared scalars and the raw structured columns."""
        return tuple(sorted((*self.columns, *self.structured)))


@dataclass(frozen=True)
class TableClassification:
    """An explicit decision for a table that is not exported, with the reason and, where relevant, its owner.

    ``represented``: another exported table is canonical (``canonical`` names it). ``separate_history``: an existing
    history mechanism owns it (``canonical`` names that mechanism). ``technical_excluded`` and ``private_excluded``
    carry no canonical owner. Every exclusion needs a reason; none ever copies the excluded value."""

    table: str
    kind: str
    reason: str
    canonical: str | None = None

    def __post_init__(self):
        if not isinstance(self.table, str) or not NAME.fullmatch(self.table):
            raise HistoryCompositionError("A classification needs a lowercase table name")
        if self.kind not in EXCLUDED_CLASSIFICATIONS:
            raise HistoryCompositionError(
                f"Table {self.table}: classification must be one of {', '.join(EXCLUDED_CLASSIFICATIONS)}; "
                "an exported table is declared by a TableExport")
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > MAXIMUM_REASON:
            raise HistoryCompositionError(f"Table {self.table}: an exclusion needs a short reason")
        if self.kind == REPRESENTED:
            if not isinstance(self.canonical, str) or not NAME.fullmatch(self.canonical) or self.canonical == self.table:
                raise HistoryCompositionError(f"Table {self.table}: a represented table names its canonical exported table")
        elif self.kind == SEPARATE_HISTORY:
            if not isinstance(self.canonical, str) or not MECHANISM.fullmatch(self.canonical):
                raise HistoryCompositionError(f"Table {self.table}: a separate-history table names its history mechanism")
        elif self.canonical is not None:
            raise HistoryCompositionError(f"Table {self.table}: {self.kind} has no canonical owner")


@dataclass(frozen=True)
class HistoryContribution:
    """One domain's exports and classifications. The owner is unique within a catalog."""

    owner: str
    exports: tuple[TableExport, ...] = ()
    classifications: tuple[TableClassification, ...] = ()

    def __post_init__(self):
        if not isinstance(self.owner, str) or not OWNER.fullmatch(self.owner):
            raise HistoryCompositionError("A contribution requires a lowercase owner name")
        exports, classifications = tuple(self.exports), tuple(self.classifications)
        if any(not isinstance(item, TableExport) for item in exports):
            raise HistoryCompositionError("Exports are TableExport values made by trusted assembly code")
        if any(not isinstance(item, TableClassification) for item in classifications):
            raise HistoryCompositionError("Classifications are TableClassification values")
        if not exports and not classifications:
            raise HistoryCompositionError(f"Contribution {self.owner} names no table")
        names = [item.table for item in (*exports, *classifications)]
        if len(set(names)) != len(names):
            raise HistoryCompositionError(f"Contribution {self.owner} defines a table more than once")
        object.__setattr__(self, "exports", exports)
        object.__setattr__(self, "classifications", classifications)
