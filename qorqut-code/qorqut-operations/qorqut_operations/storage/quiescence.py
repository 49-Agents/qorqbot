"""Generic writer barrier for full-installation recovery.

The caller must supply concrete writer controllers.  SQLite claims are only a last check: they do not identify
arbitrary processes which can still write a business checkout or persistent home.

This module owns the barrier's ordering and its refusal rules. The order is: refuse new mutations (waiting for
nothing), drain every writer controller so already admitted work finishes its receipts with the resources it owns,
and only then take the installation exclusive lock, wait for the admitted units to leave, close the database
maintenance gate and freeze the host and container writers. Closing the gate or taking the lock first would make
an admitted receipt unable to complete behind the maintenance state it was admitted before. Each controller
yields one verified ``QuiescenceProof``; a void ``stop`` is proof only by its documented stop/join contract and
any other result is none. An unverified or failed drain/thaw leaves both the gate and admission closed. The
writer inventory is complete or the barrier refuses: every required writer class has a concrete controller or a
``KnownZeroWriters`` whose emptiness is verified when the barrier runs, and a missing one is never read as zero.
Process inventory and its freeze/thaw implementation are a host controller passed in as ``host_writers``; a host
that also runs writers it can only pause (containers) passes their controller as the optional ``container_writers``.
Constructing a quiescer starts and stops nothing.

A window yields one ``RecoveryBarrierProof``, issued only after every drain and freeze succeeded, the maintenance and
admission boundary is closed, the claims are clear and the exclusive installation lock is held. The proof is not a
flag: it is the issuing quiescer's one current active generation, bound to the creating process and thread and to the
frozen source database's canonical path and file identity, and it is revoked before anything is thawed, resumed or
unlocked, on every exit. A recovery capture presents it (``verify_active``, ``require_database``) wherever it must prove
that the installation is still quiescent. It coordinates trusted code in this process; it is not isolation from code
running as the same operating-system user.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import threading
from time import monotonic
from typing import Protocol

from qorqut_operations.contracts import DomainError
from .admission import RecoveryAdmission
from .recovery_gate import DatabaseMaintenanceGate

LOGGER = logging.getLogger(__name__)
# The writer classes every installation of the engine has; a composition declares more through ``required``.
CORE_WRITERS = ("background", "signoffs", "browser")


class UnprovenStop(TimeoutError):
    """A stop returned (or reported) without positive evidence that the worker stopped; it is a failed drain like a
    join timeout, never a stop."""


@dataclass(frozen=True)
class QuiescenceProof:
    """A controller's observed, bounded drain or freeze outcome."""
    controller: str
    state: str


@dataclass(frozen=True)
class DatabaseIdentity:
    """A database file as the operating system identifies it: canonical path, device and inode."""
    path: Path
    device: int
    inode: int

    @classmethod
    def of(cls, path):
        try:
            resolved = Path(path).resolve(strict=True)
            info = resolved.stat()
        except (OSError, TypeError, ValueError):
            raise DomainError("recovery_not_quiescent", "Recovery database identity is unavailable") from None
        return cls(resolved, info.st_dev, info.st_ino)


@dataclass(frozen=True, eq=False)
class RecoveryBarrierProof:
    """The active proof that a ``RecoveryQuiescer`` window is established, yielded by ``quiesce`` and by nothing else.

    It has no switch a caller can set: it is active only while it is the issuing quiescer's current proof, in the
    process and thread that created it, with the database file it froze. ``controllers`` (the verified drain and freeze
    proofs) and ``source`` are read-only evidence, never authorization by themselves."""
    generation: int
    process_id: int
    thread_id: int
    source: DatabaseIdentity
    controllers: tuple
    issuer: object = field(repr=False)

    def verify_active(self):
        """Raise ``recovery_not_quiescent`` unless this exact proof is the issuing barrier's current window."""
        issuer = self.issuer
        if issuer._active is not self or issuer._generation != self.generation:
            raise DomainError("recovery_not_quiescent", "The recovery barrier proof is not the active window")
        if (os.getpid(), threading.get_ident()) != (self.process_id, self.thread_id):
            raise DomainError("recovery_not_quiescent", "The recovery barrier proof belongs to another process or thread")
        if not getattr(issuer.admission, "refusing", False):
            raise DomainError("recovery_not_quiescent", "Write admission is no longer closed for the recovery window")
        if DatabaseIdentity.of(self.source.path) != self.source:
            raise DomainError("recovery_not_quiescent", "The frozen database file was replaced during the recovery window")

    def require_database(self, database):
        """Verify the window, then that ``database`` is the very file it froze (canonical path and file identity)."""
        self.verify_active()
        if DatabaseIdentity.of(getattr(database, "path", None)) != self.source:
            raise DomainError("recovery_not_quiescent", "The database is not the one this recovery barrier froze")


class DrainController(Protocol):
    """A writer that can be drained and later resumed (a service with ``stop``/``start`` via ``BoundedWriter``,
    or a host controller such as the browser leases)."""

    def drain(self, *, timeout) -> "QuiescenceProof": ...

    def resume(self) -> None: ...


class FreezeController(Protocol):
    """A host controller that freezes writers it can only pause (host terminals, containers) and thaws them.
    ``host_writers`` provides ``inventory_and_freeze``; the optional ``container_writers`` provides ``freeze``."""

    def thaw(self) -> None: ...


def void_stop(writer, timeout):
    """The documented contract of a void-return service: ``stop(timeout=...)`` joins its worker, raises
    ``TimeoutError`` if the worker survived and otherwise returns ``None``. Any other result is not stop evidence."""
    result = writer.stop(timeout=timeout)
    if result is not None:
        raise UnprovenStop(f"{type(writer).__name__}.stop returned {type(result).__name__}, not the void stop evidence")


def worker_alive(writer):
    thread = getattr(writer, "_thread", None)
    return bool(thread and thread.is_alive())


class BoundedWriter:
    """Adapt a known stop/join service to the recovery controller contract.

    ``stop`` is the service's explicit stop adapter: ``stop(writer, timeout)`` returns normally only once the worker
    has positively stopped and raises (``TimeoutError``, including ``UnprovenStop``) otherwise; the default is the
    void contract above. ``running`` says whether the service was running before the barrier, so only work that was
    actually running is restarted by ``start(writer)``, and only after the capture is complete and thawed."""

    def __init__(self, writer, name, *, stop=void_stop, running=worker_alive, start=lambda writer: writer.start()):
        self.writer, self.name, self._stop, self._running, self._start = writer, name, stop, running, start
        self._resume_required = None

    def drain(self, *, timeout):
        if self._resume_required is None:
            self._resume_required = bool(self._running(self.writer))
        self._stop(self.writer, timeout)
        return QuiescenceProof(self.name, "drained")

    def resume(self):
        if self._resume_required:
            self._start(self.writer)
        self._resume_required = None


class KnownZeroWriters:
    """Explicit inventory result for a writer class that has no writer in this composition.

    ``verify`` is evaluated every time the barrier drains, so the claim is checked against the composition's actual
    state when it matters and a writer that has since appeared is a refusal rather than an unexamined zero."""

    def __init__(self, name, verify=None):
        self.name, self.verify = name, verify

    def drain(self, *, timeout):
        if self.verify is not None and self.verify() is not True:
            raise DomainError("recovery_not_quiescent", f"The {self.name} writer inventory is no longer empty")
        return QuiescenceProof(self.name, "drained")

    def resume(self):
        return None


@dataclass
class RecoveryQuiescer:
    """Yield only after concrete writers have stopped or frozen for recovery.

    The browser controller must drain its single-writer leases before returning from ``drain``.
    ``host_writers`` is mandatory whenever inventory finds legacy host writers: this barrier never treats resource
    claims or PID labels as proof that such terminals are harmless. ``writers`` holds the composition's further
    named controllers (registered through ``register_writer``) and ``required`` the further writer classes whose
    controller must exist; a required class without one, or with an object that cannot drain and resume, refuses
    the barrier before anything is stopped.
    """
    database: object
    admission: RecoveryAdmission | None = None
    background: object | None = None
    signoffs: object | None = None
    # Optional writers of the engine's own inventory: they become required when the composition names them.
    telegram: object | None = None
    browser_writers: object | None = None
    container_writers: object | None = None  # optional: frozen with the host writers when the composition has any
    host_writers: object | None = None
    installation_lock: object | None = None
    maintenance_gate: object | None = None
    writers: dict = field(default_factory=dict)
    required: tuple = ()
    # One window at a time per quiescer: ``_barrier`` refuses a second attempt before it touches anything, ``_generation``
    # numbers the windows and ``_active`` is the issued proof of the current one (None outside a verified window).
    _barrier: object = field(default_factory=threading.Lock, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _active: object = field(default=None, init=False, repr=False)

    def register_writer(self, name, controller):
        """Add one named controller to the inventory; a name is registered once."""
        named = {"background", "signoffs", "telegram", "browser"}
        if not isinstance(name, str) or not name or name in named or name in self.writers:
            raise DomainError("invalid_configuration", f"Recovery writer {name!r} is invalid or already registered")
        self.writers[name] = controller
        return controller

    def inventory(self):
        """The ordered ``(name, controller)`` drains, or a refusal that names every missing or unusable class.

        The composition's registered writers drain first (in registration order, the host's own stop order); the
        engine's classes follow. Nothing is stopped by asking."""
        named = (("background", self.background), ("signoffs", self.signoffs), ("telegram", self.telegram),
                 ("browser", self.browser_writers))
        engine = {name: controller for name, controller in named if controller is not None}
        controllers = {**self.writers, **engine}
        required = dict.fromkeys((*CORE_WRITERS, *self.required))
        problems = [name for name in required if controllers.get(name) is None]
        problems += [name for name, controller in controllers.items()
                     if not (callable(getattr(controller, "drain", None)) and callable(getattr(controller, "resume", None)))]
        if problems:
            raise DomainError("recovery_not_quiescent", "Recovery writer inventory is incomplete: " + ", ".join(sorted(set(problems))))
        return tuple(controllers.items())

    def _claims_clear(self):
        with self.database.read() as uow:
            return not uow.connection.execute("SELECT 1 FROM resource_claims LIMIT 1").fetchone()

    @staticmethod
    def _proof(value, expected):
        if not isinstance(value, QuiescenceProof) or value.state != expected:
            raise DomainError("recovery_not_quiescent", "Recovery controller returned no verified quiescence proof")
        return value

    @contextmanager
    def quiesce(self, *, timeout=30):
        if self.installation_lock is None:
            raise DomainError("recovery_not_quiescent", "Recovery installation lock is not installed")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise DomainError("validation_error", "Recovery quiescence timeout must be bounded")
        if self.admission is None:
            raise DomainError("recovery_not_quiescent", "Recovery mutation admission is not installed")
        if getattr(self.admission, "sealed", False):
            raise DomainError("recovery_not_quiescent", "Write admission is sealed for shutdown")
        if getattr(self.admission, "shutting_down", False):
            raise DomainError("recovery_not_quiescent", "Shutdown has begun; no recovery window can start")
        # A full installation can contain legacy host terminals. The caller must provide a controller that
        # inventories them; no flag can claim that they do not exist.
        if self.host_writers is None:
            raise DomainError("recovery_not_quiescent", "Recovery legacy-writer inventory is not installed")
        controllers = self.inventory()
        gate = self.maintenance_gate or DatabaseMaintenanceGate(self.database)
        source = DatabaseIdentity.of(getattr(self.database, "path", None))
        deadline = monotonic() + timeout
        drained, frozen, evidence = [], [], []
        closure, verified, reopenable = None, False, False

        def remaining():
            value = deadline - monotonic()
            if value <= 0:
                raise TimeoutError("recovery writer deadline elapsed")
            return max(1, int(value))

        # A second window in this process is refused at once and touches nothing: it could otherwise resume the services
        # or reopen admission beneath the window that is running.
        if not self._barrier.acquire(blocking=False):
            raise DomainError("recovery_not_quiescent", "Another recovery barrier is in progress", retryable=True)
        self._generation += 1
        generation = self._generation
        with ExitStack() as locks:
            locks.callback(self._barrier.release)
            try:
                # New external work is refused first and nothing waits on it; admitted work keeps its admission and
                # settles while the controllers drain, because neither the lock nor the gate exists yet.
                self.admission.refuse_new()
                for _name, writer in controllers:
                    evidence.append(self._proof(writer.drain(timeout=remaining()), "drained"))
                    drained.append(writer)
                try:
                    locks.enter_context(self.installation_lock.exclusive(timeout=remaining()))
                except TimeoutError as exc:
                    # Every drain is proved and nothing durable has changed: this refusal can be undone.
                    reopenable = True
                    raise DomainError("recovery_not_quiescent", "Admitted work or another process still holds the installation lock",
                                      retryable=True) from exc
                # The exclusive lock drained managed file writers. Closing SQLite admission also rejects
                # independent SQL-only callers. Neither transaction remains open while a controller is stopped.
                self.admission.close_and_wait(timeout=remaining())
                closure = gate.close()
                evidence.append(self._proof(self.host_writers.inventory_and_freeze(timeout=remaining()), "frozen"))
                frozen.append(self.host_writers)
                if self.container_writers is not None:
                    evidence.append(self._proof(self.container_writers.freeze(timeout=remaining()), "frozen"))
                    frozen.append(self.container_writers)
                if not self._claims_clear():
                    raise DomainError("recovery_not_quiescent", "Durable file effects remain in progress")
                if DatabaseIdentity.of(source.path) != source:
                    raise DomainError("recovery_not_quiescent", "The database file was replaced while the barrier was established")
                verified = True
                # Issued only here: every drain and freeze is proved, the boundary is closed, claims are clear and the
                # exclusive lock is held.
                proof = RecoveryBarrierProof(generation, os.getpid(), threading.get_ident(), source, tuple(evidence), self)
                self._active = proof
                yield proof
            except TimeoutError as exc:
                raise DomainError("recovery_not_quiescent", "A writer did not drain before recovery", retryable=True) from exc
            finally:
                # Revoked first, before anything is thawed, resumed or unlocked, on every exit.
                self._active = None
                resume_error = None
                for writer in reversed(frozen):
                    try:
                        writer.thaw()
                    except (DomainError, TimeoutError, OSError) as exc:
                        resume_error = resume_error or exc
                # Unknown drain/thaw outcomes retain maintenance and local admission.
                # Reopening after a timeout would advertise a recovery we did not prove.
                if (verified or reopenable) and resume_error is None:
                    try:
                        # Workers may start their loops while admission is closed;
                        # none can claim work until every restart has succeeded.
                        for writer in reversed(drained):
                            writer.resume()
                        if verified:
                            gate.restore(closure)
                    except (DomainError, OSError, TimeoutError, RuntimeError) as exc:
                        resume_error = exc
                    else:
                        self.admission.open()
                elif not verified:
                    self.admission.refuse_new()
                    if closure is None:
                        try:
                            gate.close()
                        except Exception as exc:  # noqa: BLE001 - the in-process boundary is already closed
                            LOGGER.warning("The maintenance gate could not be closed after an unproven barrier: %r", exc)
                if resume_error is not None:
                    raise resume_error
