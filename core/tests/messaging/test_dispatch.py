"""D46 host fault/recovery checks with explicit synthetic runtime receipts."""

import hashlib
import json
from uuid import UUID, uuid4

from messaging.support import ChatCase
from runner.support import synthetic_binding
from qorqut_core.contracts import CoreError, canonical
from qorqut_core.messaging.notifications import notification_text
from qorqut_core.runner.models import AttemptReceipt
from qorqut_core.runtime.input_dispatch import InputTarget, InputUnavailable
from qorqut_core.storage.database import assert_external_io_allowed
from test_foundation import InjectedCrash


class SyntheticInput:
    def __init__(self, case):
        self.case = case
        binding = synthetic_binding().model_copy(update={"agent_id": UUID(case.onbash["id"]),
            "recovery_epoch": UUID(case.config()["recovery_epoch"])})
        self.target = InputTarget(installation_id=case.settings.installation_id,
            conversation_id=case.onbash["conversation_id"], terminal_id=uuid4(), native_session_id=uuid4(),
            provider="codex", cli_version="0.160.0", idle_event_seq=100, control_epoch=0, binding=binding)
        self.submissions, self.inspections, self.receipts = [], [], {}
        self.refusal, self.failure, self.state = None, None, "submitted"
        self.before_submit = lambda callback: None

    def prepare(self, agent_id):
        assert_external_io_allowed()
        self.case.assertEqual(str(agent_id), self.case.onbash["id"])
        if self.refusal:
            raise InputUnavailable(self.refusal)
        return self.target

    def receipt(self, attempt_id, message_id, state=None):
        state = state or self.state
        payload = notification_text(message_id).encode("ascii")
        return AttemptReceipt(attempt_id=attempt_id, binding=self.target.binding,
            payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload),
            control_epoch=self.target.control_epoch, state=state,
            phase="prepared" if state == "deferred" else "finished" if state == "submitted" else "paste",
            reason="processing_not_confirmed" if state == "submitted" else "human_control" if state == "deferred" else "boundary_failure",
            created_at=self.case.clock(), updated_at=self.case.clock())

    def submit(self, target, attempt_id, message_id, *, still_current):
        assert_external_io_allowed()
        self.case.assertEqual(target, self.target)
        self.before_submit(still_current)
        if still_current() is not True:
            raise CoreError("target_changed", "fixture stale claim")
        self.submissions.append((attempt_id, message_id))
        if self.failure == "no_receipt":
            raise OSError("private native diagnostic")
        receipt = self.receipt(attempt_id, message_id)
        self.receipts[attempt_id] = receipt
        if self.failure == "lost_response":
            raise OSError("private native diagnostic")
        if self.failure == "crash":
            raise SystemExit("synthetic executor crash")
        return receipt

    def inspect(self, target, attempt_id):
        assert_external_io_allowed()
        self.case.assertEqual(target.binding, self.target.binding)
        self.inspections.append(attempt_id)
        return self.receipts.get(attempt_id)


class Dispatch(ChatCase):
    def setUp(self):
        super().setUp()
        self.runtime = SyntheticInput(self)
        self.app.runtime_input = self.runtime

    def queue(self, key="queued", body="exact body\x00\x1b\x7f\x85\n"):
        return self.send(key, body, recipient=self.onbash).data["message"]["message_id"]

    def job(self, message_id):
        return next(r for r in self.rows("jobs") if r["kind"] == "message_delivery"
                    and json.loads(r["args_json"])["message_id"] == message_id)

    def delivery(self, message_id):
        result = self.app.invoke("deliveries_get", self.context, {"message_id": message_id})
        self.assertTrue(result.ok, result)
        return result.data

    def run_until(self, message_id):
        for _ in range(30):
            job = self.job(message_id)
            if job["state"] != "queued":
                return job
            self.assertIsNotNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.fail("Delivery did not leave queue")

    def due(self, message_id, seconds=2):
        self.clock.advance(seconds)
        for _ in range(30):
            result = self.app.jobs.run_next(worker_id=uuid4())
            if result is None or str(result) == self.job(message_id)["job_id"]:
                return
        self.fail("Expected due delivery")

    def reconcile(self, message_id):
        self.clock.advance(61)
        self.app.jobs.expire()
        return self.app.jobs.reconcile(UUID(self.job(message_id)["job_id"]))

    def test_fifo_one_notification_per_idle_event_and_exact_bodies(self):
        bodies = ["first\x00\x1b\x7f\x85\n", "second different 文\r\t"]
        first, second = [self.queue(str(i), body) for i, body in enumerate(bodies)]
        self.run_until(first)
        self.run_until(second)
        if self.delivery(second)["delivery"]["reason"] == "awaiting_earlier_message":
            self.due(second)
        self.assertEqual(self.delivery(first)["delivery"]["state"], "submitted")
        self.assertEqual(self.delivery(second)["delivery"]["reason"], "awaiting_native_turn")
        self.assertEqual(self.delivery(second)["attempts"], [])
        self.assertEqual(len(self.runtime.submissions), 1)
        self.runtime.target = self.runtime.target.model_copy(update={"idle_event_seq": 101})
        self.due(second)
        self.assertEqual(len(self.runtime.submissions), 2)
        self.assertEqual([str(x[1]) for x in self.runtime.submissions], [first, second])
        self.assertEqual([r["body"] for r in self.rows("messages")], bodies)
        for mid in (first, second):
            view = self.delivery(mid)
            self.assertEqual(view["delivery"]["state"], "submitted")
            self.assertEqual(view["attempts"][0]["payload_bytes"], 51)
            self.assertEqual(view["attempts"][0]["payload_sha256"], hashlib.sha256(notification_text(UUID(mid)).encode()).hexdigest())
            wire = canonical(view)
            for private in ("socket_path", "pid_namespace", "runner_receipt", "process_json"):
                self.assertNotIn(private, wire)
        self.assertEqual(self.rows("message_handling"), [])
        self.assertFalse(self.app.invoke("runtime_get", self.context, {"agent_id": self.onbash["id"]}).data["input_ready"])

    def test_later_claim_waits_for_earlier_current_epoch_message(self):
        first, second = self.queue("first"), self.queue("second")
        first_job = self.job(first)["job_id"]
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE jobs SET state='waiting',wait_reason='retry_delay',next_attempt_at=? WHERE job_id=?",
                                 ("2099-01-01T00:00:00.000000Z", first_job))
        self.run_until(second)
        self.assertEqual(self.delivery(second)["delivery"]["reason"], "awaiting_earlier_message")
        self.assertEqual(self.runtime.submissions, [])

    def test_preflight_refusals_do_not_create_attempts_or_repeat_delivery_events(self):
        mid = self.queue()
        for reason in ("native_busy", "native_waiting_user", "human_control", "capture_unavailable", "hook_unavailable",
                       "composer_busy", "composer_blocked", "composer_unknown", "target_changed", "session_unavailable"):
            self.runtime.refusal = reason
            if self.job(mid)["state"] == "queued":
                self.run_until(mid)
            else:
                self.due(mid)
            self.assertEqual(self.delivery(mid)["delivery"]["reason"], reason)
            before = len(self.events(self.onbash["id"], "message.delivery_changed"))
            self.due(mid)
            self.assertEqual(len(self.events(self.onbash["id"], "message.delivery_changed")), before)
        self.assertEqual(self.rows("delivery_attempts"), [])
        self.assertEqual(self.runtime.submissions, [])

    def test_offline_dependency_job_wakes_without_other_dependency_jobs(self):
        mid = self.queue()
        self.runtime.refusal = "runtime_unavailable"
        self.run_until(mid)
        self.assertEqual(self.job(mid)["wait_reason"], "dependency_unavailable")
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "pending")
        other = {r["job_id"]: r for r in self.rows("jobs") if r["kind"] == "agent_provision"}
        self.runtime.refusal = None
        self.due(mid, 61)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "submitted")
        self.assertEqual({r["job_id"]: r for r in self.rows("jobs") if r["kind"] == "agent_provision"}, other)

    def test_deferred_receipt_has_linked_new_id_and_does_not_consume_idle(self):
        mid = self.queue()
        self.runtime.state = "deferred"
        self.run_until(mid)
        self.runtime.state = "submitted"
        self.due(mid)
        rows = self.delivery(mid)["attempts"]
        self.assertEqual([r["state"] for r in rows], ["deferred", "submitted"])
        self.assertEqual(rows[1]["previous_attempt_id"], rows[0]["attempt_id"])
        self.assertNotEqual(rows[0]["attempt_id"], rows[1]["attempt_id"])
        self.assertEqual(rows[0]["idle_event_seq"], rows[1]["idle_event_seq"])

    def test_lost_return_reads_original_receipt_without_replaying(self):
        mid = self.queue()
        self.runtime.failure = "lost_response"
        self.run_until(mid)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "submitted")
        self.assertEqual(self.runtime.inspections, [self.runtime.submissions[0][0]])
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_missing_or_mismatched_receipt_stays_uncertain_and_rate_limited(self):
        mid = self.queue()
        self.runtime.failure = "no_receipt"
        self.run_until(mid)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "uncertain")
        attempt_id = self.runtime.submissions[0][0]
        wrong = self.runtime.receipt(attempt_id, UUID(mid)).model_copy(update={"payload_sha256": "0" * 64})
        self.runtime.receipts[attempt_id] = wrong
        self.clock.advance(6)
        self.assertEqual(self.app.jobs.reconcile_pending(), 1)
        self.assertEqual(self.delivery(mid)["attempts"][0]["reason"], "receipt_mismatch")
        before = len(self.rows("events"))
        self.assertEqual(self.app.jobs.reconcile_pending(), 0)
        self.assertEqual(len(self.rows("events")), before)
        self.assertTrue(self.ack([mid]).ok)
        self.runtime.target = self.runtime.target.model_copy(update={"idle_event_seq": 200})
        self.clock.advance(6)
        self.app.jobs.reconcile_pending()
        self.assertEqual(len(self.runtime.submissions), 1)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "uncertain")
        self.runtime.receipts[attempt_id] = self.runtime.receipt(attempt_id, UUID(mid))
        self.clock.advance(6)
        self.assertEqual(self.app.jobs.reconcile_pending(), 1)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "submitted")
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_each_receipt_identity_field_is_required(self):
        mid = self.queue()
        self.runtime.failure = "no_receipt"
        self.run_until(mid)
        aid = self.runtime.submissions[0][0]
        good = self.runtime.receipt(aid, UUID(mid))
        for changes in ({"attempt_id": uuid4()}, {"binding": good.binding.model_copy(update={"run_id": uuid4()})},
                        {"payload_sha256": "0" * 64}, {"payload_bytes": 50}, {"control_epoch": 1}):
            self.runtime.receipts[aid] = good.model_copy(update=changes)
            self.app.jobs.reconcile(UUID(self.job(mid)["job_id"]))
            self.assertEqual(self.delivery(mid)["delivery"]["state"], "uncertain")
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_independent_message_after_uncertain_needs_new_idle_event(self):
        first = self.queue("first")
        self.runtime.state = "uncertain"
        self.run_until(first)
        self.runtime.state = "submitted"
        second = self.queue("second")
        self.run_until(second)
        self.assertEqual(self.delivery(second)["delivery"]["reason"], "awaiting_native_turn")
        self.runtime.target = self.runtime.target.model_copy(update={"idle_event_seq": 101})
        self.due(second)
        self.assertEqual(self.delivery(second)["delivery"]["state"], "submitted")
        self.assertEqual(self.delivery(first)["delivery"]["state"], "uncertain")
        self.assertEqual(len(self.runtime.submissions), 2)

    def test_host_crash_windows_never_replay_possibly_performed_input(self):
        for index, point in enumerate(("delivery_after_prepared", "delivery_before_intent_commit",
                "delivery_after_intent_commit", "delivery_after_runner_return", "delivery_before_receipt_commit",
                "delivery_after_receipt_commit")):
            self.runtime.target = self.runtime.target.model_copy(update={"idle_event_seq": 100 + index})
            mid = self.queue(point)
            before = len(self.runtime.submissions)
            self.fault.point = point
            with self.assertRaises(InjectedCrash):
                self.run_until(mid)
            self.restart()
            self.app.runtime_input = self.runtime
            self.reconcile(mid)
            row = self.delivery(mid)["attempts"][-1]
            if point in {"delivery_after_prepared", "delivery_before_intent_commit"}:
                self.assertEqual(row["state"], "deferred")
                self.assertEqual(len(self.runtime.submissions), before)
                self.due(mid)
                self.assertEqual(self.delivery(mid)["delivery"]["state"], "submitted")
            elif point == "delivery_after_intent_commit":
                self.assertEqual(row["state"], "uncertain")
                self.assertEqual(len(self.runtime.submissions), before)
            else:
                self.assertEqual(row["state"], "submitted")
                self.assertEqual(len(self.runtime.submissions), before + 1)

    def test_crash_after_possible_input_recovers_original_uncertainty(self):
        mid = self.queue()
        self.runtime.failure, self.runtime.state = "crash", "uncertain"
        with self.assertRaises(SystemExit):
            self.run_until(mid)
        self.reconcile(mid)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "uncertain")
        self.assertEqual(len(self.runtime.submissions), 1)

    def test_claim_loss_is_rechecked_at_runtime_callback_and_never_authorizes_input(self):
        mid = self.queue()
        self.runtime.before_submit = lambda callback: self.clock.advance(31)
        with self.assertRaises(CoreError):
            self.run_until(mid)
        self.reconcile(mid)
        self.assertEqual(self.delivery(mid)["delivery"]["state"], "uncertain")
        self.assertEqual(self.runtime.submissions, [])

    def test_restore_holds_all_unfinished_attempts_and_does_not_wake_them(self):
        mid = self.queue()
        self.fault.point = "delivery_after_prepared"
        with self.assertRaises(InjectedCrash):
            self.run_until(mid)
        self.recovery()
        self.assertEqual(self.app.jobs.reconcile(UUID(self.job(mid)["job_id"])).reason, "recovery_review")
        self.clock.advance(120)
        self.assertEqual(self.app.jobs.reconcile_pending(), 0)
        self.assertIsNone(self.app.jobs.claim(worker_id=uuid4()))
        self.assertEqual(self.runtime.submissions, [])
        self.assertEqual(self.delivery(mid)["attempts"][0]["state"], "prepared")
        self.assertEqual(self.delivery(mid)["delivery"]["reason"], "recovery_review")

    def test_attempt_sql_identity_final_state_foreign_keys_and_attribution(self):
        mid = self.queue()
        self.run_until(mid)
        row = self.rows("delivery_attempts")[0]
        for sql, values in (("DELETE FROM delivery_attempts", ()),
                ("UPDATE delivery_attempts SET state='uncertain'", ()),
                ("UPDATE delivery_attempts SET target_json='{}'", ()),
                ("UPDATE delivery_attempts SET payload_bytes=52", ()),
                ("UPDATE delivery_attempts SET previous_attempt_id=?", (str(uuid4()),)),
                (f"INSERT OR REPLACE INTO delivery_attempts ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))):
            with self.subTest(sql=sql), self.assertRaises(CoreError), self.app.db.transaction() as tx:
                tx.connection.execute(sql, values)
        events = self.events(self.onbash["id"], "message.attempt_changed")
        self.assertEqual([json.loads(e["payload_json"])["state"] for e in events], ["prepared", "dispatching", "submitted"])
        self.assertEqual({e["actor_id"] for e in events}, {str(self.context.actor_id)})
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual((len(self.app.registry), len(self.app.jobs.handlers)), (17, 5))
