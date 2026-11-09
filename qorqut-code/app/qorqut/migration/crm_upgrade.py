"""Explicit offline upgrade of a pre-CRM Operations store into a new CRM-capable store. Nothing here runs from the service.

    python -m qorqut.migration.crm_upgrade --source OLD.sqlite3 --destination NEW.sqlite3 [--dry-run]

The upgrade reads one stopped, pre-CRM store (the ``qorqut-v1.2`` target this release replaced) and writes a **new**
destination file for the current composed target, which now carries the CRM Domain's tables and the widened shared
discussion/private-conversation sources. It never touches the source, never writes to an existing path and has no startup
hook, default path or network effect.

Recognition is by identity, not by guesswork. The source must carry the Operations application id, the pre-CRM target name,
version and digest, and exactly the schema objects of the frozen fingerprint ``crm_baseline_fingerprint.json`` (every table,
index and trigger with its owner table and the SHA-256 of its stored definition; no definition text or row data is kept
there). Anything else - another schema, a hand-edited store, an extension's storage, an already upgraded store - is refused
with a code, the objects named and nothing written. A source with a ``-wal``/``-shm`` file or a hot rollback journal is active
and is refused: stop the service so SQLite checkpoints and removes them, or upgrade a standalone backup.

The target schema is never maintained here. It is whatever ``host_assembly`` composes, which imports the Domain packages' own
declarations; the comparison of that compiled inventory with the fingerprint is what decides which tables are new, which
are rebuilt (their stored definition changed, as for the shared source constraints that now admit a Table) and which columns
were added (existing rows take the column's declared default: an existing Table is a general Table). A source table or
column the target no longer has is refused rather than dropped.

The destination is built in a private temporary file beside it, in one transaction, with the source attached read-only and
immutable:

1. the target tables are created, then the rows of every source table are copied by column name (row ids, including those of
   ``AUTOINCREMENT`` tables and their next-id counters, are preserved byte for byte), then the target's seed rows are added
   where absent;
2. only afterwards are the indexes and triggers created, so the copy fires no trigger and writes no change-feed row of its
   own: ``inbox_changes`` is carried as it was;
3. the target identity, ``user_version`` and application id are written, the compiled inventory must equal the stored one,
   every copied table must equal its source (count and ``rowid`` + column content in both directions) and ``foreign_key_check``
   and ``integrity_check`` must be clean;
4. the engine's own ``Database.open_existing`` validation (identity, inventory, plugin validators) accepts the file, which is
   then given owner-only permissions and published by an exclusive hard link, so an existing destination can never be replaced.

Any failure removes the temporary file and leaves nothing at the destination. ``--dry-run`` performs the same build and
verification in a throwaway directory and publishes nothing. The report carries counts, names and hashes only - never a row.
The source file's SHA-256 is taken before and after and must be identical.

Upgrading creates no dataset, links no contact, converts no row to a Task and writes no Contextbase record. Running it twice
with the same destination is refused (existing destination); feeding it its own output is refused (already current). The
output is a plain database file: the operational-history store, document files and backups of the new instance are set up by
the owner's reviewed installation steps (see ``docs/CRM-UPGRADE.md``), not here."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage.database import APPLICATION_ID, Database
from qorqut_operations.storage.inventory import IDENTITY_DDL, IDENTITY_TABLE, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import host_assembly
from qorqut.extensions import installed_selection
from qorqut.service.changes import ChangeFeed

FINGERPRINT = Path(__file__).with_name("crm_baseline_fingerprint.json")
RELEASE_FINGERPRINTS = Path(__file__).with_name("crm_release_fingerprints.json")
STATEMENT = re.compile(r"^\s*(?:(CREATE)\s+(?:UNIQUE\s+)?(TABLE|INDEX|TRIGGER)|(INSERT)\s+INTO)\b", re.IGNORECASE)
INSERT_INTO = re.compile(r"^\s*INSERT\s+INTO\b", re.IGNORECASE)
SIDECARS = ("-wal", "-shm")
JOURNAL = "-journal"


def _refuse(code, message, **details):
    return DomainError(code, message, details=details or None)


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def baseline():
    """The frozen pre-CRM fingerprint: ``{"target": {...}, "objects": {(type, name): (owner, sha256)}}``."""
    with FINGERPRINT.open(encoding="utf-8") as handle:
        stored = json.load(handle)
    objects = {tuple(key.split(":", 1)): tuple(value) for key, value in stored["objects"].items()}
    return {"target": stored["target"], "objects": objects}


def current_assembly(configuration=None):
    return host_assembly(ChangeFeed(), installed_selection(configuration or {}))


def recognized_baseline(connection, target, extensions):
    """Recognize only frozen source inventories with the same explicitly selected extensions."""
    candidates = [baseline()] if not extensions else []
    for stored in json.loads(RELEASE_FINGERPRINTS.read_text(encoding="utf-8"))["baselines"]:
        if sorted(extensions) in [stored["extensions"], *stored.get('equivalent_extension_sets', [])]:
            candidates.append({"target": stored["target"], "objects": {
                tuple(key.split(":", 1)): tuple(value) for key, value in stored["objects"].items()}})
    for frozen in candidates:
        try:
            recognize(connection, frozen, target)
            return frozen
        except DomainError as exc:
            if exc.code != "upgrade_source_unrecognized":
                raise
    raise _refuse("upgrade_source_unrecognized", "The source does not match a frozen pre-CRM schema for the selected extensions")


# ----- source ---------------------------------------------------------------------------------------------------------


def require_source(source):
    source = Path(source).absolute()
    if not source.is_file() or any(path.is_symlink() for path in (source, *source.parents)):
        raise _refuse("upgrade_source_invalid", "The source database is missing, not a regular file or crosses a symbolic link")
    active = [suffix for suffix in SIDECARS if os.path.lexists(str(source) + suffix)]
    journal = Path(str(source) + JOURNAL)
    if journal.exists() and journal.stat().st_size > 0:
        active.append(JOURNAL)
    if active:
        raise _refuse("upgrade_source_active", "The source database is in use or was not closed cleanly (" + ", ".join(active)
                      + " present); stop the service so SQLite checkpoints it, or upgrade a standalone backup", present=sorted(active))
    return source


def require_destination(destination):
    destination = Path(destination).absolute()
    if any(os.path.lexists(str(destination) + suffix) for suffix in ("", *SIDECARS, JOURNAL)):
        raise _refuse("upgrade_destination_exists", "The destination already exists; the upgrade only creates a new file")
    ancestor = destination.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if any(path.is_symlink() for path in (ancestor, *ancestor.parents)) or not ancestor.is_dir():
        raise _refuse("upgrade_destination_invalid", "The destination crosses a symbolic link or has no directory to live in")
    return destination


def read_only_uri(path):
    return Path(path).absolute().as_uri() + "?mode=ro&immutable=1"


def open_source(source):
    try:
        connection = sqlite3.connect(read_only_uri(source), uri=True)
        install_sql_functions(connection)
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        raise _refuse("upgrade_source_invalid", "The source database cannot be opened") from exc
    return connection


def recognize(connection, frozen, target):
    """Raise unless the open source is exactly the frozen pre-CRM target store."""
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise _refuse("upgrade_source_unrecognized", "The source is not an Operations target store")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if IDENTITY_TABLE not in tables:
            raise _refuse("upgrade_source_unrecognized", "The source has no target identity")
        identity = [tuple(row) for row in connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"')]
        if identity == [(target.name, target.version, target.digest)]:
            raise _refuse("upgrade_already_current", "The source is already a store of the current schema; there is nothing to upgrade")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        expected = frozen["target"]
        if version != expected["version"] or identity != [(expected["name"], expected["version"], expected["digest"])]:
            raise _refuse("upgrade_source_unrecognized", "The source is not the pre-CRM target this upgrade was prepared for")
        actual = {key: (owner, _digest(sql)) for key, (owner, sql) in read_objects(connection).items()}
        problems = differences(frozen["objects"], actual)
        if problems:
            raise _refuse("upgrade_source_unrecognized", "The source schema differs from the pre-CRM fingerprint: " + describe(problems),
                          differences=problems[:8])
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _refuse("upgrade_source_invalid", "The source contains invalid references")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise _refuse("upgrade_source_invalid", "The source failed its integrity check")
    except sqlite3.Error as exc:
        raise _refuse("upgrade_source_invalid", "The source database cannot be read") from exc
    return actual


# ----- plan -----------------------------------------------------------------------------------------------------------


def plan(connection, frozen, compiled, assembly):
    """What the upgrade will do, from the frozen fingerprint against the compiled target. Refuses a lossy change."""
    before, after = frozen["objects"], compiled.objects
    source_tables = {name for kind, name in before if kind == "table"}
    target_tables = {name for kind, name in after if kind == "table"}
    dropped = sorted(source_tables - target_tables)
    if dropped:
        raise _refuse("upgrade_incompatible", "The target no longer has source tables: " + ", ".join(dropped), tables=dropped)
    carried = sorted(source_tables - {IDENTITY_TABLE})
    columns, added, lost = {}, {}, []
    for table in carried:
        columns[table] = tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
        lost += [f"{table}.{name}" for name in columns[table] if name not in compiled.columns[table]]
        new = [name for name in compiled.columns[table] if name not in columns[table]]
        if new:
            added[table] = new
    if lost:
        raise _refuse("upgrade_incompatible", "The target no longer has source columns: " + ", ".join(lost), columns=lost)
    rebuilt = sorted(table for table in carried if _digest(after[("table", table)][1]) != before[("table", table)][1])
    seeds = [statement for _owner, statements in assembly.schema for statement in statements if INSERT_INTO.match(statement)]
    return {
        "columns": columns, "carried": carried,
        "new_tables": sorted(target_tables - source_tables),
        "rebuilt_tables": rebuilt, "added_columns": added,
        "new_indexes": sorted(name for kind, name in after.keys() - before.keys() if kind == "index"),
        "new_triggers": sorted(name for kind, name in after.keys() - before.keys() if kind == "trigger"),
        "changed_indexes": sorted(name for kind, name in after.keys() & before.keys()
                                  if kind == "index" and _digest(after[(kind, name)][1]) != before[(kind, name)][1]),
        "changed_triggers": sorted(name for kind, name in after.keys() & before.keys()
                                   if kind == "trigger" and _digest(after[(kind, name)][1]) != before[(kind, name)][1]),
        "removed_objects": sorted(f"{kind}:{name}" for kind, name in before.keys() - after.keys() if name != IDENTITY_TABLE),
        "seeds": seeds,
    }


# ----- build and verification -----------------------------------------------------------------------------------------


def _classified(assembly):
    tables, others, seeds = [], [], []
    for _owner, statements in assembly.schema:
        for statement in statements:
            matched = STATEMENT.match(statement)
            if matched is None:
                raise _refuse("upgrade_failed", "The composed schema holds a statement this upgrade does not understand")
            if matched.group(3):
                seeds.append(statement)
            elif matched.group(2).upper() == "TABLE":
                tables.append(statement)
            else:
                others.append(statement)
    return tables, others, seeds


def _quoted(names):
    return ", ".join(f'"{name}"' for name in names)


def backfill_latest_completion(connection):
    """Backfill old Done Tasks from retained structured transitions, else creation time.

    The first-completion table cannot establish the latest completion after reopening.
    Creation time is a stable ordering fallback, not a claimed completion timestamp.
    This runs before target triggers are installed and changes no copied source column.
    """
    counts = {"history": 0, "fallback": 0}
    for rowid, business_id, task_id, created_at in connection.execute(
            "SELECT rowid,business_id,id,created_at FROM tasks WHERE status='done'").fetchall():
        history = connection.execute(
            "SELECT MAX(created_at) FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id=? "
            "AND kind='system' AND json_extract(change_json,'$.to')='done' "
            "AND json_extract(change_json,'$.from') IS NOT NULL "
            "AND json_extract(change_json,'$.from')!='done'", (business_id, task_id)).fetchone()[0]
        counts["history" if history else "fallback"] += 1
        connection.execute("UPDATE tasks SET latest_completed_at=? WHERE rowid=?", (history or created_at, rowid))
    return counts


def build(source, work, assembly, compiled, steps):
    """Create ``work`` as the upgraded store, verified, from the (immutable) ``source``. ``work`` is left on any failure."""
    target = assembly.target
    tables, others, seeds = _classified(assembly)
    connection = sqlite3.connect(Path(work).absolute().as_uri() + "?mode=rwc", uri=True, isolation_level=None)
    try:
        install_sql_functions(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ATTACH DATABASE ? AS src", (read_only_uri(source),))
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(IDENTITY_DDL)
        for statement in tables:
            connection.execute(statement)
        for table in steps["carried"]:
            names = _quoted(steps["columns"][table])
            connection.execute(f'INSERT INTO main."{table}" (rowid, {names}) SELECT rowid, {names} FROM src."{table}" ORDER BY rowid')
        backfill = None
        if "latest_completed_at" in steps["added_columns"].get("tasks", ()):
            backfill = backfill_latest_completion(connection)
        for statement in seeds:
            connection.execute(INSERT_INTO.sub("INSERT OR IGNORE INTO", statement, count=1))
        # The copy itself leaves a zero counter for every empty AUTOINCREMENT table; the source's counters replace all of them.
        connection.execute("DELETE FROM main.sqlite_sequence")
        if connection.execute("SELECT 1 FROM src.sqlite_master WHERE name='sqlite_sequence'").fetchone():
            for name, value in connection.execute("SELECT name, seq FROM src.sqlite_sequence").fetchall():
                if name in steps["carried"]:
                    connection.execute("INSERT INTO main.sqlite_sequence (name, seq) VALUES (?, ?)", (name, value))
        for statement in others:
            connection.execute(statement)
        connection.execute(f'INSERT INTO "{IDENTITY_TABLE}" VALUES (\'target\', ?, ?, ?, ?)',
                           (target.name, target.version, target.digest, utc_now()))
        connection.execute(f"PRAGMA user_version = {target.version}")
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        problems = differences(compiled.objects, read_objects(connection))
        if problems:
            raise _refuse("upgrade_failed", "The upgraded schema differs from the composed target: " + describe(problems))
        rows = verify_rows(connection, steps)
        if connection.execute("PRAGMA main.foreign_key_check").fetchone() is not None:
            raise _refuse("upgrade_failed", "The upgraded store contains invalid references")
        connection.execute("COMMIT")
        if connection.execute("PRAGMA main.integrity_check").fetchone()[0] != "ok":
            raise _refuse("upgrade_failed", "The upgraded store failed its integrity check")
        return rows, backfill
    except sqlite3.Error as exc:
        raise _refuse("upgrade_failed", f"The upgrade could not build the new store ({type(exc).__name__}: {exc})") from exc
    finally:
        connection.close()


def verify_rows(connection, steps):
    """Every carried table equals its source - count, ``rowid`` and every copied column - and the id counters match."""
    counts = {}
    for table in steps["carried"]:
        names = _quoted(("rowid", *steps["columns"][table]))
        before = connection.execute(f'SELECT COUNT(*) FROM src."{table}"').fetchone()[0]
        after = connection.execute(f'SELECT COUNT(*) FROM main."{table}"').fetchone()[0]
        missing = connection.execute(f'SELECT COUNT(*) FROM (SELECT {names} FROM src."{table}" EXCEPT SELECT {names} FROM main."{table}")').fetchone()[0]
        extra = connection.execute(f'SELECT COUNT(*) FROM (SELECT {names} FROM main."{table}" EXCEPT SELECT {names} FROM src."{table}")').fetchone()[0]
        if (before, missing, extra) != (after, 0, 0):
            raise _refuse("upgrade_failed", f"The upgraded rows of {table} differ from the source", table=table)
        counts[table] = after
    if connection.execute("SELECT 1 FROM src.sqlite_master WHERE name='sqlite_sequence'").fetchone():
        before = sorted(connection.execute("SELECT name, seq FROM src.sqlite_sequence").fetchall())
        after = sorted(connection.execute("SELECT name, seq FROM main.sqlite_sequence WHERE name IN (SELECT name FROM src.sqlite_sequence)").fetchall())
        if before != after:
            raise _refuse("upgrade_failed", "The upgraded id counters differ from the source")
    return counts


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_created(destination, created):
    """Remove ``destination`` only if it is still the very file this run linked there (never a replacement of another actor)."""
    try:
        found = destination.lstat()
        if (found.st_dev, found.st_ino) == created:
            destination.unlink()
    except OSError:
        pass


def publish(work, destination):
    """Give the verified file owner-only permissions and link it at ``destination``; never replaces anything.

    Nothing that can fail is left to do after the link except making the directory entry durable; if that fails the entry
    this run created (and only that inode) is removed again, so a failed upgrade never leaves an output behind."""
    work.chmod(0o600)
    with work.open("rb") as handle:
        os.fsync(handle.fileno())
    made = work.stat()
    created = (made.st_dev, made.st_ino)
    try:
        os.link(work, destination)
    except FileExistsError as exc:
        raise _refuse("upgrade_destination_exists", "The destination appeared during the upgrade; nothing was written there") from exc
    except OSError as exc:
        raise _refuse("upgrade_failed", f"The upgraded store could not be published ({type(exc).__name__}: {exc})") from exc
    try:
        _fsync_directory(destination.parent)
    except OSError as exc:
        _remove_created(destination, created)
        raise _refuse("upgrade_failed", f"The upgraded store could not be made durable ({type(exc).__name__}: {exc})") from exc


def source_state(source):
    """What identifies the source file's current content: its size, modification time, identity and SHA-256."""
    found = Path(source).stat()
    return (found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, file_sha256(source))


def require_source_unchanged(source, before):
    """The final check before anything is published: the source is still exactly the file that was read, and no sidecar of
    a running or crashed database has appeared beside it."""
    require_source(source)
    if source_state(source) != before:
        raise _refuse("upgrade_failed", "The source file changed while it was being upgraded; nothing was published")


# ----- entry points ---------------------------------------------------------------------------------------------------


def upgrade(source, destination, *, dry_run=False, assembly=None, configuration=None):
    """Upgrade ``source`` into the new ``destination`` (or only report, with ``dry_run``) and return a counts-only report."""
    source, destination = require_source(source), require_destination(destination)
    selection = installed_selection(configuration or {})
    assembly = assembly or current_assembly(configuration)
    compiled = compile_target(assembly)
    target = assembly.target
    before = source_state(source)
    before_hash = before[-1]
    connection = open_source(source)
    try:
        frozen = recognized_baseline(connection, target, selection.ids)
        steps = plan(connection, frozen, compiled, assembly)
    finally:
        connection.close()

    if dry_run:
        directory = tempfile.TemporaryDirectory(prefix="crm-upgrade-")
        work, cleanup = Path(directory.name) / "dry-run.sqlite3", directory.cleanup
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".crm-upgrade-", suffix=".sqlite3", dir=destination.parent)
        os.close(descriptor)
        work, cleanup = Path(name), lambda: [Path(str(work) + suffix).unlink(missing_ok=True) for suffix in ("", "-journal", "-wal", "-shm")]
    try:
        rows, backfill = build(source, work, assembly, compiled, steps)
        Database(work, assembly=assembly).open_existing()
        # Every check that can fail is made before the exclusive link: the store opens as the composed target, the source is
        # still the file that was read and has no active sidecar, and the report is complete.
        require_source_unchanged(source, before)
        published = None
        if not dry_run:
            published = {"path": str(destination), "sha256": file_sha256(work), "bytes": work.stat().st_size}
            publish(work, destination)
    finally:
        cleanup()
    report = {
        "mode": "dry-run" if dry_run else "upgrade", "ok": True,
        "source": {"path": str(source), "sha256": before_hash, "unchanged": True},
        "source_target": frozen["target"],
        "target": {"name": target.name, "version": target.version, "digest": target.digest},
        "destination": published,
        "rows": rows, "new_tables": steps["new_tables"], "rebuilt_tables": steps["rebuilt_tables"],
        "added_columns": steps["added_columns"], "new_indexes": steps["new_indexes"], "new_triggers": steps["new_triggers"],
        "changed_indexes": steps["changed_indexes"], "changed_triggers": steps["changed_triggers"],
        "checks": {"source_recognized": True, "schema": "equal", "rows": "identical", "foreign_keys": "ok", "integrity": "ok"},
    }
    if backfill is not None:
        report["latest_completion_backfill"] = backfill
    return report


def main(argv=None):
    """Exit 0 on success, 2 on a refusal or failure (the error code and message on stderr). Prints the report as JSON."""
    parser = argparse.ArgumentParser(prog="python -m qorqut.migration.crm_upgrade", description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="the stopped pre-CRM database (never modified)")
    parser.add_argument("--destination", required=True, help="a new path for the upgraded database (must not exist)")
    parser.add_argument("--dry-run", action="store_true", help="build and verify in a throwaway directory; write nothing")
    parser.add_argument("--installation-config", help="trusted installation JSON; selects the existing extensions without starting them")
    arguments = parser.parse_args(argv)
    try:
        configuration = None
        if arguments.installation_config:
            try:
                configuration = json.loads(Path(arguments.installation_config).read_text(encoding="utf-8"))
                if not isinstance(configuration, dict):
                    raise ValueError("configuration must be an object")
            except (OSError, ValueError) as exc:
                raise _refuse("invalid_configuration", "Cannot read the installation configuration") from exc
        report = upgrade(arguments.source, arguments.destination, dry_run=arguments.dry_run, configuration=configuration)
    except DomainError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
