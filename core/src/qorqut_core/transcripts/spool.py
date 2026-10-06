"""Atomic local byte spool in the explicitly configured runner SQLite file.

These are module-local interfaces, not a host transfer protocol. A caller of
acknowledge asserts durable host receipt; this module cannot prove host fsync.
Connections belong to one thread. Separate connections serialize writers using
SQLite. No transaction spans PTY I/O or an external effect.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4


CHUNK_BYTES = 256 * 1024
MAX_OFFSET = 2**63 - 1


class CaptureError(Exception):
    """Base for local capture failures; raw content is excluded from errors."""


class BindingConflict(CaptureError):
    pass


class CoordinateConflict(CaptureError):
    pass


class InvalidAcknowledgment(CaptureError):
    pass


class CaptureBlocked(CaptureError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid(value: str) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("identity must be a canonical UUID")
    return value


def _integer(value: int, *, minimum: int = 0, maximum: int = MAX_OFFSET) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("integer outside the supported range")
    return value


def _text(value: str | None, maximum: int = 4096) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value
    ):
        raise ValueError("invalid metadata text")
    return value


@dataclass(frozen=True)
class RunBinding:
    installation_id: str
    agent_id: str
    recovery_epoch: str
    environment_generation: int
    boot_id: str
    run_id: str
    run_generation: int

    def __post_init__(self) -> None:
        for field in ("installation_id", "agent_id", "recovery_epoch", "boot_id", "run_id"):
            _uuid(getattr(self, field))
        _integer(self.environment_generation, minimum=1)
        _integer(self.run_generation, minimum=1)


@dataclass(frozen=True)
class Source:
    source_id: str
    installation_id: str
    agent_id: str
    source_type: str
    native_session_id: str | None = None
    parent_source_id: str | None = None

    def __post_init__(self) -> None:
        for value in (self.source_id, self.installation_id, self.agent_id):
            _uuid(value)
        if self.source_type not in {"terminal", "native", "attachment"}:
            raise ValueError("unsupported raw source type")
        _text(self.native_session_id, 512)
        if self.parent_source_id is not None:
            _uuid(self.parent_source_id)
        if self.source_type == "terminal" and self.native_session_id is not None:
            raise ValueError("a terminal source is not a native semantic source")


@dataclass(frozen=True)
class Chunk:
    source_id: str
    source_epoch: str
    offset: int
    length: int
    sha256: str
    payload: bytes


@dataclass(frozen=True)
class PendingPage:
    chunks: tuple[Chunk, ...]
    next_offset: int


@dataclass(frozen=True)
class ChunkReceipt:
    offset: int
    length: int
    sha256: str


@dataclass(frozen=True)
class ReceiptPage:
    source: Source
    source_epoch: str
    through_offset: int
    receipts: tuple[ChunkReceipt, ...]
    next_offset: int


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS capture_runs (
        run_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, recovery_epoch TEXT NOT NULL,
        environment_generation INTEGER NOT NULL CHECK(environment_generation > 0),
        boot_id TEXT NOT NULL, run_generation INTEGER NOT NULL CHECK(run_generation > 0),
        observed_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS capture_sources (
        source_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, source_type TEXT NOT NULL
            CHECK(source_type IN ('terminal', 'native', 'attachment')),
        native_session_id TEXT, parent_source_id TEXT REFERENCES capture_sources(source_id),
        current_epoch TEXT NOT NULL, observed_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS capture_epochs (
        source_id TEXT NOT NULL REFERENCES capture_sources(source_id),
        source_epoch TEXT NOT NULL, previous_epoch TEXT, divergence_reason TEXT,
        locator TEXT, file_identity TEXT, capability_evidence TEXT NOT NULL,
        native_created_at TEXT, observed_at TEXT NOT NULL,
        local_captured_offset INTEGER NOT NULL DEFAULT 0 CHECK(local_captured_offset >= 0),
        host_archived_offset INTEGER NOT NULL DEFAULT 0
            CHECK(host_archived_offset >= 0 AND host_archived_offset <= local_captured_offset),
        health TEXT NOT NULL DEFAULT 'starting'
            CHECK(health IN ('starting', 'healthy', 'blocked', 'incomplete')),
        last_error TEXT,
        recorder_run_id TEXT REFERENCES capture_runs(run_id), recorder_started_at TEXT,
        recorder_finished_at TEXT, child_exit_code INTEGER, finalized_offset INTEGER,
        drain_complete INTEGER NOT NULL DEFAULT 0 CHECK(drain_complete IN (0, 1)),
        PRIMARY KEY(source_id, source_epoch),
        FOREIGN KEY(source_id, previous_epoch) REFERENCES capture_epochs(source_id, source_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS capture_run_sources (
        run_id TEXT NOT NULL REFERENCES capture_runs(run_id),
        source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, linked_at TEXT NOT NULL,
        PRIMARY KEY(run_id, source_id, source_epoch),
        FOREIGN KEY(source_id, source_epoch) REFERENCES capture_epochs(source_id, source_epoch)
    )""",
    f"""CREATE TABLE IF NOT EXISTS capture_chunks (
        source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
        offset INTEGER NOT NULL CHECK(offset >= 0),
        length INTEGER NOT NULL CHECK(length > 0 AND length <= {CHUNK_BYTES}),
        sha256 TEXT NOT NULL CHECK(length(sha256) = 64), payload BLOB,
        captured_at TEXT NOT NULL,
        PRIMARY KEY(source_id, source_epoch, offset),
        FOREIGN KEY(source_id, source_epoch) REFERENCES capture_epochs(source_id, source_epoch),
        CHECK(payload IS NULL OR length(payload) = length)
    )""",
    """CREATE TABLE IF NOT EXISTS capture_conflicts (
        conflict_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
        offset INTEGER NOT NULL, length INTEGER NOT NULL, sha256 TEXT NOT NULL,
        payload BLOB NOT NULL, observed_at TEXT NOT NULL,
        UNIQUE(source_id, source_epoch, offset, length, sha256),
        FOREIGN KEY(source_id, source_epoch) REFERENCES capture_epochs(source_id, source_epoch)
    )""",
)


@contextmanager
def _transaction(connection: sqlite3.Connection, *, write: bool = True):
    if connection.in_transaction:
        raise ValueError("capture requires its own short transaction")
    connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def ensure_schema(connection: sqlite3.Connection, *, busy_timeout_ms: int = 2000) -> None:
    """Initialize only capture_* tables; never read/write global user_version.

    Require a file-backed connection with no active transaction. Settings are
    per connection and must also be set by other users of this shared file.
    """
    _integer(busy_timeout_ms, maximum=30_000)
    if connection.in_transaction:
        raise ValueError("initialize capture outside a transaction")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
        raise CaptureBlocked("capture requires a WAL-capable file database")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise CaptureBlocked("foreign keys are unavailable")
    with _transaction(connection):
        for statement in _SCHEMA:
            connection.execute(statement)


class CaptureSpool:
    """One connection to the configured shared file, with bounded raw chunks.

    On storage failure this instance latches closed for further capture. Its
    last_error remains observable even if storage cannot save a health update.
    Reopening is not recorder liveness evidence or permission to relaunch a run.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 2000):
        _integer(busy_timeout_ms, maximum=30_000)
        self._connection = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
        self._connection.row_factory = sqlite3.Row
        self.last_error: str | None = None
        try:
            ensure_schema(self._connection, busy_timeout_ms=busy_timeout_ms)
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> CaptureSpool:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def register_run(self, binding: RunBinding) -> None:
        fields = asdict(binding)
        with _transaction(self._connection):
            row = self._connection.execute(
                "SELECT * FROM capture_runs WHERE run_id=?", (binding.run_id,)
            ).fetchone()
            if row is not None:
                if any(row[key] != value for key, value in fields.items()):
                    raise BindingConflict("run identity is already bound differently")
                return
            self._connection.execute(
                """INSERT INTO capture_runs (installation_id, agent_id, recovery_epoch,
                    environment_generation, boot_id, run_id, run_generation, observed_at)
                    VALUES (:installation_id, :agent_id, :recovery_epoch,
                    :environment_generation, :boot_id, :run_id, :run_generation, :observed_at)""",
                {**fields, "observed_at": _now()},
            )

    def _source(self, source_id: str) -> sqlite3.Row:
        _uuid(source_id)
        row = self._connection.execute(
            "SELECT * FROM capture_sources WHERE source_id=?", (source_id,)
        ).fetchone()
        if row is None:
            raise BindingConflict("unknown source")
        return row

    def _epoch(self, source_id: str, source_epoch: str) -> sqlite3.Row:
        _uuid(source_id)
        _uuid(source_epoch)
        row = self._connection.execute(
            "SELECT * FROM capture_epochs WHERE source_id=? AND source_epoch=?",
            (source_id, source_epoch),
        ).fetchone()
        if row is None:
            raise BindingConflict("unknown source/epoch")
        return row

    def state(self, source_id: str, source_epoch: str) -> dict:
        """Durable observations, not proof that a recorder is currently alive."""
        return dict(self._epoch(source_id, source_epoch))

    def recording_binding(self, source_id: str, source_epoch: str, run_id: str) -> RunBinding:
        """Read the committed exact recording/link coordinates without adopting a run."""
        for value in (source_id, source_epoch, run_id):
            _uuid(value)
        row = self._connection.execute(
            """SELECT r.* FROM capture_runs r
                JOIN capture_run_sources l ON l.run_id=r.run_id
                JOIN capture_epochs e ON e.source_id=l.source_id AND e.source_epoch=l.source_epoch
                WHERE l.source_id=? AND l.source_epoch=? AND r.run_id=? AND e.recorder_run_id=r.run_id""",
            (source_id, source_epoch, run_id),
        ).fetchone()
        if row is None:
            raise BindingConflict("unknown recording run/source/epoch link")
        return RunBinding(**{name: row[name] for name in RunBinding.__dataclass_fields__})

    def _link_run(self, source_id: str, source_epoch: str, run_id: str) -> None:
        _uuid(run_id)
        source = self._source(source_id)
        self._epoch(source_id, source_epoch)
        run = self._connection.execute("SELECT * FROM capture_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or any(run[key] != source[key] for key in ("installation_id", "agent_id")):
            raise BindingConflict("run and source lineage do not match")
        self._connection.execute(
            "INSERT OR IGNORE INTO capture_run_sources VALUES (?, ?, ?, ?)",
            (run_id, source_id, source_epoch, _now()),
        )

    def link_run(self, source_id: str, source_epoch: str, run_id: str) -> None:
        """Explicit native resume link; no byte coordinate or cursor changes."""
        with _transaction(self._connection):
            self._link_run(source_id, source_epoch, run_id)

    @staticmethod
    def _metadata(locator, file_identity, capability_evidence, native_created_at) -> dict:
        _text(locator)
        _text(file_identity)
        if _text(capability_evidence) is None:
            raise ValueError("capability evidence is required (including known gaps)")
        if native_created_at is not None:
            _text(native_created_at, 64)
            parsed = datetime.fromisoformat(native_created_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
                raise ValueError("native time must be UTC RFC 3339")
        return dict(locator=locator, file_identity=file_identity,
                    capability_evidence=capability_evidence, native_created_at=native_created_at)

    def register_source(self, source: Source, source_epoch: str, run_id: str, *,
                        capability_evidence: str, locator: str | None = None,
                        file_identity: str | None = None, native_created_at: str | None = None) -> None:
        _uuid(source_epoch)
        fields = asdict(source)
        metadata = self._metadata(locator, file_identity, capability_evidence, native_created_at)
        with _transaction(self._connection):
            row = self._connection.execute(
                "SELECT * FROM capture_sources WHERE source_id=?", (source.source_id,)
            ).fetchone()
            if row is None:
                if source.parent_source_id is not None:
                    parent = self._source(source.parent_source_id)
                    if any(parent[key] != fields[key] for key in ("installation_id", "agent_id")):
                        raise BindingConflict("parent source belongs to another lineage")
                self._connection.execute(
                    """INSERT INTO capture_sources VALUES (:source_id, :installation_id,
                        :agent_id, :source_type, :native_session_id, :parent_source_id,
                        :current_epoch, :observed_at)""",
                    {**fields, "current_epoch": source_epoch, "observed_at": _now()},
                )
                self._insert_epoch(source.source_id, source_epoch, metadata)
            else:
                if any(row[key] != value for key, value in fields.items()):
                    raise BindingConflict("source identity is already bound differently")
                epoch = self._epoch(source.source_id, source_epoch)
                if any(epoch[key] != value for key, value in metadata.items()):
                    raise BindingConflict("epoch evidence is already bound differently")
            self._link_run(source.source_id, source_epoch, run_id)

    def _insert_epoch(self, source_id, source_epoch, metadata, previous_epoch=None, reason=None):
        self._connection.execute(
            """INSERT INTO capture_epochs (source_id, source_epoch, previous_epoch,
                divergence_reason, locator, file_identity, capability_evidence,
                native_created_at, observed_at) VALUES (:source_id, :source_epoch,
                :previous_epoch, :reason, :locator, :file_identity, :capability_evidence,
                :native_created_at, :observed_at)""",
            {**metadata, "source_id": source_id, "source_epoch": source_epoch,
             "previous_epoch": previous_epoch, "reason": reason, "observed_at": _now()},
        )

    def begin_epoch(self, source_id: str, source_epoch: str, *, previous_epoch: str,
                    reason: str, run_id: str, capability_evidence: str,
                    locator: str | None = None, file_identity: str | None = None,
                    native_created_at: str | None = None) -> None:
        """Explicit divergence. Retain all prior coordinates and late-byte lineage."""
        _uuid(source_epoch)
        _uuid(previous_epoch)
        if source_epoch == previous_epoch or _text(reason) is None:
            raise ValueError("divergence requires a distinct epoch and a reason")
        metadata = self._metadata(locator, file_identity, capability_evidence, native_created_at)
        with _transaction(self._connection):
            source = self._source(source_id)
            prior = self._epoch(source_id, previous_epoch)
            existing = self._connection.execute(
                "SELECT * FROM capture_epochs WHERE source_id=? AND source_epoch=?",
                (source_id, source_epoch),
            ).fetchone()
            if existing is not None:
                expected = {**metadata, "previous_epoch": previous_epoch, "divergence_reason": reason}
                if any(existing[key] != value for key, value in expected.items()):
                    raise BindingConflict("epoch already has different divergence evidence")
            else:
                if source["current_epoch"] != prior["source_epoch"]:
                    raise BindingConflict("current epoch changed")
                self._insert_epoch(source_id, source_epoch, metadata, previous_epoch, reason)
                self._connection.execute("UPDATE capture_sources SET current_epoch=? WHERE source_id=?",
                                         (source_epoch, source_id))
            self._link_run(source_id, source_epoch, run_id)

    def block(self, source_id: str, source_epoch: str, reason: str) -> None:
        """Latch closed first. Disk-full/locked storage can prevent durable diagnostics."""
        self.last_error = reason[:512]
        try:
            with _transaction(self._connection):
                self._connection.execute(
                    "UPDATE capture_epochs SET health='blocked', last_error=? WHERE source_id=? AND source_epoch=?",
                    (self.last_error, source_id, source_epoch),
                )
        except sqlite3.Error:
            pass

    def require_healthy(self, source_id: str, source_epoch: str) -> None:
        if self.last_error is not None or self._epoch(source_id, source_epoch)["health"] != "healthy":
            raise CaptureBlocked("capture is not ready for input")

    def append(self, source_id: str, source_epoch: str, offset: int, payload: bytes) -> int:
        """Append bounded chunks plus cursor in one transaction; identical retries succeed.

        Retries must use the same chunk coordinates, even after payload reclamation.
        A giant input is split into 256 KiB chunks, never parsed or size-capped.
        """
        _integer(offset)
        if not isinstance(payload, bytes):
            raise TypeError("capture accepts immutable bytes")
        _integer(offset + len(payload))
        if self.last_error is not None:
            raise CaptureBlocked("capture is latched closed")
        conflict = None
        try:
            with _transaction(self._connection):
                epoch = self._epoch(source_id, source_epoch)
                cursor = epoch["local_captured_offset"]
                original_cursor = cursor
                if offset > cursor:
                    raise CoordinateConflict("capture must append contiguously")
                for start in range(0, len(payload), CHUNK_BYTES):
                    part = payload[start:start + CHUNK_BYTES]
                    position = offset + start
                    digest = hashlib.sha256(part).hexdigest()
                    if position < cursor:
                        accepted = self._connection.execute(
                            "SELECT length, sha256 FROM capture_chunks WHERE source_id=? AND source_epoch=? AND offset=?",
                            (source_id, source_epoch, position),
                        ).fetchone()
                        if accepted is None or accepted["length"] != len(part) or accepted["sha256"] != digest:
                            conflict = (position, part, digest)
                            raise CoordinateConflict("accepted byte coordinates conflict")
                    else:
                        if epoch["health"] in {"blocked", "incomplete"} or epoch["recorder_finished_at"]:
                            raise CaptureBlocked("epoch cannot admit more bytes without reconciliation")
                        self._connection.execute(
                            "INSERT INTO capture_chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source_id, source_epoch, position, len(part), digest, part, _now()),
                        )
                        cursor = position + len(part)
                if cursor > original_cursor:
                    self._connection.execute(
                        "UPDATE capture_epochs SET local_captured_offset=?, health='healthy' WHERE source_id=? AND source_epoch=?",
                        (cursor, source_id, source_epoch),
                    )
            return cursor
        except CoordinateConflict:
            if conflict is not None:
                position, part, digest = conflict
                try:
                    with _transaction(self._connection):
                        self._connection.execute(
                            "INSERT OR IGNORE INTO capture_conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (str(uuid4()), source_id, source_epoch, position, len(part), digest, part, _now()),
                        )
                except sqlite3.Error as exc:
                    self.block(source_id, source_epoch, type(exc).__name__)
                    raise CaptureBlocked("could not persist coordinate conflict") from exc
            raise
        except sqlite3.Error as exc:
            self.block(source_id, source_epoch, type(exc).__name__)
            raise CaptureBlocked("capture transaction failed") from exc

    def pending(self, source_id: str, source_epoch: str, *, after: int | None = None,
                limit: int = 4) -> PendingPage:
        """A bounded page; neither reading nor using next_offset acknowledges bytes."""
        _integer(limit, minimum=1, maximum=16)
        # A concurrent ACK/reclaim must not combine an old watermark with new
        # NULL payloads. Read both from one WAL snapshot, without a writer lock.
        with _transaction(self._connection, write=False):
            epoch = self._epoch(source_id, source_epoch)
            if after is not None:
                _integer(after, maximum=epoch["local_captured_offset"])
            cursor = max(epoch["host_archived_offset"], after or 0)
            rows = self._connection.execute(
                """SELECT * FROM capture_chunks WHERE source_id=? AND source_epoch=?
                    AND offset+length>? ORDER BY offset LIMIT ?""",
                (source_id, source_epoch, cursor, limit),
            ).fetchall()
        chunks = []
        for row in rows:
            data = row["payload"]
            if data is None or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise CaptureBlocked("pending payload is missing or corrupt")
            chunks.append(Chunk(source_id, source_epoch, row["offset"], row["length"], row["sha256"], data))
        return PendingPage(tuple(chunks), chunks[-1].offset + chunks[-1].length if chunks else cursor)

    def receipts(self, source_id: str, source_epoch: str, *, after: int = 0,
                 through: int | None = None, limit: int = 16) -> ReceiptPage:
        """Read original receipt boundaries, including reclaimed/historical bytes.

        Carry through_offset into the next call to retain this snapshot's target.
        This view neither authorizes ACKs nor certifies current capture health.
        """
        _uuid(source_id)
        _uuid(source_epoch)
        _integer(after)
        if through is not None:
            _integer(through)
        _integer(limit, minimum=1, maximum=16)

        def receipt(row) -> ChunkReceipt:
            try:
                offset = _integer(row["offset"])
                length = _integer(row["length"], minimum=1, maximum=CHUNK_BYTES)
                _integer(offset + length)
            except ValueError:
                raise CaptureBlocked("invalid stored receipt coordinates") from None
            digest = row["sha256"]
            if (not isinstance(digest, str) or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)):
                raise CaptureBlocked("invalid stored receipt hash")
            return ChunkReceipt(offset, length, digest)

        def boundary(end: int, error: type[CaptureError]) -> None:
            if end == 0:
                return
            # Seek using the existing coordinate index, not an offset+length
            # predicate that would walk the complete receipt history.
            row = self._connection.execute(
                """SELECT offset, length, sha256 FROM capture_chunks
                    WHERE source_id=? AND source_epoch=? AND offset<?
                    ORDER BY offset DESC LIMIT 1""", (source_id, source_epoch, end),
            ).fetchone()
            if row is None:
                raise error("receipt boundary is missing")
            prior = receipt(row)
            if prior.offset + prior.length != end:
                raise error("receipt boundary is missing")

        with _transaction(self._connection, write=False):
            source_row = self._source(source_id)
            epoch = self._epoch(source_id, source_epoch)
            try:
                captured = _integer(epoch["local_captured_offset"])
            except ValueError:
                raise CaptureBlocked("invalid stored captured cursor") from None
            target = captured if through is None else through
            _integer(target, maximum=captured)
            _integer(after, maximum=target)
            boundary(captured, CaptureBlocked)
            if target != captured:
                boundary(target, CoordinateConflict)
            if after != target:
                boundary(after, CoordinateConflict)
            source = Source(**{name: source_row[name] for name in Source.__dataclass_fields__})
            rows = self._connection.execute(
                """SELECT offset, length, sha256 FROM capture_chunks
                    WHERE source_id=? AND source_epoch=? AND offset>=? AND offset<?
                    ORDER BY offset LIMIT ?""", (source_id, source_epoch, after, target, limit),
            ).fetchall()
            values = []
            cursor = after
            for row in rows:
                item = receipt(row)
                end = item.offset + item.length
                if item.offset != cursor or end > target:
                    raise CaptureBlocked("receipt range is not contiguous")
                values.append(item)
                cursor = end
            if cursor < target and len(values) < limit:
                raise CaptureBlocked("receipt range is not contiguous")
            return ReceiptPage(source, source_epoch, target, tuple(values), cursor)

    def acknowledge(self, source_id: str, source_epoch: str, offset: int) -> None:
        """Commit a monotonic host-durable byte watermark. Do not reclaim here."""
        _integer(offset)
        with _transaction(self._connection):
            try:
                epoch = self._epoch(source_id, source_epoch)
            except BindingConflict as exc:
                raise InvalidAcknowledgment("ACK source/epoch is unknown") from exc
            cursor = epoch["host_archived_offset"]
            if not cursor <= offset <= epoch["local_captured_offset"]:
                raise InvalidAcknowledgment("ACK must be monotonic and within captured bytes")
            rows = self._connection.execute(
                """SELECT offset, length FROM capture_chunks WHERE source_id=? AND source_epoch=?
                    AND offset+length>? AND offset<? ORDER BY offset""",
                (source_id, source_epoch, cursor, offset),
            )
            for row in rows:
                if row["offset"] > cursor:
                    raise InvalidAcknowledgment("ACK crosses a missing range")
                cursor = row["offset"] + row["length"]
            if cursor < offset:
                raise InvalidAcknowledgment("ACK crosses a missing range")
            self._connection.execute(
                "UPDATE capture_epochs SET host_archived_offset=? WHERE source_id=? AND source_epoch=?",
                (offset, source_id, source_epoch),
            )

    def reclaim(self, source_id: str, source_epoch: str) -> int:
        """Release only wholly acknowledged payloads, retaining coordinate receipts."""
        with _transaction(self._connection):
            epoch = self._epoch(source_id, source_epoch)
            params = (source_id, source_epoch, epoch["host_archived_offset"])
            size = self._connection.execute(
                """SELECT coalesce(sum(length), 0) FROM capture_chunks WHERE source_id=?
                    AND source_epoch=? AND offset+length<=? AND payload IS NOT NULL""", params,
            ).fetchone()[0]
            self._connection.execute(
                """UPDATE capture_chunks SET payload=NULL WHERE source_id=? AND source_epoch=?
                    AND offset+length<=? AND payload IS NOT NULL""", params,
            )
            return size

    def claim_recording(self, source_id: str, source_epoch: str, run_id: str) -> None:
        """Reserve this terminal epoch before fork. Uncertain launches cannot repeat."""
        if self.last_error is not None:
            raise CaptureBlocked("capture is latched closed")
        with _transaction(self._connection):
            source = self._source(source_id)
            epoch = self._epoch(source_id, source_epoch)
            if (source["source_type"] != "terminal" or epoch["recorder_started_at"] is not None
                    or epoch["local_captured_offset"] or epoch["health"] != "starting"):
                raise CaptureBlocked("terminal epoch cannot admit another launch")
            self._link_run(source_id, source_epoch, run_id)
            self._connection.execute(
                """UPDATE capture_epochs SET recorder_run_id=?, recorder_started_at=?
                    WHERE source_id=? AND source_epoch=?""", (run_id, _now(), source_id, source_epoch),
            )

    def activate_recording(self, source_id: str, source_epoch: str) -> None:
        with _transaction(self._connection):
            epoch = self._epoch(source_id, source_epoch)
            if epoch["recorder_started_at"] is None or epoch["health"] != "starting":
                raise CaptureBlocked("recording was not admitted")
            self._connection.execute(
                "UPDATE capture_epochs SET health='healthy' WHERE source_id=? AND source_epoch=?",
                (source_id, source_epoch),
            )

    def finish_recording(self, source_id: str, source_epoch: str, exit_code: int) -> None:
        with _transaction(self._connection):
            self._connection.execute(
                """UPDATE capture_epochs SET recorder_finished_at=?, child_exit_code=?,
                    finalized_offset=local_captured_offset, drain_complete=1, health=?, last_error=?
                    WHERE source_id=? AND source_epoch=?""",
                (_now(), exit_code, "healthy" if exit_code == 0 else "incomplete",
                 None if exit_code == 0 else "child exited unsuccessfully", source_id, source_epoch),
            )
