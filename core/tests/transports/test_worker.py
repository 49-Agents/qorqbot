"""Receipt recovery and fixed-kind worker seams; no native publisher fixture claims."""

import time
from uuid import UUID, uuid4

from qorqut_core.app import Change, Operation
from qorqut_core.contracts import CoreError, JobProgress, JobResolution, Mutation
from qorqut_core.storage.jobs import JobHandler, Jobs, after
from qorqut_core.storage.worker import JobWorker
from agents.support import AgentCase
import test_foundation as foundation
from test_foundation import InjectedCrash, ProbeArgs


class WorkerRecovery(AgentCase):
    attach_file = foundation.FoundationCase.attach_file

    def wait_job(self, job_id, state):
        deadline = time.monotonic() + 4
        while self.app.jobs.get(job_id)["state"] != state:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    def test_restart_reconciles_file_receipt_before_new_work(self):
        target, receipt, job_id, _ = self.attach_file(b"durable synthetic bytes")
        self.fault.point = "file_after_rename"
        with self.assertRaises(InjectedCrash):
            self.app.jobs.run_next(worker_id=uuid4(), lease_seconds=1)
        self.assertEqual(target.read_bytes(), b"durable synthetic bytes")
        self.assertEqual(self.app.files.get(receipt.receipt_id)["state"], "staged")
        self.restart()
        self.clock.advance(2)
        created = self.create("queued-after-crash")
        agent_id, provision_id, final, _ = self.coordinates(created)
        observed = []
        original = self.app.jobs.run_next

        def queued(**kwargs):
            observed.append(self.app.files.get(receipt.receipt_id)["state"])
            return original(**kwargs)

        self.app.jobs.run_next = queued
        worker = JobWorker(self.app.jobs)
        self.assertFalse(worker.ready)
        worker.start()
        try:
            self.wait_job(job_id, "completed")
            self.wait_job(provision_id, "waiting")
        finally:
            worker.stop()
        self.assertTrue(observed)
        self.assertEqual(set(observed), {"installed"})
        self.assertEqual(target.read_bytes(), b"durable synthetic bytes")
        self.assertEqual(self.app.jobs.get(job_id)["attempt_count"], 1)
        self.assertEqual(self.app.jobs.get(provision_id)["wait_reason"], "dependency_unavailable")
        self.assertTrue((final / "workspace/persona.md").is_file())
        self.assertEqual(self.get(agent_id)["lifecycle"], "provisioning")

    def test_restore_file_work_stays_held_and_uninstalled(self):
        target, receipt, job_id, _ = self.attach_file(b"held synthetic bytes")
        self.assertTrue(self.recovery().ok)
        self.clock.advance(100)
        worker = JobWorker(self.app.jobs)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while not worker.ready:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        finally:
            worker.stop()
        self.assertFalse(target.exists())
        self.assertEqual(self.app.files.get(receipt.receipt_id)["state"], "staged")
        self.assertEqual(self.app.jobs.get(job_id)["wait_reason"], "recovery_review")

    def fixture_jobs(self, inspector):
        """Synthetic stand-ins for the real conversation kinds test eligibility only, not publication."""
        executed = []
        synthetic = ("conversation_publish", "message_delivery")
        def execute(claim):
            executed.append(claim.kind)
            return JobResolution(state="completed", progress=JobProgress(phase="synthetic"))
        handlers = tuple(h for h in self.app.jobs.handlers.values() if h.kind not in synthetic) + tuple(
            JobHandler(kind, ProbeArgs, True, execute, inspector) for kind in synthetic)
        self.app.jobs = Jobs(self.app.db, handlers)
        ids = []
        def create(tx, args, context, operation_id):
            for kind in ("conversation_publish",) * 5 + ("message_delivery",) * 2:
                ids.append(self.app.jobs.enqueue(tx, kind, ProbeArgs(value=1), operation_id=operation_id))
            return Change({}, None, "accepted")
        result = self.app.execute(Operation("test_receipt_jobs", Mutation, True, create), self.context,
                                  {"idempotency_key": "synthetic-jobs"})
        self.assertTrue(result.ok, result)
        with self.app.db.transaction() as tx:
            for job_id in ids:
                tx.connection.execute("""UPDATE jobs SET state='uncertain',wait_reason='reconciliation_required',
                    next_attempt_at=? WHERE job_id=?""", (self.clock(), str(job_id)))
            tx.connection.execute("UPDATE jobs SET state='waiting',wait_reason='recovery_review' WHERE job_id=?", (str(ids[2]),))
            tx.connection.execute("UPDATE jobs SET wait_reason='recovery_review' WHERE job_id=?", (str(ids[3]),))
            tx.connection.execute("UPDATE jobs SET recovery_epoch=? WHERE job_id=?", (str(uuid4()), str(ids[4])))
            tx.connection.execute("UPDATE jobs SET state='waiting',wait_reason='dependency_unavailable' WHERE job_id=?", (str(ids[6]),))
        return ids, executed

    def test_bounded_original_epoch_inspectors_delay_and_exclusions(self):
        inspected = []
        def inspect(job):
            inspected.append(UUID(job["job_id"]))
            return JobResolution(state="uncertain", progress=JobProgress(phase="synthetic_inspection"),
                reason="reconciliation_required", next_attempt_at=self.clock())
        ids, executed = self.fixture_jobs(inspect)
        eligible = ids[:2] + [ids[5]]
        excluded = ids[2:5] + [ids[6]]
        before = {job_id: self.app.jobs.get(job_id) for job_id in excluded}
        for _ in eligible:
            self.assertEqual(self.app.jobs.reconcile_pending(limit=1), 1)
        self.assertEqual(set(inspected), set(eligible))
        self.assertEqual(self.app.jobs.reconcile_pending(), 0)
        self.assertEqual(executed, [])
        for job_id in eligible:
            self.assertEqual(self.app.jobs.get(job_id)["next_attempt_at"], after(self.clock(), 5))
        self.clock.advance(5)
        self.assertEqual(self.app.jobs.reconcile_pending(), 3)
        for job_id in excluded:
            self.assertEqual(self.app.jobs.get(job_id), before[job_id])
        self.assertEqual(self.app.jobs.run_next(worker_id=uuid4()), ids[6])
        self.assertEqual(executed, ["message_delivery"])
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))

    def test_inspector_exception_and_missing_handler_remain_delayed_without_execution(self):
        def inspect(job):
            raise RuntimeError("synthetic inspector failure")
        ids, executed = self.fixture_jobs(inspect)
        with self.assertRaisesRegex(RuntimeError, "synthetic inspector failure"):
            self.app.jobs.reconcile_pending(limit=1)
        eligible = ids[:2] + [ids[5]]
        delayed = [job_id for job_id in eligible if self.app.jobs.get(job_id)["next_attempt_at"] > self.clock()]
        self.assertEqual(len(delayed), 1)
        self.app.jobs = Jobs(self.app.db, tuple(h for h in self.app.jobs.handlers.values()
            if h.kind not in {"conversation_publish", "message_delivery"}))
        for _ in range(2):
            with self.assertRaises(CoreError) as raised:
                self.app.jobs.reconcile_pending(limit=1)
            self.assertEqual(raised.exception.error.code, "unsupported_capability")
        self.assertEqual(self.app.jobs.reconcile_pending(), 0)
        self.assertEqual(executed, [])
        self.assertEqual({self.app.jobs.get(job_id)["state"] for job_id in eligible}, {"uncertain"})
