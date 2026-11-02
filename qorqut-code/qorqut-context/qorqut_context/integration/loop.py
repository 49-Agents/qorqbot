"""Bounded start/stop around the Context queue driver (P4B).

``ContextDriverLoop`` is the only place the driver runs repeatedly. ``start`` first runs the driver's mechanical ``recover``
(which never starts a model) and then one daemon thread repeats ``process_next``; ``wake`` shortens the idle wait after
new work was admitted; ``stop`` asks the thread to finish and reports honestly whether a step is still in flight. A step
that is mid-propagation cannot be interrupted from here: the Context pipeline's own runner owns that run, and a process
that exits while one is running is repaired by the next ``start`` through the driver's recovery.

Lifetime contract: while ``stop`` reports ``in_flight`` a writer (a Context draft, Git publication or a SQLite recording) is
still running. The database and the Context must stay open and usable until a later ``stop`` reports ``stopped``; a timeout
does not make closing them safe, and nothing here closes either.
"""

from __future__ import annotations

import math
import threading
import time

from qorqut_context.integration.driver import ContextQueueDriver

# A step that moved the queue forward is followed at once by the next one. ``discard_failed`` and ``unrecorded`` leave the
# same input exactly where it was (a requested discard, a publication still to be recorded), so they are not progress: the
# loop waits, with bounded backoff, instead of rewriting the same failure continuously. Every other outcome waits for new
# work or the next tick. No wait authorizes a model retry: a blocked head still needs its explicit retry control.
_PROGRESS = frozenset({"published", "discarded", "input_blocked"})
_STALLED = frozenset({"discard_failed", "unrecorded"})


class ContextDriverLoop:
    def __init__(self, driver: ContextQueueDriver, *, poll_seconds: float = 2.0, max_backoff_seconds: float = 60.0):
        if poll_seconds <= 0 or max_backoff_seconds < poll_seconds:
            raise ValueError("The driver loop needs a positive poll interval no larger than its backoff ceiling")
        self.driver, self.poll_seconds, self.max_backoff_seconds = driver, poll_seconds, max_backoff_seconds
        # The exponent stops growing once the delay reaches the ceiling, so a sustained failure never grows an unbounded count.
        self._ceiling = max(1, math.ceil(math.log2(max_backoff_seconds / poll_seconds)))
        self.failures = 0
        self._thread: threading.Thread | None = None
        self._stop, self._wake = threading.Event(), threading.Event()
        self._guard = threading.Lock()
        self.recovered: dict | None = None
        self.last: dict | None = None
        self.last_error: str | None = None

    def start(self) -> dict:
        """Recover, then run until ``stop``. Idempotent while running. Returns the recovery result."""
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return {"status": "stopping"} if self._stop.is_set() else (self.recovered or {"status": "running"})
            self._stop.clear()
            try:
                self.recovered = self.driver.recover()
            except Exception as error:       # a recovery failure must not prevent the loop; the next steps retry the work
                self.recovered = {"status": "recover_failed", "error": f"{type(error).__name__}: {error}"}
            self._thread = threading.Thread(target=self._run, name="context-queue-driver", daemon=True)
            self._thread.start()
            return self.recovered

    def wake(self) -> None:
        """New input was admitted (or a control applied): look at the queue now instead of at the next tick."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> dict:
        """Ask the loop to end and wait up to ``timeout`` seconds. ``in_flight`` is True when a step (for example a
        model run) is still going; nothing was cancelled and the next ``start`` reconciles it mechanically."""
        with self._guard:
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if thread is not None:
            thread.join(timeout)
        in_flight = thread is not None and thread.is_alive()
        if not in_flight:
            self._thread = None
        return {"stopped": not in_flight, "in_flight": in_flight}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _delay(self, failures: int) -> float:
        """The wait after ``failures`` consecutive stalled or failed steps: doubling from the poll interval, never above the
        ceiling, for any failure count."""
        return min(self.poll_seconds * 2 ** min(failures, self._ceiling), self.max_backoff_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            delay = self.poll_seconds
            try:
                self.last, self.last_error = self.driver.process_next(), None
                if self.last["status"] in _STALLED:
                    self.failures = min(self.failures + 1, self._ceiling)
                    delay = self._delay(self.failures)
                else:
                    self.failures = 0
                    if self.last["status"] in _PROGRESS:
                        continue
            except Exception as error:       # the driver reports expected outcomes; an exception is unexpected and retried
                self.failures = min(self.failures + 1, self._ceiling)
                self.last_error = f"{type(error).__name__}: {error}"
                delay = self._delay(self.failures)
            self._wake.wait(delay)
            self._wake.clear()

    def diagnostics(self) -> dict:
        return {"running": self.running, "recovered": self.recovered, "last": self.last, "last_error": self.last_error,
                "consecutive_failures": self.failures, "poll_seconds": self.poll_seconds, "observed_at": time.time()}
