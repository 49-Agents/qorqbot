"""Cross-process barrier for retiring legacy file writers during migration."""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MARKER = Path(".bazarlyk/authority.json")
STATES = frozenset({"legacy", "maintenance", "database"})
_writer_leases = threading.local()


class AuthorityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def find_authority(path: str | Path) -> Path | None:
    location = Path(path).resolve()
    for parent in (location, *location.parents):
        marker = parent / MARKER
        if marker.is_symlink() or marker.exists():
            return marker
    return None


def read_authority(marker: str | Path) -> dict[str, Any]:
    marker = Path(marker)
    if marker.is_symlink() or marker.parent.is_symlink():
        raise AuthorityError("authority_invalid", "The authority marker must not be a symbolic link.")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuthorityError("authority_invalid", "The storage authority marker is unavailable or invalid.") from exc
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
            or data.get("schema_version") != 1
            or data.get("state") not in STATES):
        raise AuthorityError("authority_invalid", "The storage authority marker has an unsupported format.")
    return data


def _require_legacy(marker: Path) -> None:
    state = read_authority(marker)["state"]
    if state == "maintenance":
        raise AuthorityError("maintenance", "Business writes are paused for storage maintenance.")
    if state == "database":
        raise AuthorityError("legacy_writer_retired",
                             "This file writer is retired. Use the shared Qorqut service.")


@contextmanager
def legacy_write(path: str | Path) -> Iterator[None]:
    """Wrap the complete mutation, including all related file writes, not parsing."""
    marker = find_authority(path)
    if marker is None:
        yield
        return
    key = (os.getpid(), str(marker.resolve()))
    held = getattr(_writer_leases, "held", set())
    if key in held:
        # Accepted synchronous workflows may finish their nested writes while draining.
        yield
        return
    _require_legacy(marker)
    lock_path = marker.parent / "legacy-writers.lock"
    if lock_path.is_symlink():
        raise AuthorityError("authority_invalid", "The legacy writer lock is invalid.")
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthorityError("maintenance", "Business writes are paused for storage maintenance.") from None
        try:
            # A freeze may have closed admission while this writer acquired its lease.
            _require_legacy(marker)
            _writer_leases.held = held | {key}
            try:
                yield
            finally:
                _writer_leases.held = held
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _write_marker(marker: Path, data: dict[str, Any]) -> None:
    if marker.is_symlink():
        raise AuthorityError("authority_invalid", "The authority marker must not be a symbolic link.")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=marker.parent,
                                         prefix=".authority-", delete=False) as handle:
            temporary = handle.name
            json.dump(data, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
        temporary = None
        directory_fd = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _metadata(root: Path) -> Path:
    root = root.absolute()
    directory = root / ".bazarlyk"
    if directory.is_symlink():
        raise AuthorityError("authority_invalid", "The authority directory must not be a symbolic link.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def prepare_legacy_authority(root: str | Path) -> dict[str, Any]:
    """Install the barrier while retaining legacy authority; never reset a cutover."""
    directory = _metadata(Path(root))
    if (directory / "cutover.lock").is_symlink():
        raise AuthorityError("authority_invalid", "The storage coordination lock is invalid.")
    with (directory / "cutover.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthorityError("maintenance_busy", "A storage cutover is already active.") from None
        marker = directory / "authority.json"
        if marker.exists() or marker.is_symlink():
            return read_authority(marker)
        data = {"schema_version": 1, "state": "legacy", "migration_id": None}
        _write_marker(marker, data)
        return data


@dataclass
class FrozenWriters:
    root: Path
    migration_id: str
    active: bool = True

    def _set(self, state: str, **values: Any) -> dict[str, Any]:
        if not self.active:
            raise AuthorityError("freeze_expired", "The writer freeze is no longer held.")
        marker = self.root / MARKER
        current = read_authority(marker)
        if current.get("migration_id") != self.migration_id:
            raise AuthorityError("authority_conflict", "A different migration owns storage authority.")
        data = {"schema_version": 1, "state": state,
                "migration_id": self.migration_id, **values}
        _write_marker(marker, data)
        return data

    def activate_database(self, database: str | Path) -> dict[str, Any]:
        database = Path(database).resolve()
        try:
            relative = database.relative_to(self.root.resolve())
        except ValueError:
            raise AuthorityError("database_outside_root", "The database must be inside the businesses root.") from None
        if not database.is_file():
            raise AuthorityError("database_missing", "The verified migration database is missing.")
        return self._set("database", database_path=relative.as_posix())

    def restore_legacy(self) -> dict[str, Any]:
        """Caller must first restore/reconcile legacy records according to the runbook."""
        return self._set("legacy")


@contextmanager
def freeze_legacy_writers(root: str | Path, migration_id: str, *, timeout: float = 30) -> Iterator[FrozenWriters]:
    """Close admission, drain leases, and hold the import snapshot stable."""
    if (not isinstance(migration_id, str) or not migration_id.strip()
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout < 0):
        raise AuthorityError("invalid_migration", "Migration ID and a nonnegative timeout are required.")
    root = Path(root).absolute()
    directory = _metadata(root)
    for filename in ("cutover.lock", "legacy-writers.lock"):
        if (directory / filename).is_symlink():
            raise AuthorityError("authority_invalid", "The storage coordination lock is invalid.")
    with (directory / "cutover.lock").open("a+b") as coordinator:
        try:
            fcntl.flock(coordinator, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthorityError("maintenance_busy", "A storage cutover is already active.") from None
        marker = directory / "authority.json"
        if not marker.exists() and not marker.is_symlink():
            raise AuthorityError("authority_not_prepared",
                                 "Install the legacy writer barrier before beginning cutover.")
        read_authority(marker)
        _write_marker(marker, {"schema_version": 1, "state": "maintenance",
                               "migration_id": migration_id})
        with (directory / "legacy-writers.lock").open("a+b") as writers:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(writers, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthorityError("writers_busy", "Accepted legacy writes have not finished.") from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            lease = FrozenWriters(root, migration_id)
            try:
                yield lease
            finally:
                lease.active = False
                fcntl.flock(writers, fcntl.LOCK_UN)
        # Failures intentionally leave maintenance closed until explicit recovery.
