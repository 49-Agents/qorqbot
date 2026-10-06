"""Runner tables in the configured recorder spool; no global schema version."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from uuid import UUID

from .models import RunnerError


def configure(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise ValueError("runner configuration requires a connection outside a transaction")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
        raise ValueError("runner requires a file-backed WAL database")
    connection.execute("PRAGMA synchronous=FULL")


def ensure_schema(connection: sqlite3.Connection) -> None:
    configure(connection)
    try:
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS runner_bindings (
                run_id TEXT PRIMARY KEY,
                binding_digest TEXT NOT NULL,
                binding_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runner_control (
                run_id TEXT PRIMARY KEY REFERENCES runner_bindings(run_id),
                mode TEXT NOT NULL CHECK (mode IN ('automation','human')),
                epoch INTEGER NOT NULL CHECK (epoch >= 0),
                viewer_id TEXT,
                last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0),
                CHECK ((mode='human' AND viewer_id IS NOT NULL)
                    OR (mode='automation' AND viewer_id IS NULL))
            );
            CREATE TABLE IF NOT EXISTS runner_attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runner_bindings(run_id),
                binding_digest TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL CHECK (payload_bytes BETWEEN 0 AND 65536),
                control_epoch INTEGER NOT NULL CHECK (control_epoch >= 0),
                state TEXT NOT NULL CHECK (state IN
                    ('pending','dispatching','deferred','submitted','uncertain','failed')),
                phase TEXT NOT NULL CHECK (phase IN ('prepared','paste','enter','finished')),
                reason TEXT NOT NULL,
                buffer_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runner_attempts_run ON runner_attempts(run_id);
            CREATE TABLE IF NOT EXISTS runner_control_receipts (
                control_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runner_bindings(run_id),
                request_hash TEXT NOT NULL,
                receipt_json TEXT NOT NULL
            );
            COMMIT;
        """)
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def connect(database: Path):
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        configure(connection)
        yield connection
    finally:
        connection.close()


@contextmanager
def input_lock(database: Path, run_id: UUID, *, timeout: float = 10):
    """Fresh flock FD serializes both threads and processes through the receipt.

    All callers use the same canonical configured DB path. Never unlink these
    files while a runner can be alive: replacing a lock inode splits ownership.
    """
    directory = database.with_name(database.name + ".runner-locks")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise RunnerError("storage_blocked", "runner lock directory must be private and owned")
    fd = os.open(directory / (str(run_id) + ".lock"),
                 os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise RunnerError("storage_blocked", "runner lock file ownership is invalid")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RunnerError("unavailable", "run input lock is busy") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)
