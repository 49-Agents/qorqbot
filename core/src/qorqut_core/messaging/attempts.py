"""SQL-only attempt identities, typed observations and attributed transitions."""

import hashlib
from uuid import UUID

from ..contracts import CoreError, canonical, canonical_hash
from ..runner.models import AttemptReceipt
from ..runtime.input_dispatch import InputTarget
from ..storage.events import EventInput, append_event
from .models import DeliveryAttempt
from .notifications import notification_text


def latest(tx, message_id):
    row = tx.connection.execute("SELECT * FROM delivery_attempts WHERE message_id=? ORDER BY attempt_seq DESC LIMIT 1",
                                (str(message_id),)).fetchone()
    return dict(row) if row else None


def target(row):
    value = InputTarget.model_validate_json(row["target_json"])
    if (canonical(value) != row["target_json"] or canonical_hash(value) != row["target_sha256"]
            or str(value.installation_id) != row["installation_id"]
            or str(value.binding.recovery_epoch) != row["recovery_epoch"]):
        raise CoreError("target_changed", "Saved delivery target is inconsistent")
    return value


def view(row):
    value = target(row)
    fields = {k: row[k] for k in ("attempt_id", "message_id", "attempt_seq", "previous_attempt_id",
        "payload_sha256", "payload_bytes", "state", "reason", "created_at", "updated_at")}
    fields.update(run_id=value.binding.run_id, run_generation=value.binding.run_generation,
        terminal_id=value.terminal_id, provider=value.provider, cli_version=value.cli_version,
        idle_event_seq=value.idle_event_seq, control_epoch=value.control_epoch)
    return DeliveryAttempt(**fields)


def list_attempts(tx, message_id):
    return [view(row) for row in tx.connection.execute(
        "SELECT * FROM delivery_attempts WHERE message_id=? ORDER BY attempt_seq", (str(message_id),))]


def event(tx, row, job, actor_id, *, old_state=None, old_reason=None):
    value = view(row)
    append_event(tx, EventInput(kind="message.attempt_changed", actor_id=actor_id,
        agent_id=job["target_id"], operation_id=job["operation_id"], payload={
            **value.model_dump(mode="json"), "old_state": old_state, "old_reason": old_reason,
            "job_id": job["job_id"], "target_sha256": row["target_sha256"],
            "recovery_epoch": row["recovery_epoch"]}))


def delivery(tx, message_id, job, actor_id, state, reason):
    tx.require_write()
    row = tx.connection.execute("SELECT * FROM message_deliveries WHERE message_id=?", (str(message_id),)).fetchone()
    if (row["state"], row["reason"]) == (state, reason):
        return
    if row["state"] in {"submitted", "failed", "cancelled"}:
        raise CoreError("invalid_transition", "Final delivery cannot change")
    tx.connection.execute("UPDATE message_deliveries SET state=?,reason=?,updated_at=? WHERE message_id=?",
                         (state, reason, tx.db.clock(), str(message_id)))
    append_event(tx, EventInput(kind="message.delivery_changed", actor_id=actor_id,
        agent_id=row["recipient_agent_id"], operation_id=job["operation_id"], payload={
            "message_id": str(message_id), "old_state": row["state"], "old_reason": row["reason"],
            "state": state, "reason": reason}))


def observe(tx, row, job, actor_id, state, reason, receipt=None):
    tx.require_write()
    encoded = canonical(receipt) if receipt is not None else row["runner_receipt_json"]
    if (row["state"], row["reason"], row["runner_receipt_json"]) == (state, reason, encoded):
        return row
    tx.connection.execute("""UPDATE delivery_attempts SET state=?,reason=?,runner_receipt_json=?,updated_at=?
        WHERE attempt_id=?""", (state, reason, encoded, tx.db.clock(), row["attempt_id"]))
    updated = dict(tx.connection.execute("SELECT * FROM delivery_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone())
    event(tx, updated, job, actor_id, old_state=row["state"], old_reason=row["reason"])
    return updated


def validate_receipt(row, receipt):
    saved = target(row)
    receipt = AttemptReceipt.model_validate(receipt)
    payload = notification_text(UUID(row["message_id"])).encode("ascii")
    if (str(receipt.attempt_id) != row["attempt_id"] or receipt.binding != saved.binding
            or receipt.binding.digest != saved.binding.digest
            or receipt.payload_sha256 != row["payload_sha256"]
            or receipt.payload_sha256 != hashlib.sha256(payload).hexdigest()
            or receipt.payload_bytes != row["payload_bytes"] or receipt.payload_bytes != len(payload)
            or receipt.control_epoch != saved.control_epoch
            or receipt.state not in {"submitted", "deferred", "uncertain", "failed"}
            or (receipt.state == "submitted" and receipt.phase != "finished")
            or (receipt.state == "deferred" and receipt.phase not in {"prepared", "paste"})):
        raise CoreError("target_changed", "Runner receipt does not match the saved attempt")
    return receipt


# Runner protocol values, never arbitrary OS/provider exception text.
_RECEIPT_REASONS = frozenset({
    "processing_not_confirmed", "interrupted_before_input", "interrupted_after_intent",
    "target_changed", "human_control", "capture_unavailable", "session_unavailable", "hook_unavailable",
    "native_busy", "native_waiting_user", "composer_busy", "composer_blocked", "composer_unknown",
    "not_ready", "paste_unverified", "boundary_failure", "unavailable", "machine_hold",
    "storage_blocked", "uncertain_effect", "unsupported_capability", "invalid_argument",
})


def receipt_reason(receipt):
    return receipt.reason if receipt.reason in _RECEIPT_REASONS else "runner_refused"
