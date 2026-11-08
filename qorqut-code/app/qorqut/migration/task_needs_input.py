"""Explicit offline upgrade ``v1.3-task-needs-input-1``: widen the Task status CHECK to accept ``needs_input``.

    python -m qorqut.migration.task_needs_input inspect --instance-root ROOT [--component NAME=PATH] [--extensions-config FILE]
    python -m qorqut.migration.task_needs_input apply   --instance-root ROOT [--component NAME=PATH] [--extensions-config FILE]

Nothing here runs from the service: normal startup never migrates a store. ``inspect`` is strictly read-only. ``apply``
takes the one instance lifetime lock (so it refuses while the service runs) and does everything in a single
``BEGIN IMMEDIATE`` transaction on the installed Operations store, with SQLite foreign keys disabled before the
transaction and the connection's previous mode restored afterwards:

1. The store must be exactly this release's predecessor (the current host composition carrying the previous six-value
   Task CHECK, rebuilt in memory from the trusted Task contribution) or, on a repeat, exactly the target. Any other
   schema, a hand-edited store or a different extension selection is refused with the objects named. The stored
   ``user_version`` marker must be that composition's own as well: a store normal startup would reject for its version
   is refused here unchanged, in both the predecessor and the target state. A leftover Task stash is refused and left
   untouched rather than deleted.
2. SQLite cannot alter a CHECK, so the Task table is rebuilt from the target composition's own DDL: every column is
   stashed unchanged, the table is dropped and recreated, and the rows are copied back *before* its indexes and
   triggers are restored, so a copy never re-fires Done backfills or completion history. Its own and external triggers
   are then recreated from the target inventory; every other table is untouched. Nothing renames the table or writes
   the schema catalog directly.
3. The copied values are compared with the stash, ``foreign_key_check`` must be clean and the compiled inventory must
   equal the target object for object before the new identity digest is committed. Any failure rolls the whole
   transaction back, schema included.

A repeat against the completed target is a verified no-op. The command makes no backup of its own; a coordinator that
wants one holds the same instance lock around this call. It is never invoked against a live path."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions
from qorqut_operations.tasks.schema import STATES

from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership

TRANSFORM = "v1.3-task-needs-input-1"
OWNER = "tasks"
TABLE = "tasks"
STASH = TABLE + "__stash"
NEW_STATE = "needs_input"
TASK_TABLE = re.compile(r"^\s*CREATE\s+TABLE\s+tasks\s*\(", re.IGNORECASE)
STATUS_CHECK = re.compile(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", re.IGNORECASE)


def _refuse(message, details=None):
    raise DomainError("invalid_database", message, details=details)


def _status_values(sql):
    found = STATUS_CHECK.search(sql)
    if not found:
        return None
    return tuple(token.strip().strip("'") for token in found.group(1).split(",") if token.strip())


def _only_widens_status_check(old_sql, new_sql):
    """True exactly when the only difference is appending ``needs_input`` to the Task status CHECK list."""
    old_values, new_values = _status_values(old_sql), _status_values(new_sql)
    if old_values is None or new_values != (*old_values, NEW_STATE):
        return False
    stripped = STATUS_CHECK.sub("CHECK(status IN (...))", old_sql)
    return stripped == STATUS_CHECK.sub("CHECK(status IN (...))", new_sql)


def _predecessor_statements(statements):
    """The Tasks contribution's DDL with only ``needs_input`` removed from the Task status CHECK."""
    found = [statement for statement in statements if TASK_TABLE.match(statement)]
    if len(found) != 1:
        _refuse("The Tasks storage contribution does not define exactly one Task table")
    values = _status_values(found[0])
    if values != tuple(STATES) or values[-1] != NEW_STATE:
        _refuse("The composed Task DDL does not list exactly the central Task states, ending with " + NEW_STATE)
    old = values[:-1]
    previous = STATUS_CHECK.sub("CHECK(status IN (" + ",".join(f"'{state}'" for state in old) + "))", found[0], count=1)
    if not _only_widens_status_check(previous, found[0]):
        _refuse("The Task table changes by more than appending needs_input to its status CHECK")
    return tuple(previous if statement is found[0] else statement for statement in statements)


def compositions(extensions=NO_EXTENSIONS):
    """(predecessor, target): the trusted host composition without, and with, ``needs_input`` in the Task CHECK."""
    target = host_assembly(ChangeFeed(), extensions)
    composition, replaced = HostComposition(), 0
    for contribution in host_contributions(ChangeFeed(), extensions):
        if type(contribution) is StorageContribution and contribution.owner == OWNER:
            contribution = replace(contribution, schema=_predecessor_statements(contribution.schema))
            replaced += 1
        composition.add(contribution)
    if replaced != 1:
        _refuse("The host composition has no single Tasks storage contribution")
    predecessor = composition.freeze()
    if (predecessor.target.name, predecessor.target.version) != (target.target.name, target.target.version):
        _refuse("This conversion only widens the Task status CHECK; it does not change the target identity")
    return predecessor, target


def _pair(predecessor, target):
    """The compiled inventories, proved to differ only in the Task table's status CHECK."""
    before, after = compile_target(predecessor).objects, compile_target(target).objects
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if changed != {("table", TABLE)}:
        _refuse("The trusted compositions differ by more than the Task status CHECK: "
                + ", ".join(sorted(f"{kind} {name}" for kind, name in changed)))
    if not _only_widens_status_check(before[("table", TABLE)][1], after[("table", TABLE)][1]):
        _refuse("The Task table changes by more than appending needs_input to its status CHECK")
    return before, after


def recognise(connection, predecessor, target):
    """'predecessor' or 'target' when the store is exactly that composition; otherwise a refusal naming differences."""
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        _refuse("The database is not an Operations target store")
    before, after = _pair(predecessor, target)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    identity = [tuple(row) for row in connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"')]
    objects, problems = read_objects(connection), {}
    for label, assembly, expected in (("predecessor", predecessor, before), ("target", target, after)):
        wanted = assembly.target
        found = differences(expected, objects)
        if version != wanted.version:
            found = [*found, f"changed user_version (expected {wanted.version}, found {version})"]
        if identity == [(wanted.name, wanted.version, wanted.digest)] and not found:
            return label
        problems[label] = found
    raise DomainError("invalid_database", "The store is neither this release's predecessor schema nor its target schema",
                      details={"identity": [list(row[:2]) for row in identity], "user_version": version,
                               "predecessor": describe(problems["predecessor"]), "target": describe(problems["target"])})


def _stash_present(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (STASH,)).fetchone() is not None


def _state(connection, predecessor, target):
    """The recognised state of a store this command may act on; a leftover stash or another store is refused."""
    if _stash_present(connection):
        _refuse(f"A previous Task stash is present ({STASH}); it is left untouched for inspection")
    return recognise(connection, predecessor, target)


def _restore(connection, after_objects):
    """Recreate exactly the target's indexes and triggers on the Task table: its own and any external ones."""
    for (kind, _name), (table, sql) in sorted(after_objects.items()):
        if table == TABLE and kind != "table":
            connection.execute(sql)


def _rebuild(connection, after_objects):
    """Replace the Task table with the exact target DDL, keeping every row; return the copied row count."""
    create = after_objects[("table", TABLE)][1]
    if not create.lstrip().upper().startswith("CREATE TABLE"):
        _refuse("The target composition does not define the Task table")
    if _status_values(create) != tuple(STATES):
        _refuse("The target Task table does not list exactly the central Task states")
    columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{TABLE}")'))
    if not columns:
        _refuse("The predecessor store has no Task table to rebuild")
    connection.execute(f'CREATE TABLE "{STASH}" AS SELECT * FROM "{TABLE}"')
    stashed = connection.execute(f'SELECT count(*) FROM "{STASH}"').fetchone()[0]
    connection.execute(f'DROP TABLE "{TABLE}"')
    connection.execute(create)
    if columns != tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{TABLE}")')):
        _refuse("The Task table columns changed; this conversion only widens its status CHECK")
    names = ", ".join(f'"{name}"' for name in columns)
    connection.execute(f'INSERT INTO "{TABLE}" ({names}) SELECT {names} FROM "{STASH}"')
    copied = connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    # Compared before the table's own triggers exist again: the copy must never re-derive a stored value.
    differs = sum(connection.execute(f'SELECT count(*) FROM (SELECT * FROM "{source}" EXCEPT SELECT * FROM "{other}")'
                                     ).fetchone()[0] for source, other in ((STASH, TABLE), (TABLE, STASH)))
    if differs or copied != stashed:
        _refuse("The copied Task rows differ from the stashed rows")
    _restore(connection, after_objects)
    connection.execute(f'DROP TABLE "{STASH}"')
    return copied


def _advance(connection, target):
    updated = connection.execute(f'UPDATE "{IDENTITY_TABLE}" SET name=?, version=?, digest=?',
                                 (target.target.name, target.target.version, target.target.digest)).rowcount
    if updated != 1:
        _refuse("The stored target identity could not be advanced")
    connection.execute(f"PRAGMA user_version = {target.target.version}")


def inspect(connection, extensions=NO_EXTENSIONS):
    """Read-only: what ``apply`` would do. Safe on a live store."""
    predecessor, target = compositions(extensions)
    if _stash_present(connection):
        return {"transform": TRANSFORM, "store": "refused", "stash": STASH, "ready": False}
    state = recognise(connection, predecessor, target)
    tasks = connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
    return {"transform": TRANSFORM, "store": state, "tasks": tasks, "rebuild": state == "predecessor", "ready": True}


def apply(connection, extensions=NO_EXTENSIONS):
    """The whole upgrade in one transaction on a read-write connection opened with ``isolation_level=None``. The caller
    must hold exclusive instance ownership. Idempotent; a refusal or failure changes nothing."""
    predecessor, target = compositions(extensions)
    after_objects = compile_target(target).objects
    install_sql_functions(connection)
    connection.row_factory = sqlite3.Row
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = _state(connection, predecessor, target)
            tasks = connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
            if state == "predecessor":
                _rebuild(connection, after_objects)
                _advance(connection, target)
            found = differences(after_objects, read_objects(connection))
            if found:
                _refuse("The upgraded schema differs from the composed target", details={"differences": describe(found)})
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                _refuse("The upgrade produced invalid references")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    finally:
        connection.execute(f"PRAGMA foreign_keys = {1 if foreign_keys else 0}")
    return {"transform": TRANSFORM, "schema": "widened" if state == "predecessor" else "already_current",
            "tasks": tasks, "target_digest": target.target.digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("inspect", "read-only recognition of the Task schema and its row count"),
                       ("apply", "widen the Task status CHECK under the exclusive instance lock")):
        command = commands.add_parser(name, help=text)
        add_instance_arguments(command)
        command.add_argument("--extensions-config", type=Path, default=None, help="Explicit extension selection (trusted config format)")
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        extensions = NO_EXTENSIONS
        if options.extensions_config is not None:
            from qorqut.extensions import select
            from qorqut.installation import extensions_configuration
            extensions = select(extensions_configuration(options.extensions_config)["extensions"])
        database = Path(paths.database)
        if options.command == "inspect":
            connection = sqlite3.connect(database.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
            try:
                install_sql_functions(connection)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only = ON")
                result = inspect(connection, extensions)
            finally:
                connection.close()
        else:
            ownership = InstanceOwnership(paths).acquire()
            try:
                connection = sqlite3.connect(database.resolve(strict=True), isolation_level=None, timeout=30)
                try:
                    result = apply(connection, extensions)
                finally:
                    connection.close()
            finally:
                ownership.release()
        response, status = {"ok": True, "result": result}, 0
    except DomainError as error:
        response, status = {"ok": False, "error": error.to_dict()}, 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        response, status = {"ok": False, "error": {"code": "invalid_request", "message": str(error), "retryable": False}}, 1
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())