"""Tables of an extension's own: the statement checker and the canonical record row.

An extension that brings tables declares them as storage contributions. Before any of its statements is composed into
the store's target, it goes through ``check_statements``: the statements run, one at a time, in a fresh in-memory
database with Qorqbot's SQL functions installed, under an SQLite authorizer that allows only creating a table, an index
or a trigger in ``main`` that is named ``<id>_...`` and sits on the extension's own tables. Everything else is refused
before it runs: ``SELECT`` statements, ``ATTACH`` (and so ``VACUUM``), ``PRAGMA``, transactions, views, temporary
objects, virtual tables, writes and deletes. Each statement must add exactly the one object it names, so
``IF NOT EXISTS`` over a name that exists is refused too. Trigger bodies are checked by preparing (never running) an
insert, an update of every column and a delete on every own table, and foreign keys may name only own tables.

``row_of`` turns a declaration into the one canonical row the store keeps for the extension, and ``contribution_of``
turns such a row back into a contribution, checking its statements first. A store's target is always composed from
contributions that went through both, so the composition and the record are built by the same code.

The store keeps one row per extension that has tables in it, in the table ``extensiontables``. That table exists only once
the first extension has its tables, and its name has no underscore, so no extension object can take it. Every place that
opens the store reads it first with ``recorded`` and composes ``host_assembly(..., tables=recorded(...))``: without the
table that is one query and today's composition, byte for byte. Its definition below is frozen, because it is part of
every such store's identity; a row carries ``format`` so that what a row holds can change without it."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3

from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.history_exports import HistoryContribution, PRIVATE_EXCLUDED, TableClassification
from qorqut_operations.history_exports.fields import NAME as HISTORY_TABLE_NAME
from qorqut_operations.storage.assembly import AssemblyError, StorageContribution
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.extensions import EXTENSION_ID

RECORD_TABLE = RECORD_OWNER = "extensiontables"
RECORD_DDL = ("CREATE TABLE extensiontables (id TEXT PRIMARY KEY NOT NULL, version TEXT NOT NULL, "
              "contribution TEXT NOT NULL)")
HISTORY_OWNER = "host.extension_tables"
FORMAT = 1
ROW_KEYS = frozenset({"id", "version", "contribution"})
CONTRIBUTION_KEYS = frozenset({"format", "statements", "tables", "immutable"})
SPEC_KEYS = frozenset({"keys", "scope_column", "mutable", "revision_column"})
# SQLite object names a statement may make as part of the one object it creates.
INTERNAL = re.compile(r"^sqlite_")
OBJECT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

_CREATE = {sqlite3.SQLITE_CREATE_TABLE: "table", sqlite3.SQLITE_CREATE_INDEX: "index",
           sqlite3.SQLITE_CREATE_TRIGGER: "trigger"}
_REFUSED = {
    sqlite3.SQLITE_SELECT: "a SELECT", sqlite3.SQLITE_ATTACH: "ATTACH", sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_PRAGMA: "PRAGMA", sqlite3.SQLITE_TRANSACTION: "a transaction", sqlite3.SQLITE_SAVEPOINT: "a savepoint",
    sqlite3.SQLITE_CREATE_VIEW: "a view", sqlite3.SQLITE_CREATE_TEMP_VIEW: "a view",
    sqlite3.SQLITE_CREATE_TEMP_TABLE: "a temporary table", sqlite3.SQLITE_CREATE_TEMP_INDEX: "a temporary index",
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER: "a temporary trigger", sqlite3.SQLITE_CREATE_VTABLE: "a virtual table",
    sqlite3.SQLITE_DELETE: "a delete", sqlite3.SQLITE_INSERT: "an insert", sqlite3.SQLITE_UPDATE: "an update",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE", sqlite3.SQLITE_REINDEX: "REINDEX", sqlite3.SQLITE_ANALYZE: "ANALYZE",
    sqlite3.SQLITE_RECURSIVE: "a recursive query"}


def _refused(ext_id, note):
    return DomainError("invalid_installation", f"The tables of {ext_id} are refused: {note}")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _objects(connection):
    return {(kind, name): table for kind, name, table in connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")}


def check_statements(ext_id, statements):
    """Check an extension's statements as described above; returns ``{(type, name): table}`` of what they create.

    Raises ``DomainError("invalid_installation")`` with a note naming the first refused statement. Nothing runs outside
    the disposable in-memory database, and a refused statement is stopped before it runs there too."""
    if not isinstance(ext_id, str) or not EXTENSION_ID.fullmatch(ext_id):
        raise DomainError("invalid_installation", "Tables need an extension id")
    statements = tuple(statements)
    if not statements:
        raise _refused(ext_id, "it declares no statement")
    prefix = ext_id + "_"
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        install_sql_functions(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        own: set[str] = set()
        for number, statement in enumerate(statements, 1):
            if not isinstance(statement, str) or not statement.strip():
                raise _refused(ext_id, f"statement {number} is not SQL text")
            named, problems = [], []

            def authorize(action, first, second, database, _trigger):
                tables = own | {name for kind, name in named if kind == "table"}
                if action in _CREATE and database == "main":
                    kind = _CREATE[action]
                    if INTERNAL.match(first or "") and (kind == "table" and first == "sqlite_sequence"
                                                        or kind == "index" and second in tables):
                        return sqlite3.SQLITE_OK
                    if not first.startswith(prefix) or not OBJECT_NAME.fullmatch(first):
                        problems.append(f"the {kind} {first} is not named {prefix}...")
                        return sqlite3.SQLITE_DENY
                    if kind != "table" and second not in tables:
                        problems.append(f"the {kind} {first} is on {second}, which is not a table of its own")
                        return sqlite3.SQLITE_DENY
                    named.append((kind, first))
                    return sqlite3.SQLITE_OK
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_READ) and database == "main" \
                        and first == "sqlite_master":
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and database == "main" and first in tables:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_REINDEX and database == "main" and ("index", first) in named:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION:
                    return sqlite3.SQLITE_OK
                problems.append(_refused_action(action, first, database))
                return sqlite3.SQLITE_DENY

            before = _objects(connection)
            connection.set_authorizer(authorize)
            try:
                connection.execute(statement)
            except (sqlite3.Error, sqlite3.Warning) as exc:
                raise _refused(ext_id, f"statement {number}: " + (problems[0] if problems else
                                                                  f"it does not run alone ({exc})")) from None
            finally:
                connection.set_authorizer(None)
            if problems:
                raise _refused(ext_id, f"statement {number}: {problems[0]}")
            added = _objects(connection).keys() - before.keys()
            if len(named) != 1 or added != set(named):
                raise _refused(ext_id, f"statement {number}: it does not create exactly the one object it names")
            kind, name = named[0]
            if kind == "table":
                if not HISTORY_TABLE_NAME.fullmatch(name):
                    raise _refused(ext_id, f"the table name {name} is longer than a table name may be")
                own.add(name)
        for table in sorted(own):
            referenced = {row[2] for row in connection.execute(f"PRAGMA foreign_key_list({_quoted(table)})")}
            if referenced - own:
                raise _refused(ext_id, f"the table {table} refers to {', '.join(sorted(referenced - own))}, "
                                       "which is not a table of its own")
        _check_triggers(ext_id, connection, own)
        return _objects(connection)
    finally:
        connection.close()


def _refused_action(action, first, database):
    if database == "temp":
        return "it holds a temporary object"
    if database not in (None, "main"):
        return f"it uses the database {database}"
    if action in _REFUSED:
        return "it holds " + _REFUSED[action]
    if first:
        return f"it uses {first}, which is not a table of its own"
    return "it holds a statement that is not allowed"


def _check_triggers(ext_id, connection, own):
    """Prepare, never run, every write to every own table, so each trigger body it fires is authorized as well."""
    problems = []

    def authorize(action, first, _second, database, trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_READ) \
                and database == "main" and first in own:
            return sqlite3.SQLITE_OK
        # A trigger body may select from its own tables (``SELECT RAISE(...) WHERE ...``); what it reads is checked above.
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        problems.append((trigger, _refused_action(action, first, database)))
        return sqlite3.SQLITE_DENY

    for table in sorted(own):
        name = _quoted(table)
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({name})")]
        writes = [f"EXPLAIN INSERT INTO {name} DEFAULT VALUES", f"EXPLAIN DELETE FROM {name}",
                  *(f"EXPLAIN UPDATE {name} SET {_quoted(column)} = {_quoted(column)}" for column in columns)]
        for write in writes:
            connection.set_authorizer(authorize)
            try:
                connection.execute(write).fetchall()
            except sqlite3.Error as exc:
                if problems:
                    trigger, note = problems[0]
                    raise _refused(ext_id, f"the trigger {trigger}: {note}" if trigger else
                                   f"a write to {table}: {note}") from None
                raise _refused(ext_id, f"a write to {table} does not prepare alone ({exc})") from None
            finally:
                connection.set_authorizer(None)


# -- the canonical record row ------------------------------------------------------------------------------------

def owner_of(ext_id):
    """The storage owner of an extension's recorded tables."""
    return f"extension.{ext_id}"


def row_of(ext_id, version, contributions):
    """The canonical record row of one extension: its id, the version that last changed its tables, and its storage
    contributions as one canonical JSON text (``format``, statements, table specifications, sorted immutable columns).

    Only tables, immutable columns and statements can be recorded; a contribution with validators, required lifecycles or
    required tables is refused."""
    if not isinstance(ext_id, str) or not EXTENSION_ID.fullmatch(ext_id):
        raise DomainError("invalid_installation", "Tables need an extension id")
    if not isinstance(version, str) or not version:
        raise _refused(ext_id, "it has no version")
    statements, tables, immutable = [], {}, {}
    for contribution in contributions:
        if type(contribution) is not StorageContribution:
            raise _refused(ext_id, "tables are declared as storage contributions")
        if contribution.validators or contribution.required_lifecycles or contribution.required_tables:
            raise _refused(ext_id, "a contribution with validators, required lifecycles or required tables cannot be "
                                   "recorded")
        if set(contribution.tables) & set(tables):
            raise _refused(ext_id, "a table is declared twice")
        statements.extend(contribution.schema)
        tables.update({name: {"keys": list(spec.keys), "scope_column": spec.scope_column, "mutable": spec.mutable,
                              "revision_column": spec.revision_column} for name, spec in contribution.tables.items()})
        immutable.update({name: sorted(fields) for name, fields in contribution.immutable_fields.items()})
    payload = {"format": FORMAT, "statements": statements, "tables": tables, "immutable": immutable}
    return {"id": ext_id, "version": version, "contribution": canonical_bytes(payload).decode("utf-8")}


def contribution_of(row):
    """The storage contribution of one record row, owned by ``extension.<id>``, after ``check_statements``.

    The row must be exactly what ``row_of`` writes: its text in canonical form, the known format and no other key.
    Raises ``DomainError("invalid_installation")`` otherwise."""
    try:
        values = {key: row[key] for key in ROW_KEYS}
    except (KeyError, IndexError, TypeError):
        raise DomainError("invalid_installation", "A record row holds an id, a version and a contribution") from None
    ext_id, version, text = values["id"], values["version"], values["contribution"]
    if not isinstance(ext_id, str) or not EXTENSION_ID.fullmatch(ext_id):
        raise DomainError("invalid_installation", "A record row needs an extension id")
    if not isinstance(version, str) or not version:
        raise _refused(ext_id, "its record has no version")
    if not isinstance(text, str):
        raise _refused(ext_id, "its record is not text")
    try:
        payload = json.loads(text, object_pairs_hook=_unique)
    except ValueError:
        raise _refused(ext_id, "its record is not valid JSON") from None
    if not isinstance(payload, dict) or set(payload) != CONTRIBUTION_KEYS:
        raise _refused(ext_id, "its record does not have the recorded form")
    if type(payload["format"]) is not int or payload["format"] != FORMAT:
        raise _refused(ext_id, "its record has a format this version does not read")
    if canonical_bytes(payload).decode("utf-8") != text:
        raise _refused(ext_id, "its record is not in canonical form")
    statements, tables, immutable = payload["statements"], payload["tables"], payload["immutable"]
    if not isinstance(statements, list) or not isinstance(tables, dict) or not isinstance(immutable, dict):
        raise _refused(ext_id, "its record does not have the recorded form")
    specs = {}
    for name, spec in tables.items():
        if (not isinstance(spec, dict) or set(spec) != SPEC_KEYS or not isinstance(spec["keys"], list)
                or not all(isinstance(key, str) for key in spec["keys"]) or type(spec["mutable"]) is not bool
                or not all(value is None or isinstance(value, str) for value in (spec["scope_column"],
                                                                                 spec["revision_column"]))):
            raise _refused(ext_id, f"the table specification of {name} does not have the recorded form")
        specs[name] = TableSpec(tuple(spec["keys"]), spec["scope_column"], spec["mutable"], spec["revision_column"])
    fields = {}
    for name, columns in immutable.items():
        if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns) \
                or columns != sorted(set(columns)):
            raise _refused(ext_id, f"the immutable columns of {name} do not have the recorded form")
        fields[name] = frozenset(columns)
    created = check_statements(ext_id, statements)
    missing = sorted(set(specs) - {name for kind, name in created if kind == "table"})
    if missing:
        raise _refused(ext_id, f"its statements do not create {', '.join(missing)}")
    try:
        return StorageContribution(owner_of(ext_id), specs, fields, tuple(statements))
    except AssemblyError as exc:
        raise _refused(ext_id, str(exc)) from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


# -- the record in the store -------------------------------------------------------------------------------------

def record_contribution():
    """The record table itself, composed first among the recorded contributions."""
    return StorageContribution(RECORD_OWNER, schema=(RECORD_DDL,))


def recorded(source):
    """The recorded contributions of a store: the record table, then one per extension in id order; ``()`` without it.

    ``source`` is an open connection to the store (read on it, as it is) or the store's path. A path is read as
    ``Database.open_existing`` reads it: a store that is a link, or crosses one, is refused the same way, and it is read
    over a read-only, query-only connection, which sees the pending WAL content and waits as ``open_existing`` does for a
    store another connection writes. A missing path gives ``()``, so the opener reports the missing store as it does
    today. Every recorded statement is checked before anything composes or compiles it; a row that fails the check, or
    is not exactly what ``row_of`` writes, refuses the store with ``invalid_database``."""
    if isinstance(source, sqlite3.Connection):
        return _recorded(source)
    path = Path(source).absolute()
    if not os.path.lexists(path):
        return ()
    if not path.is_file() or any(item.is_symlink() for item in (path, *path.parents)):
        raise DomainError("invalid_database", "Existing operational database is missing or crosses a symbolic link")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        return _recorded(connection)
    except sqlite3.Error as exc:
        raise DomainError("invalid_database", "Existing operational schema cannot be read") from exc
    finally:
        connection.close()


def _recorded(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (RECORD_TABLE,)).fetchone() is None:
        return ()
    contributions = [record_contribution()]
    for row in connection.execute(f"SELECT id, version, contribution FROM {RECORD_TABLE} ORDER BY id").fetchall():
        try:
            contributions.append(contribution_of({"id": row[0], "version": row[1], "contribution": row[2]}))
        except DomainError as exc:
            raise DomainError("invalid_database", "The store's record of extension tables is refused: "
                              + exc.message) from None
    return tuple(contributions)


def history(assembly):
    """The history classification of the record table and of every recorded table of ``assembly``: none of them is
    exported. ``()`` for an assembly composed without a record."""
    owners = [owner for owner, _ in assembly.schema]
    if RECORD_OWNER not in owners:
        return ()
    classifications = [TableClassification(RECORD_TABLE, PRIVATE_EXCLUDED, "Which tables each extension brought; "
                                                                           "kept for the store, not authored material.")]
    for owner, statements in assembly.schema[owners.index(RECORD_OWNER) + 1:]:
        ext_id = owner.removeprefix("extension.")
        classifications.extend(TableClassification(name, PRIVATE_EXCLUDED, f"A table of the extension {ext_id}; what it "
                                                   "holds is the extension's own and is not exported.")
                               for kind, name in sorted(check_statements(ext_id, statements)) if kind == "table")
    return (HistoryContribution(HISTORY_OWNER, classifications=tuple(classifications)),)
