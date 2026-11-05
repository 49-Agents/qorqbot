"""Failure delivery: every distinct failure becomes one durable inbox event for the configured Ordubashy, subscribed or not.

Ordubashy - not the owner - receives these events and decides what, if anything, to tell the owner.

A *failure identity* is one failed step attempt (``failure:step:<step id>``) or, when no step is to blame, one failed run
(``failure:run:<run id>``). A run that fails because of a step whose attempt already notified does not notify a second time; it
only pauses its automation. A failure the responder could not receive (none configured, a temporary outage) is left saved and
unnotified rather than rolled back; the reconciliation pass delivers it later and restores a missing or informational delivery to a
required one even when the event already exists. Pausing an automation also pauses its schedule (the bound routine)."""

from __future__ import annotations

import json

from qorqut_operations.contracts import utc_now

from .events import MANAGERS, ordubashy, publish
from .records import PROGRAM
from . import steps as ledger


def pause(uow, automation_id, reason):
    """Pause one active automation and the routine that schedules it, in the caller's transaction."""
    now = utc_now()
    row = uow.connection.execute("SELECT routine_id, state FROM automations WHERE id=?", (automation_id,)).fetchone()
    if row is None or row["state"] != "active":
        return False
    uow.connection.execute("UPDATE automations SET state='paused', pause_reason=?, revision=revision+1, updated_at=? WHERE id=?",
                           (reason[:1000], now, automation_id))
    if row["routine_id"]:
        uow.connection.execute("UPDATE routines SET status='paused', revision=revision+1, updated_at=? WHERE id=? AND status='active'",
                               (now, row["routine_id"]))
    return True


def _automation(uow, automation_id):
    return uow.connection.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone()


def record_step_failure(runtime, uow, automation, run_id, step, error):
    """Publish one failed step attempt (idempotent by its step id) inside the transaction that settled it. When no responder is
    configured nothing is written here and the saved failure is delivered by the reconciliation pass."""
    responder = ordubashy(uow)
    if responder is None:
        return None
    code = error.get("code", "failed")
    return publish(runtime, uow, automation, key=f"failure:step:{step['id']}", event_type="failure",
                   summary=f"Automation step {step['step_key']} failed (attempt {step['attempt']}): {code}",
                   data={"step_key": step["step_key"], "attempt": step["attempt"], "code": code,
                         "message": str(error.get("message", ""))[:500], "retryable": bool(error.get("retryable")),
                         "exhausted": bool(error.get("exhausted"))},
                   run_id=run_id, required_for=(responder,), audience=MANAGERS)


def notify_uncertain(runtime, uow, automation, run_id, step, *, recovery=False):
    """Prompt notice for Ordubashy that a step's outcome is unconfirmed (``recovery`` once bounded rechecks ran out and the
    run keeps waiting for a person; the run is never failed or replaced while the effect is unknown)."""
    responder = ordubashy(uow)
    if responder is None:
        return None
    kind = "recovery" if recovery else "uncertain"
    title = (f"Automation step {step['step_key']} needs recovery: its outcome could not be confirmed and the run is waiting"
             if recovery else f"Automation step {step['step_key']} outcome is unconfirmed; rechecking the original action")
    return publish(runtime, uow, automation, key=f"attention:{kind}:{step['id']}", event_type="attention", summary=title,
                   data={"step_key": step["step_key"], "attempt": step["attempt"], "operation": step["operation"],
                         "effect": step["effect"]},
                   run_id=run_id, required_for=(responder,), audience=MANAGERS)


def record_run_failure(runtime, uow, automation, run_id, code, message, *, details=None, step_id=None):
    """Account one failed run and pause its automation (and schedule) when this is its newest run.

    ``step_id`` names the failed step that caused the run to fail: that attempt's own event is the notification (created here when
    it is still missing), so the same failure never produces a second, generic run event."""
    step = ledger.get(uow, step_id) if step_id else None
    if step is not None and step["state"] == "failed":
        record_step_failure(runtime, uow, automation, run_id, step, step["error_json"] or {"code": code, "message": message})
        created = False
    else:
        responder = ordubashy(uow)
        created = False
        if responder is not None:
            _event, created = publish(runtime, uow, automation, key=f"failure:run:{run_id}", event_type="failure",
                                      summary=f"Automation run failed: {code}",
                                      data={"code": code, "message": str(message)[:800], "details": details or {}},
                                      run_id=run_id, required_for=(responder,), audience=MANAGERS)
    newest = uow.connection.execute(
        "SELECT id FROM jobs WHERE kind=? AND json_extract(arguments_json,'$.automation_id')=? ORDER BY created_at DESC LIMIT 1",
        (PROGRAM, automation["id"])).fetchone()
    if newest is None or newest["id"] == run_id:
        pause(uow, automation["id"], f"Run {run_id} failed: {code}")
    return created


def reconcile(runtime, uow, limit=20):
    """Sweep saved failures and uncertain waits that never reached a required delivery for the current Ordubashy (failures before
    the entrypoint, a crash between the engine's failure and ours, an outage of the responder). Bounded; returns the number
    delivered."""
    responder = ordubashy(uow)
    if responder is None:
        return 0
    delivered = 0
    jobs = uow.connection.execute(
        "SELECT j.id, j.arguments_json, j.error_json FROM jobs j WHERE j.kind=? AND j.state='failed' AND NOT EXISTS ("
        "SELECT 1 FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id AND d.agent_id=? AND d.required=1 "
        "WHERE e.source_type='automations' AND e.source_id=json_extract(j.arguments_json,'$.automation_id') "
        "AND json_extract(e.visibility_json,'$.key') IN ('failure:run:'||j.id, "
        "'failure:step:'||COALESCE(json_extract(j.error_json,'$.details.step_id'),''))) ORDER BY j.updated_at LIMIT ?",
        (PROGRAM, responder, limit)).fetchall()
    for job in jobs:
        automation = _automation(uow, json.loads(job["arguments_json"]).get("automation_id"))
        if automation is None:
            continue
        error = json.loads(job["error_json"]) if job["error_json"] else {}
        record_run_failure(runtime, uow, automation, job["id"], error.get("code", "failed"), error.get("message", "The run failed"),
                           details=error.get("details"), step_id=(error.get("details") or {}).get("step_id"))
        delivered += 1
    failed = uow.connection.execute(
        "SELECT s.* FROM automation_steps s WHERE s.state='failed' AND s.kind IN ('call','ask') AND NOT EXISTS ("
        "SELECT 1 FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id AND d.agent_id=? AND d.required=1 "
        "WHERE e.source_type='automations' AND e.source_id=s.automation_id "
        "AND json_extract(e.visibility_json,'$.key')='failure:step:'||s.id) ORDER BY s.updated_at LIMIT ?",
        (responder, limit)).fetchall()
    for row in failed:
        step = ledger._decode(row)
        automation = _automation(uow, step["automation_id"])
        if automation is not None:
            record_step_failure(runtime, uow, automation, step["run_id"], step, step["error_json"] or {})
            delivered += 1
    unsettled = uow.connection.execute(
        "SELECT s.* FROM automation_steps s WHERE s.state='waiting' AND json_extract(s.wait_json,'$.kind') IN "
        "('reconcile','recovery_required') AND NOT EXISTS (SELECT 1 FROM inbox_events e JOIN inbox_deliveries d "
        "ON d.event_id=e.id AND d.agent_id=? AND d.required=1 WHERE e.source_type='automations' AND e.source_id=s.automation_id "
        "AND json_extract(e.visibility_json,'$.key')='attention:'||(CASE json_extract(s.wait_json,'$.kind') "
        "WHEN 'recovery_required' THEN 'recovery' ELSE 'uncertain' END)||':'||s.id) ORDER BY s.updated_at LIMIT ?",
        (responder, limit)).fetchall()
    for row in unsettled:
        step = ledger._decode(row)
        automation = _automation(uow, step["automation_id"])
        if automation is not None:
            notify_uncertain(runtime, uow, automation, step["run_id"], step, recovery=step["wait_json"]["kind"] == "recovery_required")
            delivered += 1
    return delivered
