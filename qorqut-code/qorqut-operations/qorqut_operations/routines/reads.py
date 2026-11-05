"""Bounded routine reads, retaining every authorized member of each invocation."""

from datetime import timedelta
import json

from qorqut_operations.contracts import canonical_hash
from qorqut_operations.storage.repositories import decode_row


def history_scope(ctx, args):
    parameters = []
    if args.get("scope") == "global":
        allowed = sorted(ctx.allowed_business_ids)
        marks = ",".join("?" for _ in allowed) or "NULL"
        clause = f"namespace='global' AND (business_id IS NULL OR business_id IN ({marks}))"
        parameters.extend(allowed)
    else:
        clause = "business_id IS ?"
        parameters.append(ctx.business_id)
    if args.get("routine_id"):
        clause += " AND routine_id=?"
        parameters.append(args["routine_id"])
    if args.get("since"):
        clause += " AND started_at>=?"
        parameters.append(args["since"])
    return clause, parameters


# Matches transport.routine_views.recent_runs. No inference from timestamps for
# manual invocations: only explicit fanout identity joins executions together.
GROUP_KEY = """CASE WHEN namespace='global' THEN json_array(namespace,routine_id,trigger,
    COALESCE(NULLIF(slot,''),NULLIF(json_extract(result_json,'$.fire'),''),
        NULLIF(json_extract(result_json,'$.run_group_id'),''),id)) ELSE json_array(id) END"""


def activity(uow, ctx, args, now):
    clause, parameters = history_scope(ctx, args)
    if args.get("scope") != "global":
        clause += " AND namespace!='global'"
    # Group before choosing recent runs or completion hours: a long-running or
    # failed sibling must not disappear because another member finished recently.
    cte = f"WITH scoped AS (SELECT *, rowid AS _scan_rowid, {GROUP_KEY} AS group_key FROM routine_runs WHERE {clause}) "
    having, extra = "", []
    if args["view"] == "hourly":
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=47)
        having = " HAVING julianday(MAX(COALESCE(finished_at,started_at)))>=julianday(?)"
        extra.append(start.isoformat().replace("+00:00", "Z"))
    groups = [dict(row) for row in uow.connection.execute(cte + """SELECT group_key,namespace,routine_id,
        MIN(started_at) AS start, MAX(COALESCE(finished_at,started_at)) AS end,
        CASE WHEN SUM(state IN ('queued','running','applying','waiting_signoff','cancel_requested'))>0 THEN 'running'
             WHEN SUM(state='failed')>0 THEN 'failed'
             WHEN SUM(state='cancelled')>0 THEN 'cancelled' ELSE 'succeeded' END AS state
        FROM scoped GROUP BY group_key""" + having, [*parameters, *extra])]
    if args["view"] == "hourly":
        return {"items": [{k: v for k, v in row.items() if k != "group_key"} for row in groups]}
    for row in groups:
        key = json.loads(row["group_key"])
        row["id"] = "global-" + canonical_hash(key) if row["namespace"] == "global" else key[0]
    selected = sorted(groups, key=lambda row: (row["start"], row["id"]), reverse=True)[:args.get("limit", 50)]
    if not selected:
        return {"items": []}
    marks = ",".join("?" for _ in selected)
    records = []
    # An index on routine_runs can change the table's scan order; ORDER BY the
    # rowid captured before any WHERE/index choice keeps this pre-index order
    # (today's order) exact, independent of how the new indexes get used.
    for row in uow.connection.execute(cte + f"SELECT * FROM scoped WHERE group_key IN ({marks}) ORDER BY _scan_rowid",
                                      [*parameters, *(row["group_key"] for row in selected)]):
        record = decode_row(row)
        record.pop("group_key")
        record.pop("_scan_rowid")
        records.append(record)
    return {"items": records}
