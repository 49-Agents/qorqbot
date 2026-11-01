"""Serial durable background computation, followed by exact core mutations."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
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
    ``service_stopping``), and with ``{"code": "background_failed"}`` for an unexpected exception. ``row`` is
    the job row last read by the run (``None`` when the job could not be loaded). The observer cannot change
    the saved outcome (its own exceptions are swallowed), authorize an output or start a writer."""

    def job_result(self, row, error=None) -> None: ...


class BackgroundService:
    def __init__(self, application, programs, authority, *, locks: LockPaths, job_observer: JobObserver | None = None,
                 scheduler=None, clock=None, interval=5):
        if not isinstance(locks, LockPaths):
            raise TypeError("The background service needs explicit LockPaths for its serial execution claim")
        self.app, self.database = application, application.database
        self.programs, self.authority = programs, authority
        self.locks, self.job_observer = locks, job_observer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval = interval
        self._stop, self._wake, self._lock = Event(), Event(), Lock()
        self._thread = None
        self.scheduler = scheduler
        self.last_error = None
        self.scheduler_error = None
        self.instance_id = new_id()

    def context_for(self, row, uow):
        return self.app.policy.context_for(row["principal_id"], business_id=row["business_id"],
            review_business_id=row["business_id"], initiated_by_principal_id=row["initiated_by_principal_id"],
            job_id=row["id"], uow=uow)

    def _serial_id(self):
        with self.database.read() as uow:
            return str(uuid5(UUID(self.authority.installation(uow)), "background-serial-execution"))

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

    def run_one(self, job_id=None):
        admission = self.app.writer_admission.writer() if self.app.writer_admission is not None else nullcontext()
        try:
            with admission:
                return self._run_one_admitted(job_id)
        except DomainError as exc:
            if exc.code == "maintenance":
                return None
            raise

    def _run_one_admitted(self, job_id=None):
        if not self._lock.acquire(blocking=False):
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
                return self._run_admitted_job(job_id)
        finally:
            self._lock.release()

    def _run_admitted_job(self, job_id):
        try:
            with application_claim(self.locks, self._serial_id()):
                with self.database.transaction() as uow:
                    self.app.ensure_writes_open(uow)
                    if job_id:
                        row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                    else:
                        row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE state IN "
                            "('queued','running','applying','waiting_signoff','cancel_requested') "
                            "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY "
                            "CASE WHEN json_extract(definition_ref_json,'$.trigger')='manual' THEN 0 ELSE 1 END, created_at,rowid LIMIT 1",
                            (self.clock().isoformat().replace('+00:00','Z'),)).fetchone())
                    if not row or row["state"] in {"succeeded", "cancelled", "failed"}:
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
                    self._observe(row, row['result_json'].get('completion_error'))
                return {"job_id": row["id"], "state": ("failed" if row["result_json"].get("completion_error") else "succeeded") if done else "waiting_signoff"}
        except DomainError as exc:
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
            self.last_error = exc.code
            return {"job_id": row["id"] if 'row' in locals() and row else job_id, "error": exc.to_dict()}
        except Exception:
            self._observe(locals().get('row'), {'code': 'background_failed'})
            if 'row' in locals() and row:
                self._fail(row["id"], DomainError("background_failed", "Registered background program failed"))
            raise

    def tick(self):
        if self.scheduler:
            try:
                self.scheduler.tick(self.clock())
                self.scheduler_error = None
            except Exception:
                self.scheduler_error = "Routine scheduling is unavailable"
                raise
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
            current = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE business_id IS ? "
                "AND state IN ('running','applying') ORDER BY created_at,rowid LIMIT 1", (business_id,)).fetchone())
            queued = uow.connection.execute("SELECT count(*) FROM jobs WHERE business_id IS ? AND state='queued'", (business_id,)).fetchone()[0]
        return {"current": current, "queued": queued, "recent": rows, "error": self.last_error,
                "invalid": [row for row in (self.scheduler.errors if self.scheduler else []) if row["business_id"] == business_id]}

    def kick(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._work, name="operations-background", daemon=True)
        self._thread.start()

    def _work(self):
        while not self._stop.is_set():
            try:
                result = self.tick()
            except Exception as exc:
                self.last_error, result = type(exc).__name__, None
            self._wake.wait(0.05 if result and result.get("state") == "succeeded" else self.interval)
            self._wake.clear()

    def stop(self, timeout=190):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("Background program did not finish its bounded cancellation")
            self._thread = None
