"""Verify managed-upgrade effects against the original private backup and the module's durable change feed.

Git metadata and the module SQLite file are containers, not preservation exemptions. Only the selected branch's commit
bookkeeping, new reachable objects, reviewed authored changes and their exact journal/inventory rows may change.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile

from qorqut_operations.contracts import DomainError
from qorqut.private_files import read_private_file


def _refuse(message, **details):
    raise DomainError("operation_conflict", message, details=details)


def _git(root, *args):
    from qorqut.agentic.layout_git import _runner
    done = _runner(Path(root), Path(root)).run(list(args), check=False,
                                              env={"GIT_NO_REPLACE_OBJECTS": "1"}, timeout=60)
    if done.returncode:
        _refuse("The managed upgrade's Git evidence cannot be verified")
    return done.stdout.decode().strip()


def git_files(intent, paths, claims, head):
    root = Path(paths.agentic_root)
    ref = intent["git"]["ref"]
    if _git(root, "symbolic-ref", "HEAD") != ref:
        _refuse("The managed upgrade changed the selected branch")
    # The private updater publishes the index and selected ref, never commit-message files or other Git state.
    allowed = {str(root / ".git" / name) for name in
               ("index", "logs/HEAD", ref, "logs/" + ref)}
    objects = {line.split()[0] for line in _git(root, "rev-list", "--objects", head,
                                               "--not", intent["git"]["head"]).splitlines()}
    for path, claim in claims.items():
        try:
            relative = Path(path).relative_to(root / ".git").as_posix()
        except ValueError:
            continue
        if relative.startswith("objects/"):
            oid = relative.removeprefix("objects/").replace("/", "")
            if (not re.fullmatch(r"objects/[0-9a-f]{2}/[0-9a-f]{38}", relative)
                    or oid not in objects or claim["before"] is not None):
                _refuse("The managed upgrade changed an unrelated Git object", path=path)
            allowed.add(path)
    return allowed


def _rows(connection, table):
    from . import learning_removal as removal
    columns, keys = removal._key_columns(connection, table)
    if table == "sqlite_sequence":
        keys = ("name",)
    result = {}
    for row in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"'):
        image = dict(zip(columns, row))
        result[tuple(image[name] for name in keys)] = image
    return result


def _schema(connection):
    return connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type,name").fetchall()


def verify_state(intent, paths, attempt, head, retained, receipt):
    """Compare every module table with A; only exact authoring feed/inventory/commit links may differ.

    Reconstruct the original database (and any original WAL) from verified backup blobs in a private temporary directory.
    Neither the backup nor the current store is opened for writes. No row contents enter a public plan or report.
    """
    from . import learning_removal as removal
    from qorqut.agentic.layout_runtime import _offline_reader
    from qorqut_agentic.store import Store
    state = Path(paths.agentic_runtime) / "state.sqlite3"
    files = attempt.baseline_a["files"]
    with tempfile.TemporaryDirectory(prefix="managed-proof-", dir=attempt.attempt_directory) as directory:
        old_path = Path(directory) / state.name
        for suffix in ("", "-wal", "-shm"):
            saved = files.get(str(state) + suffix)
            if saved and saved["exists"]:
                raw = read_private_file(Path(attempt.attempt_directory) / "restore" / "blobs" / saved["sha256"],
                                        max_bytes=removal.MAX_PREPARATION_FILE_BYTES)
                if hashlib.sha256(raw).hexdigest() != saved["sha256"]:
                    _refuse("The original module state backup was altered")
                (Path(directory) / (state.name + suffix)).write_bytes(raw)
        if not old_path.is_file():
            _refuse("The managed upgrade has no original module state backup")
        with closing(sqlite3.connect(old_path.as_uri() + "?mode=ro", uri=True)) as old, \
                _offline_reader(Store(paths.agentic_runtime)) as new:
            new.row_factory = None
            old.execute("PRAGMA query_only=ON")
            new.execute("PRAGMA query_only=ON")
            if _schema(old) != _schema(new):
                _refuse("A module schema upgrade is a separate prerequisite to learning removal")
            names = [row[0] for row in old.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
            allowed = {"changes", "change_files", "inventory", "sqlite_sequence"}
            for table in names:
                if table not in allowed and removal.table_manifest(old, table) != removal.table_manifest(new, table):
                    _refuse("The managed upgrade changed unrelated module records", table=table)
            _verify_events(intent, paths, old, new, head, retained, receipt)
            sequences_old, sequences_new = _rows(old, "sqlite_sequence"), _rows(new, "sqlite_sequence")
            if intent["changes"]:
                prior = sequences_old.pop(("changes",), None)
                actual = sequences_new.pop(("changes",), None)
                maximum = old.execute("SELECT COALESCE(MAX(sequence),0) FROM changes").fetchone()[0]
                expected = max(maximum, (prior or {}).get("seq", 0)) + 1
                if receipt["sequence"] != expected or actual != {"name": "changes", "seq": expected}:
                    _refuse("The managed event sequence differs")
            if sequences_old != sequences_new:
                _refuse("The managed upgrade changed an unrelated module sequence")
    return {str(state), str(state) + "-wal", str(state) + "-shm"}


def _verify_events(intent, paths, old, new, head, retained, receipt):
    from qorqut.agentic import managed_feed
    before, after = _rows(old, "changes"), _rows(new, "changes")
    if any(after.get(key) != row for key, row in before.items()):
        _refuse("The managed upgrade did not preserve existing change records")
    added = {key: row for key, row in after.items() if key not in before}
    before_files, after_files = _rows(old, "change_files"), _rows(new, "change_files")
    if any(after_files.get(key) != row for key, row in before_files.items()):
        _refuse("The managed upgrade did not preserve existing change-file records")
    added_files = {key: row for key, row in after_files.items() if key not in before_files}
    changes = intent["changes"]
    if changes:
        new.row_factory = sqlite3.Row
        event = managed_feed.verify_event(new, retained, head)
        if (len(added) != 1 or event["change_id"] != receipt["change_id"]
                or event["sequence"] != receipt["sequence"]
                or len(added_files) != len(changes)
                or any(row["change_id"] != event["change_id"] for row in added_files.values())):
            _refuse("The managed upgrade's event effects differ")
    elif added or added_files or receipt["change_id"] is not None or receipt["sequence"] is not None:
        _refuse("A zero-file managed update changed the feed")
    expected = _rows(old, "inventory")
    for change in changes:
        path = change["path"]
        if change["op"] == "delete":
            expected.pop((path,), None)
        else:
            file = Path(paths.agentic_root) / path
            from .learning_removal_evidence import _sha256_file
            if _sha256_file(file) != change["desired_sha256"]:
                _refuse("A managed inventory file differs from its reviewed bytes", path=path)
            expected[(path,)] = {"path": path, "sha256": change["desired_sha256"], "size": file.stat().st_size}
    if _rows(new, "inventory") != expected:
        _refuse("The managed upgrade changed an unrelated inventory entry")
