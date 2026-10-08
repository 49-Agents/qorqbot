"""Normal message jobs: commit host intent, delegate one notification, reconcile."""

import hashlib
import json
from uuid import UUID

from ..agents.registry import get_row
from ..contracts import CoreError, JobProgress, JobResolution, canonical, canonical_hash
from ..runtime.input_dispatch import InputTarget, InputUnavailable
from ..storage.database import assert_external_io_allowed
from ..storage.jobs import after
from . import attempts, records
from .models import MessageDeliveryJob
from .notifications import notification_text


class MessageDispatcher:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def _job(self, tx, job, claim=None):
        if claim is not None:
            current = self.app.jobs._current_claim(tx, claim)
            if (claim.kind != "message_delivery" or canonical(claim.arguments) != current["args_json"]):
                raise CoreError("target_changed", "Delivery claim arguments changed")
        else:
            current = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if current is None or dict(current) != job:
            raise CoreError("target_changed", "Delivery job changed during inspection")
        args = MessageDeliveryJob.model_validate_json(job["args_json"])
        message = records.message(tx, args.message_id)
        recipient = get_row(tx, message.recipient_agent_id)
        actor = tx.connection.execute("SELECT kind,agent_id FROM actors WHERE actor_id=?", (recipient["actor_id"],)).fetchone()
        operation = tx.connection.execute("SELECT actor_id FROM operations WHERE operation_id=?", (job["operation_id"],)).fetchone()
        if (job["kind"] != "message_delivery" or not job["external_effect"]
                or job["target_id"] != str(message.recipient_agent_id)
                or tuple(actor or ()) != ("agent", recipient["id"])
                or operation is None or operation[0] != str(message.sender_actor_id)):
            raise CoreError("target_changed", "Delivery routing or attribution disagrees")
        installation = tx.connection.execute("SELECT * FROM installation").fetchone()
        delivery = records.delivery(tx, args.message_id)
        held = (job["wait_reason"] == "recovery_review" or job["recovery_epoch"] != installation["recovery_epoch"]
                or delivery.reason == "recovery_review")
        return message, delivery, dict(installation), held

    def _resolution(self, state, reason):
        if state in {"submitted", "failed", "cancelled"}:
            return JobResolution(state="failed" if state == "failed" else "cancelled" if state == "cancelled" else "completed",
                                 progress=JobProgress(phase=state))
        if state == "uncertain":
            return JobResolution(state="uncertain", reason="reconciliation_required",
                next_attempt_at=after(self.db.clock(), 5), progress=JobProgress(phase="inspection_required"),
                error=CoreError("uncertain_effect", "Message input requires its original receipt").error)
        held, offline = reason == "recovery_review", reason == "runtime_unavailable"
        return JobResolution(state="waiting", reason="recovery_review" if held else "dependency_unavailable" if offline else "retry_delay",
            next_attempt_at=after(self.db.clock(), 60 if held or offline else 2),
            progress=JobProgress(phase="recovery_held" if held else reason),
            error=CoreError("uncertain_effect" if held else "unsupported_capability",
                "Message delivery is held for recovery review" if held else "Message input is unavailable").error if held or offline else None)

    def _defer(self, job, claim, reason):
        with self.db.transaction() as tx:
            message, _, _, held = self._job(tx, job, claim)
            if held:
                return self._resolution("deferred", "recovery_review")
            state = "pending" if reason == "runtime_unavailable" else "deferred"
            attempts.delivery(tx, message.message_id, job, message.sender_actor_id, state, reason)
        return self._resolution(state, reason)

    def _receipt(self, row):
        try:
            found = self.app.runtime_input.inspect(attempts.target(row), UUID(row["attempt_id"]))
            if found is not None:
                try:
                    return attempts.validate_receipt(row, found), None
                except (CoreError, ValueError):
                    return None, "receipt_mismatch"
        except Exception:
            pass
        return None, "receipt_unavailable"

    def _record(self, job, claim, row, *, receipt=None, reason=None):
        if receipt is not None:
            receipt = attempts.validate_receipt(row, receipt)
            state, reason = receipt.state, attempts.receipt_reason(receipt)
        else:
            state, reason = "uncertain", reason or "receipt_unavailable"
        with self.db.transaction() as tx:
            message, _, _, held = self._job(tx, job, claim)
            if held:
                return self._resolution("deferred", "recovery_review")
            current = attempts.latest(tx, message.message_id)
            if current != row:
                raise CoreError("target_changed", "Saved attempt changed during receipt inspection")
            attempts.observe(tx, row, job, message.sender_actor_id, state, reason, receipt)
            attempts.delivery(tx, message.message_id, job, message.sender_actor_id, state, reason)
            self.db.fault("delivery_before_receipt_commit")
        self.db.fault("delivery_after_receipt_commit")
        return self._resolution(state, reason)

    def _saved(self, job, claim=None, *, inspecting=False):
        with self.db.read() as tx:
            message, delivery, _, held = self._job(tx, job, claim)
            if delivery.state in {"submitted", "failed", "cancelled"}:
                return self._resolution(delivery.state, delivery.reason)
            if held:
                return self._resolution("deferred", "recovery_review")
            row = attempts.latest(tx, message.message_id)
        if row is not None:
            if row["job_id"] != job["job_id"] or row["recovery_epoch"] != job["recovery_epoch"]:
                raise CoreError("target_changed", "Attempt belongs to another job epoch")
            if row["state"] == "prepared":
                # No host dispatching intent was ever committed. Inspection
                # can prove no input here without relying on a live session.
                with self.db.transaction() as tx:
                    self._job(tx, job, claim)
                    if attempts.latest(tx, message.message_id) != row:
                        raise CoreError("target_changed", "Prepared attempt changed")
                    attempts.observe(tx, row, job, message.sender_actor_id, "deferred", "interrupted_before_dispatch")
                    attempts.delivery(tx, message.message_id, job, message.sender_actor_id, "deferred", "interrupted_before_dispatch")
                return self._resolution("deferred", "interrupted_before_dispatch")
            if row["state"] in {"dispatching", "uncertain"}:
                receipt, reason = self._receipt(row)
                return self._record(job, claim, row, receipt=receipt, reason=reason)
            if row["state"] in {"submitted", "failed"}:
                # Crash after the host receipt commit is already authoritative.
                receipt = attempts.validate_receipt(row, json.loads(row["runner_receipt_json"]))
                return self._record(job, claim, row, receipt=receipt)
        if delivery.state in {"uncertain", "dispatching"}:
            # Missing host evidence cannot be replaced by fresh readiness.
            with self.db.transaction() as tx:
                self._job(tx, job, claim)
                attempts.delivery(tx, message.message_id, job, message.sender_actor_id, "uncertain", "receipt_unavailable")
            return self._resolution("uncertain", "receipt_unavailable")
        if inspecting:
            if row is None and delivery.state == "pending" and delivery.reason == "runtime_unavailable":
                return self._resolution("pending", "runtime_unavailable")
            return self._defer(job, claim, row["reason"] if row else "not_dispatched")
        return None

    @staticmethod
    def _older(tx, message, epoch):
        return tx.connection.execute("""SELECT 1 FROM messages m
            JOIN message_deliveries d USING(message_id)
            JOIN jobs j ON j.kind='message_delivery' AND json_extract(j.args_json,'$.message_id')=m.message_id
            WHERE m.recipient_agent_id=? AND m.message_seq<? AND d.state IN ('pending','deferred')
                AND d.reason IS NOT 'recovery_review' AND j.recovery_epoch=?
                AND j.wait_reason IS NOT 'recovery_review' AND j.state IN ('queued','waiting','running','uncertain') LIMIT 1""",
            (str(message.recipient_agent_id), message.message_seq, epoch)).fetchone() is not None

    def _still_current(self, job, claim, row):
        try:
            with self.db.read() as tx:
                message, delivery, installation, held = self._job(tx, job, claim)
                current = attempts.latest(tx, message.message_id)
                return (not held and installation["installation_id"] == row["installation_id"]
                    and current == row and current["state"] == "dispatching" and delivery.state == "dispatching")
        except Exception:
            return False

    def execute(self, claim):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                job = dict(self.app.jobs._current_claim(tx, claim))
            saved = self._saved(job, claim)
            if saved is not None:
                return saved
            with self.db.read() as tx:
                message, _, _, _ = self._job(tx, job, claim)
            try:
                target = InputTarget.model_validate(self.app.runtime_input.prepare(message.recipient_agent_id))
            except InputUnavailable as exc:
                return self._defer(job, claim, "runtime_unavailable" if exc.reason in {
                    "runtime_unavailable", "unsupported_target"} else exc.reason)
            with self.db.read() as tx:
                self._job(tx, job, claim)
                older = self._older(tx, message, job["recovery_epoch"])
            if older:
                return self._defer(job, claim, "awaiting_earlier_message")
            with self.db.transaction() as tx:
                message, _, installation, held = self._job(tx, job, claim)
                if held:
                    return self._resolution("deferred", "recovery_review")
                agent = get_row(tx, message.recipient_agent_id)
                if (target.binding.agent_id != message.recipient_agent_id
                        or str(target.conversation_id) != agent["conversation_id"]
                        or str(target.installation_id) != installation["installation_id"]
                        or str(target.binding.recovery_epoch) != installation["recovery_epoch"]):
                    raise CoreError("target_changed", "Prepared input target changed")
                consumed = tx.connection.execute("""SELECT max(json_extract(target_json,'$.idle_event_seq'))
                    FROM delivery_attempts WHERE json_extract(target_json,'$.binding.run_id')=?
                    AND state IN ('dispatching','submitted','uncertain')""", (str(target.binding.run_id),)).fetchone()[0]
                if consumed is not None and target.idle_event_seq <= consumed:
                    attempts.delivery(tx, message.message_id, job, message.sender_actor_id, "deferred", "awaiting_native_turn")
                    return self._resolution("deferred", "awaiting_native_turn")
                previous = attempts.latest(tx, message.message_id)
                if previous is not None and previous["state"] != "deferred":
                    raise CoreError("target_changed", "An unresolved input attempt already exists")
                payload = notification_text(message.message_id).encode("ascii")
                now = self.db.clock()
                row = dict(attempt_id=str(self.db.id_factory()), message_id=str(message.message_id),
                    attempt_seq=previous["attempt_seq"] + 1 if previous else 1,
                    previous_attempt_id=previous["attempt_id"] if previous else None, job_id=job["job_id"],
                    installation_id=installation["installation_id"], recovery_epoch=installation["recovery_epoch"],
                    target_json=canonical(target), target_sha256=canonical_hash(target),
                    payload_sha256=hashlib.sha256(payload).hexdigest(), payload_bytes=len(payload), created_at=now,
                    state="prepared", reason="prepared", runner_receipt_json=None, updated_at=now)
                tx.connection.execute(f"INSERT INTO delivery_attempts ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                                      tuple(row.values()))
                attempts.event(tx, row, job, message.sender_actor_id)
            self.db.fault("delivery_after_prepared")
            with self.db.transaction() as tx:
                self._job(tx, job, claim)
                if attempts.latest(tx, message.message_id) != row:
                    raise CoreError("target_changed", "Prepared input attempt changed")
                row = attempts.observe(tx, row, job, message.sender_actor_id, "dispatching", "dispatch_intent")
                attempts.delivery(tx, message.message_id, job, message.sender_actor_id, "dispatching", "dispatch_intent")
                self.db.fault("delivery_before_intent_commit")
            self.db.fault("delivery_after_intent_commit")
            try:
                receipt = self.app.runtime_input.submit(target, UUID(row["attempt_id"]), message.message_id,
                    still_current=lambda: self._still_current(job, claim, row))
                receipt = attempts.validate_receipt(row, receipt)
            except Exception:
                receipt, reason = self._receipt(row)
            else:
                reason = None
            self.db.fault("delivery_after_runner_return")
            return self._record(job, claim, row, receipt=receipt, reason=reason)

    def inspect(self, job):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            return self._saved(job, inspecting=True)
