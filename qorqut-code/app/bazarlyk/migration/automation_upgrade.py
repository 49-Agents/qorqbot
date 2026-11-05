"""Explicit offline upgrade that adds the scripting-and-automation extension's two tables to an existing store. Nothing here runs from the service.

    python -m bazarlyk.migration.automation_upgrade --source OLD.sqlite3 --destination NEW.sqlite3 \\
        --installation-config CONFIG.json [--dry-run]

The upgrade adds the two tables and widens one CHECK. ``CONFIG.json`` is the trusted installation configuration *with* the ``qorqut_automation.extension:create``
entry. The source must be one stopped store (no ``-wal``/``-shm`` sidecar, no hot journal) whose schema is **exactly** the composed target of
the same configuration *without* that entry, except that ``owner_conversations`` may still carry its previous ``source_type`` CHECK (one
value short of ``'automation'``, which the private owner-Ordubashy discussion of an Automation needs); anything else (another schema, an
extension's storage, an already upgraded store) is refused with a code and nothing written. The destination is a **new** file built in a
private temporary file beside it:

1. the source is copied with SQLite's backup interface from an immutable read-only connection;
2. in one transaction the extension's own DDL (``qorqut_automation.schema.SCHEMA``: ``automations``, ``automation_steps`` and their indexes)
   is created, ``owner_conversations`` is rebuilt from the target's exact DDL with every row kept (only when it carries the previous CHECK;
   its indexes and triggers are recreated, the same conversion ``project_sources`` performed for ``project``), and the stored target identity
   and ``user_version`` become the composed target's;
3. the schema must equal the composed target's compiled inventory, every pre-existing table must equal the source (count and every row in
   both directions), the new tables must be empty, and ``foreign_key_check`` and ``integrity_check`` must be clean;
4. the engine's own ``Database.open_existing`` validation accepts the file, which is then published by an exclusive hard link, so an existing
   destination can never be replaced. The source's SHA-256 is taken before and after and must be identical.

No row is changed or deleted (the rebuilt table is refilled and compared row for row), no routine, job or Signoff is created, and no automation is installed: the first automation is created by
Ordubashy's tools after the new release is selected. ``--dry-run`` does the same build and verification in a throwaway directory and
publishes nothing. The report carries counts, names and hashes only. Recovery is the owner's retained source file; this tool never restores it."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage.database import APPLICATION_ID, Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from bazarlyk.extensions import installed_selection
from bazarlyk.migration.project_sources import _rebuild_owner_conversations, _source_types
from bazarlyk.migration.crm_upgrade import (current_assembly, file_sha256, open_source, publish, read_only_uri, require_destination,
                                            require_source, require_source_unchanged, source_state)

FACTORY_PREFIX = "qorqut_automation."
CONVERSATIONS = ("table", "owner_conversations")


def _refuse(code, message, **details):
    return DomainError(code, message, details=details or None)


def split_configuration(configuration):
    """``(with automation, without automation)`` installation configurations; the entry must be selected."""
    entries = list((configuration or {}).get("extensions") or ())
    chosen = [entry for entry in entries if str(entry.get("factory", "")).startswith(FACTORY_PREFIX)]
    if len(chosen) != 1:
        raise _refuse("invalid_configuration", "The configuration must select qorqut_automation.extension:create exactly once")
    without = {**configuration, "extensions": [entry for entry in entries if entry not in chosen]}
    return configuration, without


def automation_statements():
    from qorqut_automation.schema import SCHEMA
    return SCHEMA


def legacy_objects(objects):
    """The inventory with ``owner_conversations`` as it was before ``'automation'`` joined its source CHECK, or ``None``."""
    owner, sql = objects[CONVERSATIONS]
    legacy = sql.replace(",'automation'", "", 1)
    types, before = _source_types(sql), _source_types(legacy)
    if legacy == sql or types is None or before is None or types != (*before, "automation"):
        return None
    return {**objects, CONVERSATIONS: (owner, legacy)}


def build(source, work, assembly, compiled, previous):
    """Create ``work`` as the upgraded store, verified, from the immutable ``source``. ``work`` is left on any failure.
    Returns ``(rows per carried table, whether owner_conversations was rebuilt)``."""
    target = assembly.target
    connection = sqlite3.connect(Path(work).absolute().as_uri() + "?mode=rwc", uri=True, isolation_level=None)
    try:
        install_sql_functions(connection)
        with sqlite3.connect(read_only_uri(source), uri=True) as original:
            original.backup(connection)
        observed = read_objects(connection)
        problems = differences(previous.objects, observed)
        rebuild = bool(problems)
        if rebuild:
            legacy = legacy_objects(previous.objects)
            problems = differences(legacy, observed) if legacy else problems
        if problems:
            raise _refuse("upgrade_source_unrecognized",
                          "The source is not the composed target without the automation extension: " + describe(problems))
        identity = [tuple(row) for row in connection.execute(f'SELECT name, version FROM "{IDENTITY_TABLE}"')]
        if identity != [(target.name, target.version)] or connection.execute("PRAGMA user_version").fetchone()[0] != target.version:
            raise _refuse("upgrade_source_unrecognized", "The source's stored target name or version is not this composition's")
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ATTACH DATABASE ? AS src", (read_only_uri(source),))
        connection.execute("BEGIN IMMEDIATE")
        for statement in automation_statements():
            connection.execute(statement)
        if rebuild:
            _rebuild_owner_conversations(connection, compiled.objects)
        connection.execute(f'DELETE FROM "{IDENTITY_TABLE}"')
        connection.execute(f'INSERT INTO "{IDENTITY_TABLE}" VALUES (\'target\', ?, ?, ?, ?)',
                           (target.name, target.version, target.digest, utc_now()))
        connection.execute(f"PRAGMA user_version = {target.version}")
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        problems = differences(compiled.objects, read_objects(connection))
        if problems:
            raise _refuse("upgrade_failed", "The upgraded schema differs from the composed target: " + describe(problems))
        rows = {}
        for (table,) in connection.execute("SELECT name FROM src.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            before = connection.execute(f'SELECT COUNT(*) FROM src."{table}"').fetchone()[0]
            missing = connection.execute(f'SELECT COUNT(*) FROM (SELECT * FROM src."{table}" EXCEPT SELECT * FROM main."{table}")').fetchone()[0]
            extra = connection.execute(f'SELECT COUNT(*) FROM (SELECT * FROM main."{table}" EXCEPT SELECT * FROM src."{table}")').fetchone()[0]
            after = connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
            if (before, missing, extra) != (after, 0, 0):
                raise _refuse("upgrade_failed", f"The upgraded rows of {table} differ from the source", table=table)
            rows[table] = after
        for table in ("automations", "automation_steps"):
            if connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]:
                raise _refuse("upgrade_failed", f"The new table {table} is not empty")
        if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise _refuse("upgrade_failed", "The upgraded store contains invalid references")
        connection.execute("COMMIT")
        if connection.execute("PRAGMA main.integrity_check").fetchone()[0] != "ok":
            raise _refuse("upgrade_failed", "The upgraded store failed its integrity check")
        return rows, rebuild
    except sqlite3.Error as exc:
        raise _refuse("upgrade_failed", f"The upgrade could not build the new store ({type(exc).__name__}: {exc})") from exc
    finally:
        connection.close()


def upgrade(source, destination, *, configuration, dry_run=False):
    """Upgrade ``source`` into the new ``destination`` (or only report, with ``dry_run``) and return a counts-only report."""
    with_automation, without = split_configuration(configuration)
    source, destination = require_source(source), require_destination(destination)
    assembly = current_assembly(with_automation)
    previous = compile_target(current_assembly(without))
    compiled = compile_target(assembly)
    before = source_state(source)
    connection = open_source(source)
    connection.close()
    if dry_run:
        directory = tempfile.TemporaryDirectory(prefix="automation-upgrade-")
        work, cleanup = Path(directory.name) / "dry-run.sqlite3", directory.cleanup
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".automation-upgrade-", suffix=".sqlite3", dir=destination.parent)
        os.close(descriptor)
        work, cleanup = Path(name), lambda: [Path(str(work) + suffix).unlink(missing_ok=True) for suffix in ("", "-journal", "-wal", "-shm")]
    try:
        rows, rebuilt = build(source, work, assembly, compiled, previous)
        Database(work, assembly=assembly).open_existing()
        require_source_unchanged(source, before)
        published = None
        if not dry_run:
            published = {"path": str(destination), "sha256": file_sha256(work), "bytes": work.stat().st_size}
            publish(work, destination)
    finally:
        cleanup()
    target = assembly.target
    return {"mode": "dry-run" if dry_run else "upgrade", "ok": True,
            "source": {"path": str(source), "sha256": before[-1], "unchanged": True},
            "target": {"name": target.name, "version": target.version, "digest": target.digest},
            "destination": published, "rows": rows, "new_tables": ["automation_steps", "automations"],
            "rebuilt_tables": ["owner_conversations"] if rebuilt else [],
            "checks": {"source_recognized": True, "schema": "equal", "rows": "identical", "foreign_keys": "ok", "integrity": "ok"}}


def main(argv=None):
    """Exit 0 on success, 2 on a refusal or failure (the error code and message on stderr). Prints the report as JSON."""
    parser = argparse.ArgumentParser(prog="python -m bazarlyk.migration.automation_upgrade", description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="the stopped database to upgrade (never modified)")
    parser.add_argument("--destination", required=True, help="a new path for the upgraded database (must not exist)")
    parser.add_argument("--installation-config", required=True, help="trusted installation JSON that selects the automation extension")
    parser.add_argument("--dry-run", action="store_true", help="build and verify in a throwaway directory; write nothing")
    arguments = parser.parse_args(argv)
    try:
        try:
            configuration = json.loads(Path(arguments.installation_config).read_text(encoding="utf-8"))
            if not isinstance(configuration, dict):
                raise ValueError("configuration must be an object")
        except (OSError, ValueError) as exc:
            raise _refuse("invalid_configuration", "Cannot read the installation configuration") from exc
        installed_selection(configuration)          # the selection itself must be valid before anything is built
        report = upgrade(arguments.source, arguments.destination, configuration=configuration, dry_run=arguments.dry_run)
    except DomainError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
