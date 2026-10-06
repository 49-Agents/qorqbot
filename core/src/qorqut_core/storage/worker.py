"""Explicit, sequential host job processing; construction starts no work."""

from __future__ import annotations

import logging
import threading
from uuid import uuid4

from ..contracts import CoreError
from .jobs import Jobs

_LOG = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, jobs: Jobs):
        self.jobs = jobs
        self.worker_id = uuid4()
        self._stop = threading.Event()
        self._healthy = threading.Event()
        self._thread = threading.Thread(target=self._run, name="qorqut-jobs", daemon=False)

    @property
    def ready(self) -> bool:
        return self._thread.is_alive() and self._healthy.is_set() and not self._stop.is_set()

    def start(self):
        with self.jobs.db.lock.use():
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join()  # A running durable effect must settle before lock close.

    def _run(self):
        errors = 0
        try:
            while not self._stop.is_set():
                try:
                    # Distinguish lost lifetime ownership from a recoverable job
                    # failure. A maintenance hold is temporary, a lost lock is not.
                    with self.jobs.db.lock.use():
                        pass
                except CoreError as exc:
                    self._healthy.clear()
                    if not exc.error.retryable:
                        _LOG.error("job_worker_lock_lost worker_id=%s", self.worker_id)
                        return
                    self._stop.wait(1)
                    continue
                try:
                    # Check shutdown between individual durable inspections too.
                    for _ in range(16):
                        if self._stop.is_set() or not self.jobs.reconcile_pending(limit=1):
                            break
                    if self._stop.is_set():
                        break
                    worked = self.jobs.run_next(worker_id=self.worker_id)
                except Exception:
                    self._healthy.clear()
                    errors = min(errors + 1, 5)
                    _LOG.warning("job_worker_iteration_failed worker_id=%s", self.worker_id)
                    self._stop.wait(errors)
                    continue
                errors = 0
                self._healthy.set()
                if worked is None:
                    self._stop.wait(1)
        except BaseException:
            # Thread exception hooks must not print arbitrary handler content.
            _LOG.error("job_worker_stopped worker_id=%s", self.worker_id)
        finally:
            self._healthy.clear()
