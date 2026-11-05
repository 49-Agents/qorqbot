"""Bounded soft accounting and admission for persistent sandbox storage.

This module measures only host-owned registered paths. It is deliberately a
monitoring/admission guard: on filesystems without quotas (including the
current ext4 installation), a same-UID process can still write past the soft
budget after admission succeeds.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import time

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io


_GIB = 1024 ** 3
_BLOCK = 512
_DEFAULT_HOST_FLOOR_GIB = 1
_DEFAULT_MAX_SECONDS = 5.0
_DEFAULT_MAX_ENTRIES = 1_000_000
_MAX_TMP_BYTES = 1 << 50


class _DiskScanFailure(Exception):
    def __init__(self, code):
        self.code = code


@dataclass(frozen=True)
class DiskReport:
    used_bytes: int
    budget_bytes: int
    reserved_bytes: int
    available_bytes: int
    host_floor_bytes: int
    checked_at: str
    error_code: str | None = None

    @property
    def admitted(self):
        return self.error_code is None

    def to_dict(self):
        status = "ok" if self.error_code is None else self.error_code
        return {
            "used_bytes": self.used_bytes,
            "budget_bytes": self.budget_bytes,
            "reserved_bytes": self.reserved_bytes,
            "available_bytes": self.available_bytes,
            "host_floor_bytes": self.host_floor_bytes,
            "checked_at": self.checked_at,
            "error_code": self.error_code,
            "status": status,
        }


class _UsageScanner:
    def __init__(self, root, *, deadline, max_entries):
        self.root = Path(root)
        self.deadline = deadline
        self.max_entries = max_entries
        self.entries = 0
        self.seen = set()
        self.total = 0

    def _bounded(self):
        self.entries += 1
        if self.entries > self.max_entries or time.monotonic() > self.deadline:
            raise _DiskScanFailure("environment_unavailable")

    def _visit(self, directory):
        self._bounded()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    self._bounded()
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        raise _DiskScanFailure("environment_unavailable") from None
                    except OSError:
                        raise _DiskScanFailure("environment_unavailable") from None
                    identity = (info.st_dev, info.st_ino)
                    if identity in self.seen:
                        continue
                    self.seen.add(identity)
                    blocks = getattr(info, "st_blocks", 0)
                    if type(blocks) is not int or blocks < 0 or blocks > _MAX_TMP_BYTES // _BLOCK:
                        raise _DiskScanFailure("environment_unavailable")
                    self.total += blocks * _BLOCK
                    # Links are persistent directory entries, but their target is
                    # outside the accounting trust boundary. Count the link and
                    # never follow it. This keeps ordinary venv/node trees usable.
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        continue
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY |
                                        os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                    except FileNotFoundError:
                        raise _DiskScanFailure("environment_unavailable") from None
                    except OSError as error:
                        if getattr(error, "errno", None) in {40, 20}:  # ELOOP/ENOTDIR after a swap
                            raise _DiskScanFailure("environment_conflict") from None
                        raise _DiskScanFailure("environment_unavailable") from None
                    try:
                        child_info = os.fstat(child)
                        if (not stat.S_ISDIR(child_info.st_mode)
                                or (child_info.st_dev, child_info.st_ino) != identity):
                            raise _DiskScanFailure("environment_conflict")
                        self._visit(child)
                    finally:
                        os.close(child)
        except (OSError, _DiskScanFailure) as error:
            if isinstance(error, _DiskScanFailure):
                raise
            raise _DiskScanFailure("environment_unavailable") from None

    def scan(self, path):
        path = Path(path)
        try:
            resolved = path.resolve(strict=False)
            # Registered roots are canonical. A missing root is permitted because
            # the lifecycle creates it later; an existing root symlink is not.
            if resolved != path:
                raise _DiskScanFailure("environment_conflict")
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise _DiskScanFailure("environment_unavailable") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _DiskScanFailure("environment_conflict")
        try:
            with safe_io.directory(path) as directory:
                opened = os.fstat(directory)
                if ((not stat.S_ISDIR(opened.st_mode)
                     or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino))):
                    raise _DiskScanFailure("environment_conflict")
                identity = (opened.st_dev, opened.st_ino)
                if identity in self.seen:
                    return
                self.seen.add(identity)
                blocks = getattr(opened, "st_blocks", 0)
                if type(blocks) is not int or blocks < 0 or blocks > _MAX_TMP_BYTES // _BLOCK:
                    raise _DiskScanFailure("environment_unavailable")
                self.total += blocks * _BLOCK
                self._visit(directory)
        except FileNotFoundError:
            return
        except DomainError as error:
            if error.code == "forbidden":
                raise _DiskScanFailure("environment_conflict") from None
            raise


class DiskBudgetAdmission:
    """Measure registered persistent paths and reserve configured budgets."""

    def __init__(self, root, *, host_floor_gb=_DEFAULT_HOST_FLOOR_GIB,
                 max_seconds=_DEFAULT_MAX_SECONDS, max_entries=_DEFAULT_MAX_ENTRIES):
        self.root = Path(root).resolve()
        if not self.root.is_dir() or type(host_floor_gb) is not int or not 1 <= host_floor_gb <= 1024:
            raise ValueError("Disk admission root and host floor are invalid")
        if type(max_seconds) not in (int, float) or not 0 < max_seconds <= 30:
            raise ValueError("Disk admission timeout is invalid")
        if type(max_entries) is not int or not 1 <= max_entries <= 5_000_000:
            raise ValueError("Disk admission entry limit is invalid")
        self.host_floor_bytes = host_floor_gb * _GIB
        self.max_seconds, self.max_entries = max_seconds, max_entries

    @staticmethod
    def _checked_at():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _paths(self, paths):
        roots = (paths.workspace, paths.home, paths.browser / "profile", paths.browser / "downloads")
        checked = []
        for candidate in roots:
            candidate = Path(candidate)
            try:
                resolved = candidate.resolve(strict=False)
            except OSError:
                raise _DiskScanFailure("environment_unavailable") from None
            if candidate != resolved or not resolved.is_relative_to(self.root):
                raise _DiskScanFailure("environment_conflict")
            checked.append(candidate)
        return checked

    def _reservations(self, database):
        # Fetch and close this read before any filesystem walk. No database
        # handle is held across stat/scandir or statvfs.
        with database.read() as uow:
            return [dict(row) for row in uow.connection.execute(
                "SELECT business_id, disk_used_bytes, disk_budget_bytes, disk_checked_at, disk_error_code "
                "FROM business_sandboxes").fetchall()]

    @staticmethod
    def _reservation(rows, business_id, budget, used):
        total = 0
        for row in rows:
            if row["business_id"] == business_id:
                observed, known = used, True
            else:
                observed = row.get("disk_used_bytes")
                known = (row.get("disk_checked_at") is not None
                         and row.get("disk_error_code") is None
                         and type(observed) is int and observed >= 0)
            remaining = budget - min(observed, budget) if known else budget
            total += max(0, remaining)
        return total

    def check(self, database, paths, config):
        budget = config.limits.disk_gb * _GIB
        if budget <= 0 or budget > _MAX_TMP_BYTES:
            raise DomainError("configuration_missing", "Sandbox disk budget is invalid")
        checked_at = self._checked_at()
        try:
            roots = self._paths(paths)
            rows = self._reservations(database)
            scanner = _UsageScanner(self.root, deadline=time.monotonic() + self.max_seconds,
                                    max_entries=self.max_entries)
            for candidate in roots:
                scanner.scan(candidate)
            try:
                stats = os.statvfs(self.root)
                block_size = stats.f_frsize or stats.f_bsize
                available = stats.f_bavail * block_size
            except OSError:
                raise _DiskScanFailure("environment_unavailable") from None
            used = scanner.total
            reserved = self._reservation(rows, paths.business_id, budget, used)
            error = None
            if used > budget or available < reserved + self.host_floor_bytes:
                error = "environment_unavailable"
            return DiskReport(used, budget, reserved, available, self.host_floor_bytes,
                              checked_at, error)
        except _DiskScanFailure as failure:
            return DiskReport(0, budget, 0, 0, self.host_floor_bytes, checked_at, failure.code)
