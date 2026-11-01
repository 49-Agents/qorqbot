"""Writer admission and the installation recovery lock.

Docker/process freezing, quiescence proofs and writer inventories stay with the host; these two classes are
the generic part every mutation path shares. ``RecoveryAdmission`` is the in-process counter and
``InstallationRecoveryLock`` the cross-process shared/exclusive file lock it takes first."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from threading import Condition, local
import time
from time import monotonic

from qorqut_operations.contracts import DomainError


class InstallationRecoveryLock:
    """Shared with reconciliation; recovery takes exclusive ownership first."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise DomainError("invalid_configuration", "The recovery lock needs an explicit absolute path")

    @contextmanager
    def acquire(self, *, exclusive, timeout):
        if type(timeout) is not int or not 0 <= timeout <= 600:
            raise ValueError("Recovery lock timeout must be bounded")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        deadline = monotonic() + timeout
        flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            while True:
                try:
                    fcntl.flock(descriptor, flag | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if monotonic() >= deadline:
                        raise TimeoutError("installation recovery lock is busy") from None
                    # A short bounded wait avoids holding any SQLite transaction.
                    time.sleep(0.01)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def shared(self, *, timeout=600):
        return self.acquire(exclusive=False, timeout=timeout)

    def exclusive(self, *, timeout):
        return self.acquire(exclusive=True, timeout=timeout)

    @contextmanager
    def writer(self):
        entered = False
        try:
            with self.shared(timeout=0):
                entered = True
                yield
        except TimeoutError:
            if entered:
                raise
            raise DomainError("maintenance", "Writes are unavailable during recovery", retryable=True) from None


class RecoveryAdmission:
    """In-process write admission shared by Application and recovery.

    ``close_and_wait`` rejects new mutations and waits for operations already
    admitted on this process.  It intentionally cannot vouch for host shells or
    containers; the host's quiescer handles those through explicit controllers.
    """

    def __init__(self, *, installation_lock=None):
        self.installation_lock = installation_lock
        self._condition = Condition()
        self._closed = False
        self._active = 0
        self._local = local()

    @contextmanager
    def writer(self):
        depth = getattr(self._local, "depth", 0)
        lock_context = None
        if depth == 0 and self.installation_lock is not None:
            # Cross-process exclusion precedes local admission/counting.
            lock_context = self.installation_lock.writer()
            lock_context.__enter__()
        try:
            with self._condition:
                if depth == 0:
                    if self._closed:
                        raise DomainError("maintenance", "Writes are unavailable during recovery", retryable=True)
                    self._active += 1
                self._local.depth = depth + 1
            try:
                yield
            finally:
                with self._condition:
                    remaining = self._local.depth - 1
                    self._local.depth = remaining
                    if remaining == 0:
                        self._active -= 1
                        self._condition.notify_all()
        finally:
            if lock_context is not None:
                lock_context.__exit__(None, None, None)

    def close_and_wait(self, *, timeout):
        deadline = monotonic() + timeout
        with self._condition:
            self._closed = True
            while self._active:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("admitted write did not drain")
                self._condition.wait(remaining)

    def open(self):
        with self._condition:
            self._closed = False
            self._condition.notify_all()
