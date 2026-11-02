"""An inert, coalescing history observer: commit hints plus a bounded periodic scan, no database and no routine.

``notify_commit()`` is called by the host only after an Operations transaction has closed; it merely marks a pass as
wanted. One background thread waits on a coalesced wake (or, failing that, the periodic interval) and then runs the
injected ``snapshot`` callable, so a burst of hints during a snapshot leaves exactly one further pass and a missed
hint is recovered by the next periodic pass. Automatic passes keep a monotonic minimum gap: no pass begins before
``min_gap`` seconds after the previous one finished, however many hints arrive, and a failed pass waits at least the
larger of that gap and ``retry_delay``, so there is neither a back-to-back nor a tight retry loop. Hints that arrive
during a pass or its cooldown stay coalesced in the wake and are served by the one pass that follows the deadline; the
first pass after ``start`` is prompt. ``stop`` is bounded and explicit, interrupts a cooldown at once, never closes
anything and never claims a stopped thread while a snapshot is still running. The same object can be started again, but
only after a ``stop`` that has positively reported the worker thread ended.

Nothing here starts on construction, registers with the existing change publisher or schedules a user routine;
starting it is the host owner's later decision.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time

__all__ = ["HistoryObserver", "ObserverState", "ObserverStop"]

_LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 60.0


@dataclass(frozen=True)
class ObserverState:
    started: bool
    stopping: bool
    snapshot_running: bool
    pending: bool
    passes: int
    failures: int
    last_error: str | None


@dataclass(frozen=True)
class ObserverStop:
    """The honest outcome of ``stop``: ``stopped`` is true only when the worker thread has actually ended."""

    stopped: bool
    snapshot_running: bool
    pending: bool
    last_error: str | None


class HistoryObserver:
    """Run ``snapshot`` after commit hints and on a bounded periodic scan.

    ``snapshot`` is a zero-argument callable (normally ``History.snapshot``) and may raise; the exception is recorded,
    never propagated. ``wake`` is a ``threading.Event``-compatible object (``set``/``clear``/``wait``), ``pause`` a
    bounded interruptible delay callable and ``clock`` a monotonic seconds reading; all three are injectable for focused
    tests. The first pass runs promptly after ``start`` so changes made while the host was down (or while the observer was
    stopped) are reconciled; it is not delayed by an earlier generation's cooldown.

    ``min_gap`` (default: ``interval``) is the least time between the end of one automatic pass and the start of the next.
    After a failed pass the least time is the larger of ``min_gap`` and ``retry_delay``. The deadline is measured on the
    monotonic clock from the moment the pass finished and applies to hints, the periodic scan and retries alike, so the
    periodic scan still recovers a missed hint after ``max(interval, min_gap)`` rather than after their sum. A cooldown only
    defers a pass; hints sent meanwhile remain coalesced, and ``stop`` interrupts the wait.

    The observer is restartable: ``start`` is accepted when it has never been started or when the last ``stop`` returned
    ``stopped=True``. It is refused while a worker thread may still exist (running, or stopped without proof, even if
    that thread has since ended: only a later ``stop`` that reports ``stopped=True`` confirms it) and while a ``start`` or
    ``stop`` is in progress. Each start runs one worker generation with its own stop signal, so a signal is never cleared
    under a thread that may still own it. Counters and the last error survive a restart; a pass that was merely pending
    at stop is abandoned because the next start's initial pass reconciles the current state."""

    def __init__(self, snapshot: Callable[[], object], *, interval: float = DEFAULT_INTERVAL,
                 retry_delay: float | None = None, min_gap: float | None = None, wake=None,
                 pause: Callable[[float], object] | None = None, clock: Callable[[], float] | None = None):
        if not callable(snapshot):
            raise TypeError("The observer needs a snapshot callable")
        if not 0 < interval <= 3600:
            raise ValueError("The periodic scan interval is positive and bounded")
        retry = interval if retry_delay is None else retry_delay
        if not 0 <= retry <= 3600:
            raise ValueError("The retry delay is non-negative and bounded")
        gap = interval if min_gap is None else min_gap
        if not 0 <= gap <= 3600:
            raise ValueError("The minimum gap between passes is non-negative and bounded")
        self._snapshot, self._interval, self._retry, self._gap = snapshot, interval, retry, gap
        self._wake = wake if wake is not None else threading.Event()
        self._pause = pause
        self._clock = clock if clock is not None else time.monotonic
        # ``_transition`` serializes start and stop, including a stop's join; the worker never takes it. ``_lock`` guards the
        # state the worker also updates and is never held across a join.
        self._transition = threading.Lock()
        self._lock = threading.Lock()
        self._stop = threading.Event()          # the current generation's signal; replaced, never cleared, by a start
        self._thread: threading.Thread | None = None  # set from start until a stop has confirmed the thread ended
        self._pending = self._running = False
        self._passes = self._failures = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if not self._transition.acquire(blocking=False):
            raise RuntimeError("The history observer is starting or stopping")
        try:
            with self._lock:
                if self._thread is not None:
                    raise RuntimeError("The history observer is already started or its previous stop is not confirmed; "
                                       "call stop until it reports stopped")
                if self._stop.is_set():
                    self._stop = threading.Event()
                stop = self._stop
                self._pending = True
                self._wake.set()
                thread = self._thread = threading.Thread(target=self._loop, args=(stop,), name="operations-history-observer",
                                                         daemon=True)
            try:
                thread.start()
            except BaseException:
                with self._lock:
                    self._thread = None
                raise
        finally:
            self._transition.release()

    def notify_commit(self) -> None:
        """Advisory hint that an Operations transaction has closed. Never blocks, never raises, coalesces."""
        with self._lock:
            if self._stop.is_set():
                return
            self._pending = True
            self._wake.set()

    def stop(self, timeout: float) -> ObserverStop:
        """Ask the worker to end and wait at most ``timeout`` seconds for it.

        A snapshot already running is allowed to finish within the timeout; nothing new is started and a pass that was
        still only pending is abandoned (the next start's first pass recovers it). ``stopped`` is true only when the worker
        thread has been confirmed ended (or never existed); otherwise ``snapshot_running`` says whether a snapshot is the
        reason, the observer cannot be started, and a later ``stop`` can still confirm. A ``stop`` that cannot begin within
        ``timeout`` because a start or another stop is in progress changes nothing and reports ``stopped=False``."""
        if timeout < 0:
            raise ValueError("The stop timeout is not negative")
        deadline = time.monotonic() + timeout
        if not self._transition.acquire(timeout=timeout):
            with self._lock:
                return ObserverStop(False, self._running, self._pending, self._last_error)
        try:
            with self._lock:
                thread = self._thread
                self._stop.set()
            self._wake.set()
            if thread is not None:
                thread.join(max(0.0, deadline - time.monotonic()))
            with self._lock:
                if thread is not None and not thread.is_alive():
                    self._thread = None
                return ObserverStop(self._thread is None, self._running, self._pending, self._last_error)
        finally:
            self._transition.release()

    def state(self) -> ObserverState:
        with self._lock:
            return ObserverState(self._thread is not None, self._thread is not None and self._stop.is_set(), self._running,
                                 self._pending, self._passes, self._failures, self._last_error)

    def _loop(self, stop: threading.Event) -> None:
        # The deadline belongs to this generation: the first pass after a start is prompt and a stop clears nothing shared.
        not_before: float | None = None
        while not stop.is_set():
            self._wake.wait(self._interval)
            if not_before is not None and not stop.is_set():
                # A hint or the periodic scan woke us inside the cooldown: wait out only what remains, so neither can
                # bring the next pass forward. Further hints just keep the wake set and stay coalesced; a stop ends the wait.
                remaining = not_before - self._clock()
                if remaining > 0:
                    (self._pause if self._pause is not None else stop.wait)(remaining)
            # Hints that arrive after this point leave the wake set and so schedule exactly one further pass. The stop
            # signal is read under the lock that sets it, so no pass begins after a stop was requested.
            with self._lock:
                if stop.is_set():
                    break
                self._wake.clear()
                self._pending = False
                self._running = True
            not_before = self._run_pass()

    def _run_pass(self) -> float:
        """Run one snapshot and return the monotonic reading before which the next automatic pass must not begin."""
        failed = False
        try:
            self._snapshot()
        except Exception as error:
            failed = True
            code = getattr(error, "code", type(error).__name__)
            _LOGGER.warning("History snapshot pass failed: %s", code)
            with self._lock:
                self._failures += 1
                self._last_error = str(code)
        else:
            with self._lock:
                self._last_error = None
        finally:
            finished = self._clock()
            with self._lock:
                self._running = False
                self._passes += 1
        # After a failure the existing retry delay still applies when it is longer than the gap.
        return finished + (max(self._gap, self._retry) if failed else self._gap)
