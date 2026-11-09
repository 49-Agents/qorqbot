"""Offline browser-cleanup enrollment upgrade; never run by normal startup.

python -m qorqut.reminders.defaults_upgrade inspect|apply --instance-root ROOT
An exact predecessor or already-current target is required. Apply holds the
instance lock, adds only the enrollment table/triggers and updates the schema
identity atomically. Enrollment happens later through the owned inbox writer.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.inventory import IDENTITY_TABLE
from qorqut_operations.storage.sqlfunctions import install_sql_functions
from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership
from . import defaults
from .upgrade import recognise


def compositions(extensions=NO_EXTENSIONS):
    predecessor = HostComposition()
    removed = 0
    for contribution in host_contributions(ChangeFeed(), extensions):
        if isinstance(contribution, StorageContribution) and contribution.owner == defaults.OWNER:
            removed += 1
        else:
            predecessor.add(contribution)
    if removed != 1:
        raise DomainError("invalid_installation", "Expected one browser cleanup storage contribution")
    return predecessor.freeze(), host_assembly(ChangeFeed(), extensions)


def inspect(connection, extensions=NO_EXTENSIONS):
    state = recognise(connection, *compositions(extensions))
    return {"store": state, "tables_to_create": [defaults.TABLE] if state == "predecessor" else []}


def apply(connection, extensions=NO_EXTENSIONS):
    """Caller holds instance ownership; any failure rolls back schema and data."""
    predecessor, target = compositions(extensions)
    install_sql_functions(connection)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        state = recognise(connection, predecessor, target)
        if state == "predecessor":
            for statement in defaults.SCHEMA:
                connection.execute(statement)
            updated = connection.execute(f'UPDATE "{IDENTITY_TABLE}" SET digest=? WHERE id=\'target\'',
                                         (target.target.digest,)).rowcount
            if updated != 1:
                raise DomainError("invalid_database", "The target identity could not be advanced")
        if recognise(connection, predecessor, target) != "target":
            raise DomainError("invalid_database", "Upgrade did not reach the exact target")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise DomainError("invalid_database", "The store contains invalid references")
        connection.execute("COMMIT")
        return {"schema": "created" if state == "predecessor" else "already_current"}
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "apply"))
    add_instance_arguments(parser)
    parser.add_argument("--extensions-config", type=Path)
    args = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(args, required=())
        extensions = NO_EXTENSIONS
        if args.extensions_config:
            from qorqut.extensions import select
            from qorqut.installation import extensions_configuration
            extensions = select(extensions_configuration(args.extensions_config)["extensions"])
        ownership = InstanceOwnership(paths).acquire() if args.command == "apply" else None
        try:
            mode = "rw" if ownership else "ro"
            with closing(sqlite3.connect(Path(paths.database).resolve(strict=True).as_uri() + f"?mode={mode}",
                                         uri=True, isolation_level=None, timeout=30)) as connection:
                install_sql_functions(connection)
                connection.row_factory = sqlite3.Row
                if not ownership:
                    connection.execute("PRAGMA query_only=ON")
                result = (apply if ownership else inspect)(connection, extensions)
        finally:
            if ownership:
                ownership.release()
        print(json.dumps({"ok": True, "result": result}))
        return 0
    except DomainError as error:
        print(json.dumps({"ok": False, "error": error.to_dict()}))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
