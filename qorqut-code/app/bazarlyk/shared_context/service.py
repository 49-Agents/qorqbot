"""The host's one Context composition and its one lifecycle owner (``runtime.context_services``).

``compose_context`` builds the pieces the stock host needs *before* its domains are registered, and builds every one of them
once: the terminal-outcome and execution-fact contributions, the single ``ContextQueueOperations`` over them (which the
filing ports, the domain factory and the driver all share), the controlled graph-edit handler with the retained
``INSTRUCT_ROLES`` and the application's own authority, and one ``Context``/``Pipeline`` over ``InstancePaths.context`` and
``InstancePaths.context_runtime``. Nothing here touches the disk beyond resolving two paths: no repository, runtime directory,
principal, thread, provider or model is created or contacted by composition, by a read, or by ``Runtime`` construction. The
external question store (``InstancePaths.context_questions``) is only named there: its file is created by the first delivery or
question mutation, never by composition or a read.

``ContextServices`` is the lifecycle contract the host lifetime owns (``start()``, bounded ``stop(timeout=...)``, ``running``,
``wake()``, ``diagnostics()``). Started only by the owner of the instance, it registers the dedicated backend ``system``
principal and the ``context.file`` / ``context.instruct`` completion workers, adding only missing memberships for
unarchived businesses. Identities use the existing stable installation-derived IDs; an existing principal of another
kind or a disabled one is a visible conflict, never repurposed or re-enabled. Existing disabled memberships are preserved.
It runs the existing ``ContextQueueDriver`` through the existing
``ContextDriverLoop`` on its one thread. That thread also runs the bounded, synchronous ``ContextUsageCollector`` before and
after each driver step. It is the only worker: ``stop`` is true exactly when that thread, with any in-flight Context draft, Git
publication, SQLite recording or usage page, has ended. After mechanical recovery and on every step of a ready published Context
the same thread also drains a bounded number of published question batches from the engine's outbox into the external question
store, independently of whether the queue recorded its last outcome; a delivery failure is a diagnostic and a retry on a later
step, never a refiling and never a change to a published event.

The service never initializes anything. While the published Context is missing, uninitialized or unusable, or the registered
coordinator cannot act, queued inputs simply stay queued (or blocked by the driver's own rules) and ordinary operational work
is unaffected: the driver step is not even attempted for a Context that is not ready, so no input is blocked for a repository
that was never there. A blocked input is only ever retried by its explicit control. The only model the Pipeline can run is its
runner: the library's normal adapter by default, or the one an explicit constructor/config injection supplies."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import logging
import os
from pathlib import Path
import stat
import threading
import uuid

from qorqut_context import Context, Pipeline
from qorqut_context.runner import RunnerConfig
from qorqut_operations.application.plans import utc_clock
from qorqut_operations.context_filing import FilingOutcomes
from qorqut_operations.context_graph_edits import ExecutionFacts
from qorqut_operations.context_queue import ContextQueueOperations
from qorqut_operations.contracts import DomainError

from bazarlyk.context_questions import QuestionOutbox, QuestionStore
from bazarlyk.context_usage.collector import ContextUsageCollector
from bazarlyk.background.context_filing import FILE, INSTRUCT
from bazarlyk.domains.context_sources import INSTRUCT_ROLES
from bazarlyk.domains.documents.filing import FilingPorts
from bazarlyk.service.changes import CONTEXT_QUEUE_KIND
from bazarlyk.shared_context.driver import ContextQueueDriver
from bazarlyk.shared_context.graph_edit import GraphEditPorts, GraphEdits
from bazarlyk.shared_context.loop import ContextDriverLoop

LOGGER = logging.getLogger(__name__)

DRIVER_NAME = "Context queue driver"
USAGE_LOCK = "context-usage.lock"
CONFIG_BYTES = 64 * 1024
# The driver's own transactions must not wake the loop that made them.
DRIVER_OPERATION_PREFIX = "context_queue_driver"


def driver_principal_id(installation_id: str) -> str:
    """The stable identity of the backend Context driver: derived from the installation, never from a user or agent."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "qorqut-v1.2:" + installation_id + ":context-driver"))


def runner_config_from_file(path) -> RunnerConfig:
    """The library's ``RunnerConfig`` from an explicit JSON file (only its own field names, validated by the library).

    The file is operator configuration, not a credential store: an absolute path, a regular file that is not a symlink, bounded in
    size. Anything missing keeps the library default."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise DomainError("invalid_configuration", "The Context runner configuration needs an absolute path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise DomainError("invalid_configuration", "The Context runner configuration cannot be read") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DomainError("invalid_configuration", "The Context runner configuration is not a regular file")
        data = os.read(descriptor, CONFIG_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > CONFIG_BYTES:
        raise DomainError("invalid_configuration", "The Context runner configuration is too large")
    try:
        values = json.loads(data)
    except ValueError as error:
        raise DomainError("invalid_configuration", "The Context runner configuration is not valid JSON") from error
    known = {item.name for item in fields(RunnerConfig)}
    if not isinstance(values, dict) or set(values) - known:
        raise DomainError("invalid_configuration", "The Context runner configuration names only RunnerConfig fields")
    try:
        return RunnerConfig(**values)
    except Exception as error:      # the library validates model, effort, tier and every limit
        raise DomainError("invalid_configuration", f"Invalid Context runner configuration: {error}") from error


@dataclass(frozen=True)
class ContextSettings:
    """Explicit Context injection for the host: nothing here is read from a business, the environment or an old default.

    ``runner`` is any object satisfying the library's runner contract (checks inject a deterministic one); ``config`` is the
    library's validated ``RunnerConfig``; neither means the library defaults and its normal provider adapter."""

    runner: object = None
    config: RunnerConfig | None = None
    clock: object = utc_clock
    poll_seconds: float = 2.0
    max_backoff_seconds: float = 60.0
    usage_page_size: int = 100
    usage_max_pages: int = 5

    def resolve(self) -> tuple[object, RunnerConfig]:
        if self.config is not None and not isinstance(self.config, RunnerConfig):
            raise DomainError("invalid_configuration", "The Context configuration must be the library's RunnerConfig")
        runner_config = getattr(self.runner, "config", None) if self.runner is not None else None
        if self.runner is not None and not callable(getattr(self.runner, "run", None)):
            raise DomainError("invalid_configuration", "The Context runner must provide the library's run contract")
        if self.config is not None and runner_config is not None and runner_config != self.config:
            raise DomainError("invalid_configuration", "The Context runner and its configuration must match")
        config = self.config or runner_config or RunnerConfig()
        if not isinstance(config, RunnerConfig):
            raise DomainError("invalid_configuration", "The Context runner configuration is not the library's RunnerConfig")
        return self.runner, config


@dataclass(frozen=True)
class ContextComposition:
    """What ``build_runtime`` hands the domain factory and the lifecycle owner: one of each, shared."""

    context: Context
    pipeline: Pipeline
    outcomes: FilingOutcomes
    executions: ExecutionFacts
    queue: ContextQueueOperations
    filing: FilingPorts
    graph_edits: GraphEdits
    settings: ContextSettings
    questions: QuestionStore | None = None


def compose_context(application, paths, settings: ContextSettings | None = None) -> ContextComposition:
    """Compose the Context ports of one instance. Inert: see the module documentation."""
    settings = settings or ContextSettings()
    runner, config = settings.resolve()
    context = Context(paths.context, paths.context_runtime)
    pipeline = Pipeline(context, runner=runner, config=config)
    outcomes, executions = FilingOutcomes(), ExecutionFacts()
    queue = ContextQueueOperations(outcomes=outcomes, clock=settings.clock, executions=executions,
                                   max_content_bytes=config.source_bytes)
    filing = FilingPorts(queue, source_bytes=config.source_bytes)
    graph_edits = GraphEdits(application, GraphEditPorts(context, source_bytes=config.source_bytes, supervised_pipeline=pipeline), roles=INSTRUCT_ROLES)
    questions = QuestionStore(paths.context_questions)
    return ContextComposition(context, pipeline, outcomes, executions, queue, filing, graph_edits, settings, questions)


class _Steps:
    """The one worker body the loop repeats: usage collection around each driver step, behind the published-Context gate.

    It has exactly the two calls ``ContextDriverLoop`` makes of a driver. It never raises for an unready Context."""

    def __init__(self, service: ContextServices):
        self.service = service

    def recover(self) -> dict:
        return self.service.recover()

    def process_next(self) -> dict:
        return self.service.step()


class ContextServices:
    def __init__(self, composition: ContextComposition, database, paths, *, programs, reader, changes=None):
        self.composition, self.database, self.paths = composition, database, paths
        self.programs, self.reader, self.changes = programs, reader, changes
        self._guard = threading.Lock()
        self._driver_id: str | None = None
        self._driver: ContextQueueDriver | None = None
        self._collector: ContextUsageCollector | None = None
        self._outbox: QuestionOutbox | None = None
        self._loop: ContextDriverLoop | None = None
        self._recovered = False
        self._blocked: dict | None = None
        self._collection_error: dict | None = None
        self._collection_failures = 0
        self._last_gate: dict | None = None

    # -- the lifecycle contract ------------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        loop = self._loop
        return loop is not None and loop.running

    @property
    def driver(self) -> ContextQueueDriver | None:
        """The one queue driver once built (``None`` before ``build``); the loop's only driver."""
        return self._driver

    @property
    def collector(self) -> ContextUsageCollector | None:
        return self._collector

    def build(self) -> str:
        """Register the driver principal and build the driver, collector and loop, without starting any thread. Idempotent.

        Only the owner of the instance calls this (``start`` does). Returns the driver principal ID; raises ``DomainError`` when
        Context progress is blocked (no installation identity, an occupied or disabled driver identity)."""
        with self._guard:
            self._build()
            return self._driver_id

    def start(self) -> dict:
        """Register the driver principal, build the driver and collector once, run mechanical recovery and start the loop.

        Called by the owner of the instance only. Idempotent while running. A condition that blocks Context progress (no
        installation identity, a conflicting driver principal) is reported and diagnosed, not raised: ordinary operational
        work does not depend on it."""
        with self._guard:
            if self._loop is not None and self._loop.running:
                return {"status": "running"}
            try:
                self._build()
            except DomainError as refusal:
                self._blocked = {"code": refusal.code, "message": refusal.message}
                LOGGER.error("context_services_blocked code=%s", refusal.code)
                return {"status": "blocked", **self._blocked}
            self._blocked = None
            if self.changes is not None:
                self.changes.add_listener(self._on_commit)
            loop = self._loop
        # Mechanical recovery runs on this call, outside the guard: a stop request is never queued behind it.
        return {"status": "started", "recovered": loop.start()}

    def stop(self, timeout: float = 5.0) -> dict:
        """Ask the worker to end and wait up to ``timeout`` seconds. ``{stopped, in_flight}``: ``stopped`` only when the
        worker (a Context draft, publication, recording or usage page included) has ended. Nothing is cancelled."""
        with self._guard:
            loop = self._loop
        if loop is None:
            return {"stopped": True, "in_flight": False}
        result = loop.stop(timeout)
        if result["stopped"] and self.changes is not None:
            self.changes.remove_listener(self._on_commit)
        return result

    def wake(self) -> None:
        """New work was admitted or a control applied: look at the queue now instead of at the next tick."""
        loop = self._loop
        if loop is not None:
            loop.wake()

    def diagnostics(self) -> dict:
        """Bounded, side-effect-free facts. It reads, never steps, and never initializes or repairs anything."""
        loop, driver, collector = self._loop, self._driver, self._collector
        outbox = self._outbox
        result = {"state": "blocked" if self._blocked else "running" if self.running else "stopped" if loop else "not_started",
                  "blocked": self._blocked, "driver_id": self._driver_id, "context": self.reader.status(),
                  "gate": self._last_gate, "loop": None if loop is None else loop.diagnostics(),
                  "questions": None if outbox is None else outbox.diagnostics(),
                  "collection": {"failures": self._collection_failures, "last_error": self._collection_error,
                                 "collector": None if collector is None else collector.diagnostics()},
                  "driver": None}
        if driver is not None:
            try:
                result["driver"] = driver.diagnostics()
            except Exception as error:      # diagnostics never raise into a health read
                result["driver"] = {"error": f"{type(error).__name__}"}
        return result

    # -- construction at start -------------------------------------------------------------------------------------------

    def _build(self) -> None:
        if self._loop is not None:
            return
        driver_id = self._register_driver()
        pieces, settings = self.composition, self.composition.settings
        driver = ContextQueueDriver(self.database, pieces.context, pieces.pipeline, driver_id=driver_id, outcomes=pieces.outcomes,
                                    programs=self.programs, graph_authority=pieces.graph_edits.authority)
        collector = ContextUsageCollector(self.database, pieces.pipeline, driver_id=driver_id, lock_path=self.paths.locks / USAGE_LOCK,
                                          page_size=settings.usage_page_size, max_pages=settings.usage_max_pages)
        self._driver_id, self._driver, self._collector = driver_id, driver, collector
        if pieces.questions is not None:
            self._outbox = QuestionOutbox(pieces.context, pieces.pipeline, pieces.questions)
        self._loop = ContextDriverLoop(_Steps(self), poll_seconds=settings.poll_seconds,
                                       max_backoff_seconds=settings.max_backoff_seconds)

    def _register_driver(self) -> str:
        """Register the driver and its filing workers at owned startup, never during reads or admission.

        Converted installations may have the old upload worker but no instruction worker. Both completion
        programs need their stable system identities and memberships before they can admit work. Existing
        disabled principals or memberships are never re-enabled; unrelated programs are untouched.
        """
        with self.database.transaction(operation="context_driver_register") as uow:
            installation = uow.repository("installation").get("service")
            if installation is None:
                raise DomainError("operation_unavailable", "The installation identity is not recorded; Context progress is blocked")
            identity = driver_principal_id(installation["installation_id"])
            existing = uow.repository("principals").get(identity)
            if existing is None:
                uow.repository("principals").insert({"id": identity, "kind": "system", "display_name": DRIVER_NAME})
            elif existing["kind"] != "system" or not existing["active"]:
                raise DomainError("invalid_principal", "The Context driver identity is occupied or disabled; it is not changed")
            authority = self.programs.authority
            if authority is None:
                raise DomainError("operation_unavailable", "The Context filing authority is not installed")
            businesses = uow.connection.execute("SELECT id FROM businesses WHERE archived_at IS NULL").fetchall()
            for program_id in (FILE, INSTRUCT):
                worker_id = authority.worker_id(program_id, uow)
                worker = uow.repository("principals").get(worker_id)
                if worker is None:
                    uow.repository("principals").insert({"id": worker_id, "kind": "system",
                                                         "display_name": program_id + " worker"})
                authority.require_worker(worker_id, None, uow)
                for business in businesses:
                    members = uow.repository("business_memberships", business_id=business["id"])
                    if members.get(worker_id) is None:
                        members.insert({"principal_id": worker_id})
        return identity

    def _on_commit(self, event: dict) -> None:
        """Change-feed hook (non-blocking): a committed operation that can admit or control Context work wakes the loop."""
        operation = event.get("operation") or ""
        if CONTEXT_QUEUE_KIND in event.get("kinds", ()) and not operation.startswith(DRIVER_OPERATION_PREFIX):
            self.wake()

    # -- one worker step -------------------------------------------------------------------------------------------------

    def _ready(self) -> dict | None:
        """``None`` when the published Context can be worked on, else the truthful reason. Read-only: it creates nothing."""
        status = self.reader.status()
        if status["status"] == "ready":
            self._last_gate = None
            return None
        self._last_gate = {"status": "context_" + status["status"], "reason": status.get("reason")}
        return {"status": "not_ready", "reason": "context_" + status["status"], "context": status.get("reason")}

    def recover(self) -> dict:
        """Mechanical reconciliation (never a model run or a new attempt), or why the Context is not ready for it."""
        gate = self._ready()
        if gate is not None:
            return gate
        self._recovered = True
        try:
            return self._driver.recover()
        finally:
            self._drain_questions()

    def step(self) -> dict:
        """One worker step, exactly the loop body: usage collection, one driver step (only for a ready published Context), usage
        collection again. ``build`` must have run. A concurrent step (this process or another) answers ``busy`` from the driver."""
        if self._driver is None:
            raise DomainError("operation_unavailable", "The Context services have not been built")
        self._collect()
        gate = self._ready()
        try:
            if gate is None:
                if not self._recovered:
                    # Recovery was deferred because the Context was not ready at start; it is mechanical and runs once first.
                    self._recovered = True
                    self._driver.recover()
                result = self._driver.process_next()
            else:
                result = gate
        finally:
            if gate is None:
                self._drain_questions()
        self._collect()
        return result

    def _drain_questions(self) -> None:
        """One bounded delivery pass over the published question outbox. A failure is evidence in the diagnostics and a retry on
        a later step: it never raises, refiles, or touches a published event."""
        outbox = self._outbox
        if outbox is None:
            return
        try:
            outbox.drain()
        except Exception as error:
            outbox.last = {"status": "drain_failed", "failed": 1, "errors": [{"event_id": None, "stage": "drain",
                                                                              "error": type(error).__name__}]}
            LOGGER.warning("context_question_drain_failed type=%s", type(error).__name__)

    def _collect(self) -> None:
        """One bounded usage step. A failure is evidence in the diagnostics, never a zero, an erasure or a rerun."""
        try:
            self._collector.collect_next()
        except Exception as error:
            self._collection_failures += 1
            self._collection_error = {"type": type(error).__name__, "message": str(error)[:300]}
            LOGGER.warning("context_usage_collection_failed type=%s", type(error).__name__)
