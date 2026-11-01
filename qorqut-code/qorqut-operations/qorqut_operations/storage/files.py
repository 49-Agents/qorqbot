"""Hash-checked filesystem effects, separate from the final domain transaction."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import time
from uuid import UUID

from qorqut_operations.contracts import ChangeSet, DomainError
from .locks import LockPaths
from .receipts import ApplyReceipts
from . import safe_io


@dataclass(frozen=True)
class RegisteredFileTarget:
    root: Path
    relative_path: str

    def resolve(self) -> Path:
        relative = PurePosixPath(self.relative_path)
        if (not self.relative_path or relative.is_absolute() or ".." in relative.parts
                or "\0" in self.relative_path or "\\" in self.relative_path):
            raise DomainError("validation_error", "Invalid registered file path")
        root = Path(self.root).absolute()
        with safe_io.directory(root):
            pass
        if not root.is_dir():
            raise DomainError("validation_error", "Registered file root is not a directory")
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise DomainError("forbidden", "Registered file path crosses a symbolic link")
        if not current.resolve(strict=False).is_relative_to(root) or current == root:
            raise DomainError("forbidden", "Registered file escaped its namespace")
        return current


def file_values(content: bytes | None, *, before_hash: str | None):
    return {"content_b64": base64.b64encode(content).decode("ascii") if content is not None else None,
            "before_hash": before_hash.removeprefix("sha256:") if before_hash else None,
            "after_hash": hashlib.sha256(content).hexdigest() if content is not None else None}


def file_hash(path: Path):
    return safe_io.file_hash(path)


class FileApplier:
    """Finish exact file effects; the caller commits domain rows and finish together."""

    def __init__(self, database, resolve_target, *, locks: LockPaths, after_replace=None):
        if not isinstance(locks, LockPaths):
            raise TypeError("File application needs explicit LockPaths")
        self.database, self.resolve_target = database, resolve_target
        self.locks = locks
        self.after_replace = after_replace

    def apply(self, receipt_id):
        with self.receipt_lock(receipt_id):
            return self._apply_locked(receipt_id)

    @contextmanager
    def receipt_lock(self, receipt_id):
        """Serialize local processes without holding a SQLite write transaction."""
        try:
            if str(UUID(receipt_id)) != receipt_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise DomainError("validation_error", "Invalid application receipt ID") from exc
        root = self.locks.apply
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(root / (receipt_id + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        started = time.monotonic()
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() - started >= self.database.busy_timeout:
                        raise DomainError("busy", "Application receipt is already executing", retryable=True) from exc
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    def release_unchanged(self, receipt_id, error: DomainError):
        """Release claims only after proving every managed file still has its before bytes."""
        with self.receipt_lock(receipt_id):
            with self.database.read() as uow:
                row = ApplyReceipts(uow).get(receipt_id)
            plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
            verified_before = {}
            for change in plan.changes:
                if change.kind == "file":
                    actual = file_hash(self.resolve_target(change.resource).resolve())
                    if actual != change.values["before_hash"]:
                        raise DomainError("apply_incomplete", "Application has file effects; claims must remain for recovery")
                    verified_before[ApplyReceipts.progress_key(change.resource)] = actual
            with self.database.transaction() as uow:
                return ApplyReceipts(uow).release_unapplied(receipt_id, error, verified_before=verified_before)

    def _apply_locked(self, receipt_id):
        with self.database.transaction() as uow:
            row = ApplyReceipts(uow).claim_resources(receipt_id)
            if row["state"] == "applied":
                return row["progress_json"]
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        try:
            for change in plan.changes:
                if change.kind != "file":
                    continue
                target = self.resolve_target(change.resource)
                if not isinstance(target, RegisteredFileTarget):
                    raise TypeError("File resolver must return a registered target")
                path = target.resolve()
                values = change.values
                if set(values) != {"content_b64", "before_hash", "after_hash"}:
                    raise DomainError("invalid_plan", "Prepared file effect has invalid fields")
                try:
                    content = (base64.b64decode(values["content_b64"], validate=True)
                               if values["content_b64"] is not None else None)
                except (ValueError, TypeError) as exc:
                    raise DomainError("invalid_plan", "Prepared file content is invalid") from exc
                after = hashlib.sha256(content).hexdigest() if content is not None else None
                if after != values["after_hash"] or change.action not in ("write", "delete"):
                    raise DomainError("invalid_plan", "Prepared file hash/action is invalid")
                if (change.action == "delete") != (content is None):
                    raise DomainError("invalid_plan", "Prepared file action does not match content")
                current = file_hash(path)
                if current not in (values["before_hash"], after):
                    raise DomainError("revision_conflict", "Managed file changed outside the prepared operation")
                if current != after:
                    if content is None:
                        safe_io.unlink(path, expected_hash=values["before_hash"])
                    else:
                        safe_io.atomic_write(path, content, expected_hash=values["before_hash"])
                    if self.after_replace is not None:
                        self.after_replace(change.resource)
                if file_hash(target.resolve()) != after:
                    raise DomainError("revision_conflict", "Managed file changed before its result was recorded")
                with self.database.transaction() as uow:
                    ApplyReceipts(uow).record_progress(receipt_id, ApplyReceipts.progress_key(change.resource),
                                                       {"verified": True, "after_hash": after})
            with self.database.read() as uow:
                return ApplyReceipts(uow).get(receipt_id)["progress_json"]
        except DomainError as exc:
            with self.database.transaction() as uow:
                ApplyReceipts(uow).fail(receipt_id, exc)
            raise
        except OSError as exc:
            error = DomainError("file_apply_failed", "Managed file application failed", retryable=False)
            with self.database.transaction() as uow:
                ApplyReceipts(uow).fail(receipt_id, error)
            raise error from exc
