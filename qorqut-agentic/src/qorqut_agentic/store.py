"""Runtime SQLite state: journal, change feed, cursors, receipts and workflow runs.

The database lives in the explicit state directory, never inside the authoring
Git tree. Connections are short-lived; write transactions are ``BEGIN
IMMEDIATE`` so concurrent processes serialize cleanly.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import AgenticError, StoreError

SCHEMA_VERSION = 1
DB_NAME = "state.sqlite3"
LOCK_NAME = "authoring.lock"
SCRATCH_DIR = "scratch"
LOCK_TIMEOUT_SECONDS = 30.0

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    trust TEXT NOT NULL,
    package_ids TEXT NOT NULL,
    entry_refs TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_files (
    change_id TEXT NOT NULL REFERENCES changes(change_id),
    ordinal INTEGER NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    before_sha256 TEXT,
    after_sha256 TEXT,
    commit_id TEXT,
    PRIMARY KEY (change_id, ordinal)
);
CREATE INDEX IF NOT EXISTS change_files_path ON change_files(path);

CREATE TABLE IF NOT EXISTS inventory (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_changes (
    change_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    attribution TEXT NOT NULL,
    package_ids TEXT NOT NULL,
    entry_refs TEXT NOT NULL,
    details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_files (
    change_id TEXT NOT NULL REFERENCES pending_changes(change_id),
    ordinal INTEGER NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    before_sha256 TEXT,
    after_sha256 TEXT,
    before_content BLOB,
    after_content BLOB,
    mode INTEGER,
    PRIMARY KEY (change_id, ordinal)
);

CREATE TABLE IF NOT EXISTS consumers (
    name TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invocations (
    invocation_id TEXT PRIMARY KEY,
    tool_ref TEXT NOT NULL,
    purpose TEXT NOT NULL,
    actor TEXT,
    request_sha256 TEXT NOT NULL,
    definition_revision TEXT NOT NULL,
    code_revision TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_code TEXT,
    effect_state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    runner TEXT,
    exit_code INTEGER,
    output TEXT,
    diagnostics TEXT,
    limits TEXT NOT NULL,
    reconciliation TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    workflow_ref TEXT NOT NULL,
    workflow_revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    control TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT,
    selection TEXT NOT NULL,
    snapshot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_arguments (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    confirmed INTEGER NOT NULL,
    supplied_by TEXT,
    at TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT NOT NULL,
    state TEXT NOT NULL,
    verification TEXT NOT NULL,
    started_at TEXT,
    started_by TEXT,
    completed_at TEXT,
    completed_by TEXT,
    completion_note TEXT,
    output TEXT,
    PRIMARY KEY (run_id, step_id)
);

CREATE TABLE IF NOT EXISTS run_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    step_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT,
    output TEXT,
    status TEXT NOT NULL,
    feedback TEXT,
    invocation_id TEXT,
    block_reason TEXT,
    details TEXT,
    UNIQUE (run_id, operation_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    ordinal INTEGER NOT NULL,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS workflow_ops (
    scope TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, operation_id)
);
"""


class Store:
    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / DB_NAME
        self.lock_path = self.state_dir / LOCK_NAME
        self.scratch_dir = self.state_dir / SCRATCH_DIR

    # -- connections -----------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. ``BaseException`` rolls back, so an injected crash
        inside the block leaves nothing half-written."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- lifecycle -------------------------------------------------------------
    def exists(self) -> bool:
        if not self.db_path.is_file():
            return False
        with self.reader() as conn:
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = 'initialized_at'").fetchone()
            except sqlite3.DatabaseError:
                return False
        return row is not None

    def create(self, root_path: str, initialized_at: str, instance_id: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.scratch_dir.mkdir(exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(DDL)
            conn.execute("BEGIN IMMEDIATE")
            for key, value in (
                ("schema_version", str(SCHEMA_VERSION)),
                ("root_path", root_path),
                ("instance_id", instance_id),
                ("initialized_at", initialized_at),
            ):
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
            conn.execute("COMMIT")
        finally:
            conn.close()
        self.lock_path.touch(mode=0o600, exist_ok=True)

    def verify(self, root_path: str, *, rebind_root: bool = False) -> dict[str, str]:
        """Check the store exists, has a supported version and matches ``root_path``."""
        if not self.exists():
            raise StoreError(
                f"no initialized agentic state at {self.state_dir}; open never creates one",
                code="store_not_initialized",
                details={"state": str(self.state_dir)},
            )
        with self.reader() as conn:
            meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise StoreError(
                f"unsupported state schema version {meta.get('schema_version')!r}",
                code="state_mismatch",
                details={"found": meta.get("schema_version"), "supported": SCHEMA_VERSION},
            )
        if meta.get("root_path") != root_path:
            if not rebind_root:
                raise StoreError(
                    "the state store is bound to a different authoring root; pass rebind_root=True to move it",
                    code="state_mismatch",
                    details={"bound_root": meta.get("root_path"), "given_root": root_path},
                )
            with self.transaction() as conn:
                conn.execute("UPDATE meta SET value = ? WHERE key = 'root_path'", (root_path,))
            meta["root_path"] = root_path
        self.scratch_dir.mkdir(exist_ok=True)
        self.lock_path.touch(mode=0o600, exist_ok=True)
        return meta

    # -- authoring lock ---------------------------------------------------------
    @contextmanager
    def lock(self, *, exclusive: bool, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Cross-process advisory lock (flock). Shared for coherent reads, exclusive for mutation."""
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            deadline = time.monotonic() + timeout
            flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            while True:
                try:
                    fcntl.flock(fd, flag | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AgenticError(
                            "could not acquire the authoring lock in time",
                            code="lock_timeout",
                            details={"timeout_seconds": timeout},
                        ) from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
