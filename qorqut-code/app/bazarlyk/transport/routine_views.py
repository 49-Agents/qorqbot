"""Owner presentation of authorized routine executions; no storage or execution."""

from datetime import datetime, timedelta, timezone
import json

from qorqut_operations.contracts import canonical_hash

PENDING = frozenset({"queued", "running", "applying", "waiting_signoff", "cancel_requested"})


def run_view(row, *, businesses=None, definitions=None):
    result, costs = row.get("result_json", {}), row.get("cost_json", {})
    data = result.get("data") or {}
    definition = {**(definitions or {}).get((row["namespace"], row["routine_id"]), {}),
                  **result.get("definition", {})}
    calls = costs.get("calls") or []
    models = list(dict.fromkeys(call["model"] for call in calls if call.get("model")))
    model = ", ".join(models) or costs.get("model")
    error = result.get("error")
    stderr = result.get("stderr_tail") or (error.get("message") if isinstance(error, dict) else error) or ""
    if data.get("errors"):
        stderr = "\n".join(filter(None, [stderr, json.dumps(data["errors"], ensure_ascii=False, indent=2)]))
    stdout = result.get("stdout_tail") or data.get("stdout") or (json.dumps(data, ensure_ascii=False, indent=2) if data else "")
    seconds = 0
    if row.get("finished_at"):
        seconds = max(0, int((datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00")) -
            datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))).total_seconds()))
    business = row.get("business_id")
    member = {"id": row["id"], "business_id": business,
        "slug": (businesses or {}).get(business, business) if business else "root",
        "ok": row["state"] == "succeeded", "state": row["state"], "model": model,
        "stdout_tail": stdout, "stderr_tail": stderr, "rc": data.get("returncode", result.get("rc")),
        "usd": costs.get("usd", 0), "error": error, "job_id": result.get("job_id")}
    return {**row, "run": row["id"], "routine": row["routine_id"],
        "name": definition.get("name", row["routine_id"]), "zone": definition.get("timezone"),
        "scope": "global" if row["namespace"] == "global" else "biz",
        "start": row["started_at"], "end": row.get("finished_at"), "seconds": seconds,
        "usd": costs.get("usd", 0), "ok": member["ok"], "running": row["state"] in PENDING,
        "job_id": result.get("job_id"), "error": error, "rc": member["rc"],
        "usage_complete": costs.get("calls") is not None or bool(model), "runs": [member]}


def recent_runs(rows, *, limit=50, businesses=None, definitions=None):
    groups = {}
    for row in rows:
        result = row.get("result_json", {})
        group = result.get("run_group_id")
        # Legacy scheduled/manual fanout records have an explicit common fire.
        # Never infer a common manual invocation from an absent slot or a date.
        fire = row.get("slot") or result.get("fire")
        key = (row["namespace"], row["routine_id"], row["trigger"], fire or group or row["id"]) if row["namespace"] == "global" else (row["id"],)
        groups.setdefault(key, {})[row["id"]] = row
    output = []
    for key, records in groups.items():
        members = [run_view(row, businesses=businesses, definitions=definitions)
                   for row in sorted(records.values(), key=lambda r: (r["started_at"], r["id"]))]
        first = members[0]
        running = any(x["running"] for x in members)
        failed = any(x["state"] == "failed" for x in members)
        state = "running" if running else "failed" if failed else "cancelled" if any(x["state"] == "cancelled" for x in members) else "succeeded"
        identity = "global-" + canonical_hash(key) if first["scope"] == "global" else first["id"]
        output.append({**first, "id": identity, "run": identity, "state": state,
            "ok": state == "succeeded", "running": running,
            "start": min(x["start"] for x in members),
            "end": None if running else max(x["end"] or x["start"] for x in members),
            "seconds": sum(x["seconds"] for x in members), "usd": round(sum(x["usd"] for x in members), 8),
            "error": next((x["error"] for x in members if x["error"]), None),
            "usage_complete": all(x["usage_complete"] for x in members),
            "runs": [item for x in members for item in x["runs"]]})
    return sorted(output, key=lambda x: (x["start"], x["id"]), reverse=True)[:limit]


def hourly_runs(runs, *, now=None):
    """48 UTC hourly buckets of completed invocations, including global fanout once."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=47)
    output = {}
    for run in runs:
        if run["state"] not in {"succeeded", "failed"} or not run.get("end"):
            continue
        finished = datetime.fromisoformat(run["end"].replace("Z", "+00:00"))
        bucket = int((finished - start).total_seconds() // 3600)
        if not 0 <= bucket < 48 or finished > now:
            continue
        key = run["namespace"] + "/" + run["routine_id"]
        slots = output.setdefault(key, [{"hour": (start + timedelta(hours=i)).isoformat(), "succeeded": 0, "failed": 0} for i in range(48)])
        slots[bucket][run["state"]] += 1
    return {"start": start.isoformat(), "hours": 48, "routines": output}
