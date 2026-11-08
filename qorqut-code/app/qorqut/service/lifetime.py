"""The one ordered owner of the retained host services' lifetime.

``Runtime.start``/``Runtime.stop`` delegate here, over one ``LifetimeState`` the runtime caches, so a later call always
sees what an earlier one left behind.

Starting first takes the instance's exclusive ownership (``service.ownership``): nothing else, not target validation, not
prepared-effect or native-input recovery, not a principal write and not a worker, happens in a backend that does not own the
instance. Mutation admission is refused from assembly until ownership is held and the stored target and maintenance state are
validated; only that owned path (``own``, which ``start`` and the CLI preparation share) admits it. Starting then reconciles
every already prepared effect and starts the retained services in their fixed order; starting again while running verifies
the held ownership and does nothing else. A failure while starting refuses new mutations first (admitted receipts finish),
then unwinds exactly the services this attempt started, recovery orchestrators first and the rest newest first, including
the service whose start raised (it may have begun work), and leaves everything else alone. An orchestrator whose stop is not
proved keeps every other started service where it is. The original error is always the one raised, annotated with the cleanup
evidence. A service whose unwind is not *proved* (its stop
raised, timed out or returned without positive evidence) is recorded as incomplete in the persistent state; SQLite and
ownership stay with it, and no new start is accepted until a later ``stop`` proves it drained.

Stopping separates refusing new work from draining admitted work: it first begins the persistent shutdown refusal of new
external mutations (waiting for nothing; a recovery window ending cannot lift it), then asks each service to stop in the
retained drain order so that already admitted effects finish their receipts with the resources they own (the recovery
orchestrators, whose in-flight capture and thaw own the other services, go first and both are asked), then seals write
admission and waits for the admitted writers, and closes SQLite last. Every
service stop must return positive evidence (``service.stopping``); a service that cannot prove its drain makes the shutdown
*incomplete*: the database is left open beneath the possibly still running writer, ownership is kept, the other services are
still stopped, and the caller receives ``ShutdownIncomplete`` with the full report. Only a complete drain closes SQLite,
releases ownership and ends the lifetime; a stopped runtime is not started again.
Native product sessions are not owned here and survive shutdown: stopping a service only drains the host's writer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import threading

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import QuiescenceProof, UnprovenStop

LOGGER = logging.getLogger(__name__)
ADMISSION_DRAIN_SECONDS = 5


@dataclass(frozen=True)
class ServiceStep:
    """One retained service: how it starts and how it drains. ``absent`` names the existing reason an optional
    service is not installed in this composition (``service`` is then ``None`` and the step is skipped).

    ``stop`` is the explicit stop adapter ``stop(name, service, timeout)``: it returns a ``QuiescenceProof`` only on
    positive evidence and raises otherwise. ``required`` marks a writer class every composition has (a missing service
    is then an incomplete composition, not an absence). ``recovery`` says how the step takes part in the recovery
    barrier: ``writer`` (drained and resumed by it), ``engine`` (the barrier's own named slot holds its controller) or
    ``caller`` (a recovery orchestrator that asks for the barrier from its own thread, such as recovery administration
    or the nightly capture: the barrier never drains it, and the lifetime stops it before the services it quiesces).
    ``running`` tells whether it is running now."""

    name: str
    service: object | None
    start: Callable[[], object] | None = None
    stop: Callable[[str, object, float], object] | None = None
    absent: str | None = None
    timeout: float = 5
    required: bool = False
    recovery: str = "writer"
    running: Callable[[object], bool] | None = None

    @property
    def installed(self):
        return self.service is not None

    @property
    def state(self):
        if not self.installed:
            return "absent: " + (self.absent or "not configured")
        return "disabled" if getattr(self.service, "enabled", True) is False else "installed"

    def drain(self, timeout=None):
        """Stop the service and return the positive evidence, or raise (``UnprovenStop`` for an absent or false proof)."""
        proof = self.stop(self.name, self.service, self.timeout if timeout is None else timeout)
        if not (isinstance(proof, QuiescenceProof) and proof.controller == self.name and proof.state == "drained"):
            raise UnprovenStop(f"{self.name}: the stop adapter returned no positive drain evidence")
        return proof


@dataclass(frozen=True)
class ShutdownReport:
    """What a stop attempt did. ``clean`` is true only when every service drained, admission was sealed and drained,
    the database was closed and instance ownership was released. ``held`` names the services left untouched because a
    recovery orchestrator above them did not prove its drain."""

    stopped: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    admission_closed: bool = False
    database_closed: bool = False
    ownership_released: bool = False
    held: tuple[str, ...] = ()

    @property
    def clean(self):
        return not self.failed and self.admission_closed and self.database_closed and self.ownership_released


class ShutdownIncomplete(RuntimeError):
    """A writer drain failed or could not be proved. The database was deliberately left open."""

    def __init__(self, report: ShutdownReport):
        names = ", ".join(name for name, _ in report.failed) or "write admission"
        super().__init__(f"Shutdown is incomplete; the database remains open: {names}")
        self.report = report


@dataclass
class LifetimeState:
    """What survives any single ``start``/``stop`` call: ownership, the running services, the services whose drain is
    unproven (name -> evidence) and the phase (``new``, ``running``, ``failed_start``, ``stopping``, ``stopped``)."""

    ownership: object | None = None
    running: set = field(default_factory=set)
    incomplete: dict = field(default_factory=dict)
    phase: str = "new"
    report: ShutdownReport | None = None
    mutex: threading.RLock = field(default_factory=threading.RLock, repr=False)


@dataclass
class ServiceLifetime:
    """``prepare`` steps run after target validation and before any service starts; ``steps`` are in start order and
    drain in ``stop_order`` (names)."""

    database: object
    admission: object | None
    validate: Callable[[], object]
    prepare: tuple[Callable[[], object], ...]
    steps: tuple[ServiceStep, ...]
    stop_order: tuple[str, ...]
    state: LifetimeState

    @property
    def running(self):
        return self.state.running

    def inventory(self):
        """Every retained service with its installed/disabled/absent state, in start order."""
        return tuple((step.name, step.state) for step in self.steps)

    def _guard(self):
        """The refusals every owned entry shares; call under the mutex."""
        state = self.state
        if state.phase in ("stopping", "stopped"):
            raise DomainError("lifetime_closed", "This runtime's lifetime has ended or is being shut down; assemble a new one")
        if state.incomplete:
            raise DomainError("lifetime_incomplete", "A service whose drain is unproven remains ("
                              + ", ".join(sorted(state.incomplete)) + "); complete stop() before starting again")
        if state.ownership is None:
            raise DomainError("ownership_unavailable", "This runtime has no instance ownership to take")

    def _admit(self):
        """Validation, then admission, once ownership is held. A failure leaves admission refused."""
        try:
            self.validate()
            if self.admission is not None:
                self.admission.admit()
        except BaseException:
            if self.admission is not None:
                self.admission.withhold()
            raise

    def own(self):
        """The owned entry without services: take the instance, validate the target and admit mutations. Used by the CLI
        preparation, which starts no service; a serving backend goes through ``start``."""
        with self.state.mutex:
            self._guard()
            # Before anything can validate, recover, register, admit or run; a refusal here leaves nothing behind.
            self.state.ownership.acquire()
            self._admit()
            return self

    def start(self):
        state = self.state
        with state.mutex:
            if state.phase == "running":
                # Already started: the held ownership is the only thing to check; recovery and preparation never repeat
                # beneath running writers.
                state.ownership.verify()
                return self
            self._guard()
            # Before anything can validate, recover, register, admit or run; a refusal here leaves nothing behind.
            state.ownership.acquire()
            started = []
            try:
                self._admit()
                for prepare in self.prepare:
                    prepare()
                for step in self.steps:
                    if not step.installed or step.start is None or step.name in state.running:
                        continue
                    started.append(step)
                    step.start()
            except BaseException as error:
                # New mutations are refused before any cleanup, while admitted receipts keep settling.
                if self.admission is not None:
                    self.admission.withhold()
                self._unwind(started, error)
                state.phase = "failed_start"
                raise
            state.running.update(step.name for step in started)
            state.phase = "running"
            return self

    def release(self):
        """End the CLI preparation: refuse mutations again and give the instance back (no service was started)."""
        with self.state.mutex:
            if self.admission is not None:
                self.admission.withhold()
            if self.state.phase == "new" and self.state.ownership is not None:
                self.state.ownership.release()

    def _unwind(self, started, error):
        """Stop only what this attempt started (the failing step included: it may have started a thread before it
        raised): recovery orchestrators first, then the rest newest first, keeping proof of each. A step is unwound
        only by positive evidence; one that is not is incomplete, and the database and ownership stay with it. An
        orchestrator that is not proved keeps everything else this attempt started where it is: its capture or thaw
        owns those services. The original failure is never replaced."""
        stopped, unproven, held = [], [], []
        order = [step for step in reversed(started) if step.recovery == "caller"] + [
            step for step in reversed(started) if step.recovery != "caller"]
        blocked = None
        for step in order:
            if blocked is not None and step.recovery != "caller":
                held.append(step.name)
                self.state.incomplete[step.name] = f"held beneath the unproven {blocked}"
                continue
            if step.stop is None:
                continue
            try:
                step.drain()
                stopped.append(step.name)
            except Exception as unwinding:
                LOGGER.warning("Service %s did not unwind after a failed start: %s", step.name, unwinding)
                self.state.incomplete[step.name] = repr(unwinding)
                unproven.append(f"{step.name}: {unwinding!r}")
                if step.recovery == "caller":
                    blocked = blocked or step.name
        if held:
            self.state.running.update(held)                  # still running, and stop() must drain them
        error.add_note("backend cleanup after the failed start: " + (
            f"unwound {', '.join(stopped) or 'nothing'}; "
            + (f"unproven {'; '.join(unproven)}; " if unproven else "")
            + (f"held beneath an unproven recovery orchestrator {', '.join(held)}; " if held else "")
            + "SQLite stays open and instance ownership is held"))

    def stop(self):
        state = self.state
        with state.mutex:
            if state.phase == "stopped" and state.report is not None:
                return state.report
            state.phase = "stopping"
            by_name = {step.name: step for step in self.steps}
            order = [by_name[name] for name in self.stop_order]
            if {step.name for step in order} != set(by_name):
                raise ValueError("The stop order must name every retained service exactly once")
            if self.admission is not None:
                # Refuse new external mutations first, persistently: a recovery window thawing while this waits cannot
                # reopen them. Admitted work and its nested receipt writes keep running.
                self.admission.begin_shutdown()
            stopped, failed, held, blocked = [], [], [], False
            # The recovery orchestrators drain first, every one of them asked; the services their capture or thaw owns
            # are touched only once all of them are proved.
            order = [step for step in order if step.recovery == "caller"] + [step for step in order if step.recovery != "caller"]
            for step in order:
                if blocked and step.recovery != "caller":
                    if step.installed and step.stop is not None:
                        held.append(step.name)
                    continue
                if not step.installed or step.stop is None:
                    continue
                try:
                    step.drain()
                    stopped.append(step.name)
                    state.incomplete.pop(step.name, None)
                except BaseException as error:
                    if not isinstance(error, Exception):
                        raise
                    LOGGER.warning("Service %s could not prove its drain: %r", step.name, error)
                    failed.append((step.name, repr(error)))
                    state.incomplete[step.name] = repr(error)
                    if step.recovery == "caller":
                        # An orchestrator whose capture or thaw may still be running owns the services below it: none of
                        # them is stopped, admission is not sealed and SQLite stays open beneath it.
                        blocked = True
            admission_closed = False
            if blocked:
                pass
            elif self.admission is not None:
                try:
                    # Seal admission (no reopening) and wait for the writers it already admitted; a writer that does
                    # not drain is an unproven drain like any service's.
                    self.admission.seal(timeout=ADMISSION_DRAIN_SECONDS)
                    admission_closed = True
                    state.incomplete.pop("write admission", None)
                except TimeoutError as error:
                    failed.append(("write admission", repr(error)))
                    state.incomplete["write admission"] = repr(error)
            else:
                admission_closed = True
            database_closed = released = False
            if not failed:
                self.database.close()
                database_closed = True
                state.running.clear()
                state.incomplete.clear()
                if state.ownership is not None:
                    state.ownership.release()
                released = True
                state.phase = "stopped"
            report = ShutdownReport(tuple(stopped), tuple(failed), admission_closed, database_closed, released, tuple(held))
            if not report.clean:
                raise ShutdownIncomplete(report)
            state.report = report
            return report
