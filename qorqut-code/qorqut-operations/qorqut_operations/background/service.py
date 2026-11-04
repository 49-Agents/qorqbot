"""Durable background computation, serial within each configured queue, followed by exact core mutations."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread, current_thread
from types import MappingProxyType
import time
from typing import Protocol
from uuid import UUID, uuid5

from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain, utc_now
from qorqut_operations.storage.executor import application_claim
from qorqut_operations.storage.locks import LockPaths
from qorqut_operations.storage.repositories import decode_row
from .programs import ProgramPending, ProgramResult


@dataclass
class JobControl:
    service: object
    job: dict
    deadline: float

    @property
    def business_id(self):
        return self.job["business_id"]

    def check(self, *, deadline=True):
        if self.service._stop.is_set():
            raise DomainError("service_stopping", "Background service is stopping")
        if deadline and time.monotonic() >= self.deadline:
            raise DomainError("timeout", "Registered program exceeded its time limit")
        with self.service.database.read() as uow:
            row = uow.repository("jobs", business_id=self.business_id).get(self.job["id"], required=True)
            if row["state"] == "cancel_requested":
                raise DomainError("cancelled", "Owner stopped this routine")

    def context(self, uow):
        return self.service.context_for(self.job, uow)

    def remaining_seconds(self):
        return max(0.001, self.deadline - time.monotonic())

    def record_usage(self, model, usage, cost):
        with self.service.database.transaction() as uow:
            repo = uow.repository("jobs", business_id=self.business_id)
            row = repo.get(self.job["id"], required=True)
            result = row["result_json"] or {}
            calls = list(result.get("calls", []))
            if len(calls) >= 1000:
                raise DomainError("budget_exceeded", "Job model-call limit reached")
            calls.append({"model": model, "usage": plain(usage), "usd": cost, "at": utc_now()})
            repo.update(row["id"], {"result_json": {**result, "calls": calls}}, expected_revision=row["revision"])

    def spent_usd(self):
        with self.service.database.read() as uow:
            row = uow.repository("jobs", business_id=self.business_id).get(self.job["id"], required=True)
            return sum(call["usd"] for call in (row["result_json"] or {}).get("calls", []))

    def checkpoint(self, name, value, *, uow=None):
        if name not in {"dispatch_intent", "external_receipt"}:
            raise DomainError("invalid_result", "Unknown external job checkpoint")
        if uow is None:
            with self.service.database.transaction() as unit:
                return self.checkpoint(name, value, uow=unit)
        uow.require_write()
        repo = uow.repository("jobs", business_id=self.business_id)
        row = repo.get(self.job["id"], required=True)
        result = row["result_json"] or {}
        if name == "dispatch_intent" and row["state"] == "cancel_requested":
            raise DomainError("cancelled", "Owner stopped this routine before dispatch")
        if name in result and canonical_hash(result[name]) != canonical_hash(value):
            raise DomainError("invalid_plan", "External checkpoint is already frozen")
        repo.update(row["id"], {"result_json": {**result, name: value}}, expected_revision=row["revision"])

    def saved(self, name):
        with self.service.database.read() as uow:
            row = uow.repository("jobs", business_id=self.business_id).get(self.job["id"], required=True)
            return (row["result_json"] or {}).get(name)


class JobObserver(Protocol):
    """Completion and incident notification for background jobs (the host's incident reporter).

    ``job_result(row, error)`` is called outside any SQL transaction, after the engine has committed the
    state it reports: with ``error=None`` or the saved ``completion_error`` when a job's outputs have all been
    applied, with the error payload when a run raises ``DomainError`` (including ``busy``, ``cancelled`` and
    ``service_stopping``), and with ``{"code": "background_failed"}`` for an unexpected exception. For every
    terminal outcome (completion, failure, cancellation) ``row`` is the job row re-read after that outcome
    committed, so its ``state`` and ``error_json`` are the saved ones; nothing is observed when it cannot be
    re-read. ``busy`` and ``service_stopping`` leave the job queued and are observed with the row the run last
    read (``None`` when the job could not be loaded), before it is deferred. The observer cannot change the
    saved outcome (its own exceptions are swallowed), authorize an output or start a writer."""

    def job_result(self, row, error=None) -> None: ...


class BackgroundService:
    def __init__(self, application, programs, authority, *, locks: LockPaths, job_observer: JobObserver | None = None,
                 scheduler=None, clock=None, interval=5, queues=None):
        if not isinstance(locks, LockPaths):
            raise TypeError("The background service needs explicit LockPaths for its serial execution claim")
        self.app, self.database = application, application.database
        self.programs, self.authority = programs, authority
        self.locks, self.job_observer = locks, job_observer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval = interval
        # Routing is host configuration, not part of the frozen program definition or caller-controlled job arguments.
        routing = {}
        names = ["general"]
        for name, kinds in (queues or {}).items():
            if not isinstance(name, str) or not name.isascii() or not name.isidentifier() or name == "general":
                raise ValueError("Background queues need unique identifier names other than general")
            if isinstance(kinds, str):
                raise ValueError("Queue kinds must be a collection of program names")
            names.append(name)
            for kind in kinds:
                if not isinstance(kind, str) or not kind or kind in routing:
                    raise ValueError("Every program kind belongs to exactly one background queue")
                routing[kind] = name
        self.queues = tuple(names)
        self._routing = MappingProxyType(routing)
        self._queue_sql = ("CASE kind " + " ".join("WHEN ? THEN ?" for _ in routing) + " ELSE 'general' END"
                           if routing else "'general'")
        self._queue_parameters = tuple(value for pair in routing.items() for value in pair)
        self._stop, self._wake = Event(), Event()
        self._queue_locks = {name: Lock() for name in self.queues}
        self._queue_wakes = {name: Event() for name in self.queues}
        self._lifecycle_lock, self._scheduler_lock = Lock(), Lock()
        self._threads = {}
        self._thread = None
        self.scheduler = scheduler
        self.last_error = None
        self.scheduler_error = None
        self.instance_id = new_id()

    def context_for(self, row, uow):
        return self.app.policy.context_for(row["principal_id"], business_id=row["business_id"],
            review_business_id=row["business_id"], initiated_by_principal_id=row["initiated_by_principal_id"],
            job_id=row["id"], uow=uow)

    def _queue_for(self, kind):
        return self._routing.get(kind, "general")

    def _serial_id(self, queue="general"):
        with self.database.read() as uow:
            name = "background-serial-execution" + ("" if queue == "general" else ":" + queue)
            return str(uuid5(UUID(self.authority.installation(uow)), name))

    def _next_job(self, uow, queue=None):
        predicate = "" if queue is None else "AND (" + self._queue_sql + ")=? "
        parameters = () if queue is None else (*self._queue_parameters, queue)
        return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE state IN "
            "('queued','running','applying','waiting_signoff','cancel_requested') "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) " + predicate + "ORDER BY "
            "CASE WHEN next_retry_at IS NULL AND json_extract(definition_ref_json,'$.trigger')='manual' THEN 0 ELSE 1 END, "
            "COALESCE(next_retry_at,created_at),created_at,rowid LIMIT 1",
            (self.clock().isoformat().replace('+00:00','Z'), *parameters)).fetchone())

    def _update(self, row, values, uow):
        return uow.repository("jobs", business_id=row["business_id"]).update(row["id"], values, expected_revision=row["revision"])

    def _routine_finish(self, row, uow):
        run_id = row["definition_ref_json"].get("routine_run_id")
        if not run_id:
            return
        repo = uow.repository("routine_runs", business_id=row["business_id"])
        run = repo.get(run_id, required=True)
        result = row["result_json"] or {}
        calls = result.get("calls", [])
        state = {"succeeded": "succeeded", "cancelled": "cancelled", "failed": "failed"}.get(row["state"], row["state"])
        repo.update(run_id, {"state": state, "finished_at": utc_now(),
            "result_json": {**run["result_json"], "job_id": row["id"], "data": result.get("data", {}), "error": row["error_json"]},
            "cost_json": {"usd": round(sum(call["usd"] for call in calls), 8), "calls": calls}}, expected_revision=run["revision"])
        slots = uow.repository("routine_slots")
        key = {"namespace": run["namespace"], "routine_id": run["routine_id"], "target_namespace": row["business_id"] or "global"}
        slot = slots.get(key)
        if slot and slot["state_json"].get("job_id") == row["id"]:
            slots.update(key, {"state_json": {**slot["state_json"], "done": True}}, expected_revision=slot["revision"])

    def _fail(self, job_id, error):
        with self.database.transaction() as uow:
            row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if row and row["state"] not in {"succeeded", "cancelled"}:
                row = self._update(row, {"state": "cancelled" if error.code == "cancelled" else "failed",
                    "error_json": error.to_dict(), "claimed_by": None, "lease_until": None}, uow)
                self._routine_finish(row, uow)

    def _committed(self, row):
        """The job row as currently saved, for an observer that reports a terminal state; ``None`` if unreadable."""
        if not row:
            return None
        try:
            with self.database.read() as uow:
                return uow.repository("jobs", business_id=row["business_id"]).get(row["id"])
        except Exception:
            return None

    def _freeze(self, row, result):
        if not isinstance(result, ProgramResult):
            raise DomainError("invalid_result", "Registered program must return a typed result")
        outputs = plain(result.outputs)
        if len(outputs) > 100:
            raise DomainError("result_too_large", "A job may produce at most 100 bounded mutations")
        program = self.programs.require(row["kind"])
        for output in outputs:
            if (set(output) - {"operation", "arguments", "options"} or output.get("operation") not in program.outputs
                    or not isinstance(output.get("arguments"), dict)):
                raise DomainError("invalid_result", "Program returned an undeclared output operation")
            options = output.get("options", {})
            if set(options) - {"expected_revision"}:
                raise DomainError("invalid_result", "Job outputs cannot replace invocation identity or routing")
            descriptor = self.app.registry.get(output["operation"])
            self.app.registry.bind_arguments(descriptor.name, output["arguments"])
        with self.database.transaction() as uow:
            current = uow.repository("jobs", business_id=row["business_id"]).get(row["id"], required=True)
            if current["state"] == "cancel_requested":
                raise DomainError("cancelled", "Owner stopped this routine")
            previous = current["result_json"] or {}
            if "outputs" in previous:
                if previous["outputs_hash"] != canonical_hash(outputs):
                    raise DomainError("invalid_plan", "Job output was already frozen")
                return current
            values = {**previous, **result.to_dict(), "outputs_hash": canonical_hash(outputs), "applied": {}}
            return self._update(current, {"state": "applying", "result_json": values}, uow)

    def _apply_outputs(self, row, control):
        outputs = row["result_json"]["outputs"]
        for index, output in enumerate(outputs):
            control.check(deadline=False)
            with self.database.read() as uow:
                current = uow.repository("jobs", business_id=row["business_id"]).get(row["id"], required=True)
                prior = (current["result_json"].get("applied") or {}).get(str(index))
                if prior and prior["status"] == "signoff_required":
                    signoff = uow.repository("signoffs", business_id=row["business_id"]).get(prior["signoff"]["id"], required=True)
                    if signoff["state"] in {"pending", "approved"}:
                        self._defer(row["id"], 5, state="waiting_signoff")
                        return False
                    if signoff["state"] != "applied":
                        raise DomainError("signoff_not_applied", "A job output was rejected, conflicted or failed")
                if prior:
                    continue
                context = self.context_for(current, uow)
            result = self.app.invoke(context, output["operation"], output["arguments"],
                {**output.get("options", {}), "idempotency_key": str(uuid5(UUID(row["id"]), f"output:{index}"))})
            if result.status == "error":
                raise DomainError(result.error["code"], result.error["message"], retryable=result.error.get("retryable", False))
            if result.status not in {"ok", "accepted", "signoff_required"}:
                raise DomainError("invalid_result", "Unexpected result from a declared job output")
            with self.database.transaction() as uow:
                current = uow.repository("jobs", business_id=row["business_id"]).get(row["id"], required=True)
                data = current["result_json"]
                data["applied"][str(index)] = result.to_dict()
                current = self._update(current, {"state": "waiting_signoff" if result.status == "signoff_required" else "applying",
                    "result_json": data}, uow)
            if result.status == "signoff_required":
                self._defer(row["id"], 5, state="waiting_signoff")
                return False
        with self.database.transaction() as uow:
            current = uow.repository("jobs", business_id=row["business_id"]).get(row["id"], required=True)
            error = current["result_json"].get("completion_error")
            current = self._update(current, {"state": "failed" if error else "succeeded", "error_json": error,
                "claimed_by": None, "lease_until": None}, uow)
            self._routine_finish(current, uow)
        return True

    def _defer(self, job_id, seconds, *, state="queued"):
        with self.database.transaction() as uow:
            row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            return self._update(row, {"state": state, "claimed_by": None, "lease_until": None,
                "next_retry_at": (self.clock() + timedelta(seconds=max(1, min(seconds, 3600)))).isoformat().replace('+00:00','Z')}, uow)

    def _observe(self, row, error=None):
        if self.job_observer is not None and row:
            try:
                self.job_observer.job_result(row, error)
            except Exception:
                pass  # Reporting cannot change a job's saved execution outcome.

    def run_one(self, job_id=None, *, queue=None):
        if queue is not None and queue not in self.queues:
            raise ValueError("Unknown background queue")
        admission = self.app.writer_admission.writer() if self.app.writer_admission is not None else nullcontext()
        try:
            with admission:
                return self._run_one_admitted(job_id, queue)
        except DomainError as exc:
            if exc.code == "maintenance":
                return None
            raise

    def _run_one_admitted(self, job_id=None, queue=None):
        if queue is None:
            # Explicit recovery/manual calls resolve the same queue as the automatic workers. This read is only
            # routing: selection, state and kind are checked again under that queue's process-wide claim.
            with self.database.read() as uow:
                row = (decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                       if job_id else self._next_job(uow))
            if not row:
                return None
            queue = self._queue_for(row["kind"])
        lock = self._queue_locks[queue]
        if not lock.acquire(blocking=False):
            return None
        try:
            # Every nested database.transaction() in this method's call tree
            # -- direct, or through _freeze/_apply_outputs/_fail/_defer --
            # passes no operation of its own, so without this ambient tag the
            # change feed (PR2) would see none of them: no dashboard route
            # could ever learn a job or routine actually finished.
            # app.invoke() inside _apply_outputs still correctly overrides
            # this with the output's own real operation name for its own
            # nested writes, then this resumes once it returns.
            with self.database.observe("jobs_run"):
                try:
                    # Keep the claim through failure/deferral bookkeeping as well as provider work. Another process
                    # must not recover a running row while its first executor is still recording the outcome.
                    with application_claim(self.locks, self._serial_id(queue)):
                        return self._run_admitted_job(job_id, queue)
                except DomainError as exc:
                    if exc.code == "busy":
                        return None
                    raise
        finally:
            lock.release()

    def _run_admitted_job(self, job_id, queue):
        try:
            if self._stop.is_set():
                return None
            with self.database.transaction() as uow:
                self.app.ensure_writes_open(uow)
                if job_id:
                    row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                else:
                    row = self._next_job(uow, queue)
                if not row or row["state"] in {"succeeded", "cancelled", "failed"}:
                    return None
                if self._queue_for(row["kind"]) != queue:
                    return None
                program = self.programs.require(row["kind"])
                self.authority.require_worker(row["principal_id"], row["business_id"], uow)
                if row["definition_ref_json"].get("digest") != program.definition["digest"]:
                    raise DomainError("policy_denied", "Registered program definition changed before dispatch")
                if row["state"] == "cancel_requested":
                    raise DomainError("cancelled", "Owner stopped this routine")
                timeout = min(7200, row["definition_ref_json"].get("run_timeout_seconds", program.timeout_seconds))
                frozen = "outputs" in (row["result_json"] or {})
                row = self._update(row, {"state": "applying" if frozen else "running", "claimed_by": self.instance_id,
                    "lease_until": (self.clock() + timedelta(seconds=timeout + 30)).isoformat().replace('+00:00','Z'),
                    "attempts": row["attempts"] + (not frozen)}, uow)
                run_id = row["definition_ref_json"].get("routine_run_id")
                if run_id:
                    runs = uow.repository("routine_runs", business_id=row["business_id"])
                    run = runs.get(run_id, required=True)
                    if run["state"] == "queued":
                        runs.update(run_id, {"state": "running", "started_at": utc_now()}, expected_revision=run["revision"])
            control = JobControl(self, row, time.monotonic() + timeout)
            if not frozen:
                control.check()
                compute = program.recover if program.external and row["attempts"] > 1 else program.run
                result = compute(control, plain(row["arguments_json"]))
                if isinstance(result, ProgramPending):
                    self._defer(row["id"], result.retry_after)
                    return {"job_id": row["id"], "state": "queued"}
                row = self._freeze(row, result)
            done = self._apply_outputs(row, control)
            if done:
                committed = self._committed(row)
                self._observe(committed, (committed["result_json"] or {}).get("completion_error") if committed else None)
            return {"job_id": row["id"], "state": ("failed" if row["result_json"].get("completion_error") else "succeeded") if done else "waiting_signoff"}
        except DomainError as exc:
            nonterminal = exc.code in {"busy", "service_stopping"}
            if nonterminal:
                self._observe(locals().get('row'), exc.to_dict())
            if exc.code == "busy":
                if 'row' in locals() and row:
                    self._defer(row["id"], 5, state="applying" if "outputs" in (row["result_json"] or {}) else "queued")
                return None
            if exc.code == "service_stopping" and 'row' in locals() and row:
                with self.database.read() as uow:
                    current = uow.repository("jobs", business_id=row["business_id"]).get(row["id"], required=True)
                self._defer(row["id"], 1, state="applying" if "outputs" in (current["result_json"] or {}) else "queued")
                return {"job_id": row["id"], "state": "paused_for_restart"}
            if 'row' in locals() and row:
                self._fail(row["id"], exc)
            if not nonterminal:
                self._observe(self._committed(locals().get('row')), exc.to_dict())
            self.last_error = exc.code
            return {"job_id": row["id"] if 'row' in locals() and row else job_id, "error": exc.to_dict()}
        except Exception:
            if 'row' in locals() and row:
                self._fail(row["id"], DomainError("background_failed", "Registered background program failed"))
            self._observe(self._committed(locals().get('row')), {'code': 'background_failed'})
            raise

    def _schedule(self):
        if not self.scheduler:
            return
        with self._scheduler_lock:
            try:
                queued = self.scheduler.tick(self.clock())
                self.scheduler_error = None
                if queued:
                    for wake in self._queue_wakes.values():
                        wake.set()
            except Exception:
                self.scheduler_error = "Routine scheduling is unavailable"
                raise

    def tick(self):
        self._schedule()
        return self.run_one()

    def routine_status(self, context):
        """Already-authorized owner view, independent of other programs' errors."""
        global_view = "manage_global_routines" in context.global_capabilities
        allowed = sorted(context.allowed_business_ids)
        marks = ",".join("?" for _ in allowed) or "NULL"
        scope = "r.namespace=?"
        parameters = [context.business_id]
        if global_view:
            scope += f" OR (r.namespace='global' AND (r.business_id IS NULL OR r.business_id IN ({marks})))"
            parameters.extend(allowed)
        with self.database.read() as uow:
            found = uow.connection.execute(
                "SELECT j.*,r.namespace AS routine_namespace,r.routine_id AS registered_routine_id,r.started_at AS run_started_at FROM jobs j "
                "JOIN routine_runs r ON r.id=json_extract(j.definition_ref_json,'$.routine_run_id') "
                "AND r.business_id IS j.business_id "
                "WHERE j.state IN ('queued','running','applying','waiting_signoff','cancel_requested') AND (" + scope + ") "
                "ORDER BY j.created_at,j.id LIMIT 2001", parameters).fetchall()
        if len(found) > 2000:
            raise DomainError("result_too_large", "Too many pending routine executions")
        jobs = []
        for raw in found:
            row = decode_row(raw)
            namespace = row.pop("routine_namespace")
            identity = row.pop("registered_routine_id")
            started = row.pop("run_started_at")
            # Derive scope for pre-upgrade jobs without rewriting immutable bindings.
            row["definition_ref_json"] = {**row["definition_ref_json"], "scope": "global" if namespace == "global" else "business", "routine_id": identity}
            row.update(routine=identity, key=namespace + "/" + identity,
                seconds=max(0, int((self.clock() - datetime.fromisoformat(started.replace('Z', '+00:00'))).total_seconds())))
            jobs.append(row)
        return {"current": next((row for row in jobs if row["state"] != "queued"), None),
            "queued": [row for row in jobs if row["state"] == "queued"], "error": self.scheduler_error,
            "invalid": [row for row in (self.scheduler.errors if self.scheduler else [])
                if row["namespace"] == context.business_id or global_view and row["namespace"] == "global"]}

    def status(self, business_id=None):
        with self.database.read() as uow:
            rows = uow.repository("jobs", business_id=business_id).list(limit=20, order_by=("created_at", "id"), descending=True)
            running = [decode_row(row) for row in uow.connection.execute("SELECT * FROM jobs WHERE business_id IS ? "
                "AND state IN ('running','applying') ORDER BY created_at,rowid", (business_id,))]
            counts = uow.connection.execute("SELECT kind,count(*) FROM jobs WHERE business_id IS ? AND state='queued' "
                "GROUP BY kind", (business_id,)).fetchall()
        queues = {name: {"running": [row["id"] for row in running if self._queue_for(row["kind"]) == name],
                         "queued": sum(count for kind, count in counts if self._queue_for(kind) == name)} for name in self.queues}
        return {"current": next(iter(running), None), "running": running, "queues": queues,
                "queued": sum(count for _, count in counts), "recent": rows, "error": self.last_error,
                "invalid": [row for row in (self.scheduler.errors if self.scheduler else []) if row["business_id"] == business_id]}

    def kick(self):
        self._wake.set()
        for wake in self._queue_wakes.values():
            wake.set()

    def is_running(self):
        return any(thread and thread.is_alive() for thread in (self._thread, *self._threads.values()))

    def is_worker_thread(self):
        return current_thread() in (self._thread, *self._threads.values())

    def start(self):
        with self._lifecycle_lock:
            if self._stop.is_set() and self.is_running():
                raise TimeoutError("Background workers are still stopping")
            self._stop.clear()
            for queue in self.queues:
                thread = self._threads.get(queue)
                if not thread or not thread.is_alive():
                    thread = Thread(target=self._work_queue, args=(queue,), name="background-" + queue, daemon=True)
                    self._threads[queue] = thread
                    thread.start()
            if not self._thread or not self._thread.is_alive():
                self._thread = Thread(target=self._work, name="background-scheduler", daemon=True)
                self._thread.start()

    def _work(self):
        # Scheduling has no provider work. It must keep admitting due jobs even when the general queue is occupied.
        while not self._stop.is_set():
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._schedule()
            except Exception as exc:
                self.last_error = type(exc).__name__
            self._wake.wait(self.interval)

    def _work_queue(self, queue):
        wake = self._queue_wakes[queue]
        while not self._stop.is_set():
            wake.clear()
            if self._stop.is_set():
                break
            try:
                result = self.run_one(queue=queue)
            except Exception as exc:
                self.last_error, result = type(exc).__name__, None
            wake.wait(0.05 if result and result.get("state") == "succeeded" else self.interval)

    def stop(self, timeout=190):
        if self.is_worker_thread():
            raise RuntimeError("A background worker cannot drain itself")
        deadline = time.monotonic() + timeout
        with self._lifecycle_lock:
            self._stop.set()
            self.kick()
            for thread in (self._thread, *self._threads.values()):
                if thread and thread.ident is not None:
                    thread.join(max(0, deadline - time.monotonic()))
            if self.is_running():
                raise TimeoutError("Background program did not finish its bounded cancellation")
            self._thread = None
            self._threads.clear()
