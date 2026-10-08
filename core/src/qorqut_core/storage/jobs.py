"""Durable claims for an assembly-time set of typed handlers, never stored code."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Callable
from uuid import UUID

from ..contracts import CoreError, JobProgress, JobRecord, JobResolution, Model, canonical
from .database import Database, Transaction, assert_external_io_allowed
from .events import EventInput, append_event


@dataclass(frozen=True)
class JobHandler:
    kind: str
    arguments: type[Model]
    external_effect: bool
    execute: Callable[[JobClaim], JobResolution]
    inspect: Callable[[dict], JobResolution]


@dataclass(frozen=True)
class JobClaim:
    job_id: UUID
    token: UUID
    recovery_epoch: UUID
    kind: str
    arguments: Model


def after(now: str, seconds: float) -> str:
    return (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=seconds)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def job_view(row) -> dict:
    item = dict(row)
    for key in ("args", "progress", "last_error"):
        value = item.pop(key + "_json")
        item[key] = json.loads(value) if value is not None else None
    return JobRecord.model_validate(item).model_dump(mode="json")


class Jobs:
    def __init__(self, db: Database, handlers: tuple[JobHandler, ...]):
        if len({h.kind for h in handlers}) != len(handlers):
            raise ValueError("Duplicate fixed job handler")
        self.db = db
        self.handlers = MappingProxyType({h.kind: h for h in handlers})
        # Claim/execute/reconcile share this local gate. Leases do not fence effects.
        self._gate = db.lock.effects

    def enqueue(self, tx: Transaction, kind: str, arguments: Model, *, operation_id: UUID,
                target_id: UUID | None = None, expected_generation: int | None = None) -> UUID:
        tx.require_write()
        handler = self.handlers.get(kind)
        if handler is None:
            raise CoreError("unsupported_capability", "No implemented handler for this job kind")
        validated = handler.arguments.model_validate(arguments.model_dump(mode="json"))
        args_json = canonical(validated)
        if len(args_json.encode()) > 65536:
            raise CoreError("invalid_argument", "Job arguments exceed 64 KiB")
        if expected_generation is not None and (type(expected_generation) is not int or expected_generation < 0):
            raise CoreError("invalid_argument", "Invalid target generation")
        job_id, now = self.db.id_factory(), self.db.clock()
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        tx.connection.execute("""INSERT INTO jobs (job_id,operation_id,kind,args_json,target_id,
            expected_generation,external_effect,recovery_epoch,state,progress_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,'queued',?,?,?)""",
            (str(job_id), str(operation_id), kind, args_json, str(target_id) if target_id else None,
             expected_generation, int(handler.external_effect), epoch,
             canonical(JobProgress(phase="queued")), now, now))
        return job_id

    @staticmethod
    def _event(tx: Transaction, row, old_state: str, new_state: str, reason=None, *, actor_id=None):
        operation = tx.connection.execute("SELECT actor_id FROM operations WHERE operation_id=?",
                                          (row["operation_id"],)).fetchone()
        return append_event(tx, EventInput(kind="job.state_changed", actor_id=actor_id or operation[0],
            operation_id=row["operation_id"], payload={"job_id": row["job_id"], "old_state": old_state,
            "new_state": new_state, "reason": reason, "attempt_count": row["attempt_count"],
            "previous_lease_owner": row["lease_owner"], "previous_lease_token": row["lease_token"],
            "previous_lease_until": row["lease_until"], "recovery_epoch": row["recovery_epoch"],
            "previous_progress": json.loads(row["progress_json"]),
            "previous_error": json.loads(row["last_error_json"]) if row["last_error_json"] else None}))

    def _expire(self, tx: Transaction):
        now = self.db.clock()
        for row in tx.connection.execute("SELECT * FROM jobs WHERE state='running' AND lease_until<=?", (now,)).fetchall():
            state = "uncertain" if row["external_effect"] else "queued"
            reason = "reconciliation_required" if row["external_effect"] else None
            error = {"code": "uncertain_effect", "message": "External lease expired; inspect actual effect", "retryable": False} if row["external_effect"] else None
            tx.connection.execute("""UPDATE jobs SET state=?,wait_reason=?,next_attempt_at=?,last_error_json=?,
                lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE job_id=?""",
                (state, reason, now, canonical(error) if error else None, now, row["job_id"]))
            self._event(tx, row, "running", state, reason)

    def expire(self):
        with self.db.transaction() as tx:
            self._expire(tx)

    def claim(self, *, worker_id: UUID, lease_seconds: int = 30) -> JobClaim | None:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise CoreError("invalid_argument", "Lease must be 1–3600 seconds")
        with self.db.transaction() as tx:
            self._expire(tx)
            epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
            row = tx.connection.execute("""SELECT * FROM jobs WHERE recovery_epoch=? AND wait_reason IS NOT 'recovery_review' AND
                (state='queued' OR (state='waiting' AND next_attempt_at<=? AND
                    (wait_reason='retry_delay' OR (kind='message_delivery' AND wait_reason='dependency_unavailable'))))
                ORDER BY created_at,job_id LIMIT 1""", (epoch, self.db.clock())).fetchone()
            if row is None:
                return None
            handler = self.handlers.get(row["kind"])
            if handler is None:
                raise CoreError("unsupported_capability", "Saved job has no compatible fixed handler")
            arguments = handler.arguments.model_validate_json(row["args_json"])
            token, now = self.db.id_factory(), self.db.clock()
            tx.connection.execute("""UPDATE jobs SET state='running',attempt_count=attempt_count+1,
                wait_reason=NULL,next_attempt_at=NULL,lease_owner=?,lease_token=?,lease_until=?,updated_at=? WHERE job_id=?""",
                (str(worker_id), str(token), after(now, lease_seconds), now, row["job_id"]))
            self._event(tx, row, row["state"], "running")
            return JobClaim(UUID(row["job_id"]), token, UUID(epoch), row["kind"], arguments)

    def _current_claim(self, tx: Transaction, claim: JobClaim):
        row = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(claim.job_id),)).fetchone()
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if (row is None or row["state"] != "running" or row["lease_token"] != str(claim.token)
                or row["recovery_epoch"] != str(claim.recovery_epoch) or epoch != str(claim.recovery_epoch)
                or row["lease_until"] <= self.db.clock()):
            raise CoreError("target_changed", "Job claim is stale; reconcile before further effects")
        return row

    def progress(self, claim: JobClaim, progress: JobProgress, *, lease_seconds=30):
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise CoreError("invalid_argument", "Lease must be 1–3600 seconds")
        with self.db.transaction() as tx:
            self._current_claim(tx, claim)
            now = self.db.clock()
            tx.connection.execute("UPDATE jobs SET progress_json=?,lease_until=?,updated_at=? WHERE job_id=?",
                (canonical(progress), after(now, lease_seconds), now, str(claim.job_id)))

    def _resolve(self, tx: Transaction, row, resolution: JobResolution):
        tx.require_write()
        tx.connection.execute("""UPDATE jobs SET state=?,progress_json=?,wait_reason=?,next_attempt_at=?,
            last_error_json=?,lease_owner=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE job_id=?""",
            (resolution.state, canonical(resolution.progress), resolution.reason, resolution.next_attempt_at,
             canonical(resolution.error) if resolution.error else None, self.db.clock(), row["job_id"]))
        self._event(tx, row, row["state"], resolution.state, resolution.reason)

    def finish(self, claim: JobClaim, resolution: JobResolution):
        with self.db.transaction() as tx:
            row = self._current_claim(tx, claim)
            self._resolve(tx, row, resolution)

    def run_next(self, *, worker_id: UUID, lease_seconds=30) -> UUID | None:
        assert_external_io_allowed()
        with self.db.lock.use(), self._gate:
            claim = self.claim(worker_id=worker_id, lease_seconds=lease_seconds)
            if claim is None:
                return None
            try:
                resolution = self.handlers[claim.kind].execute(claim)
            except CoreError as exc:
                external = self.handlers[claim.kind].external_effect
                resolution = JobResolution(state="uncertain" if external else "failed",
                    progress=JobProgress(phase="inspection_required" if external else "failed"),
                    reason="reconciliation_required" if external else None,
                    next_attempt_at=self.db.clock() if external else None, error=exc.error)
            # Unexpected failures leave the durable running intent for lease reconciliation.
            self.finish(claim, resolution)
            return claim.job_id

    def reconcile(self, job_id: UUID) -> JobResolution:
        """Internal inspection of a fixed handler; never public arbitrary retry code."""
        assert_external_io_allowed()
        with self.db.lock.use(), self._gate:
            with self.db.transaction() as tx:
                self._expire(tx)
                row = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
                if row is None:
                    raise CoreError("not_found", "Job not found")
                if row["state"] not in ("uncertain", "waiting"):
                    raise CoreError("invalid_transition", "Only unresolved jobs can be inspected")
                saved = dict(row)
            handler = self.handlers.get(saved["kind"])
            if handler is None:
                raise CoreError("unsupported_capability", "No compatible job inspector")
            resolution = handler.inspect(saved)
            with self.db.transaction() as tx:
                current = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
                if dict(current) != saved:
                    raise CoreError("target_changed", "Job changed while being inspected")
                self._resolve(tx, current, resolution)
            return resolution

    def reconcile_pending(self, *, limit: int = 16, retry_seconds: int = 5) -> int:
        """Inspect only this epoch's uncertain, receipt-backed work.

        Keep the lifetime/effect gates across selection and inspection so recovery
        cannot turn an eligible original-epoch job into held work between them.
        Persist the delay even if an inspector raises or returns immediate retry.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CoreError("invalid_argument", "Inspection batch must be 1–100 jobs")
        if type(retry_seconds) is not int or not 1 <= retry_seconds <= 3600:
            raise CoreError("invalid_argument", "Inspection delay must be 1–3600 seconds")
        assert_external_io_allowed()
        with self.db.lock.use(), self._gate:
            with self.db.transaction() as tx:
                self._expire(tx)
                rows = tx.connection.execute("""SELECT job_id FROM jobs WHERE
                    recovery_epoch=(SELECT recovery_epoch FROM installation) AND
                    external_effect=1 AND state='uncertain' AND
                    wait_reason='reconciliation_required' AND
                    kind IN ('file_install','conversation_publish','message_delivery') AND
                    (next_attempt_at IS NULL OR next_attempt_at<=?)
                    ORDER BY next_attempt_at,created_at,job_id LIMIT ?""",
                    (self.db.clock(), limit)).fetchall()
            for row in rows:
                job_id = row["job_id"]
                retry_at = after(self.db.clock(), retry_seconds)
                with self.db.transaction() as tx:
                    tx.connection.execute("UPDATE jobs SET next_attempt_at=? WHERE job_id=?",
                                          (retry_at, job_id))
                try:
                    self.reconcile(UUID(job_id))
                finally:
                    retry_at = after(self.db.clock(), retry_seconds)
                    with self.db.transaction() as tx:
                        tx.connection.execute("""UPDATE jobs SET next_attempt_at=? WHERE job_id=?
                            AND state='uncertain' AND wait_reason='reconciliation_required'
                            AND (next_attempt_at IS NULL OR next_attempt_at<?)""",
                            (retry_at, job_id, retry_at))
            return len(rows)

    def get(self, job_id: UUID) -> dict:
        with self.db.read() as tx:
            row = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
            if row is None:
                raise CoreError("not_found", "Job not found")
            return job_view(row)
