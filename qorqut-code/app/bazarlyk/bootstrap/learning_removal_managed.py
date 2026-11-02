"""Verify managed-upgrade effects against the original private backup and the module's durable change feed.

Git metadata and the module SQLite file are containers, not preservation exemptions. Only the selected branch's commit
bookkeeping, new reachable objects, reviewed authored changes and their exact journal/inventory rows may change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from qorqut_operations.contracts import DomainError
from bazarlyk.telegram.private_files import read_private_file


def _refuse(message, **details):
    raise DomainError("operation_conflict", message, details=details)


def _git(root, *args):
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
    if done.returncode:
        _refuse("The managed upgrade's Git evidence cannot be verified")
    return done.stdout.decode().strip()


def git_files(intent, paths, claims, head):
    root = Path(paths.agentic_root)
    ref = intent["git"]["ref"]
    if _git(root, "symbolic-ref", "HEAD") != ref:
        _refuse("The managed upgrade changed the selected branch")
    # A commit writes these five bookkeeping files, never configuration, hooks, other branches or existing objects.
    allowed = {str(root / ".git" / name) for name in
               ("index", "COMMIT_EDITMSG", "logs/HEAD", ref, "logs/" + ref)}
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


def verify_state(intent, paths, attempt, head):
    """Compare every module table with A; only exact authoring feed/inventory/commit links may differ.

    Reconstruct the original database (and any original WAL) from verified backup blobs in a private temporary directory.
    Neither the backup nor the current store is opened for writes. No row contents enter a public plan or report.
    """
    from . import learning_removal as removal
    from .learning_removal_evidence import _sha256_file
    state = Path(paths.agentic_runtime) / "state.sqlite3"
    files = attempt.baseline_a["files"]
    commits = set(_git(paths.agentic_root, "rev-list", head, "--not", intent["git"]["head"]).splitlines())
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
        old = sqlite3.connect(old_path.as_uri() + "?mode=ro", uri=True)
        new = sqlite3.connect(state.as_uri() + "?mode=ro", uri=True)
        try:
            old.execute("PRAGMA query_only=ON")
            new.execute("PRAGMA query_only=ON")
            if _schema(old) != _schema(new):
                _refuse("A module schema upgrade is a separate prerequisite to learning removal")
            names = [row[0] for row in old.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
            allowed = {"changes", "change_files", "inventory", "sqlite_sequence"}
            for table in names:
                if table not in allowed and removal.table_manifest(old, table) != removal.table_manifest(new, table):
                    _refuse("The managed upgrade changed unrelated module records", table=table)
            _verify_events(intent, paths, old, new, commits, files)
            sequences_old, sequences_new = _rows(old, "sqlite_sequence"), _rows(new, "sqlite_sequence")
            sequences_old.pop(("changes",), None)
            changed_sequence = sequences_new.pop(("changes",), None)
            maximum = new.execute("SELECT max(sequence) FROM changes").fetchone()[0]
            if sequences_old != sequences_new or (changed_sequence or {}).get("seq") != maximum:
                _refuse("The managed upgrade changed an unrelated module sequence")
        finally:
            old.close()
            new.close()
    return {str(state), str(state) + "-wal", str(state) + "-shm"}


def _verify_events(intent, paths, old, new, commits, baseline_files):
    from .learning_removal_evidence import _sha256_file
    root = Path(paths.agentic_root)
    reviewed = {item["path"]: item for item in intent["changes"]}
    previous, current = _rows(old, "changes"), _rows(new, "changes")
    if any(current.get(key) != row for key, row in previous.items()):
        _refuse("The managed upgrade rewrote existing change history")
    added = {row["change_id"]: row for key, row in current.items() if key not in previous}
    for row in added.values():
        reasons = {intent["attribution"]["reason"], "Offline upgrade of the packaged agentic-management guidance",
                   "Offline upgrade of the packaged overseer guidance"}
        authored = (row["kind"] == "apply" and row["actor_id"] == intent["actor"] and row["actor_kind"] == "owner"
                    and row["reason"] in reasons and row["trust"] == "host_installer")
        observed = row["kind"] == "external_scan" and row["actor_kind"] == "external" and row["trust"] == "external_unknown"
        if not authored and not observed:
            _refuse("The managed upgrade recorded an unrelated change event")
    before_files, after_files = _rows(old, "change_files"), _rows(new, "change_files")
    for key, row in before_files.items():
        after = after_files.get(key)
        if after == row:
            continue
        if (after is None or row["commit_id"] is not None or after["commit_id"] not in commits
                or {k: v for k, v in row.items() if k != "commit_id"} !=
                   {k: v for k, v in after.items() if k != "commit_id"}):
            _refuse("The managed upgrade rewrote existing change-file history")
    event_files = [row for key, row in after_files.items() if key not in before_files]
    if {row["change_id"] for row in event_files} != set(added):
        _refuse("The managed upgrade's events do not have exact file effects")
    original_inventory, inventory = _rows(old, "inventory"), _rows(new, "inventory")
    expected_inventory = dict(original_inventory)
    chains = {}
    for row in sorted(event_files, key=lambda value: (added.get(value["change_id"], {}).get("sequence", -1), value["ordinal"])):
        path = row["path"]
        if path not in reviewed or row["change_id"] not in added or row["commit_id"] not in commits:
            _refuse("The managed upgrade's change feed contains an unreviewed file", path=path)
        prior = chains.get(path, (original_inventory.get((path,)) or {}).get("sha256"))
        if row["before_sha256"] != prior:
            _refuse("A managed change event does not continue the original file history", path=path)
        if added[row["change_id"]]["kind"] == "external_scan":
            expected = (baseline_files.get(str(root / path)) or {}).get("sha256")
        else:
            expected = reviewed[path]["sha256"]
        if row["after_sha256"] != expected:
            _refuse("A managed change event does not hold its reviewed file bytes", path=path)
        chains[path] = expected
    if chains != {path: change["sha256"] for path, change in reviewed.items()}:
        _refuse("The managed change feed does not cover every reviewed file")
    for path, digest in chains.items():
        if digest is None:
            expected_inventory.pop((path,), None)
        else:
            if _sha256_file(root / path) != digest:
                _refuse("A managed inventory file differs from its reviewed bytes", path=path)
            expected_inventory[(path,)] = {"path": path, "sha256": digest, "size": (root / path).stat().st_size}
    if inventory != expected_inventory:
        _refuse("The managed upgrade changed an unrelated inventory entry")
