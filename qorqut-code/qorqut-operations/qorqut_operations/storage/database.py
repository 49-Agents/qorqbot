"""Explicit SQLite units of work over one composed, frozen target schema.

Construction is inert: it touches no file. A store is created only by the explicit ``initialize_target``
(an isolated empty instance or a test) and is later opened by ``open_existing``, which validates the stored
target identity without creating or converting anything. There is no migration chain and no reader for an
older format; an incompatible store is refused.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import re
import weakref

from qorqut_operations.contracts import DomainError, utc_now
from .assembly import Assembly
from .inventory import CompiledTarget, IDENTITY_DDL, IDENTITY_TABLE, compile_target, describe, differences, read_objects
from .repositories import Repository, UNSET
from .sqlfunctions import install_sql_functions

# Distinct from the deployed v1 application ID ('BZLK'): a v1 database is never accepted as a target store.
APPLICATION_ID = 0x514F5053  # 'QOPS'
LOGGER = logging.getLogger(__name__)
OBSERVATION = ContextVar('storage_operation', default=('unlabelled', None, None, None))
REQUEST_TIMING = ContextVar('request_timing', default=None)
# The catalog of the unit running on this thread/task, so catalog-dependent helpers that keep their v1
# signatures (``ref``, ``record_key``, ``PlanBuilder``) resolve the same table specs as the repositories.
ACTIVE_ASSEMBLY = ContextVar('storage_assembly', default=None)

# Throttle for the `storage_transaction` slow-unit warning below: at most one
# line per operation name per this many seconds, so a poll burst hitting the
# same slow operation repeatedly does not flood the log. Plain dict, no lock:
# writes are already serialized by SQLite and a little slop under concurrent
# reads is acceptable.
_SLOW_UNIT_WARNING_WINDOW_SECONDS = 10.0
_SLOW_UNIT_LAST_WARNED_AT: dict[str, float] = {}


class RequestTiming:
    """Accumulates per-HTTP-request storage cost for the owner Server-Timing header.

    Mutated in place, never re-`.set()`: `_unit` can run inside
    `anyio.to_thread.run_sync`, which executes on a worker thread holding a
    *copy* of the context, so a `.set()` there would be invisible once the
    call returns to the event loop. Mutating the one shared object is
    visible on both sides.

    Every figure is a sum over the units recorded for the request, in seconds
    or counts. Units can nest (a read inside a write) or run on several
    threads at once, so the durations may overlap one another and can exceed
    the request's wall time: never subtract them from it. Nothing here is SQL
    execution time.

    * ``db_seconds``: time each unit was held, from its transaction opening to
      its release (commit or rollback, finalizers and publication included).
    * ``db_read_seconds`` / ``db_write_seconds``: the same hold time, split by
      read or write unit; the two always add up to ``db_seconds``.
    * ``db_wait_seconds``: time each unit spent opening its transaction (a
      writer's lock wait under the busy timeout, plus the two statements that
      open it). A unit whose transaction never opened records its whole wait,
      with no hold time. Connection checkout from the pool is not timed.
    * ``units`` = ``read_units`` + ``write_units``, including units that failed.

    No SQL text, operation name, argument or row content is kept.
    """
    __slots__ = ('started', 'db_seconds', 'units', 'db_wait_seconds', 'db_read_seconds', 'db_write_seconds',
                 'read_units', 'write_units', '_lock')

    def __init__(self):
        self.started = time.monotonic()
        self.db_seconds = 0.0
        self.units = 0
        self.db_wait_seconds = 0.0
        self.db_read_seconds = 0.0
        self.db_write_seconds = 0.0
        self.read_units = 0
        self.write_units = 0
        # Units of one request may finish on different worker threads; `+=` on an attribute is not atomic.
        self._lock = threading.Lock()

    def record(self, *, writable, wait, held):
        """Add one finished unit: its transaction-open wait and its hold time, both in seconds."""
        with self._lock:
            self.units += 1
            self.db_seconds += held
            self.db_wait_seconds += wait
            if writable:
                self.write_units += 1
                self.db_write_seconds += held
            else:
                self.read_units += 1
                self.db_read_seconds += held


def storage_error(exc: sqlite3.Error) -> DomainError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return DomainError("busy", "Operational storage is busy; retry with the same idempotency key", retryable=True)
    if isinstance(exc, sqlite3.IntegrityError):
        name = getattr(exc, "sqlite_errorname", "")
        if name in ("SQLITE_CONSTRAINT_UNIQUE", "SQLITE_CONSTRAINT_PRIMARYKEY"):
            return DomainError("revision_conflict", "A conflicting resource already exists")
        return DomainError("validation_error", "Record violates a storage reference or data constraint")
    return DomainError("storage_error", "Operational storage failed")


def active_tables(tables=None):
    """The table catalog for a catalog-dependent helper: an explicit mapping, else the running unit's."""
    if tables is not None:
        return tables
    assembly = ACTIVE_ASSEMBLY.get()
    if assembly is None:
        raise DomainError("storage_uninitialized", "Table specifications are only available inside a unit of work")
    return assembly.tables


class UnitOfWork:
    def __init__(self, database, connection, *, writable: bool, business_id=UNSET):
        self.database, self.connection = database, connection
        self.writable, self.business_id = writable, business_id
        self.actor_id = None
        self.completed_file_documents = set()
        # Document lifecycle events waiting for this unit's verified file finalisation.
        self.history_events = {}
        # Memoized derived reads for this unit only. Read units see one
        # snapshot, so a pure derivation cannot change while they are open.
        # Writers must not rely on it: their own changes could make it stale.
        self.memo = {}
        # SQLite holds this callback in C where gc cannot see it: a strong ref would pin
        # this unit and its Database for as long as the pooled connection lives.
        unit = weakref.ref(self)
        connection.create_function("inbox_actor", 0, lambda: getattr(unit(), "actor_id", None))

    def require_write(self):
        if not self.writable:
            raise DomainError("read_only", "Preparation and reads cannot change operational data")

    def repository(self, table: str, *, business_id=UNSET) -> Repository:
        return Repository(self, table, business_id)

    def next_sequence(self, name: str) -> int:
        self.require_write()
        row = self.connection.execute(
            "UPDATE change_sequences SET value = value + 1 WHERE name = ? RETURNING value", (name,)).fetchone()
        if row is None:
            raise ValueError("Unknown change sequence")
        return row[0]


class Database:
    # anyio's default worker pool plus the background threads that open units
    # (see docs/perf/... Stage 2 report); a bounded LIFO pool this size covers
    # ordinary concurrency without retaining connections nothing will reuse.
    POOL_SIZE = 16

    def __init__(self, path, *, assembly: Assembly, busy_timeout: float = 5.0):
        if str(path) == ":memory:":
            raise ValueError("Use an explicit local file; units of work use separate connections")
        if not isinstance(assembly, Assembly):
            raise TypeError("A Database needs the frozen Assembly built by trusted composition")
        self.path = Path(path).absolute()
        self.assembly = assembly
        if busy_timeout <= 0 or busy_timeout > 60:
            raise ValueError("Busy timeout must be positive and bounded")
        self.busy_timeout = busy_timeout
        self._columns: dict[str, frozenset[str]] = {}
        self._compiled: CompiledTarget | None = None
        self._pool: deque = deque()
        self._pool_lock = threading.Lock()
        self._closed = False

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        install_sql_functions(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}")
        # Pool-lifetime cache/mmap sizing, set once at connection creation
        # rather than per acquire: these are page-cache tuning, not per-unit
        # transaction state, and there is no reason to reissue them on reuse.
        connection.execute("PRAGMA cache_size = -65536")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise DomainError("storage_configuration", "SQLite foreign keys are unavailable")
        return connection

    def dedicated_connection(self):
        """A fresh connection the caller owns exclusively, outside the pool.

        For a long-lived background reader (the change feed's external-writer
        ticker) that must never be handed to a unit of work and never opens a
        transaction of its own. Callers are responsible for closing it.
        """
        return self._connect()

    def _acquire(self):
        with self._pool_lock:
            if self._pool:
                return self._pool.pop()
        return self._connect()

    def _release(self, connection, *, healthy):
        if healthy:
            try:
                healthy = not connection.in_transaction
            except sqlite3.ProgrammingError:
                healthy = False
        if healthy:
            with self._pool_lock:
                if not self._closed and len(self._pool) < self.POOL_SIZE:
                    self._pool.append(connection)
                    return
        connection.close()

    def close(self):
        """Drain the pool. Call from service shutdown and test fixture teardown.

        Idempotent, and safe to call even if units are still in flight: a
        connection currently checked out is simply closed by `_release` (via
        the now-`_closed` pool) instead of being pooled, once its unit ends.
        """
        with self._pool_lock:
            self._closed = True
            pool, self._pool = list(self._pool), deque()
        for connection in pool:
            connection.close()

    def _remember_columns(self, connection):
        self._columns = {table: frozenset(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))
                         for table in self.assembly.tables}

    def compiled(self) -> CompiledTarget:
        """The expected schema inventory, compiled in a disposable in-memory database from the composed DDL.

        Only explicit initialization and validation call this; it never touches a store."""
        if self._compiled is None:
            self._compiled = compile_target(self.assembly)
        return self._compiled

    def initialize_target(self):
        """Create a new, empty store for this composed target. Explicit, for isolated instances and tests.

        Refuses any existing store that already has tables: it never upgrades, converts or re-initializes one.
        The target name, version and digest are recorded so a later ``open_existing`` can prove it is looking
        at exactly this composition, and the created schema is checked against the compiled inventory."""
        target = self.assembly.target
        expected = self.compiled()  # an invalid composition fails before any file or directory exists
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        connection = self._connect()
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                raise DomainError("invalid_database", "Refusing to initialize a store that already has tables")
            if connection.execute("PRAGMA application_id").fetchone()[0] not in (0, APPLICATION_ID):
                raise DomainError("invalid_database", "Database belongs to another application")
            if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise DomainError("storage_configuration", "SQLite WAL mode is unavailable")
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(IDENTITY_DDL)
            for _owner, statements in self.assembly.schema:
                for statement in statements:
                    connection.execute(statement)
            connection.execute(f'INSERT INTO "{IDENTITY_TABLE}" VALUES (\'target\', ?, ?, ?, ?)',
                               (target.name, target.version, target.digest, utc_now()))
            connection.execute(f"PRAGMA user_version = {target.version}")
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            problems = differences(expected.objects, read_objects(connection))
            if problems:
                raise DomainError("invalid_database", f"Created schema differs from the composition: {describe(problems)}")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DomainError("invalid_database", "Target schema produced invalid references")
            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
            self._remember_columns(connection)
        except sqlite3.Error as exc:
            connection.rollback()
            raise storage_error(exc) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        if created:
            self.path.chmod(0o600)
        return self

    def columns(self, table: str) -> frozenset[str]:
        if table not in self.assembly.tables:
            raise ValueError("Unknown repository")
        if table not in self._columns:
            raise DomainError("storage_uninitialized", "Initialize the database before use")
        return self._columns[table]

    def _validate_target(self, connection, *, error="invalid_database"):
        """Check an open read-only connection against this composition; raises ``DomainError``.

        The stored identity must be exactly this target, and the schema catalog must hold exactly the compiled
        inventory: the same tables, indexes and triggers, each with its owner table and stored definition. No rows
        are read and nothing is repaired."""
        target = self.assembly.target
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise DomainError(error, "Existing database is not an Operations target store")
        if connection.execute("PRAGMA user_version").fetchone()[0] != target.version:
            raise DomainError("unsupported_schema", "Existing database is not this target schema version; "
                              "no conversion is performed")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if IDENTITY_TABLE not in tables:
            raise DomainError(error, "Existing database has no target identity")
        identity = connection.execute(f'SELECT name, version, digest FROM "{IDENTITY_TABLE}"').fetchall()
        if [tuple(row) for row in identity] != [(target.name, target.version, target.digest)]:
            raise DomainError(error, "Existing database target identity does not match this composition")
        problems = differences(self.compiled().objects, read_objects(connection))
        if problems:
            raise DomainError(error, f"Existing database schema differs from the composed target: {describe(problems)}")
        for validator in self.assembly.validators:
            validator(connection)

    def open_existing(self):
        """Validate an already initialized target store without creating, converting or scanning rows.

        Full integrity/reference checks belong to explicit snapshot validation, not every process start.
        """
        if (not self.path.is_file() or any(path.is_symlink() for path in (self.path, *self.path.parents))):
            raise DomainError("invalid_database", "Existing operational database is missing or crosses a symbolic link")
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            install_sql_functions(connection)
            connection.execute("PRAGMA query_only = ON")
            self._validate_target(connection)
            self._remember_columns(connection)
        except sqlite3.Error as exc:
            raise DomainError("invalid_database", "Existing operational schema cannot be read") from exc
        finally:
            connection.close()
        return self

    @contextmanager
    def _unit(self, *, writable, business_id=UNSET, operation=None):
        if not self._columns:
            raise DomainError("storage_uninitialized", "Initialize the database before use")
        connection = self._acquire()
        started, acquired, outcome, healthy = time.monotonic(), None, 'ok', True
        catalog_token = ACTIVE_ASSEMBLY.set(self.assembly)
        try:
            # A pooled connection may have last been used by a unit that set
            # this the other way; a fresh connection defaults to OFF, but that
            # must not be relied on now that connections outlive one unit.
            connection.execute("PRAGMA query_only = OFF" if writable else "PRAGMA query_only = ON")
            connection.execute("BEGIN IMMEDIATE" if writable else "BEGIN")
            acquired = time.monotonic()
            # A coarse-tagged unit (the routine scheduler tick, job admission) opens
            # this on a fixed cadence whether or not there is anything to do; only a
            # real row change means the coarse kind actually moved (change_feed.py's
            # `_publish_commit`). Compared, never assumed: `total_changes` is
            # cumulative over the pooled connection's whole lifetime, so only the
            # delta across this one unit means anything.
            changes_before = connection.total_changes if writable else None
            uow = UnitOfWork(self, connection, writable=writable, business_id=business_id)
            yield uow
            publisher, captured = self.assembly.change_publisher, None
            if writable:
                # Same-transaction half: the publisher's capture, then each contributed finalizer in registration
                # order. A failure here propagates and rolls the whole unit back.
                if publisher is not None:
                    captured = publisher.capture(connection)
                for finalizer in self.assembly.finalizers:
                    finalizer(uow)
            connection.commit()
            if writable and publisher is not None:
                # Post-commit half: the unit is durable, so a notification failure can neither undo nor fail it.
                name, _, _, _ = OBSERVATION.get()
                try:
                    publisher.publish_commit(captured=captured, operation=operation or name,
                        business_id=None if business_id is UNSET else business_id,
                        wrote=connection.total_changes != changes_before)
                except Exception:
                    LOGGER.exception("change_publish_failed operation=%s", operation or name)
        except sqlite3.Error as exc:
            outcome = storage_error(exc).code
            healthy = False
            connection.rollback()
            raise storage_error(exc) from exc
        except BaseException:
            outcome = 'rolled_back'
            healthy = False
            connection.rollback()
            raise
        finally:
            ACTIVE_ASSEMBLY.reset(catalog_token)
            self._release(connection, healthy=healthy)
            ended = time.monotonic()
            wait = (ended if acquired is None else acquired) - started
            held = ended - acquired if acquired is not None else 0
            timing = REQUEST_TIMING.get()
            if timing is not None:
                timing.record(writable=writable, wait=wait, held=held)
            if wait >= .25 or held >= .25 or outcome == 'busy':
                name, request_id, job_id, receipt_id = OBSERVATION.get()
                resolved_operation = operation or name
                # Reuse `ended`, already sampled above, instead of a fresh
                # `time.monotonic()` call, so the throttle adds no new clock
                # read for callers (and test fake-clock iterators) to account for.
                last_warned = _SLOW_UNIT_LAST_WARNED_AT.get(resolved_operation)
                if last_warned is None or ended - last_warned >= _SLOW_UNIT_WARNING_WINDOW_SECONDS:
                    _SLOW_UNIT_LAST_WARNED_AT[resolved_operation] = ended
                    LOGGER.warning('storage_transaction operation=%s request_id=%s job_id=%s receipt_id=%s wait_ms=%.1f hold_ms=%.1f outcome=%s',
                        resolved_operation, request_id, job_id, receipt_id, wait * 1000, held * 1000, outcome)

    @contextmanager
    def observe(self, operation, request_id=None, *, job_id=None, receipt_id=None):
        # Never log SQL, arguments, credentials or exception text.
        name = operation if isinstance(operation, str) and re.fullmatch(r'[a-z_.]{1,100}', operation) else 'unknown'
        def identity(value):
            return value if isinstance(value, str) and re.fullmatch(r'[0-9a-f-]{36}', value) else None
        token = OBSERVATION.set((name, identity(request_id), identity(job_id), identity(receipt_id)))
        try:
            yield
        finally:
            OBSERVATION.reset(token)

    def read(self, *, business_id=UNSET):
        return self._unit(writable=False, business_id=business_id)

    def transaction(self, *, business_id=UNSET, operation=None):
        return self._unit(writable=True, business_id=business_id, operation=operation)

    def validate_snapshot(self, path):
        """Check a copied store against this composition's target identity, integrity and references."""
        path = Path(path).resolve(strict=True)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            install_sql_functions(connection)
            try:
                self._validate_target(connection, error="invalid_backup")
            except DomainError as exc:
                if exc.code == "unsupported_schema":
                    raise DomainError("unsupported_schema", "Snapshot schema is unsupported") from exc
                raise
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise DomainError("invalid_backup", "Snapshot failed integrity validation")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DomainError("invalid_backup", "Snapshot contains invalid references")
            counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                      for table in self.assembly.tables}
            return {"schema_version": self.assembly.target.version, "record_counts": counts}
        except sqlite3.Error as exc:
            raise DomainError("invalid_backup", "Snapshot cannot be read") from exc
        finally:
            connection.close()

    def _publish_snapshot(self, source, destination, *, timeout=60.0):
        destination = Path(destination).absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise DomainError("backup_exists", "Snapshot destination already exists")
        fd, name = tempfile.mkstemp(prefix=".snapshot-", suffix=".sqlite3", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        target = sqlite3.connect(temporary)
        started = time.monotonic()

        def progress(_status, _remaining, _total):
            if time.monotonic() - started > timeout:
                raise DomainError("busy", "Snapshot exceeded its bounded deadline", retryable=True)

        try:
            source.backup(target, pages=128, progress=progress, sleep=0.05)
            target.close()
            metadata = self.validate_snapshot(temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            os.link(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"path": str(destination), "checksum": digest, **metadata}
        except FileExistsError as exc:
            raise DomainError("backup_exists", "Snapshot destination already exists") from exc
        finally:
            target.close()
            temporary.unlink(missing_ok=True)

    def backup(self, destination, *, timeout=60.0):
        if not self._columns:
            raise DomainError("storage_uninitialized", "Initialize the database before use")
        connection = self._connect()
        try:
            return self._publish_snapshot(connection, destination, timeout=timeout)
        except sqlite3.Error as exc:
            raise storage_error(exc) from exc
        finally:
            connection.close()

    def restore(self, snapshot, destination, *, expected_checksum=None):
        """Publish a validated snapshot at a new path and return a Database for it (opened and validated)."""
        snapshot = Path(snapshot).resolve(strict=True)
        self.validate_snapshot(snapshot)
        if expected_checksum is not None:
            with snapshot.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != expected_checksum:
                    raise DomainError("invalid_backup", "Snapshot checksum does not match")
        source = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
        try:
            self._publish_snapshot(source, destination)
        finally:
            source.close()
        return Database(destination, assembly=self.assembly, busy_timeout=self.busy_timeout).open_existing()
