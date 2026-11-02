"""Authorized application of a filing's terminal outcome, and the read model over filings.

``authorize`` is the engine half of the host's ``context_filing_receipt`` operation. It takes only the stable receipt ID;
everything else is re-resolved from storage: the filing, the registered job, its program definition and worker, the input
and the recorded outcome. A caller cannot supply a success, a body, a commit or a replacement source or job. It refuses
unless the invocation is the bound job's own claimed worker with the frozen definition, so a matching receipt presented by
a different job, a cancelled job, a revoked worker or a changed definition is forbidden and the receipt stays
unapplied. The host half then applies its source bookkeeping and ``applied`` marks the receipt in the same prepared plan.

The read model (``describe``) is the one ``filing`` object document and job reads expose. It never claims more than the
database says: a queued input is not processed, a published outcome is ``completion_state=pending`` until its receipt is
applied, and ``source_current`` is false for every version that is not the applicable latest content.
"""

from __future__ import annotations

import json

from qorqut_context.integration.filing.outcomes import decode
from qorqut_context.integration.filing.sources import require_system
from qorqut_operations.contracts import DomainError, new_id, utc_now

APPLY_STATES = ("running", "applying", "waiting_signoff")
STOPPED_JOB_STATES = ("cancel_requested", "cancelled")
FILING_FIELDS = ("filing_id", "source_revision", "input_id", "job_id", "state", "published_commit", "completion_state",
                 "source_current", "error")
UNFILED = {"filing_id": None, "source_revision": None, "input_id": None, "job_id": None, "state": "unfiled",
           "published_commit": None, "completion_state": None, "source_current": None, "error": None}
ERROR_MESSAGE_LIMIT = 500


def receipt(uow, receipt_id: str) -> dict | None:
    return decode(uow.connection.execute("SELECT * FROM context_completions WHERE id=?", (receipt_id,)).fetchone())


def receipt_for_filing(uow, filing_id: str) -> dict | None:
    return decode(uow.connection.execute("SELECT * FROM context_completions WHERE filing_id=?", (filing_id,)).fetchone())


def filing(uow, filing_id: str):
    return uow.connection.execute(
        "SELECT f.*, v.source_kind, v.source_id, v.source_revision, v.operation, v.source_path, v.source_digest "
        "FROM context_filings f JOIN context_source_versions v ON v.id = f.source_version_id WHERE f.id=?", (filing_id,)).fetchone()


def filing_for_job(uow, job_id: str):
    row = uow.connection.execute("SELECT id FROM context_filings WHERE job_id=?", (job_id,)).fetchone()
    return None if row is None else filing(uow, row["id"])


def _job(uow, job_id: str):
    return uow.connection.execute(
        "SELECT id, business_id, kind, state, principal_id, initiated_by_principal_id, definition_ref_json, result_json, error_json "
        "FROM jobs WHERE id=?", (job_id,)).fetchone()


def authorize(uow, ctx, receipt_id: str) -> dict:
    """The stored receipt, its filing and its job, once ``ctx`` is proven to be that job's own claimed worker.

    Raises ``forbidden`` for any other invocation and ``not_found`` for an unknown receipt. A receipt already ``applied`` is
    returned as such so the caller can replay it; one in ``conflict`` or ``pending`` can be applied."""
    if (ctx.source != "worker" or ctx.principal_kind != "system" or ctx.job_id is None):
        raise DomainError("forbidden", "A Context completion applies only through a claimed registered job")
    stored = receipt(uow, receipt_id)
    if stored is None:
        raise DomainError("not_found", "Context completion receipt does not exist")
    bound = filing(uow, stored["filing_id"])
    job = _job(uow, ctx.job_id)
    if bound is None or job is None or bound["job_id"] != ctx.job_id or stored["job_id"] != ctx.job_id:
        raise DomainError("forbidden", "This completion receipt belongs to another job")
    definition = json.loads(job["definition_ref_json"])
    if (job["business_id"] != bound["business_id"] or job["kind"] != bound["program_id"]
            or job["principal_id"] != ctx.principal_id or bound["worker_id"] != ctx.principal_id
            or job["initiated_by_principal_id"] != ctx.initiated_by_principal_id
            or definition.get("digest") != bound["definition_digest"] or definition.get("version") != bound["program_version"]):
        raise DomainError("forbidden", "The job or its frozen program definition no longer matches this filing")
    if job["state"] not in APPLY_STATES:
        raise DomainError("forbidden", "Only a running, applying or waiting claimed job can apply its completion")
    worker = uow.connection.execute(
        "SELECT p.kind, p.active, m.active AS member FROM principals p LEFT JOIN business_memberships m "
        "ON m.principal_id = p.id AND m.business_id IS ? WHERE p.id=?", (bound["business_id"], ctx.principal_id)).fetchone()
    if worker is None or worker["kind"] != "system" or not worker["active"] or (bound["business_id"] and not worker["member"]):
        raise DomainError("forbidden", "The registered worker is not active for this scope")
    return {"receipt": stored, "filing": bound, "job": job}


def applied_marker(builder, uow, stored: dict, application: dict) -> None:
    """Mark the receipt applied in the caller's prepared plan (the host adds its bookkeeping to the same plan)."""
    if stored["state"] == "applied":
        raise DomainError("invalid_transition", "This completion was already applied")
    before = uow.repository("context_completions", business_id=stored["business_id"]).get(stored["id"], required=True)
    builder.record("context_completions", stored["id"], before, {"state": "applied", "application_json": application,
                                                                 "resolved_at": builder.at},
                   business_id=stored["business_id"], label="Context completion")


def mark_conflict(uow, *, driver_id: str, receipt_id: str, error: dict) -> dict:
    """Make an unapplied receipt's inability to apply visible (system principal only; the outcome itself is untouched)."""
    require_system(uow, driver_id)
    stored = receipt(uow, receipt_id)
    if stored is None:
        raise DomainError("not_found", "Context completion receipt does not exist")
    if stored["state"] == "applied":
        raise DomainError("invalid_transition", "An applied completion cannot conflict")
    detail = {"code": str(error.get("code") or "completion_conflict")[:100],
              "message": str(error.get("message") or "The completion cannot be applied")[:ERROR_MESSAGE_LIMIT], "at": utc_now()}
    uow.connection.execute("UPDATE context_completions SET state='conflict', application_json=?, revision=revision+1 WHERE id=?",
                           (json.dumps(detail, sort_keys=True), receipt_id))
    return receipt(uow, receipt_id)


def verify_pending(uow, *, driver_id: str, programs) -> list[dict]:
    """Mechanical recovery check of unapplied receipts against the registered programs (``programs.get(kind)`` returns the
    current definition). A receipt whose bound job is gone or cancelled (a cancelled job is not a completion worker and is
    never revived), or whose frozen program definition is no longer the registered one, can never be applied by the generic
    authority, so it is marked ``conflict`` with the reason instead of staying an unexplained pending item; a
    ``binding_changed`` one that matches again returns to ``pending``. Nothing is applied and no job is changed."""
    require_system(uow, driver_id)
    changed = []
    for row in uow.connection.execute("SELECT id FROM context_completions WHERE state != 'applied' ORDER BY created_at, id").fetchall():
        stored = receipt(uow, row["id"])
        bound = filing(uow, stored["filing_id"])
        job = _job(uow, bound["job_id"])
        current = programs.get(bound["program_id"])
        reason = None
        if job is None:
            reason = "The bound job no longer exists"
        elif job["state"] in STOPPED_JOB_STATES:
            if stored["state"] == "pending":
                changed.append(mark_conflict(uow, driver_id=driver_id, receipt_id=stored["id"], error={
                    "code": "job_cancelled", "message": "The bound job was cancelled, so this completion cannot be applied by it"}))
            continue
        elif current is None or current["digest"] != bound["definition_digest"]:
            reason = "The registered program definition changed since this filing was accepted; its job cannot apply the completion"
        else:
            definition = json.loads(job["definition_ref_json"])
            if (job["business_id"] != bound["business_id"] or job["kind"] != bound["program_id"]
                    or job["principal_id"] != bound["worker_id"] or definition.get("digest") != bound["definition_digest"]):
                reason = "The bound job no longer matches the filing"
        if reason and stored["state"] == "pending":
            changed.append(mark_conflict(uow, driver_id=driver_id, receipt_id=stored["id"],
                                         error={"code": "binding_changed", "message": reason}))
        elif reason is None and stored["state"] == "conflict" and stored["application_json"].get("code") == "binding_changed":
            uow.connection.execute("UPDATE context_completions SET state='pending', application_json=NULL, revision=revision+1 "
                                   "WHERE id=?", (stored["id"],))
            changed.append(receipt(uow, stored["id"]))
    return changed


# -- reads ------------------------------------------------------------------------------------------------------------

def versions_of(uow, kind: str, source_id: str) -> list[dict]:
    return [dict(row) for row in uow.connection.execute(
        "SELECT v.*, f.id AS filing_id FROM context_source_versions v LEFT JOIN context_filings f ON f.source_version_id = v.id "
        "WHERE v.source_kind=? AND v.source_id=? ORDER BY v.source_revision", (kind, source_id))]


def source_current(uow, bound, current_digest) -> bool:
    """Whether this filing's version is the applicable latest accepted content: the latest version of its source, and the
    source's current content still has the version's frozen digest. A retraction is compared like any other version: its
    frozen digest names the exact published content it removes, so changed, missing or archived content is not current.
    ``current_digest`` is the digest of the source as it is now (``None`` when the source no longer exists)."""
    latest = uow.connection.execute("SELECT MAX(source_revision) FROM context_source_versions WHERE source_kind=? AND source_id=?",
                                    (bound["source_kind"], bound["source_id"])).fetchone()[0]
    if bound["source_revision"] != latest:
        return False
    return current_digest is not None and current_digest == bound["source_digest"]


def queue_position(uow, input_row) -> int | None:
    """How many unfinished inputs are ahead of an unfinished input (0 is the head); ``None`` for a final one."""
    if input_row["state"] in ("published", "discarded"):
        return None
    return uow.connection.execute("SELECT count(*) FROM context_inputs WHERE sequence < ? AND state NOT IN ('published','discarded')",
                                  (input_row["sequence"],)).fetchone()[0]


def _error(uow, bound, input_row, stored, job) -> dict | None:
    if stored is not None and stored["state"] == "conflict":
        return {"code": stored["application_json"]["code"], "message": stored["application_json"]["message"]}
    if input_row["state"] in ("blocked", "discard_requested") and input_row["error_json"]:
        error = json.loads(input_row["error_json"])
        return {"code": error.get("code", "blocked"), "message": str(error.get("message", ""))[:ERROR_MESSAGE_LIMIT]}
    if input_row["state"] == "discarded":
        reason = json.loads(input_row["result_json"] or "{}")
        shown = reason.get("reason") or (reason.get("request_evidence") or {}).get("reason") or reason.get("outcome") or "discarded"
        return {"code": "discarded", "message": str(shown)[:ERROR_MESSAGE_LIMIT]}
    if job is not None and job["state"] in STOPPED_JOB_STATES and (stored is None or stored["state"] != "applied"):
        return {"code": "job_" + job["state"], "message": "The registered job was stopped before this filing was completed"}
    return None


def describe(uow, filing_id: str, *, current_digest) -> dict:
    """The ``filing`` object for one filing, plus ``queue_position`` while its input is unfinished."""
    bound = filing(uow, filing_id)
    if bound is None:
        raise DomainError("not_found", "Context filing does not exist")
    input_row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (bound["input_id"],)).fetchone()
    stored = receipt_for_filing(uow, filing_id)
    published = None
    if input_row["state"] == "published":
        published = json.loads(input_row["result_json"])["published_commit"]
    return {"filing_id": filing_id, "source_revision": bound["source_revision"], "input_id": bound["input_id"],
            "job_id": bound["job_id"], "state": input_row["state"], "published_commit": published,
            "completion_state": None if stored is None else stored["state"],
            "source_current": source_current(uow, bound, current_digest),
            "error": _error(uow, bound, input_row, stored, _job(uow, bound["job_id"])),
            "queue_position": queue_position(uow, input_row)}


def applicable(uow, kind: str, source_id: str, current_digest) -> dict:
    """The filing of the source's current content: its latest accepted version if that is still the content, else unfiled.

    ``describe`` output for the latest version when it matches ``current_digest`` (a retraction's digest is the content it
    removes, so changed content after a retraction reads unfiled too); otherwise the explicit unfiled object (no
    identifiers) so an older version's result never stands in for content nobody has filed."""
    accepted = versions_of(uow, kind, source_id)
    if accepted:
        latest = accepted[-1]
        if current_digest is not None and latest["source_digest"] == current_digest:
            return describe(uow, latest["filing_id"], current_digest=current_digest)
    return {**UNFILED, "queue_position": None}


def stopped_unfinished(uow) -> list[dict]:
    """Unfinished bound inputs whose registered job was cancelled (or is being cancelled), in queue order."""
    return [dict(row) for row in uow.connection.execute(
        "SELECT i.*, f.id AS filing_id, f.job_id, j.state AS job_state FROM context_inputs i "
        "JOIN context_filings f ON f.input_id = i.id JOIN jobs j ON j.id = f.job_id "
        "WHERE i.state NOT IN ('published','discarded') AND j.state IN ('cancel_requested','cancelled') ORDER BY i.sequence")]


def reconcile_stopped(uow, *, driver_id: str, outcomes) -> list[dict]:
    """Mechanical reconciliation of bound inputs whose registered job was cancelled (or is being cancelled), through the
    queue's own discard mechanism and with its evidence.

    Only the stored job state decides; nothing is supplied by a caller. The driver is the recorded requester; the human who
    cancelled is not recorded on the job and is never invented. A published input stays published (it is not listed as
    unfinished). An input that never started (``queued``, no attempt, no Context event) is discarded at once: a
    ``context_input_controls`` discard request by the system principal is stored already applied, the input becomes
    ``discarded`` and the required terminal outcome records its receipt in the same transaction. A blocked head records a
    pending discard request and becomes ``discard_requested``: the driver completes it only after it has established that
    nothing was published. That pending request freezes the cancellation evidence in its ``result_json.request_evidence``
    when it is created, and the queue keeps that object (and the control ID) in the final control, input and receipt results.
    A retry request still pending on the blocked head is rejected with the same evidence and superseded by the discard
    request in the same transaction, so cancellation can never start another attempt. A pending discard already continues
    toward its mechanical resolution, and a ``processing`` input is left alone: its in-flight effect stays unresolved until
    the driver has checked Context."""
    require_system(uow, driver_id)
    handled = []
    for stopped in stopped_unfinished(uow):
        row, job = stopped["id"], _job(uow, stopped["job_id"])
        evidence = {"reason": "job_cancelled", "job_id": job["id"], "job_state": job["state"],
                    "job_error": None if job["error_json"] is None else json.loads(job["error_json"]), "requested_by": driver_id}
        pending = uow.connection.execute("SELECT * FROM context_input_controls WHERE input_id=? AND status='pending'", (row,)).fetchone()
        unstarted = stopped["state"] == "queued" and stopped["attempt"] == 0 and stopped["context_event_id"] is None
        if pending is not None and (pending["intent"] != "retry" or stopped["state"] != "blocked"):
            continue
        if pending is None and not (unstarted or stopped["state"] == "blocked"):
            continue
        at, control_id = utc_now(), new_id()
        if unstarted:
            result = json.dumps({"outcome": "discarded", **evidence, "mechanical": False}, sort_keys=True)
            uow.connection.execute(
                "INSERT INTO context_input_controls(id,business_id,input_id,intent,expected_state,requested_by,requested_at,status,"
                "resolved_at,result_json) VALUES (?,?,?,'discard','queued',?,?,'applied',?,?)",
                (control_id, stopped["business_id"], row, driver_id, at, at, result))
            uow.connection.execute("UPDATE context_inputs SET state='discarded', result_json=? WHERE id=?", (result, row))
            outcomes.record(uow, dict(uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (row,)).fetchone()))
            handled.append({"input_id": row, "filing_id": stopped["filing_id"], "state": "discarded"})
        else:
            if pending is not None:
                # The explicit retry loses to the cancellation: it is rejected with the evidence and names its successor.
                uow.connection.execute(
                    "UPDATE context_input_controls SET status='rejected',resolved_at=?,result_json=? WHERE id=? AND status='pending'",
                    (at, json.dumps({"outcome": "superseded_by_cancellation", "superseded_by": control_id,
                                     "request_evidence": evidence}, sort_keys=True), pending["id"]))
            uow.connection.execute(
                "INSERT INTO context_input_controls(id,business_id,input_id,intent,expected_state,requested_by,requested_at,status,"
                "result_json) VALUES (?,?,?,'discard','blocked',?,?,'pending',?)",
                (control_id, stopped["business_id"], row, driver_id, at, json.dumps({"request_evidence": evidence}, sort_keys=True)))
            uow.connection.execute("UPDATE context_inputs SET state='discard_requested' WHERE id=?", (row,))
            handled.append({"input_id": row, "filing_id": stopped["filing_id"], "state": "discard_requested"})
    return handled
