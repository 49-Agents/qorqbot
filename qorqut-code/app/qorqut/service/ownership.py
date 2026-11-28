"""The one OS-backed exclusive owner of an instance's backend lifetime.

Two backends on one instance directory would both recover submitting native inputs, deliver nudges, run routines and
write the same SQLite file. Another port is not another instance, so the owner is identified by the instance itself:
one lock file, ``instance-lifetime.lock``, directly under the canonical instance runtime directory derived from
``InstancePaths`` (``operations/.runtime``). It is an ``flock`` held on an open descriptor, so the operating system
drops it when the process exits however it exits, and it is independent of the installation recovery lock, which
stays the per-operation and recovery-window lock under ``locks/``.

``acquire`` is the only place the file is created. The handle then stays open for the actual backend lifetime,
including an incomplete failed-start unwind or an incomplete stop; only a stop that proved every writer drained calls
``release``. The file is never unlinked (a live lock file removed and recreated would let a second owner in), and a
runtime directory or lock file that is a symlink, was replaced, is a hard-linked or foreign-owned file, or is
group/other writable is refused rather than trusted. Constructing the owner touches nothing.
"""

from __future__ import annotations

import os
import stat
import threading

from qorqut.instance_paths import InstancePathError, InstancePaths
from qorqut_operations.contracts import DomainError
from qorqut_system import locks, private_files
from qorqut_system.errors import PrivateFileError

LOCK_NAME = "instance-lifetime.lock"


class InstanceOwnership:
    def __init__(self, paths: InstancePaths):
        if not isinstance(paths, InstancePaths):
            raise DomainError("invalid_configuration", "Instance ownership requires the explicit instance paths")
        self._paths = paths
        self._guard = threading.Lock()
        self._lock = None
        self._identity: tuple[int, int] | None = None

    @property
    def held(self) -> bool:
        return self._lock is not None

    @property
    def path(self):
        return self._paths.runtime / LOCK_NAME

    @staticmethod
    def _unavailable(reason: str) -> DomainError:
        return DomainError("ownership_unavailable", f"Instance ownership cannot be taken: {reason}")

    def acquire(self) -> "InstanceOwnership":
        """Take the exclusive lock, or refuse. Idempotent while held (the held lock is re-verified)."""
        with self._guard:
            if self._lock is not None:
                self._verify()
                return self
            try:
                directory = self._paths.runtime
            except InstancePathError as error:
                raise self._unavailable("the instance runtime location crosses a symlink") from error
            try:
                folder = private_files.open_folder(directory)
            except OSError as error:
                raise self._unavailable("the instance runtime directory is missing or is not a plain directory") from error
            try:
                named = os.stat(directory, follow_symlinks=False)
                opened = private_files.stat_folder(folder)
                if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
                    raise self._unavailable("the instance runtime directory was replaced")
                try:
                    handle = locks.open_lock(folder, LOCK_NAME)
                except OSError as error:
                    raise self._unavailable("the lock file is a symlink or cannot be opened") from error
                try:
                    info = os.fstat(handle.descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not private_files.is_mine(info):
                        raise self._unavailable("the lock file is not a plain file owned by this user")
                    if private_files.mode_of(info) & 0o022:
                        raise self._unavailable("the lock file is writable by other users")
                    try:
                        handle.acquire(exclusive=True, wait=False)
                    except BlockingIOError:
                        raise DomainError("instance_in_use", "Another backend already owns this instance") from None
                    try:
                        handle.verify()
                    except PrivateFileError:
                        raise self._unavailable("the lock file was replaced while it was being locked") from None
                    os.ftruncate(handle.descriptor, 0)
                    os.write(handle.descriptor, f"{os.getpid()}\n".encode())
                except BaseException:
                    handle.close()
                    raise
            finally:
                folder.close()
            self._lock, self._identity = handle, (info.st_dev, info.st_ino)
            return self

    def verify(self) -> None:
        """Prove the held lock still is the file the instance names (it was neither unlinked nor replaced)."""
        with self._guard:
            self._verify()

    def _verify(self) -> None:
        if self._lock is None:
            raise DomainError("ownership_lost", "This backend does not own the instance")
        try:
            current = os.stat(self.path, follow_symlinks=False)
        except (OSError, InstancePathError):
            raise DomainError("ownership_lost", "The instance lifetime lock file is gone or unsafe") from None
        if (current.st_dev, current.st_ino) != self._identity:
            raise DomainError("ownership_lost", "The instance lifetime lock file was replaced")

    def release(self) -> None:
        """End ownership once the backend lifetime is over. The file stays where it is."""
        with self._guard:
            handle, self._lock, self._identity = self._lock, None, None
            if handle is not None:
                handle.close()
