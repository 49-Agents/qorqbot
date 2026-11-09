"""Offline source upgrade for Project conversation sources and explicit Project links.

Narrow, explicitly callable and transaction-safe. It is never called by startup, a service command, a release
worker, an operation, a test fixture or deployment: later integration work qualifies the concrete combined artifact.
It takes the *before* and *after* composed ``Assembly`` values, proves the store really is the before target, rebuilds
only the objects this feature changes, rewrites the target identity digest to the after target, and verifies the
result against the after composition. Private conversation keys, IDs, peer bindings, revisions, message sequences,
read cursors, deliveries and histories are read before and compared after; any difference rolls the whole unit back.

This is v1.3's own composed-schema conversion. It imports no v1.2 release machinery and executes no SQL against a
live installation.

The changed objects are deliberately limited to:

* ``owner_conversations``: its ``source_type`` CHECK is widened to accept ``project``. The table is rebuilt (SQLite
  cannot alter a CHECK), its rows are copied unchanged, and its indexes/triggers are recreated from the after
  assembly;
* ``project_item_links`` and its index: created exactly once, owned by the after composition.

Any other difference between the two compositions, or a store whose catalog is not exactly the before composition,
is refused before a row is copied.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import Assembly
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

REBUILT = "owner_conversations"
CREATED = "project_item_links"
STASH = REBUILT + "__stash"
PRIVATE_TABLES = ("owner_conversations", "owner_conversation_messages", "owner_conversation_reads",
                  "inbox_deliveries", "change_sequences")


def _refuse(message):
    raise DomainError("invalid_database", message)


def _private_snapshot(connection):
    """Every private conversation fact this conversion must preserve, read in one fixed order."""
    return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
            for table in PRIVATE_TABLES if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()}


def _changed_objects(before, after):
    """The compiled objects this conversion is allowed to change, by name only."""
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


SOURCE_CHECK = re.compile(r"CHECK\s*\(\s*source_type\s+IN\s*\(([^)]*)\)\s*\)", re.IGNORECASE)


def _source_types(sql):
    found = SOURCE_CHECK.search(sql)
    if not found:
        return None
    return tuple(token.strip().strip("'") for token in found.group(1).split(",") if token.strip())


def _only_widens_source_check(old_sql, new_sql):
    """True exactly when the only difference is adding ``project`` to the source_type CHECK list."""
    old_types, new_types = _source_types(old_sql), _source_types(new_sql)
    if old_types is None or new_types is None:
        return False
    if new_types != (*old_types, "project"):
        return False
    stripped = SOURCE_CHECK.sub("CHECK(source_type IN (...))", old_sql)
    return stripped == SOURCE_CHECK.sub("CHECK(source_type IN (...))", new_sql)


def _check_scope(connection, before, after):
    before_objects, after_objects = compile_target(before).objects, compile_target(after).objects
    changed = _changed_objects(before_objects, after_objects)
    added = {key for key in changed if key not in before_objects
             and after_objects.get(key, (None, ""))[0] == CREATED}
    allowed = {("table", REBUILT), ("table", CREATED)} | added
    if changed - allowed:
        _refuse("This conversion only widens owner_conversations and adds project_item_links; unexpected schema "
                "difference: " + ", ".join(sorted(f"{kind} {name}" for kind, name in changed - allowed)))
    if ("table", CREATED) not in after_objects:
        _refuse("The after composition does not own the project_item_links table")
    if ("table", CREATED) in before_objects:
        _refuse("The before composition already owns project_item_links; nothing to create")
    if ("table", REBUILT) not in after_objects or ("table", REBUILT) not in before_objects:
        _refuse("Both compositions must own owner_conversations")
    if not _only_widens_source_check(before_objects[("table", REBUILT)][1], after_objects[("table", REBUILT)][1]):
        _refuse("owner_conversations changes by more than adding 'project' to its source CHECK")
    actual = read_objects(connection)
    problems = differences(before_objects, actual)
    if problems:
        _refuse("The store is not the before composition: " + "; ".join(problems[:8]))
    return before_objects, after_objects


def _identity(connection):
    row = connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"').fetchone()
    return tuple(row) if row is not None else None


def _rebuild_owner_conversations(connection, after_objects):
    """Replace the table with the exact after DDL, keeping every row in place.

    SQLite cannot alter a CHECK. The stored definition must end up byte-identical to the after composition, so the
    table is stashed, dropped, recreated from the after statement itself and refilled; the stash carries only rows.
    """
    old_columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{REBUILT}")'))
    create = after_objects[("table", REBUILT)][1]
    if not create.lstrip().upper().startswith("CREATE TABLE"):
        _refuse("The after composition does not define owner_conversations as a table")
    connection.execute(f'DROP TABLE IF EXISTS "{STASH}"')
    connection.execute(f'CREATE TABLE "{STASH}" AS SELECT * FROM "{REBUILT}"')
    stashed = connection.execute(f'SELECT count(*) FROM "{STASH}"').fetchone()[0]
    connection.execute(f'DROP TABLE "{REBUILT}"')
    connection.execute(create)
    new_columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{REBUILT}")'))
    if old_columns != new_columns:
        _refuse("owner_conversations columns changed; this conversion only widens its source CHECK")
    columns = ", ".join(f'"{name}"' for name in old_columns)
    connection.execute(f'INSERT INTO "{REBUILT}" ({columns}) SELECT {columns} FROM "{STASH}"')
    copied = connection.execute(f'SELECT count(*) FROM "{REBUILT}"').fetchone()[0]
    connection.execute(f'DROP TABLE "{STASH}"')
    if stashed != copied:
        _refuse("owner_conversations row count changed during the rebuild")
    _recreate(connection, after_objects, owner=REBUILT, tables=False)


def _create_links(connection, after_objects):
    _recreate(connection, after_objects, owner=CREATED, tables=True)
    _recreate(connection, after_objects, owner=CREATED, tables=False)


def _recreate(connection, after_objects, *, owner, tables):
    """Recreate exactly the after objects owned by one table: the table first, then its indexes and triggers."""
    ordered = sorted(after_objects.items(), key=lambda item: (item[0][0] != "table", item[0][1]))
    for (kind, _name), (table, sql) in ordered:
        if table == owner and (kind == "table") == tables:
            connection.execute(sql)


def _verify(connection, after_objects, snapshot):
    problems = differences(after_objects, read_objects(connection))
    if problems:
        _refuse("The converted schema is not the after composition: " + "; ".join(problems[:8]))
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        _refuse("The converted schema produced invalid references")
    if _private_snapshot(connection) != snapshot:
        _refuse("Private conversation rows, sequences, deliveries or history changed during the conversion")


def upgrade(path, *, before, after):
    """Convert one existing store from ``before`` to ``after`` in a single transaction; return a summary.

    The whole conversion is one ``BEGIN IMMEDIATE`` unit. A refusal or failure rolls back every statement, so the
    store is left exactly as it was. Running it against a store that is not the before composition is refused.
    """
    if not isinstance(before, Assembly) or not isinstance(after, Assembly):
        _refuse("The conversion needs the before and after compositions")
    if before.target.version != after.target.version:
        _refuse("The conversion only widens the composed schema; it does not change the target version")
    if not isinstance(path, (str, Path)) or not Path(path).is_file():
        _refuse("The conversion needs an existing target store; it never creates one")
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        install_sql_functions(connection)
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            _refuse("Existing database is not an Operations target store")
        if connection.execute("PRAGMA user_version").fetchone()[0] != before.target.version:
            _refuse("The store's schema version is not the before composition")
        if _identity(connection) != (before.target.name, before.target.version, before.target.digest):
            _refuse("The store's target identity is not the before composition")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            before_objects, after_objects = _check_scope(connection, before, after)
            snapshot = _private_snapshot(connection)
            _rebuild_owner_conversations(connection, after_objects)
            _create_links(connection, after_objects)
            connection.execute(
                f'UPDATE "{IDENTITY_TABLE}" SET name=?, version=?, digest=? ',
                (after.target.name, after.target.version, after.target.digest))
            connection.execute(f"PRAGMA user_version = {after.target.version}")
            _verify(connection, after_objects, snapshot)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error as exc:
        _refuse(f"The conversion could not complete: {exc}")
    finally:
        connection.close()
    return {"rebuilt": REBUILT, "created": CREATED,
            "conversations": len(snapshot[REBUILT]), "target_digest": after.target.digest,
            "target_version": after.target.version}


__all__ = ["CREATED", "REBUILT", "upgrade"]