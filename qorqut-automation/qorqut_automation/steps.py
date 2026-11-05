"""The durable step ledger of a run: one row per attempt of one stable step key.

A step key binds its request immutably (``request_hash``); a retry is a *new* attempt row linked to its parent, never a replay of the
failed invocation. Rows are written inside the caller's transaction (``uow``); this module opens none."""

from __future__ import annotations

import json

from qorqut_operations.contracts import DomainError, new_id, utc_now

OPEN = ("running", "waiting", "unknown")
CLOSED = ("succeeded", "failed", "cancelled")
MAX_ATTEMPTS = 3          # the initial attempt and at most two linked retries, counted from the ledger
CHECKED = "settle_checked_at"     # debug metadata: when a closed-run sweep last examined this still-unresolved step


def _decode(row):
    if row is None:
        return None
    return {key: json.loads(value) if key.endswith("_json") and value is not None else value for key, value in dict(row).items()}


def _json(value):
    return None if value is None else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def latest(uow, run_id: str, key: str):
    """The newest attempt of one step key in a run."""
    return _decode(uow.connection.execute("SELECT * FROM automation_steps WHERE run_id=? AND step_key=? "
                                          "ORDER BY attempt DESC LIMIT 1", (run_id, key)).fetchone())


def get(uow, step_id: str):
    return _decode(uow.connection.execute("SELECT * FROM automation_steps WHERE id=?", (step_id,)).fetchone())


def begin(uow, automation, run_id, key, kind, operation, request_hash, *, attempt=1, parent=None, effect="none", debug=None):
    """Insert one running attempt. ``automation`` is the automation row (its scope is copied onto the step)."""
    uow.require_write()
    step_id, now = new_id(), utc_now()
    uow.connection.execute(
        "INSERT INTO automation_steps(id,automation_id,business_id,run_id,step_key,attempt,parent_step_id,kind,operation,request_hash,"
        "state,effect,debug_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'running',?,?,?,?)",
        (step_id, automation["id"], automation["business_id"], run_id, key, attempt, parent, kind, operation, request_hash, effect,
         _json(debug or {}), now, now))
    return get(uow, step_id)


def settle(uow, step_id, state, *, effect=None, result=None, error=None, wait=None, debug=None, keep_wait=False):
    """Move one step to ``state``. A closed step (succeeded, failed, cancelled) never changes again. ``wait`` is replaced
    (cleared when omitted) unless ``keep_wait``."""
    uow.require_write()
    row = get(uow, step_id)
    if row is None:
        raise DomainError("not_found", "Automation step does not exist")
    if row["closed_at"] is not None:
        if row["state"] == state:
            return row
        raise DomainError("invalid_transition", "A closed automation step cannot change")
    now = utc_now()
    closed = now if state in CLOSED else None
    merged_debug = {**(row["debug_json"] or {}), **(debug or {})}
    kept_wait = row["wait_json"] if keep_wait else wait
    uow.connection.execute(
        "UPDATE automation_steps SET state=?, effect=?, wait_json=?, result_json=?, error_json=?, debug_json=?, closed_at=?, "
        "revision=revision+1, updated_at=? WHERE id=? AND closed_at IS NULL",
        (state, effect or row["effect"], _json(kept_wait), _json(result if result is not None else row["result_json"]),
         _json(error if error is not None else row["error_json"]), _json(merged_debug), closed, now, step_id))
    return get(uow, step_id)


def waiting_for_run(uow, run_id):
    """Every open waiting step of one run (not limited by how many earlier steps the run has)."""
    return [_decode(row) for row in uow.connection.execute(
        "SELECT * FROM automation_steps WHERE run_id=? AND state='waiting' ORDER BY created_at, attempt", (run_id,))]


def waiting(uow, limit=50):
    """Open steps blocked on a decision or an outcome, oldest first (bounded)."""
    return [_decode(row) for row in uow.connection.execute(
        "SELECT * FROM automation_steps WHERE state='waiting' ORDER BY updated_at LIMIT ?", (limit,))]


def unresolved_sql(alias: str = "") -> str:
    """The SQL condition for an external step whose outcome the ledger does not yet settle: a call or question that may have had an
    effect and is still running, waiting for its decision or outcome, or interrupted. While one survives, its run - and so its
    automation - stays unresolved."""
    p = alias + "." if alias else ""
    return f"{p}kind IN ('call','ask') AND {p}effect='possible' AND {p}state IN ('running','waiting','unknown')"


def unresolved_for_run(uow, run_id: str, limit=50) -> list:
    return [_decode(row) for row in uow.connection.execute(
        f"SELECT * FROM automation_steps WHERE run_id=? AND {unresolved_sql()} ORDER BY created_at, attempt LIMIT ?", (run_id, limit))]


def unresolved_of_automation(uow, automation_id: str):
    """``(id, state)`` of the run - and its job state - holding the oldest unresolved external step of this automation, or ``None``."""
    return uow.connection.execute(
        "SELECT s.run_id AS id, COALESCE(j.state,'unknown') AS state FROM automation_steps s LEFT JOIN jobs j ON j.id=s.run_id "
        f"WHERE s.automation_id=? AND {unresolved_sql('s')} ORDER BY s.created_at LIMIT 1", (automation_id,)).fetchone()


def unresolved_of_closed_runs(uow, limit=5) -> list:
    """The next unresolved external steps of runs that already reached a terminal state, for reconciliation (bounded). A step is skipped
    while its own wait says it is not yet due (a provider job rechecked every few minutes at most), and the rest are taken least recently
    examined first (never examined first, then ``CHECKED``), so every eligible step is reached however many persist."""
    return [_decode(row) for row in uow.connection.execute(
        "SELECT s.* FROM automation_steps s JOIN jobs j ON j.id=s.run_id WHERE j.state IN ('succeeded','failed','cancelled') "
        f"AND {unresolved_sql('s')} AND (json_extract(s.wait_json,'$.not_before') IS NULL "
        "OR json_extract(s.wait_json,'$.not_before') <= strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        f"ORDER BY COALESCE(json_extract(s.debug_json,'$.{CHECKED}'),''), s.updated_at, s.id LIMIT ?", (limit,))]


def narrowed(uow, run_id: str, operation: str) -> bool:
    """Whether an open call step of this run asked for its operation's full Signoff (``approval='required'``)."""
    return uow.connection.execute(
        "SELECT 1 FROM automation_steps WHERE run_id=? AND kind='call' AND operation=? AND state IN ('running','waiting','unknown') "
        "AND json_extract(debug_json,'$.approval')='required' LIMIT 1", (run_id, operation)).fetchone() is not None


def grant_steps(uow, run_id: str) -> list:
    """The run-only grant steps still in force: owner-approved grants stay ``running`` until revoked (``cancelled``); they
    end with the run because the gate only serves an open run."""
    return [_decode(row) for row in uow.connection.execute(
        "SELECT * FROM automation_steps WHERE run_id=? AND kind='grant' AND state='running' ORDER BY created_at", (run_id,))]


def run_grants(uow, run_id: str) -> list:
    return [step["result_json"]["grant"] for step in grant_steps(uow, run_id)]


def page_for_run(uow, run_id, *, after=None, limit=100):
    """One page of a run's steps, oldest first. ``after`` is the ``(created_at, id)`` of the previous page's last step."""
    parameters = [run_id]
    clause = "run_id=?"
    if after is not None:
        clause += " AND (created_at, id) > (?, ?)"
        parameters += list(after)
    rows = uow.connection.execute(f"SELECT * FROM automation_steps WHERE {clause} ORDER BY created_at, id LIMIT ?",
                                  [*parameters, limit + 1]).fetchall()
    return [_decode(row) for row in rows]
