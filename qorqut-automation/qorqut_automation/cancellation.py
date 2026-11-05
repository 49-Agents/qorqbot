"""Settling a stopped run's unresolved external effects against the canonical records, without a script child.

A run whose owner stop was requested - or any closed run that still holds an external step - must not free its automation while an
effect it may have caused is unresolved. ``settle_step`` examines one such step and does only what the evidence supports; it never
sends, approves, answers, re-invokes an operation or starts a script:

* a waiting step is first reconciled exactly as a poll would: a decision or an outcome that already exists settles it, and a queued
  provider job stays tied to its exact original job, request, Signoff and receipt (``waits``), never replaced or repeated;
* a decision nobody has given - an unanswered question, or an action proposal the owner has not decided - had no effect, and is closed
  *for this run*. The Signoff itself is left as it is, but the policy gate refuses to approve or apply it once the run is not open;
* a call that was interrupted before any outcome was recorded is closed only on positive evidence: a transaction-only operation without
  a receipt had no effect, and a committed receipt is read exactly as the bridge reads a live result (``waits.outcome``) - a completed
  synchronous result is the confirmed outcome, while a queued provider job or a proposal becomes the saved job or Signoff wait and is then
  followed or closed like any other wait. Anything else - an external provider callback without a receipt, a file application in
  progress, a receipt naming a record that is not this run's - becomes the existing visible recovery wait with the required notice;
* an approved proposal whose application may be under way, a queued or dispatching provider job and an unconfirmed outcome all stay
  unresolved. A cancelled parent or provider job is never taken as proof that nothing happened.
"""

from __future__ import annotations

import json

from qorqut_operations.contracts import canonical_hash, utc_now

from . import steps, waits
from .bridge import idempotency, outcome_path
from .failures import notify_uncertain
from .records import bounded


def settle_run(ext, run_id, check=None) -> int:
    """Settle what can be settled of one run's unresolved external steps; returns how many remain unresolved."""
    with ext.runtime.database.read() as uow:
        open_steps = steps.unresolved_for_run(uow, run_id)
    for step in open_steps:
        if check is not None:
            check()
        settle_step(ext, step["id"])
    with ext.runtime.database.read() as uow:
        return len(steps.unresolved_for_run(uow, run_id))


def sweep(ext, limit) -> None:
    """One bounded pass over the unresolved external steps of runs that already ended, for the background tick. Each row is stamped with
    its check time *before* it is examined, so a row that stays unresolved (a provider job still queued, a recovery wait) rotates to the end
    and the next pass reaches the others - a few persistent waits can never occupy every pass - and a row that fails is not retried first."""
    with ext.runtime.database.read() as uow:
        due = steps.unresolved_of_closed_runs(uow, limit)
    for step in due:
        with ext.writing() as uow:
            current = steps.get(uow, step["id"])
            if current is None or current["closed_at"] is not None:
                continue
            # Same state, wait, effect, result and error: only the check time in the debug metadata (and the revision) changes.
            steps.settle(uow, current["id"], current["state"], keep_wait=True, debug={steps.CHECKED: utc_now()})
        settle_step(ext, step["id"])


def settle_step(ext, step_id) -> None:
    with ext.runtime.database.read() as uow:
        step = steps.get(uow, step_id)
    if step is None or step["closed_at"] is not None or step["kind"] not in ("call", "ask"):
        return
    if step["state"] == "waiting":
        outcome = waits.settle_waiting(ext, step_id)
        if outcome == "pending":
            _release_undecided(ext, step_id)
            return
        if outcome != "ready":
            return                      # the canonical record settled it (done or failed)
        with ext.runtime.database.read() as uow:
            step = steps.get(uow, step_id)      # a due recheck of an engine call left it interrupted (unknown)
    if step is not None and step["state"] in ("running", "unknown"):
        _settle_interrupted(ext, step)


def _stopped(step, what):
    return {"code": "run_cancelled", "message": f"The run stopped before {what}; the step was closed for this run and nothing was sent or applied "
            "on its behalf", "details": {"step_key": step["step_key"]}}


def _close(ext, step_id, state, *, effect, error=None, result=None) -> None:
    with ext.writing() as uow:
        current = steps.get(uow, step_id)
        if current is not None and current["closed_at"] is None and current["state"] in ("running", "waiting", "unknown"):
            steps.settle(uow, step_id, state, effect=effect, error=error, result=result)


def _release_undecided(ext, step_id) -> None:
    with ext.writing() as uow:
        step = steps.get(uow, step_id)
        if step is None or step["state"] != "waiting":
            return
        row = waits.undecided(uow, step)
        if row is not None:
            steps.settle(uow, step_id, "cancelled", effect="none", error={
                **_stopped(step, "a decision was given"), "details": {"step_key": step["step_key"], "signoff_id": row["id"]}})


def _settle_interrupted(ext, step) -> None:
    if step["kind"] == "ask":
        # A question is a record, not an external effect: once the run is not open nothing waits for its answer.
        return _close(ext, step["id"], "cancelled", effect="none", error=_stopped(step, "the question was recorded"))
    key = idempotency(step["run_id"], step["step_key"], step["attempt"])
    path = outcome_path(ext, step["operation"], key)
    if path == "none":
        return _close(ext, step["id"], "cancelled", effect="none", error=_stopped(step, "the call took effect"))
    if path == "replay":
        saved = _receipt(ext, step["operation"], key)
        if saved is not None:
            return _adopt(ext, step, *saved)
    _hold(ext, step, recovery=path == "unresolved")


def _receipt(ext, operation, key):
    """``(principal, envelope)`` of the engine's committed receipt for this exact idempotency key - the saved result of the call, whatever
    its status - else ``None``."""
    with ext.runtime.database.read() as uow:
        row = uow.connection.execute("SELECT principal_id, result_json FROM idempotency_receipts WHERE operation_name=? AND "
                                     "idempotency_key=? LIMIT 1", (operation, key)).fetchone()
    try:
        saved = json.loads(row["result_json"]) if row is not None else None
    except ValueError:
        return None
    return (row["principal_id"], saved) if isinstance(saved, dict) else None


def _adopt(ext, step, principal, envelope) -> None:
    """Record what a committed receipt says the interrupted call did, read exactly as the bridge reads a live result: a completed
    synchronous result finishes the step; a queued provider job or a proposal is saved as its exact wait (never re-invoked, never
    resent) and followed from there; a receipt that is not understood or names a record that is not this run's stays unresolved."""
    with ext.runtime.database.read() as uow:
        found = waits.outcome(uow, step["operation"], envelope, principal, step["run_id"], step["business_id"])
    if found is None or found[0] == "error":
        return _hold(ext, step, recovery=True)
    status, value = found
    if status == "ok":
        try:
            value = bounded(value, ext.config.max_step_bytes, "Step result")
        except Exception:  # noqa: BLE001 - an oversized stored result is kept by digest, like any bounded record
            value = {"truncated": True, "digest": canonical_hash(value)}
        return _close(ext, step["id"], "succeeded", effect="confirmed", result=value)
    with ext.writing() as uow:
        current = steps.get(uow, step["id"])
        if current is None or current["closed_at"] is not None or current["state"] not in ("running", "unknown"):
            return
        steps.settle(uow, step["id"], "waiting", wait=value)
    settle_step(ext, step["id"])


def _hold(ext, step, *, recovery) -> None:
    """Keep an interrupted call's original identity unresolved as the existing bounded recheck (or, when nothing canonical can recheck
    it, the visible recovery wait), with the required Ordubashy notice in the same unit. Idempotent per step."""
    reconciles = int((step["debug_json"] or {}).get("reconciles") or 0) + 1
    wait = waits.reconcile_wait(waits.MAX_RECONCILES + 1 if recovery else reconciles)
    with ext.writing() as uow:
        current = steps.get(uow, step["id"])
        if current is None or current["closed_at"] is not None or current["state"] not in ("running", "unknown"):
            return
        closed = steps.settle(uow, step["id"], "waiting", wait=wait, effect="possible", debug={"reconciles": reconciles}, error={
            "code": "external_unconfirmed", "operation": step["operation"], "uncertain": True,
            "message": "The run stopped while this step was in flight and its outcome cannot be confirmed from a receipt; it was not repeated"})
        automation = uow.connection.execute("SELECT * FROM automations WHERE id=?", (closed["automation_id"],)).fetchone()
        if automation is not None:
            notify_uncertain(ext.runtime, uow, automation, closed["run_id"], closed, recovery=wait["kind"] == "recovery_required")
