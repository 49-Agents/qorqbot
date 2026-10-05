"""Staged same-directory installs and durable receipts for files outside SQLite.

Cooperating writes are serialized by the installation lock and this local gate.
The expected hash is an optimistic check, not a promise to fence arbitrary shell
writers. Directory descriptors prevent traversing replaced symlinks.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
from uuid import UUID

from ..contracts import CoreError, Counter, Digest, JobProgress, JobResolution, Model
from .database import Database, Transaction, assert_external_io_allowed
from .events import EventInput, append_event
from .jobs import JobClaim, JobHandler, Jobs


@contextmanager
def directory(path: Path):
    assert_external_io_allowed()
    if not path.is_absolute() or ".." in path.parts:
        raise CoreError("invalid_argument", "An absolute directory without '..' is required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise CoreError("invalid_argument", "File path crosses a symbolic link or non-directory") from exc
        raise CoreError("storage_blocked", "File storage operation failed") from exc
    finally:
        os.close(fd)


def file_hash(fd: int, name: str, *, sync=False) -> tuple[str, int] | None:
    assert_external_io_allowed()
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CoreError("invalid_argument", "File effect requires regular files")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        end = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (end.st_size, end.st_mtime_ns, end.st_ctime_ns):
            raise CoreError("target_changed", "File changed while hashing")
        if sync:
            os.fsync(stream.fileno())
        return digest, end.st_size


class FileJob(Model):
    receipt_id: UUID


class StagedFile(Model):
    receipt_id: UUID
    target_path: str
    stage_name: str
    sha256: Digest
    length: Counter
    expected_sha256: Digest | None
    mode: Literal["replace", "immutable"]
    parent_device: Counter
    parent_inode: Counter


@dataclass(frozen=True)
class VerifiedInstall:
    receipt_id: UUID
    recovery_epoch: UUID
    sha256: str
    length: int


class Files:
    def __init__(self, db: Database, root: Path):
        self.db, self.root = db, root
        self.jobs: Jobs | None = None
        self._gate = db.lock.effects

    def _target(self, relative: str) -> Path:
        path = Path(relative)
        if (not relative or not path.parts or path.is_absolute() or ".." in path.parts or "\0" in relative
                or len(relative) > 4096 or str(path) != relative
                or path.parts[0] in {"installation.json", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm", "locks"}
                or any(p.startswith(".qorqut-stage-") for p in path.parts)):
            raise CoreError("invalid_argument", "File target must be a normalized data-relative file path")
        return self.root / path

    def stage(self, target_path: str, chunks: Iterable[bytes], *, expected_sha256: str | None,
              mode: Literal["replace", "immutable"] = "replace") -> StagedFile:
        """Stream/fsync outside SQL. Caller creates the intended parent explicitly.

        Unattached files are retained as unknown orphans. No cleanup scans run.
        """
        assert_external_io_allowed()
        if mode not in ("replace", "immutable") or (mode == "immutable" and expected_sha256 is not None):
            raise CoreError("invalid_argument", "Immutable installs expect an absent or identical target")
        # Validate the digest even before creating a staging file.
        if expected_sha256 is not None and (len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256)):
            raise CoreError("invalid_argument", "Expected file hash must be SHA-256")
        target = self._target(target_path)
        with self.db.lock.use(), self._gate, directory(target.parent) as fd:
            receipt_id = self.db.id_factory()
            stage_name = ".qorqut-stage-" + str(receipt_id)
            handle = os.open(stage_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=fd)
            digest, length = hashlib.sha256(), 0
            with os.fdopen(handle, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise CoreError("invalid_argument", "Staging accepts byte chunks")
                    stream.write(chunk)
                    digest.update(chunk)
                    length += len(chunk)
                stream.flush()
                self.db.fault("file_before_stage_fsync")
                os.fsync(stream.fileno())
            self.db.fault("file_after_stage_fsync")
            os.fsync(fd)
            self.db.fault("file_after_stage_directory_fsync")
            info = os.fstat(fd)
            return StagedFile(receipt_id=receipt_id, target_path=target_path, stage_name=stage_name,
                sha256=digest.hexdigest(), length=length, expected_sha256=expected_sha256, mode=mode,
                parent_device=info.st_dev, parent_inode=info.st_ino)

    def attach(self, tx: Transaction, staged: StagedFile, *, operation_id: UUID) -> UUID:
        """SQL only: attach already durable bytes and their fixed job to a mutation."""
        tx.require_write()
        self._target(staged.target_path)  # Pure lexical validation.
        if staged.stage_name != ".qorqut-stage-" + str(staged.receipt_id) or staged.length < 0:
            raise CoreError("invalid_argument", "Invalid staging receipt")
        if self.jobs is None:
            raise CoreError("not_ready", "File job handler is not assembled")
        if tx.connection.execute("SELECT 1 FROM file_receipts WHERE target_path=? AND state<>'installed'",
                                 (staged.target_path,)).fetchone():
            raise CoreError("revision_conflict", "Destination has an unfinished file receipt")
        job_id = self.jobs.enqueue(tx, "file_install", FileJob(receipt_id=staged.receipt_id),
                                  operation_id=operation_id, target_id=staged.receipt_id)
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        tx.connection.execute("""INSERT INTO file_receipts (receipt_id,operation_id,job_id,target_path,stage_name,
            sha256,length,expected_sha256,mode,parent_device,parent_inode,recovery_epoch,state,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'staged',?)""",
            (str(staged.receipt_id), str(operation_id), str(job_id), staged.target_path, staged.stage_name,
             staged.sha256, staged.length, staged.expected_sha256, staged.mode, staged.parent_device,
             staged.parent_inode, epoch, self.db.clock()))
        self.db.fault("file_after_receipt_insert")
        return job_id

    def get(self, receipt_id: UUID) -> dict:
        with self.db.read() as tx:
            row = tx.connection.execute("SELECT * FROM file_receipts WHERE receipt_id=?", (str(receipt_id),)).fetchone()
            if row is None:
                raise CoreError("not_found", "File receipt not found")
            return dict(row)

    def install(self, receipt_id: UUID, *, inspection_only=False) -> VerifiedInstall:
        """Verify, optionally rename, fsync and return evidence for a short final TX.

        A recovery hold permits inspection/fsync only, never a rename. Both the
        held job and prior receipt stay durable when final bytes cannot be proved.
        """
        assert_external_io_allowed()
        with self.db.lock.use(), self._gate:
            receipt = self.get(receipt_id)
            with self.db.read() as tx:
                epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
                job = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (receipt["job_id"],)).fetchone()
            held = job["wait_reason"] == "recovery_review" or receipt["recovery_epoch"] != epoch
            target = self._target(receipt["target_path"])
            with directory(target.parent) as fd:
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (receipt["parent_device"], receipt["parent_inode"]):
                    raise CoreError("target_changed", "Receipt's parent directory has changed")
                wanted = (receipt["sha256"], receipt["length"])
                current = file_hash(fd, target.name)
                if current != wanted:
                    if held or inspection_only or receipt["state"] == "installed":
                        raise CoreError("uncertain_effect", "Final file bytes do not establish the saved effect")
                    if receipt["mode"] == "immutable" and current is not None:
                        raise CoreError("target_changed", "Immutable destination contains different bytes")
                    if (current[0] if current else None) != receipt["expected_sha256"]:
                        raise CoreError("revision_conflict", "Authored file changed before install")
                    if file_hash(fd, receipt["stage_name"]) != wanted:
                        raise CoreError("uncertain_effect", "Staged bytes are missing or damaged")
                    # Recheck the optimistic precondition after streaming/hash work.
                    if file_hash(fd, target.name) != current:
                        raise CoreError("target_changed", "Destination changed before rename")
                    self.db.fault("file_before_rename")
                    os.replace(receipt["stage_name"], target.name, src_dir_fd=fd, dst_dir_fd=fd)
                    self.db.fault("file_after_rename")
                if file_hash(fd, target.name, sync=True) != wanted:
                    raise CoreError("uncertain_effect", "Installed file failed verification")
                self.db.fault("file_before_parent_fsync")
                os.fsync(fd)
                self.db.fault("file_after_parent_fsync")
            return VerifiedInstall(receipt_id, UUID(epoch), receipt["sha256"], receipt["length"])

    def finalize(self, tx: Transaction, verified: VerifiedInstall):
        """SQL only; callers may commit their domain rows/ACK watermark alongside it."""
        tx.require_write()
        row = tx.connection.execute("SELECT * FROM file_receipts WHERE receipt_id=?", (str(verified.receipt_id),)).fetchone()
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if (row is None or epoch != str(verified.recovery_epoch)
                or (row["sha256"], row["length"]) != (verified.sha256, verified.length)):
            raise CoreError("target_changed", "File verification belongs to another receipt/recovery epoch")
        if row["state"] == "installed":
            return
        tx.connection.execute("UPDATE file_receipts SET state='installed',installed_at=? WHERE receipt_id=?",
                             (self.db.clock(), row["receipt_id"]))
        actor = tx.connection.execute("SELECT actor_id FROM operations WHERE operation_id=?", (row["operation_id"],)).fetchone()[0]
        append_event(tx, EventInput(kind="file.installed", actor_id=actor, operation_id=row["operation_id"],
            payload={"receipt_id": row["receipt_id"], "sha256": row["sha256"], "length": row["length"]}))
        self.db.fault("file_before_receipt_commit")

    def _execute(self, claim: JobClaim) -> JobResolution:
        assert self.jobs is not None
        with self.db.read() as tx:
            self.jobs._current_claim(tx, claim)
        verified = self.install(claim.arguments.receipt_id)
        with self.db.transaction() as tx:
            self.jobs._current_claim(tx, claim)
            self.finalize(tx, verified)
        self.db.fault("file_after_receipt_commit")
        return JobResolution(state="completed", progress=JobProgress(phase="installed", completed=verified.length, total=verified.length))

    def _inspect(self, job: dict) -> JobResolution:
        receipt_id = UUID(json.loads(job["args_json"])["receipt_id"])
        try:
            verified = self.install(receipt_id, inspection_only=job["wait_reason"] == "recovery_review")
            with self.db.transaction() as tx:
                self.finalize(tx, verified)
            return JobResolution(state="completed", progress=JobProgress(phase="installed", completed=verified.length, total=verified.length))
        except (CoreError, OSError) as exc:
            held = job["wait_reason"] == "recovery_review"
            error = exc.error if isinstance(exc, CoreError) else CoreError("storage_blocked", "File inspection failed").error
            return JobResolution(state="waiting" if held else "uncertain",
                progress=JobProgress(phase="inspection_required"),
                reason="recovery_review" if held else "reconciliation_required",
                next_attempt_at=self.db.clock(), error=error)

    def handler(self) -> JobHandler:
        return JobHandler("file_install", FileJob, True, self._execute, self._inspect)
