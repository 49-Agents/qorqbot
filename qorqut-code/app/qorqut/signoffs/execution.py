"""The execution-state vocabulary of a Signoff and its SQL mirror (generic: no provider adapter contributes to it).

An internal change is ``not_started`` until it settles as applied, conflicted or failed. A stored platform-action
Signoff (the engine's generic state machine) reports the state its ``execution_json`` records."""

EXECUTION_STATES = ("not_applicable", "not_started", "applied", "conflicted", "failed", "awaiting_approval", "queued",
                    "dispatched", "completed", "canceled", "reconciliation_required")


def execution_state(row, uow=None):
    if row["category"] in {"input_request", "side_chat"}:
        return "not_applicable"
    if uow is not None:
        return uow.connection.execute("SELECT " + execution_sql("s") + " FROM signoffs s WHERE s.id=?", (row["id"],)).fetchone()[0]
    if row["category"] == "internal_change":
        return row["state"] if row["state"] in {"applied", "conflicted", "failed"} else "not_started"
    state = row["execution_json"].get("state")
    if state:
        return state
    if row["superseded_by_id"] or row["state"] == "rejected":
        return "canceled"
    return {"pending": "awaiting_approval", "approved": "not_started", "applied": "queued",
            "conflicted": "conflicted", "failed": "failed"}[row["state"]]


def execution_sql(alias):
    # Fixed internal alias, never caller SQL. Mirrors execution_state for pagination.
    if alias != "s":
        raise ValueError("Unsupported SQL alias")
    return """(CASE WHEN s.category IN ('input_request','side_chat') THEN 'not_applicable' WHEN s.category = 'internal_change' THEN
        CASE WHEN s.state IN ('applied','conflicted','failed') THEN s.state ELSE 'not_started' END
        WHEN json_extract(s.execution_json, '$.state') IS NOT NULL THEN
            CASE WHEN json_extract(s.execution_json, '$.state') = 'dispatched' AND EXISTS (
                SELECT 1 FROM jobs j WHERE j.id=json_extract(s.execution_json, '$.reconcile_job_id')
                    AND j.business_id=s.review_business_id AND j.state IN ('failed','cancelled'))
            THEN 'reconciliation_required' ELSE json_extract(s.execution_json, '$.state') END
        WHEN s.superseded_by_id IS NOT NULL OR s.state = 'rejected' THEN 'canceled'
        WHEN s.state = 'pending' THEN 'awaiting_approval'
        WHEN s.state = 'approved' THEN 'not_started'
        WHEN s.state = 'applied' THEN COALESCE((SELECT CASE
            WHEN json_extract(j.result_json, '$.dispatch_intent') IS NOT NULL THEN 'reconciliation_required'
            WHEN j.state = 'failed' THEN 'failed'
            WHEN j.state = 'cancelled' THEN 'canceled' ELSE 'queued' END
            FROM jobs j WHERE j.id=s.execution_job_id AND j.business_id=s.review_business_id), 'queued')
        ELSE s.state END)"""
