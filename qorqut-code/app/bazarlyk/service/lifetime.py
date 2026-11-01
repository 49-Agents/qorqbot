"""The one ordered owner of the retained host services' lifetime.

``Runtime.start``/``Runtime.stop`` delegate here. Starting validates the stored target and the maintenance state,
reconciles every already prepared effect, and only then starts the retained services in their fixed order. A
failure while starting unwinds exactly the services this attempt started, newest first, and leaves everything else
(including a service an earlier attempt left running) alone.

Stopping closes write admission only after every service has been asked to drain, waits for the writers it already
admitted, and closes SQLite last. A service that cannot prove its drain (it raises, including ``TimeoutError``) makes
the shutdown *incomplete*: the database is left open beneath the possibly still running writer, the other services
are still stopped, and the caller receives ``ShutdownIncomplete`` with the full report instead of a clean stop.
Native product sessions are not owned here and survive shutdown: stopping a service only drains the host's writer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging

LOGGER = logging.getLogger(__name__)
ADMISSION_DRAIN_SECONDS = 5


@dataclass(frozen=True)
class ServiceStep:
    """One retained service: how it starts and how it drains. ``absent`` names the existing reason an optional
    service is not installed in this composition (``service`` is then ``None`` and the step is skipped)."""

    name: str
    service: object | None
    start: Callable[[], object] | None = None
    stop: Callable[[], object] | None = None
    absent: str | None = None

    @property
    def installed(self):
        return self.service is not None

    @property
    def state(self):
        if not self.installed:
            return "absent: " + (self.absent or "not configured")
        return "disabled" if getattr(self.service, "enabled", True) is False else "installed"


@dataclass(frozen=True)
class ShutdownReport:
    """What a stop attempt did. ``clean`` is true only when every service drained, admission was closed and drained,
    and the database was closed."""

    stopped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    admission_closed: bool = False
    database_closed: bool = False

    @property
    def clean(self):
        return not self.failed and self.admission_closed and self.database_closed


class ShutdownIncomplete(RuntimeError):
    """A writer drain failed or could not be proved. The database was deliberately left open."""

    def __init__(self, report: ShutdownReport):
        names = ", ".join(name for name, _ in report.failed) or "write admission"
        super().__init__(f"Shutdown is incomplete; the database remains open: {names}")
        self.report = report


@dataclass
class ServiceLifetime:
    """``prepare`` steps run after target validation and before any service starts; ``steps`` are in start order and
    drain in ``stop_order`` (names). ``running`` is the persistent set of services a completed start left running."""

    database: object
    admission: object | None
    validate: Callable[[], object]
    prepare: tuple[Callable[[], object], ...]
    steps: tuple[ServiceStep, ...]
    stop_order: tuple[str, ...]
    running: set = field(default_factory=set)

    def inventory(self):
        """Every retained service with its installed/disabled/absent state, in start order."""
        return tuple((step.name, step.state) for step in self.steps)

    def start(self):
        self.validate()
        started = []
        try:
            for prepare in self.prepare:
                prepare()
            for step in self.steps:
                if not step.installed or step.start is None or step.name in self.running:
                    continue
                started.append(step)
                step.start()
        except BaseException as error:
            self._unwind(started, error)
            raise
        self.running.update(step.name for step in started)
        return self

    def _unwind(self, started, error):
        """Stop only what this attempt started (the failing step included: it may have started a thread before it
        raised), newest first. The original failure is never replaced by an unwinding failure."""
        for step in reversed(started):
            try:
                if step.stop is not None:
                    step.stop()
            except Exception as unwinding:
                LOGGER.warning("Service %s did not unwind after a failed start: %s", step.name, unwinding)
                error.add_note(f"service {step.name} did not unwind: {unwinding!r}")

    def stop(self):
        by_name = {step.name: step for step in self.steps}
        order = [by_name[name] for name in self.stop_order]
        if {step.name for step in order} != set(by_name):
            raise ValueError("The stop order must name every retained service exactly once")
        stopped, failed = [], []
        for step in order:
            if not step.installed or step.stop is None:
                continue
            try:
                step.stop()
                stopped.append(step.name)
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                LOGGER.warning("Service %s could not prove its drain: %r", step.name, error)
                failed.append((step.name, repr(error)))
        admission_closed = False
        if self.admission is not None:
            try:
                # Reject new writes and wait for the ones already admitted; a writer that does not drain is an
                # unproven drain like any service's.
                self.admission.close_and_wait(timeout=ADMISSION_DRAIN_SECONDS)
                admission_closed = True
            except TimeoutError as error:
                failed.append(("write admission", repr(error)))
        else:
            admission_closed = True
        database_closed = False
        if not failed:
            self.database.close()
            database_closed = True
            self.running.clear()
        report = ShutdownReport(tuple(stopped), tuple(failed), admission_closed, database_closed)
        if not report.clean:
            raise ShutdownIncomplete(report)
        return report
