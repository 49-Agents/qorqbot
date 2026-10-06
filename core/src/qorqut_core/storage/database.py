"""One host writer, bounded SQLite transactions and explicit schema maintenance."""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..contracts import CoreError, new_id, utc_now
from .migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION, Migration

_write_depth: ContextVar[int] = ContextVar("qorqut_write_depth", default=0)


def assert_external_io_allowed() -> None:
    if _write_depth.get():
        raise CoreError("invalid_transition", "External I/O is forbidden inside a write transaction")


def storage_error(exc: sqlite3.Error) -> CoreError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    busy = code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return CoreError("storage_blocked", "Storage is busy" if busy else "Storage operation failed", retryable=busy)


class HostLock:
    """Lifetime advisory lock, never unlinked. It fences cooperating host processes.

    Every effect and write keeps a use guard; close waits for those guards. A fork
    cannot reuse the parent's lock authority. This is not a lease or OS sandbox.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None
        self._pid: int | None = None
        self._condition = threading.Condition()
        self._users = 0
        self._closing = False
        self._maintenance: int | None = None
        self._threads: dict[int, int] = {}
        self.effects = threading.RLock()

    def acquire(self):
        assert_external_io_allowed()
        if not self.path.is_absolute() or self.path.resolve() != self.path:
            raise CoreError("invalid_argument", "Host lock path cannot cross symlinks")
        with self._condition:
            if self._fd is not None:
                raise CoreError("invalid_transition", "Host lock is already held")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise CoreError("unavailable", "Another host owns this installation", retryable=True) from None
            except BaseException:
                os.close(fd)
                raise
            self._fd, self._pid, self._closing = fd, os.getpid(), False
        return self

    @contextmanager
    def use(self):
        thread = threading.get_ident()
        with self._condition:
            if self._fd is None or self._pid != os.getpid() or self._closing:
                raise CoreError("not_ready", "An active installation writer lock is required")
            if self._maintenance not in (None, thread) and thread not in self._threads:
                raise CoreError("not_ready", "Installation is held for maintenance", retryable=True)
            self._users += 1
            self._threads[thread] = self._threads.get(thread, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                self._users -= 1
                self._threads[thread] -= 1
                if not self._threads[thread]:
                    del self._threads[thread]
                self._condition.notify_all()

    @contextmanager
    def maintenance(self):
        assert_external_io_allowed()
        thread = threading.get_ident()
        with self._condition:
            if self._fd is None or self._pid != os.getpid() or self._closing:
                raise CoreError("not_ready", "Maintenance requires the installation lock")
            if self._maintenance is not None or thread in self._threads:
                raise CoreError("invalid_transition", "Maintenance cannot nest inside active work")
            self._maintenance = thread
            try:
                while self._users:
                    self._condition.wait()
            except BaseException:
                self._maintenance = None
                raise
        try:
            yield
        finally:
            with self._condition:
                self._maintenance = None
                self._condition.notify_all()

    def close(self):
        assert_external_io_allowed()
        with self._condition:
            thread = threading.get_ident()
            if thread in self._threads or self._maintenance == thread:
                raise CoreError("invalid_transition", "Cannot close the host from inside its active work")
            self._closing = True
            while self._users or self._maintenance is not None:
                self._condition.wait()
            if self._fd is not None:
                os.close(self._fd)
            self._fd = self._pid = None


@dataclass(frozen=True)
class TransactionTiming:
    writable: bool
    wait_seconds: float
    hold_seconds: float
    committed: bool


@dataclass
class Transaction:
    db: Database
    connection: sqlite3.Connection
    writable: bool
    active: bool = True

    def require_write(self):
        if not self.writable or not self.active or not self.connection.in_transaction:
            raise CoreError("invalid_transition", "An active write transaction is required")


class Database:
    def __init__(self, path: Path, lock: HostLock, *, busy_timeout_ms=1000,
                 clock=utc_now, id_factory=new_id, fault: Callable[[str], None] | None = None):
        if not path.is_absolute() or not 1 <= busy_timeout_ms <= 60000:
            raise ValueError("An absolute database path and bounded busy timeout are required")
        self.path, self.lock = path, lock
        self.busy_timeout_ms, self.clock, self.id_factory = busy_timeout_ms, clock, id_factory
        self.fault = fault or (lambda point: None)
        self.last_timing: TransactionTiming | None = None

    def _connect(self, *, readonly=False, create=False):
        mode = "ro" if readonly else ("rwc" if create else "rw")
        conn = sqlite3.connect(self.path.as_uri() + "?mode=" + mode, uri=True,
                               isolation_level=None, timeout=self.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            if readonly:
                conn.execute("PRAGMA query_only=ON")
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _validate(conn, *, allow_older=False, migrations=MIGRATIONS):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        maximum = migrations[-1].version
        if version > maximum or version < 0:
            raise CoreError("unsupported_schema", "Database schema is not supported by this binary")
        if not names and version == 0 and app_id == 0 and allow_older:
            return 0
        if app_id != APPLICATION_ID or "schema_migrations" not in names:
            raise CoreError("unsupported_schema", "Database is not a recognized core schema")
        applied = dict(conn.execute("SELECT version,checksum FROM schema_migrations"))
        expected = {m.version: m.checksum for m in migrations if m.version <= version}
        if applied != expected or (not allow_older and version != maximum):
            raise CoreError("unsupported_schema", "Schema requires explicit compatible migration")
        required = {"installation", "defaults", "actors", "operations", "events"} if version >= 1 else set()
        if version >= 2:
            required |= {"jobs", "file_receipts"}
        if not required <= names:
            raise CoreError("unsupported_schema", "Required schema tables are missing")
        return version

    def check_installation_identity(self, expected):
        """Initialization preflight after schema validation, including older schemas."""
        assert_external_io_allowed()
        if not self.path.exists():
            return
        with self.lock.use():
            conn = self._connect(readonly=True)
            try:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='installation'").fetchone()
                if exists:
                    row = conn.execute("SELECT installation_id FROM installation WHERE singleton=1").fetchone()
                    if row is not None and row[0] != str(expected):
                        raise CoreError("target_changed", "Database belongs to another installation")
            except sqlite3.Error as exc:
                raise storage_error(exc) from exc
            finally:
                conn.close()

    def validate(self, *, allow_older=False, migrations=MIGRATIONS):
        """Refusal changes neither DB nor WAL/SHM, including a crashed WAL writer.

        SQLite mode=ro can create/mutate WAL sidecars. A checkpointed file can be
        probed immutable; a nonempty WAL needs a private disposable copy instead.
        This is a compatibility probe, not a backup or recovery activation.
        """
        assert_external_io_allowed()
        if self.path.resolve() != self.path:
            raise CoreError("invalid_argument", "Database path cannot cross symlinks")

        def fingerprint(path):
            try:
                info = path.stat()
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                return None

        def inspect(path, *, immutable):
            conn = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""),
                                   uri=True, timeout=self.busy_timeout_ms / 1000)
            try:
                return self._validate(conn, allow_older=allow_older, migrations=migrations)
            finally:
                conn.close()

        wal = Path(str(self.path) + "-wal")
        before = (fingerprint(self.path), fingerprint(wal))
        if before[0] is None:
            raise CoreError("not_ready", "Database has not been initialized")
        try:
            if before[1] is not None and before[1][2] > 0:
                with tempfile.TemporaryDirectory(prefix="qorqut-schema-") as temporary:
                    copied = Path(temporary) / "state.sqlite3"
                    shutil.copyfile(self.path, copied)
                    shutil.copyfile(wal, Path(str(copied) + "-wal"))
                    version = inspect(copied, immutable=False)
            else:
                version = inspect(self.path, immutable=True)
            if before != (fingerprint(self.path), fingerprint(wal)):
                raise CoreError("unavailable", "Schema changed while being inspected", retryable=True)
            return version
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        except OSError as exc:
            raise CoreError("storage_blocked", "Schema probe could not read a stable local copy", retryable=True) from exc

    def migrate(self, *, migrations: tuple[Migration, ...] = MIGRATIONS):
        assert_external_io_allowed()
        if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
            raise ValueError("Migrations must be contiguous")
        # The alternate tuple is a failure-test seam, not a plugin mechanism.
        if self.path.exists():
            self.validate(allow_older=True, migrations=migrations)
        with self.lock.use():
            conn = self._connect(create=True)
            try:
                self._validate(conn, allow_older=True, migrations=migrations)
                if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise CoreError("storage_blocked", "Local WAL storage is required")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
                applied = dict(conn.execute("SELECT version,checksum FROM schema_migrations"))
                for migration in migrations:
                    if migration.version in applied:
                        continue
                    for statement in migration.statements:
                        conn.execute(statement)
                    self.fault("migration_before_version")
                    conn.execute("INSERT INTO schema_migrations VALUES (?,?,?)",
                                 (migration.version, migration.checksum, self.clock()))
                conn.execute(f"PRAGMA user_version={migrations[-1].version}")
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise CoreError("storage_blocked", "Migration violated a foreign key")
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise storage_error(exc) from exc
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def transaction(self, *, writable=True):
        if _write_depth.get():
            raise CoreError("invalid_transition", "Nested transactions are not supported")
        # All host accesses share lifetime ownership, including snapshot reads.
        with self.lock.use():
            conn, unit, token = None, None, None
            started, acquired, committed = time.monotonic(), None, False
            try:
                conn = self._connect(readonly=not writable)
                if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    raise CoreError("storage_blocked", "Database is not in WAL mode")
                if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise CoreError("unsupported_schema", "Schema is incompatible")
                conn.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
                acquired = time.monotonic()
                token = _write_depth.set(1 if writable else 0)
                unit = Transaction(self, conn, writable)
                yield unit
                if writable:
                    self.fault("transaction_before_commit")
                conn.commit()
                committed = True
            except sqlite3.Error as exc:
                if conn is not None:
                    conn.rollback()
                raise storage_error(exc) from exc
            except BaseException:
                if conn is not None:
                    conn.rollback()
                raise
            finally:
                if unit:
                    unit.active = False
                if token is not None:
                    _write_depth.reset(token)
                if conn is not None:
                    conn.close()
                ended = time.monotonic()
                self.last_timing = TransactionTiming(writable, (acquired or ended) - started,
                                                     ended - acquired if acquired else 0, committed)
            if writable:
                self.fault("transaction_after_commit")

    def read(self):
        return self.transaction(writable=False)
