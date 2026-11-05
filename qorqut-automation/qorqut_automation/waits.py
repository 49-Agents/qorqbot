"""Reconciling a saved wait against the canonical record, without a script child.

A waiting step names exactly what it waits for:

``signoff``    a full action Signoff, bound to its operation, proposer, proposal revision and payload hash. An owner edit supersedes it
               with a revised proposal of the *same* operation and proposer in the *same* run; resolution follows only that lineage.
               When the newest revision is applied and queued a provider job, the step does not finish: it becomes a ``job`` wait.
``job``        the exact job the applied Signoff queued. The step ends only with that job's real outcome (a receipt result, a
               definite failure) - never on the approval alone and never by sending again. A terminal job that is not a success
               is classified from its own durable evidence (``job_verdict``): a final provider refusal, or a job that never
               recorded a dispatch intent (a pre-dispatch refusal or a cancellation before dispatch), is a known failure with no
               effect; a recorded dispatch intent without a final receipt - including every cancellation after it and the
               engine's ``external_unconfirmed`` - is an *unknown* outcome, never a failure.
``input``      a question, bound to its Signoff's proposal revision and payload hash. A superseded or changed question is not
               followed; only an answer by an eligible answerer (the owner, or the configured coordinator when the question's own
               policy and routing allow management) settles it.
``reconcile``  a bounded recheck of an uncertain effect; after its last recheck it becomes ``recovery_required``, which waits for a
               person (the run is never failed or replaced while an effect is unknown). Two sources: without ``source`` it is a
               transaction-safe engine call re-invoked under its original idempotency key (the engine answers it from its receipt or
               resumes its own file recovery); with ``source: "job"`` it stays tied to the original provider job, request and
               Signoff (with the approved content and identity unchanged) and only ever reads that job's canonical receipt through the
               provider's read operation, outside any transaction. It never queues, re-invokes or sends again; without a canonical
               receipt it remains visible and recovery-required, with the required Ordubashy notification.

``outcome`` is the one reading of a call's canonical result envelope (live, or the engine's saved idempotency receipt): only an actual
synchronous result is a finished step; a queued provider job - explicit, or named by an ``ok`` result of a queue-returning operation
whose job carries the trusted operation and run marker - and a proposal become the ``job`` and ``signoff`` waits above.

Nothing here sends, approves or answers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from qorqut_operations.contracts import ChangeSet, DomainError, InvocationOptions, canonical_hash, plain
from qorqut_operations.storage.repositories import decode_row

from . import steps
from .guard import automation_row, job_row

MAX_LINEAGE = 20
RECONCILE_SECONDS = 30
RECOVERY_RECHECK_SECONDS = 900      # a recovery-required job wait rereads its canonical receipt this rarely, until a person acts
MAX_RECONCILES = 5
MAX_APPROVED_BYTES = 32768
PENDING_JOB_STATES = ("queued", "running", "applying", "waiting_signoff", "cancel_requested")
# The fields that tie an unresolved wait to the original provider job, request and Signoff and to the content that was approved.
JOB_BINDING = ("job_id", "signoff_id", "original_signoff_id", "operation", "proposer_principal_id", "proposal_revision",
               "payload_hash", "approved")
# A provider job's definition marker -> the provider's own read operation for that job's canonical receipt (request id = job id).
RECEIPT_READS = {"fastmail_operation": "fastmail_receipt_get"}
RECEIPT_DONE = ("completed", "accepted")


def _row(uow, signoff_id):
    return decode_row(uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (signoff_id,)).fetchone())


def _failure(code, message, **details):
    return {"code": code, "message": message, "retryable": False, "details": details}


def _owner(uow):
    return (uow.repository("installation").get("service") or {}).get("owner_principal_id")


def route_for(uow, signoff_id):
    return uow.connection.execute("SELECT * FROM management_routing WHERE source_type='signoffs' AND source_id=?",
                                  (signoff_id,)).fetchone()


def _lineage(uow, wait, run_id):
    """The newest revision of the exact proposal the step waited for, or ``None`` when the lineage is not this run's own."""
    row = _row(uow, wait["signoff_id"])
    if (row is None or row["operation_name"] != wait["operation"] or row["proposer_principal_id"] != wait["proposer_principal_id"]
            or row["payload_hash"] != wait["payload_hash"] or row["proposal_revision"] != wait["proposal_revision"]):
        return None
    hops = 0
    while row["superseded_by_id"] and hops < MAX_LINEAGE:
        nxt = _row(uow, row["superseded_by_id"])
        if (nxt is None or nxt["supersedes_id"] != row["id"] or nxt["operation_name"] != row["operation_name"]
                or nxt["proposer_principal_id"] != row["proposer_principal_id"]
                or (nxt["policy_binding_json"] or {}).get("automation_run_id") != run_id):
            return None
        row, hops = nxt, hops + 1
    return row


def _approved(row):
    plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
    arguments = plain(plan.arguments)
    if len(json.dumps(arguments, default=str)) > MAX_APPROVED_BYTES:
        return {"truncated": True, "digest": canonical_hash(arguments)}
    return arguments


def _answerer(uow, row, audience):
    """Whether the recorded answer came from someone eligible for this question."""
    by = (row["decision_json"] or {}).get("by")
    if by is not None and by == _owner(uow):
        return "owner"
    route = route_for(uow, row["id"])
    form = (row["preview_json"] or {}).get("input") or {}
    if (route is not None and by == route["coordinator_id"] and route["management_allowed"]
            and route["attention_destination"] == "management" and form.get("response_policy") == "management_allowed"
            and form.get("kind") == "question"):
        return "agent"
    return None


def _moment(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _due(wait):
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(wait["not_before"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return True


def job_verdict(job):
    """What a terminal, unsuccessful provider job proves, from its own durable checkpoints only (the same evidence the host's email
    outcome reads): ``"completed"`` the provider's final receipt says it was done; ``"refused"`` the provider finally refused it, or no
    dispatch intent was ever recorded (the intent is written before the point of no return and is refused after a cancellation request),
    so nothing was sent; ``"unknown"`` anything else. A cancellation or a failure after a recorded intent is never proof of no effect,
    and the engine's ``external_unconfirmed`` is unknown by definition."""
    data = job["result_json"] if isinstance(job["result_json"], dict) else {}
    error = job["error_json"] if isinstance(job["error_json"], dict) else {}
    receipt = data.get("external_receipt") if isinstance(data.get("external_receipt"), dict) else {}
    if receipt.get("state") in RECEIPT_DONE:
        return "completed", receipt
    if receipt.get("state") == "failed":
        return "refused", receipt
    if error.get("code") == "external_unconfirmed" or data.get("dispatch_intent"):
        return "unknown", None
    return "refused", None


def _job_done(wait, job, result, *, reconciled=False):
    return {"signoff_id": wait["signoff_id"], "original_signoff_id": wait["original_signoff_id"], "state": "applied",
            "job": {"id": job["id"], "state": job["state"], **({"reconciled": True} if reconciled else {})},
            "approved": wait["approved"], "result": result}


def _unresolved_job_wait(wait, job, attempts):
    """The wait of a provider job whose outcome is unknown: the same job, request and Signoff, with the approved content and identity
    untouched, rechecking only its canonical receipt (bounded), then a visible recovery wait for a person. Nothing is queued or sent."""
    due = reconcile_wait(attempts)
    if due["kind"] == "recovery_required":
        due["not_before"] = _moment(RECOVERY_RECHECK_SECONDS)
    return {**due, **{key: wait.get(key) for key in JOB_BINDING}, "source": "job", "job_state": job["state"] if job else None}


def _refused(wait, job, receipt=None):
    code = "external_failed" if receipt else (job["error_json"] or {}).get("code") or "job_" + job["state"]
    message = ("The provider finally refused the action; nothing was applied" if receipt
               else (job["error_json"] or {}).get("message") or f"The queued action ended as {job['state']} before any dispatch")
    return "failed", {**_failure(code, message, job_id=job["id"], signoff_id=wait.get("signoff_id")), "effect_possible": False}


def _resolve_unresolved_job(uow, wait, receipt):
    """One recheck of an unresolved provider job wait. The job row is reread first (it may have settled or resumed); the canonical
    receipt (``receipt``, read before the transaction) is the only other evidence. Never ``ready``: a re-invocation would send again."""
    job = job_row(uow, wait["job_id"])
    if job is not None and job["initiated_by_principal_id"] == wait["proposer_principal_id"]:
        if job["state"] in PENDING_JOB_STATES:
            return "wait", {"kind": "job", **{key: wait.get(key) for key in JOB_BINDING}}
        if job["state"] == "succeeded":
            data = job["result_json"] or {}
            return "done", _job_done(wait, job, data.get("data", data), reconciled=True)
        verdict, evidence = job_verdict(job)
        if verdict == "completed":
            return "done", _job_done(wait, job, {"operation": wait.get("operation"), "receipt": plain(evidence)}, reconciled=True)
        if verdict == "refused":
            return _refused(wait, job, evidence)
    if isinstance(receipt, dict) and receipt.get("state") == "completed":
        return "done", _job_done(wait, job or {"id": wait["job_id"], "state": "unknown"},
                                 {"operation": wait.get("operation"), "receipt": plain(receipt)}, reconciled=True)
    if isinstance(receipt, dict) and receipt.get("state") == "failed" and job is not None:
        return _refused(wait, job, receipt)
    if not _due(wait):
        return "pending", None
    attempts = int(wait.get("attempts") or 0)
    return "wait", _unresolved_job_wait(wait, job, attempts if wait["kind"] == "recovery_required" else attempts + 1)


def _receipt_binds(receipt, job):
    """Whether a provider receipt is for exactly this job's recorded dispatch (request id, and the account, action and digest its
    own dispatch intent froze)."""
    if not isinstance(receipt, dict) or receipt.get("request_id") != job["id"]:
        return False
    intent = (job["result_json"] or {}).get("dispatch_intent") if isinstance(job["result_json"], dict) else None
    intent = intent if isinstance(intent, dict) else {}
    return all(receipt.get(field) == intent[field] for field in ("account_id", "action", "digest") if intent.get(field))


def canonical_receipt(ext, step, wait):
    """The original job's canonical provider receipt through the provider's own read operation, as the configured Ordubashy, with no
    transaction or writer admission held. ``None`` when the provider offers no such read, cannot be asked now or returns anything that
    is not for exactly this job; the caller then keeps waiting. It reads only; it never queues, retries or sends."""
    from .events import ordubashy
    app = ext.runtime.application
    with ext.runtime.database.read() as uow:
        job = job_row(uow, wait.get("job_id"))
        read = next((name for marker, name in RECEIPT_READS.items() if job is not None and marker in (job["definition_ref_json"] or {})), None)
        responder = ordubashy(uow)
        automation = automation_row(uow, step["automation_id"])
        if job is None or read is None or responder is None or automation is None:
            return None
        scope = automation["business_id"]
        ctx = app.policy.context_for(responder, business_id=scope, review_business_id=scope, uow=uow)
    try:
        result = app.invoke(ctx, read, {"request_id": job["id"]}, InvocationOptions(business_id=scope, review_business_id=scope))
    except DomainError:
        return None
    if result.status != "ok":
        return None
    receipt = (plain(result.data) or {}).get("receipt")
    return receipt if _receipt_binds(receipt, job) else None


def run_marked(ref, operation, proposer, run_id):
    """Whether a provider job's frozen definition carries the trusted marker its provider wrote for exactly this operation, principal and
    run: Fastmail's ``fastmail_operation`` with ``fastmail_automation_run``, or WhatsApp's ``whatsapp_send`` object."""
    ref = ref if isinstance(ref, dict) else {}
    return ((ref.get("fastmail_operation") == operation and ref.get("fastmail_automation_run") == run_id)
            or ref.get("whatsapp_send") == {"operation": operation, "agent_id": proposer, "automation_run_id": run_id})


def outcome(uow, operation, envelope, proposer, run_id, scope):
    """The one reading of a canonical operation result envelope - a live ``OperationResult.to_dict()`` or the idempotency receipt the engine
    saved for the same call - shared by the bridge and the cancellation settlement, so a call is classified the same way whether the script
    or a stopped run's recovery reads it. Never queues, sends or re-invokes anything.

    ``("ok", data)``    a completed synchronous result: the only one that is a succeeded step.
    ``("wait", wait)``  the effect is still ahead of the run. A ``job`` wait on the exact queued provider job: an explicit ``accepted``
                        result, or an ``ok`` result of a queue-returning operation (a non-send Fastmail mutation, a WhatsApp send) whose
                        ``job_id`` names a job initiated by ``proposer`` in the automation's scope that carries the trusted operation
                        and run marker - a ``job_id`` in any other result dictionary is only data. Or the exact ``signoff`` wait of a
                        ``signoff_required`` result, bound to this run, operation, proposer, proposal revision and payload hash.
    ``("error", failure)``  the envelope names a job or proposal that is not this run's own record; the caller keeps it visible.
    ``None``            an error envelope, or one that is not understood."""
    status = envelope.get("status") if isinstance(envelope, dict) else None
    if status == "accepted":
        job = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
        job_id = job.get("id") or job.get("job_id")
        if not job_id:
            return "error", _failure("job_unavailable", "The queued action has no job to follow")
        found = job_row(uow, job_id)
        if found is None or found["initiated_by_principal_id"] != proposer or found["business_id"] != scope:
            return "error", _failure("job_unavailable", "The queued action's job is not recorded for this run")
        return "wait", queued_wait(operation, proposer, job_id)
    if status == "ok":
        data = plain(envelope.get("data"))
        job_id = data.get("job_id") if isinstance(data, dict) else None
        found = job_row(uow, job_id) if isinstance(job_id, str) else None
        if (found is not None and found["initiated_by_principal_id"] == proposer and found["business_id"] == scope
                and run_marked(found["definition_ref_json"], operation, proposer, run_id)):
            return "wait", queued_wait(operation, proposer, job_id)
        return "ok", data
    if status == "signoff_required":
        summary = envelope.get("signoff") if isinstance(envelope.get("signoff"), dict) else {}
        row = _row(uow, summary.get("id"))
        if (row is None or row["operation_name"] != operation or row["proposer_principal_id"] != proposer
                or (row["policy_binding_json"] or {}).get("automation_run_id") != run_id):
            return "error", _failure("signoff_unavailable", "The proposal was not recorded for this run")
        return "wait", {"kind": "signoff", "signoff_id": row["id"], "operation": operation, "payload_hash": row["payload_hash"],
                        "proposal_revision": row["proposal_revision"], "proposer_principal_id": proposer}
    return None


def queued_wait(operation, proposer, job_id):
    """The wait of an action that is queued as a provider job and has not been approved through a Signoff of its own."""
    return {"kind": "job", "job_id": job_id, "signoff_id": None, "original_signoff_id": None, "operation": operation,
            "proposer_principal_id": proposer, "approved": None}


def resolve(uow, step, receipt=None):
    """``("pending"|"ready", None)``, ``("done", result)``, ``("failed", error)`` or ``("wait", new wait)`` for one waiting step.
    ``receipt`` is the canonical provider receipt read before the transaction for an unresolved job wait (never read here)."""
    wait = step["wait_json"] or {}
    kind = wait.get("kind")
    if kind in ("reconcile", "recovery_required") and wait.get("source") == "job":
        return _resolve_unresolved_job(uow, wait, receipt)
    if kind == "recovery_required":
        return "pending", None
    if kind == "reconcile":
        return ("ready", None) if _due(wait) else ("pending", None)
    if kind == "job":
        job = job_row(uow, wait["job_id"])
        if job is None or job["initiated_by_principal_id"] != wait["proposer_principal_id"]:
            return "failed", _failure("wait_binding_mismatch", "The queued action no longer matches this step", job_id=wait.get("job_id"))
        if job["state"] in PENDING_JOB_STATES:
            return "pending", None
        if job["state"] == "succeeded":
            result = job["result_json"] or {}
            return "done", _job_done(wait, job, result.get("data", result))
        verdict, evidence = job_verdict(job)
        if verdict == "completed":
            return "done", _job_done(wait, job, {"operation": wait.get("operation"), "receipt": plain(evidence)}, reconciled=True)
        if verdict == "unknown":
            return "wait", _unresolved_job_wait(wait, job, 1)
        return _refused(wait, job, evidence)
    if kind not in {"signoff", "input"} or not wait.get("signoff_id"):
        return "failed", _failure("wait_invalid", "The saved wait is not a recognized Signoff wait")
    if kind == "input":
        return _resolve_input(uow, step, wait)
    row = _lineage(uow, wait, step["run_id"])
    if row is None:
        return "failed", _failure("wait_binding_mismatch", "The Signoff or its revision lineage no longer matches this run's proposal")
    state = row["state"]
    if state in ("pending", "approved"):
        return "pending", None
    if state != "applied":
        return "failed", _failure("signoff_" + state, f"The owner decision ended as {state}", signoff_id=row["id"])
    applied = row["apply_result_json"] or {}
    data = applied.get("data", applied) if isinstance(applied, dict) else {}
    approved = _approved(row)
    job_id = data.get("job_id") if isinstance(data, dict) else None
    if job_id:
        return "wait", {"kind": "job", "job_id": job_id, "signoff_id": row["id"], "original_signoff_id": wait["signoff_id"],
                        "operation": wait["operation"], "proposer_principal_id": wait["proposer_principal_id"],
                        "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"], "approved": approved}
    return "done", {"signoff_id": row["id"], "original_signoff_id": wait["signoff_id"], "state": "applied",
                    "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"], "approved": approved,
                    "result": data}


def _resolve_input(uow, step, wait):
    row = _row(uow, wait["signoff_id"])
    if (row is None or row["category"] != "input_request" or row["proposer_principal_id"] != wait["proposer_principal_id"]
            or row["proposal_revision"] != wait["proposal_revision"] or row["payload_hash"] != wait["payload_hash"]):
        return "failed", _failure("wait_binding_mismatch", "The input request no longer matches this run's question")
    if row["superseded_by_id"]:
        return "failed", _failure("input_superseded", "The question was replaced; this run asked a different question", signoff_id=row["id"])
    state = row["state"]
    if state == "pending":
        return "pending", None
    if state != "answered":
        return "failed", _failure("input_" + state, f"The question ended as {state}", signoff_id=row["id"])
    by = _answerer(uow, row, wait.get("audience"))
    if by is None:
        return "failed", _failure("input_answerer_not_eligible", "The answer did not come from an eligible answerer", signoff_id=row["id"])
    decision = row["decision_json"] or {}
    note = uow.connection.execute("SELECT body FROM signoff_notes WHERE id=?", (decision.get("message_id"),)).fetchone()
    return "done", {"signoff_id": row["id"], "state": "answered", "selected_choices": decision.get("selected_choices", []),
                    "notes": note["body"] if note else "", "answered_at": row["updated_at"], "answered_by": by}


def pending_question(uow, step):
    """The details of an unanswered question this step waits on, for the manager's reads (``None`` for any other wait)."""
    wait = step["wait_json"] or {}
    if wait.get("kind") != "input":
        return None
    row = _row(uow, wait["signoff_id"])
    if row is None or row["state"] != "pending" or row["superseded_by_id"]:
        return None
    form = (row["preview_json"] or {}).get("input") or {}
    return {"step_key": step["step_key"], "signoff_id": row["id"], "audience": wait.get("audience"),
            "kind": form.get("kind"), "response_policy": form.get("response_policy"), "title": form.get("title"),
            "explanation": form.get("explanation"), "selection_mode": form.get("selection_mode"),
            "choices": form.get("choices", []), "selection_required": form.get("selection_required", False),
            "notes_required": form.get("notes_required", False), "proposal_revision": row["proposal_revision"],
            "payload_hash": row["payload_hash"], "status_revision": row["status_revision"], "asked_at": row["created_at"]}


def undecided(uow, step):
    """The Signoff a waiting step is waiting on while nobody has decided it - an unanswered question, or an action proposal the owner has
    not yet decided - else ``None``. Such a step has had no effect; an approved proposal is not undecided (its application may be under
    way), nor is a queued provider job or an unconfirmed outcome."""
    wait = step["wait_json"] or {}
    if wait.get("kind") not in ("input", "signoff") or not wait.get("signoff_id"):
        return None
    row = _row(uow, wait["signoff_id"]) if wait["kind"] == "input" else _lineage(uow, wait, step["run_id"])
    return row if row is not None and row["state"] == "pending" and not row["superseded_by_id"] else None


def settle_waiting(ext, step_id):
    """Resolve one waiting step in its own short writer-admitted transaction. Returns ``"pending"``, ``"ready"``, ``"done"`` or
    ``"failed"`` (a step that moved on to a further wait - the queued job - is still ``"pending"``)."""
    with ext.runtime.database.read() as uow:
        step = steps.get(uow, step_id)
    receipt = None
    wait = (step or {}).get("wait_json") or {}
    if step is not None and step["state"] == "waiting" and wait.get("source") == "job" and _due(wait):
        receipt = canonical_receipt(ext, step, wait)        # a provider read: no transaction and no writer admission is held
    with ext.writing() as uow:
        step = steps.get(uow, step_id)
        if step is None or step["state"] != "waiting":
            return "done" if step and step["state"] == "succeeded" else "failed"
        outcome, value = resolve(uow, step, receipt)
        if outcome == "done":
            steps.settle(uow, step_id, "succeeded", effect="confirmed" if step["kind"] != "ask" else "none", result=value)
        elif outcome == "failed":
            flag = value.pop("effect_possible", None) if isinstance(value, dict) else None
            closed = steps.settle(uow, step_id, "failed", error=value, effect=None if flag is None else ("possible" if flag else "none"))
            automation = uow.connection.execute("SELECT * FROM automations WHERE id=?", (closed["automation_id"],)).fetchone()
            if automation is not None:
                from .failures import record_step_failure
                record_step_failure(ext.runtime, uow, automation, closed["run_id"], closed, value)
        elif outcome == "wait":
            if value["kind"] in ("reconcile", "recovery_required"):
                # The provider job's outcome is unknown: keep the effect possible, keep the run, and tell Ordubashy (idempotent per step).
                closed = steps.settle(uow, step_id, "waiting", wait=value, effect="possible", debug={"reconciles": value.get("attempts")},
                                      error={"code": "external_unconfirmed", "message": "The provider action's outcome is not confirmed; "
                                             "its original job and receipt are being rechecked and nothing is sent again",
                                             "uncertain": True, "details": {"job_id": value.get("job_id")}})
                automation = uow.connection.execute("SELECT * FROM automations WHERE id=?", (closed["automation_id"],)).fetchone()
                if automation is not None:
                    from .failures import notify_uncertain
                    notify_uncertain(ext.runtime, uow, automation, closed["run_id"], closed, recovery=value["kind"] == "recovery_required")
            else:
                steps.settle(uow, step_id, "waiting", wait=value)
            outcome = "pending"
        elif outcome == "ready":
            steps.settle(uow, step_id, "unknown")
        return outcome


def reconcile_wait(attempts):
    """The wait of a step whose effect is unknown: a bounded recheck, then a visible recovery wait (never a terminal failure)."""
    if attempts > MAX_RECONCILES:
        return {"kind": "recovery_required", "attempts": attempts,
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")}
    due = datetime.now(timezone.utc) + timedelta(seconds=RECONCILE_SECONDS)
    return {"kind": "reconcile", "attempts": attempts, "not_before": due.isoformat(timespec="seconds").replace("+00:00", "Z")}
