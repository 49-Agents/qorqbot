"""Generic writer barrier for full-installation recovery.

The caller must supply concrete writer controllers.  SQLite claims are only a last check: they do not identify
arbitrary processes which can still write a business checkout or persistent home.

This module owns the barrier's ordering and its refusal rules: the installation exclusive lock, the database
maintenance gate, the shared in-process admission (``qorqut_operations.storage.admission``), one verified
``QuiescenceProof`` per controller, and the rule that an unverified or failed drain/thaw leaves both the gate and
admission closed. Docker, container and process inventory and their freeze/thaw implementations are host
controllers passed in as ``host_writers`` and ``container_writers``; a missing controller is a refusal, never a
bypass. Constructing a quiescer starts and stops nothing.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Protocol

from qorqut_operations.contracts import DomainError
from .admission import RecoveryAdmission
from .recovery_gate import DatabaseMaintenanceGate


@dataclass(frozen=True)
class QuiescenceProof:
    """A controller's observed, bounded drain or freeze outcome."""
    controller: str
    state: str


class DrainController(Protocol):
    """A writer that can be drained and later resumed (a service with ``stop``/``start`` via ``BoundedWriter``,
    or a host controller such as the browser/sandbox leases)."""

    def drain(self, *, timeout) -> "QuiescenceProof": ...

    def resume(self) -> None: ...


class FreezeController(Protocol):
    """A host controller that freezes writers it can only pause (host terminals, containers) and thaws them.
    ``host_writers`` provides ``inventory_and_freeze``; ``container_writers`` provides ``freeze``."""

    def thaw(self) -> None: ...


class BoundedWriter:
    """Adapt a known stop/join service to the recovery controller contract."""

    def __init__(self, writer, name):
        self.writer, self.name = writer, name
        self._resume_required = None

    def drain(self, *, timeout):
        if self._resume_required is None:
            thread = getattr(self.writer, "_thread", None)
            self._resume_required = bool(thread and thread.is_alive())
        self.writer.stop(timeout=timeout)
        return QuiescenceProof(self.name, "drained")

    def resume(self):
        if self._resume_required:
            self.writer.start()
        self._resume_required = None


class KnownZeroWriters:
    """Explicit trusted inventory result for a writer class known to be absent."""

    def __init__(self, name):
        self.name = name

    def drain(self, *, timeout):
        return QuiescenceProof(self.name, "drained")

    def resume(self):
        return None


@dataclass
class RecoveryQuiescer:
    """Yield only after concrete writers have stopped or frozen for recovery.

    Browser and sandbox controllers must drain their single-writer leases before
    returning from ``stop``. ``host_writers`` is mandatory whenever inventory
    finds legacy host writers: this barrier never treats resource claims or PID
    labels as proof that such terminals are harmless.
    """
    database: object
    admission: RecoveryAdmission | None = None
    background: object | None = None
    signoffs: object | None = None
    email_history: object | None = None
    # Optional writer: an installation without the Telegram bridge has nothing to drain.
    telegram: object | None = None
    content_sessions: object | None = None
    browser_writers: object | None = None
    sandbox_writers: object | None = None
    container_writers: object | None = None
    host_writers: object | None = None
    installation_lock: object | None = None
    maintenance_gate: object | None = None

    def _claims_clear(self):
        with self.database.read() as uow:
            return not uow.connection.execute("SELECT 1 FROM resource_claims LIMIT 1").fetchone()

    @staticmethod
    def _proof(value, expected):
        if not isinstance(value, QuiescenceProof) or value.state != expected:
            raise DomainError("recovery_not_quiescent", "Recovery controller returned no verified quiescence proof")

    @contextmanager
    def quiesce(self, *, timeout=30):
        if self.installation_lock is None:
            raise DomainError("recovery_not_quiescent", "Recovery installation lock is not installed")
        with self.installation_lock.exclusive(timeout=timeout):
            with self._quiesce(timeout=timeout) as value:
                yield value

    @contextmanager
    def _quiesce(self, *, timeout):
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise DomainError("validation_error", "Recovery quiescence timeout must be bounded")
        stopped, frozen, admission_closed = [], [], False
        deadline = monotonic() + timeout
        gate = self.maintenance_gate or DatabaseMaintenanceGate(self.database)
        closure, verified = None, False

        def remaining():
            value = deadline - monotonic()
            if value <= 0:
                raise TimeoutError("recovery writer deadline elapsed")
            return max(1, int(value))

        try:
            if self.admission is None:
                raise DomainError("recovery_not_quiescent", "Recovery mutation admission is not installed")
            # A full installation can contain legacy host terminals. The caller
            # must provide a controller that inventories them; no flag can claim
            # that they do not exist.
            if self.host_writers is None:
                raise DomainError("recovery_not_quiescent", "Recovery legacy-writer inventory is not installed")
            controllers = {"background": self.background, "signoffs": self.signoffs, "email_history": self.email_history,
                           "browser": self.browser_writers, "sandbox": self.sandbox_writers,
                           "containers": self.container_writers}
            missing = [name for name, controller in controllers.items() if controller is None]
            if missing:
                raise DomainError("recovery_not_quiescent", "Recovery writer inventory is incomplete")
            # The installation exclusive lock has drained managed file writers.
            # Closing SQLite admission also rejects independent SQL-only callers.
            # Neither transaction remains open while a controller is stopped.
            closure = gate.close()
            self.admission.close_and_wait(timeout=remaining())
            admission_closed = True
            for writer in (self.background, self.signoffs, self.email_history, self.telegram, self.content_sessions, self.browser_writers, self.sandbox_writers):
                if writer is None:
                    continue
                self._proof(writer.drain(timeout=remaining()), "drained")
                stopped.append(writer)
            self._proof(self.host_writers.inventory_and_freeze(timeout=remaining()), "frozen")
            frozen.append(self.host_writers)
            self._proof(self.container_writers.freeze(timeout=remaining()), "frozen")
            frozen.append(self.container_writers)
            if not self._claims_clear():
                raise DomainError("recovery_not_quiescent", "Durable file effects remain in progress")
            verified = True
            yield self
        except TimeoutError as exc:
            raise DomainError("recovery_not_quiescent", "A writer did not drain before recovery", retryable=True) from exc
        finally:
            resume_error = None
            for writer in reversed(frozen):
                try:
                    writer.thaw()
                except (DomainError, TimeoutError, OSError) as exc:
                    resume_error = resume_error or exc
            # Unknown drain/thaw outcomes retain maintenance and local admission.
            # Reopening after a timeout would advertise a recovery we did not prove.
            if verified and resume_error is None:
                try:
                    # Workers may start their loops while admission is closed;
                    # none can claim work until every restart has succeeded.
                    for writer in reversed(stopped):
                        writer.resume()
                    gate.restore(closure)
                except (DomainError, OSError, TimeoutError, RuntimeError) as exc:
                    resume_error = exc
                else:
                    if admission_closed:
                        self.admission.open()
            if resume_error is not None:
                raise resume_error
