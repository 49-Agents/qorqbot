"""Admitted native files -> runner spool -> local host Archive (D39).

One thread owns an instance and both SQLite connections. Construction completes
only saved, idempotent spool registrations; file polling and host transfer are
explicit calls. No native parsing, process admission or background worker lives
here. A retained descriptor lasts until close or admission of a divergent epoch.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
from uuid import uuid4

from ..adapters.managed_io import ManagedTree
from ..adapters.types import AdapterError, ManagedRoots, NativeSource
from ..contracts import CoreError, Invocation
from .archive import Archive
from .archive_models import ArchiveMetadata, ArchiveReceipt
from .prefix import FilePrefixChanged, FilePrefixProof, verify_file_prefix
from .spool import CHUNK_BYTES, MAX_OFFSET, CaptureError, CaptureSpool, RunBinding, Source


class NativeCaptureError(CaptureError):
    """Fixed diagnostic only; neither native content nor filesystem errors escape."""


@dataclass(frozen=True)
class NativeRegistration:
    source_id: str
    source_epoch: str


@dataclass(frozen=True)
class NativeCheckpoint:
    proof: FilePrefixProof
    recorded_at: str
    gap: str | None


@dataclass(frozen=True)
class NativeState:
    source_id: str
    source_epoch: str
    provider: str
    native_session_id: str
    relative_path: str
    runs: tuple[RunBinding, ...]
    local_captured_offset: int
    host_archived_offset: int
    gap: str | None
    observation_required: bool
    previous_epoch: str | None
    divergence_reason: str | None
    checkpoint: NativeCheckpoint | None
    transfer_pending: bool
    last_error: str | None


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS native_sources (
        source_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, provider TEXT NOT NULL, native_session_id TEXT NOT NULL,
        relative_path TEXT NOT NULL, home TEXT NOT NULL, workspace TEXT NOT NULL,
        current_epoch TEXT NOT NULL,
        UNIQUE(installation_id, agent_id, provider, native_session_id, relative_path)
    )""",
    """CREATE TABLE IF NOT EXISTS native_epochs (
        source_id TEXT NOT NULL REFERENCES native_sources(source_id), source_epoch TEXT NOT NULL,
        observation TEXT NOT NULL, metadata TEXT NOT NULL,
        gap TEXT, divergence TEXT, capture_closed INTEGER NOT NULL DEFAULT 0,
        last_error TEXT, PRIMARY KEY(source_id, source_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS native_runs (
        source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, run_id TEXT NOT NULL,
        binding TEXT NOT NULL, registered INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(source_id, source_epoch, run_id),
        FOREIGN KEY(source_id, source_epoch) REFERENCES native_epochs(source_id, source_epoch)
    )""",
    """CREATE TABLE IF NOT EXISTS native_checkpoints (
        checkpoint_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
        proof TEXT NOT NULL, recorded_at TEXT NOT NULL, gap TEXT,
        FOREIGN KEY(source_id, source_epoch) REFERENCES native_epochs(source_id, source_epoch)
    )""",
    """CREATE INDEX IF NOT EXISTS native_checkpoint_source
        ON native_checkpoints(source_id, source_epoch)""",
    """CREATE TABLE IF NOT EXISTS native_transfers (
        source_id TEXT NOT NULL, source_epoch TEXT NOT NULL, binding TEXT NOT NULL,
        offset INTEGER NOT NULL, length INTEGER NOT NULL, sha256 TEXT NOT NULL,
        recovery_epoch TEXT NOT NULL, receipt TEXT,
        PRIMARY KEY(source_id, source_epoch),
        FOREIGN KEY(source_id, source_epoch) REFERENCES native_epochs(source_id, source_epoch)
    )""",
)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _stat(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise NativeCaptureError("invalid_capture_limit")


class NativeCapture:
    def __init__(self, database: str | Path, roots: ManagedRoots):
        self.roots = roots
        self._fds: dict[tuple[str, str], int] = {}
        self._closed = False
        self.spool = CaptureSpool(database)
        try:
            self._catalog = sqlite3.connect(database, isolation_level=None, timeout=2)
            self._catalog.row_factory = sqlite3.Row
            self._catalog.execute("PRAGMA busy_timeout=2000")
            if self._catalog.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise NativeCaptureError("catalog_storage_unavailable")
            self._catalog.execute("PRAGMA synchronous=FULL")
            self._catalog.execute("PRAGMA foreign_keys=ON")
            with self._transaction():
                for statement in _SCHEMA:
                    self._catalog.execute(statement)
            for registration in self.list_registered():
                self._complete_registration(registration.source_id)
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self):
        try:
            self._catalog.execute("BEGIN IMMEDIATE")
            yield
            self._catalog.execute("COMMIT")
        except BaseException as exc:
            if self._catalog.in_transaction:
                self._catalog.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise NativeCaptureError("catalog_storage_unavailable") from None
            raise

    def close(self):
        if not self._closed:
            self._closed = True
            for fd in self._fds.values():
                os.close(fd)
            self._fds.clear()
            if hasattr(self, "_catalog"):
                self._catalog.close()
            self.spool.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _source(self, source_id):
        row = self._catalog.execute("SELECT * FROM native_sources WHERE source_id=?", (source_id,)).fetchone()
        if row is None or (row["home"], row["workspace"]) != (str(self.roots.home), str(self.roots.workspace)):
            raise NativeCaptureError("unknown_managed_source")
        return row

    def _epoch(self, source_id, source_epoch=None):
        source = self._source(source_id)
        row = self._catalog.execute("SELECT * FROM native_epochs WHERE source_id=? AND source_epoch=?",
            (source_id, source_epoch or source["current_epoch"])).fetchone()
        if row is None:
            raise NativeCaptureError("unknown_native_epoch")
        return row

    def list_registered(self) -> tuple[NativeRegistration, ...]:
        """Read current registrations, including an interrupted registration intent."""
        return tuple(NativeRegistration(row[0], row[1]) for row in self._catalog.execute(
            "SELECT source_id,current_epoch FROM native_sources WHERE home=? AND workspace=? ORDER BY rowid",
            (str(self.roots.home), str(self.roots.workspace))))

    def list_states(self) -> tuple[NativeState, ...]:
        """Read every admitted epoch, including retained divergent history."""
        return tuple(self.state(row[0], row[1]) for row in self._catalog.execute(
            """SELECT e.source_id,e.source_epoch FROM native_epochs e
                JOIN native_sources s USING(source_id) WHERE s.home=? AND s.workspace=?
                ORDER BY e.rowid""", (str(self.roots.home), str(self.roots.workspace))))

    def state(self, source_id: str, source_epoch: str | None = None) -> NativeState:
        """Read saved progress, including historical epochs; no polling or retries."""
        source, epoch = self._source(source_id), self._epoch(source_id, source_epoch)
        key = (source_id, epoch["source_epoch"])
        saved = self.spool.state(*key)
        checkpoint = self._catalog.execute("""SELECT * FROM native_checkpoints
            WHERE source_id=? AND source_epoch=? ORDER BY rowid DESC LIMIT 1""", key).fetchone()
        metadata = ArchiveMetadata.model_validate_json(epoch["metadata"])
        return NativeState(*key, source["provider"], source["native_session_id"], source["relative_path"],
            tuple(RunBinding(**json.loads(row[0])) for row in self._catalog.execute(
                "SELECT binding FROM native_runs WHERE source_id=? AND source_epoch=? ORDER BY rowid", key)),
            saved["local_captured_offset"], saved["host_archived_offset"], epoch["gap"], bool(epoch["divergence"]),
            metadata.previous_epoch, metadata.divergence_reason,
            NativeCheckpoint(FilePrefixProof(**json.loads(checkpoint["proof"])),
                checkpoint["recorded_at"], checkpoint["gap"]) if checkpoint else None,
            self._catalog.execute("SELECT 1 FROM native_transfers WHERE source_id=? AND source_epoch=?", key).fetchone() is not None,
            epoch["last_error"] or saved["last_error"])

    def _complete_registration(self, source_id):
        source_row = self._source(source_id)
        source = Source(source_id, source_row["installation_id"], source_row["agent_id"],
                        "native", source_row["native_session_id"])
        rows = self._catalog.execute("""SELECT r.*, e.metadata FROM native_runs r
            JOIN native_epochs e USING(source_id,source_epoch)
            WHERE r.source_id=? AND r.registered=0 ORDER BY e.rowid,r.rowid""", (source_id,)).fetchall()
        for row in rows:
            run = RunBinding(**json.loads(row["binding"]))
            metadata = ArchiveMetadata.model_validate_json(row["metadata"])
            self.spool.register_run(run)
            fields = metadata.model_dump(exclude={"previous_epoch", "divergence_reason"})
            if metadata.previous_epoch is None:
                self.spool.register_source(source, row["source_epoch"], run.run_id, **fields)
            else:
                self.spool.begin_epoch(source_id, row["source_epoch"], previous_epoch=metadata.previous_epoch,
                    reason=metadata.divergence_reason, run_id=run.run_id, **fields)
            with self._transaction():
                self._catalog.execute("UPDATE native_runs SET registered=1 WHERE source_id=? AND source_epoch=? AND run_id=?",
                    (source_id, row["source_epoch"], run.run_id))

    def _open(self, relative):
        try:
            with ManagedTree(self.roots.home) as home, home.open_read(relative) as stream:
                return os.dup(stream.fileno())
        except FileNotFoundError:
            raise NativeCaptureError("source_missing") from None
        except (OSError, AdapterError, ValueError):
            raise NativeCaptureError("source_unavailable") from None

    @staticmethod
    def _ownership(fd, observation):
        before = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (observation.device, observation.inode):
            raise NativeCaptureError("identity_changed")
        for span in observation.ownership:
            digest, cursor = hashlib.sha256(), span.offset
            end = span.offset + span.length
            while cursor < end:
                data = os.pread(fd, min(65536, end - cursor), cursor)
                if not data:
                    raise NativeCaptureError("ownership_changed")
                digest.update(data)
                cursor += len(data)
            if digest.hexdigest() != span.sha256:
                raise NativeCaptureError("ownership_changed")
        if _stat(os.fstat(fd)) != _stat(before):
            raise NativeCaptureError("changed_during_read")
        return before

    def _named_gap(self, observation):
        try:
            fd = self._open(observation.relative_path)
            try:
                info = os.fstat(fd)
                return None if (info.st_dev, info.st_ino) == (observation.device, observation.inode) else "identity_changed"
            finally:
                os.close(fd)
        except NativeCaptureError as exc:
            return str(exc)

    def _gap(self, key, reason, *, close=False):
        divergence = reason if reason in {"identity_changed", "prefix_shorter", "prefix_mismatch", "ownership_changed"} else None
        with self._transaction():
            self._catalog.execute("""UPDATE native_epochs SET gap=?,
                divergence=coalesce(divergence,?), capture_closed=max(capture_closed,?)
                WHERE source_id=? AND source_epoch=?""", (reason, divergence, int(close), *key))
        if close and key in self._fds:
            os.close(self._fds.pop(key))

    def _validate(self, observation, run):
        try:
            if not isinstance(observation, NativeSource) or not isinstance(run, RunBinding):
                raise ValueError
            observation = NativeSource.model_validate(observation.model_dump())
            RunBinding(**asdict(run))
            for name, value in asdict(run).items():
                if name != "installation_id" and str(getattr(observation.binding.run, name)) != str(value):
                    raise ValueError
            relative = PurePosixPath(observation.relative_path)
            bases = (".claude/projects/",) if observation.provider == "claude" else (".codex/sessions/", ".codex/archived_sessions/")
            if (relative.is_absolute() or ".." in relative.parts or str(relative) != observation.relative_path
                    or not observation.relative_path.startswith(bases)
                    or observation.path != self.roots.home / relative or len(observation.relative_path) > 4096
                    or not observation.ownership or not any(s.kind == "native_session_id" for s in observation.ownership)):
                raise ValueError
            for value in (observation.device, observation.inode, observation.observed_size):
                if type(value) is not int or not 0 <= value <= MAX_OFFSET:
                    raise ValueError
            for span in observation.ownership:
                if (type(span.offset) is not int or type(span.length) is not int or span.offset < 0
                        or span.length < 1 or span.offset + span.length > observation.observed_size
                        or len(span.sha256) != 64 or any(c not in "0123456789abcdef" for c in span.sha256)):
                    raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise NativeCaptureError("invalid_native_observation") from None
        return observation

    def register(self, observation: NativeSource, capture_run: RunBinding) -> NativeRegistration:
        """Admit verified ownership, persisting UUID/epoch/link intent before spool I/O."""
        observation = self._validate(observation, capture_run)
        identity = (capture_run.installation_id, capture_run.agent_id, observation.provider,
                    str(observation.binding.native_session_id), observation.relative_path)
        existing = self._catalog.execute("""SELECT source_id FROM native_sources WHERE
            installation_id=? AND agent_id=? AND provider=? AND native_session_id=? AND relative_path=?""", identity).fetchone()
        previous, reason = None, None
        if existing:
            source_id = existing[0]
            self._complete_registration(source_id)
            previous = self._epoch(source_id)
            reason = previous["divergence"]
        else:
            source_id = str(uuid4())
        fd = self._open(observation.relative_path)
        try:
            info = self._ownership(fd, observation)
            if info.st_size < observation.observed_size:
                raise NativeCaptureError("stale_native_observation")
            if previous:
                old = NativeSource.model_validate_json(previous["observation"])
                if (old.device, old.inode) != (observation.device, observation.inode):
                    reason = reason or "identity_changed"
                else:
                    try:
                        self._ownership(fd, old)
                        verify_file_prefix(self.spool, source_id, previous["source_epoch"], fd,
                            expected_device=old.device, expected_inode=old.inode)
                    except FilePrefixChanged as exc:
                        if exc.reason == "changed_during_read":
                            raise NativeCaptureError(exc.reason) from None
                        reason = reason or exc.reason
                    except NativeCaptureError as exc:
                        if str(exc) != "ownership_changed":
                            raise
                        reason = reason or str(exc)
            # Fresh ownership and the named path must still match after prefix I/O.
            self._ownership(fd, observation)
            if (gap := self._named_gap(observation)) is not None:
                raise NativeCaptureError(gap)
            epoch = str(uuid4()) if previous is None or reason else previous["source_epoch"]
            key = (source_id, epoch)
            metadata = ArchiveMetadata(locator=observation.relative_path,
                file_identity=_json({"device": observation.device, "inode": observation.inode}),
                capability_evidence=f"{observation.provider}: verified adapter ownership spans; raw bytes only; child and attachment coverage unverified",
                previous_epoch=previous["source_epoch"] if previous is not None and reason else None,
                divergence_reason=reason)
            with self._transaction():
                if existing is None:
                    self._catalog.execute("INSERT INTO native_sources VALUES (?,?,?,?,?,?,?,?,?)",
                        (source_id, *identity, str(self.roots.home), str(self.roots.workspace), epoch))
                elif self._source(source_id)["current_epoch"] != previous["source_epoch"]:
                    raise NativeCaptureError("native_registration_changed")
                if previous is None or reason:
                    self._catalog.execute("INSERT INTO native_epochs(source_id,source_epoch,observation,metadata) VALUES (?,?,?,?)",
                        (*key, observation.model_dump_json(), metadata.model_dump_json()))
                    self._catalog.execute("UPDATE native_sources SET current_epoch=? WHERE source_id=?", (epoch, source_id))
                    if previous is not None:
                        self._catalog.execute("UPDATE native_epochs SET gap=coalesce(gap,?),divergence=coalesce(divergence,?),capture_closed=1 WHERE source_id=? AND source_epoch=?",
                            (reason, reason, source_id, previous["source_epoch"]))
                saved = self._catalog.execute("SELECT binding FROM native_runs WHERE source_id=? AND source_epoch=? AND run_id=?",
                    (*key, capture_run.run_id)).fetchone()
                if saved and saved[0] != _json(asdict(capture_run)):
                    raise NativeCaptureError("native_run_conflict")
                self._catalog.execute("INSERT OR IGNORE INTO native_runs(source_id,source_epoch,run_id,binding) VALUES (?,?,?,?)",
                    (*key, capture_run.run_id, _json(asdict(capture_run))))
            if previous is not None and reason:
                old_fd = self._fds.pop((source_id, previous["source_epoch"]), None)
                if old_fd is not None:
                    os.close(old_fd)
            self._complete_registration(source_id)
            if key in self._fds:
                os.close(self._fds[key])
            self._fds[key], fd = fd, None
            return NativeRegistration(*key)
        except OSError:
            raise NativeCaptureError("source_unavailable") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _verified(self, epoch):
        key = (epoch["source_id"], epoch["source_epoch"])
        if epoch["capture_closed"]:
            raise NativeCaptureError("fresh_native_observation_required")
        observation = NativeSource.model_validate_json(epoch["observation"])
        if (gap := self._named_gap(observation)) is not None:
            self._gap(key, gap)
        fd = self._fds.get(key)
        if fd is None:
            if gap:
                raise NativeCaptureError(gap)
            fd = self._open(observation.relative_path)
            self._fds[key] = fd
        try:
            owned = self._ownership(fd, observation)
            proof = verify_file_prefix(self.spool, *key, fd,
                expected_device=observation.device, expected_inode=observation.inode)
            if _stat(owned) != (proof.device, proof.inode, proof.file_size, proof.mtime_ns, proof.ctime_ns):
                raise NativeCaptureError("changed_during_read")
            return fd, observation, proof
        except (FilePrefixChanged, NativeCaptureError) as exc:
            reason = exc.reason if isinstance(exc, FilePrefixChanged) else str(exc)
            self._gap(key, reason, close=reason != "changed_during_read")
            raise NativeCaptureError(reason) from None

    def poll(self, source_id: str, *, max_bytes: int = 1048576) -> NativeState:
        """Capture a bounded raw suffix immediately, including partial/giant lines."""
        _limit(max_bytes, MAX_OFFSET)
        epoch = self._epoch(source_id)
        key = (source_id, epoch["source_epoch"])
        if epoch["capture_closed"]:
            return self.state(source_id)
        try:
            fd, observation, proof = self._verified(epoch)
            cursor, end = proof.through_offset, min(proof.file_size, proof.through_offset + max_bytes)
            while cursor < end:
                before = os.fstat(fd)
                data = os.pread(fd, min(CHUNK_BYTES, end - cursor), cursor)
                if (not data or _stat(before) != _stat(os.fstat(fd))
                        or _stat(before) != (proof.device, proof.inode, proof.file_size, proof.mtime_ns, proof.ctime_ns)):
                    raise NativeCaptureError("changed_during_read")
                cursor = self.spool.append(*key, cursor, data)
            if (gap := self._named_gap(observation)) is not None:
                self._gap(key, gap)
        except NativeCaptureError as exc:
            self._gap(key, str(exc))
        except OSError:
            self._gap(key, "source_unavailable")
        return self.state(source_id)

    def checkpoint(self, source_id: str) -> NativeCheckpoint:
        """Save a finite full-prefix proof; this neither closes nor drains a source."""
        epoch = self._epoch(source_id)
        key = (source_id, epoch["source_epoch"])
        try:
            _, observation, proof = self._verified(epoch)
            if (gap := self._named_gap(observation)) is not None:
                self._gap(key, gap)
        except OSError:
            self._gap(key, "source_unavailable")
            raise NativeCaptureError("source_unavailable") from None
        result = NativeCheckpoint(proof, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                                  self._epoch(*key)["gap"])
        with self._transaction():
            self._catalog.execute("INSERT INTO native_checkpoints VALUES (?,?,?,?,?,?)",
                (str(uuid4()), *key, _json(asdict(proof)), result.recorded_at, result.gap))
        return result

    @staticmethod
    def _receipt(receipt, intent):
        try:
            if not isinstance(receipt, ArchiveReceipt):
                raise ValueError
            receipt = ArchiveReceipt.model_validate(receipt.model_dump())
            if (asdict(receipt.binding) != json.loads(intent["binding"])
                    or receipt.source_id != intent["source_id"] or receipt.source_epoch != intent["source_epoch"]
                    or receipt.recovery_epoch != intent["recovery_epoch"]
                    or receipt.host_archived_offset != intent["offset"] + intent["length"]):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise NativeCaptureError("archive_receipt_mismatch") from None
        return receipt

    def transfer(self, source_id: str, archive: Archive, context: Invocation, *, max_chunks: int = 16) -> NativeState:
        """Explicitly retry exact saved transfer intent; ACK only matching receipts.

        Older epochs drain first. Every admitted run is linked on the host; the
        epoch's first run supplies its transfer binding, retained across retries.
        No host state/read watermark is used as authorization to acknowledge.
        The local host's lifetime/effects guards fence recovery and conflicts
        through runner ACK, reclamation and saved-intent settlement.
        """
        _limit(max_chunks, 16)
        with archive.db.lock.use(), archive.db.lock.effects:
            return self._transfer(source_id, archive, context, max_chunks=max_chunks)

    def _transfer(self, source_id, archive, context, *, max_chunks):
        self._complete_registration(source_id)
        source_row = self._source(source_id)
        source = Source(source_id, source_row["installation_id"], source_row["agent_id"], "native", source_row["native_session_id"])
        epochs = self._catalog.execute("SELECT * FROM native_epochs WHERE source_id=? ORDER BY rowid", (source_id,)).fetchall()
        count = 0
        for epoch in epochs:
            key = (source_id, epoch["source_epoch"])
            try:
                runs = [RunBinding(**json.loads(row[0])) for row in self._catalog.execute(
                    "SELECT binding FROM native_runs WHERE source_id=? AND source_epoch=? ORDER BY rowid", key)]
                metadata = ArchiveMetadata.model_validate_json(epoch["metadata"])
                for run in runs:
                    host = archive.register(context, run, source, key[1], metadata)
                    if host.recovery_held:
                        raise NativeCaptureError("archive_recovery_held")
                    if host.conflicted:
                        raise NativeCaptureError("archive_conflicted")
                while count < max_chunks:
                    intent = self._catalog.execute("SELECT * FROM native_transfers WHERE source_id=? AND source_epoch=?", key).fetchone()
                    if intent is not None and host.recovery_epoch != intent["recovery_epoch"]:
                        raise NativeCaptureError("archive_recovery_changed")
                    if intent is not None and self.spool.state(*key)["host_archived_offset"] >= intent["offset"] + intent["length"]:
                        if intent["receipt"] is None:
                            raise NativeCaptureError("transfer_receipt_missing")
                        self._receipt(ArchiveReceipt.model_validate_json(intent["receipt"]), intent)
                        self.spool.reclaim(*key)
                        with self._transaction():
                            self._catalog.execute("DELETE FROM native_transfers WHERE source_id=? AND source_epoch=?", key)
                        continue
                    page = self.spool.pending(*key, limit=1)
                    if not page.chunks:
                        if intent is not None:
                            raise NativeCaptureError("transfer_intent_mismatch")
                        self.spool.reclaim(*key)
                        break
                    chunk = page.chunks[0]
                    if intent is None:
                        with self._transaction():
                            self._catalog.execute("INSERT INTO native_transfers VALUES (?,?,?,?,?,?,?,NULL)",
                                (*key, _json(asdict(runs[0])), chunk.offset, chunk.length, chunk.sha256, host.recovery_epoch))
                        intent = self._catalog.execute("SELECT * FROM native_transfers WHERE source_id=? AND source_epoch=?", key).fetchone()
                    if (chunk.offset, chunk.length, chunk.sha256) != (intent["offset"], intent["length"], intent["sha256"]):
                        raise NativeCaptureError("transfer_intent_mismatch")
                    binding = RunBinding(**json.loads(intent["binding"]))
                    receipt = self._receipt(archive.receive(context, binding, chunk), intent)
                    with self._transaction():
                        self._catalog.execute("UPDATE native_transfers SET receipt=? WHERE source_id=? AND source_epoch=?",
                            (receipt.model_dump_json(), *key))
                    self.spool.acknowledge(*key, receipt.host_archived_offset)
                    self.spool.reclaim(*key)
                    with self._transaction():
                        self._catalog.execute("DELETE FROM native_transfers WHERE source_id=? AND source_epoch=?", key)
                    count += 1
                with self._transaction():
                    self._catalog.execute("UPDATE native_epochs SET last_error=NULL WHERE source_id=? AND source_epoch=?", key)
            except (CoreError, OSError, CaptureError, sqlite3.Error) as exc:
                reason = str(exc) if isinstance(exc, NativeCaptureError) else "archive_transfer_failed"
                with self._transaction():
                    self._catalog.execute("UPDATE native_epochs SET last_error=? WHERE source_id=? AND source_epoch=?", (reason, *key))
                raise NativeCaptureError(reason) from None
        return self.state(source_id)
