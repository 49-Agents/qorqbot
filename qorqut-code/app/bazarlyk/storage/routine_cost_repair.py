"""Explicit, reviewed maintenance repair of imported extraction costs.

Preview reads SQLite only. Apply requires a closed maintenance gate and an exact
unchanged preview. This never reads transcripts or reruns a provider/job.
"""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sqlite3

from bazarlyk.contracts import DomainError, canonical_hash, utc_now
from .database import Database
from .repositories import decode_row


def _plan(connection):
    installation = connection.execute("SELECT installation_id FROM installation WHERE id='service'").fetchone()
    if installation is None:
        raise DomainError("invalid_database", "Registered installation is required")
    sources = []
    for raw in connection.execute("SELECT r.* FROM routine_runs r JOIN routines d ON d.namespace=r.namespace AND d.id=r.routine_id "
                                  "WHERE d.job_kind='learnings.extract' ORDER BY r.id"):
        row = decode_row(raw)
        result = row["result_json"]
        match = re.match(r"\Arun ([A-Za-z0-9_-]+)\n", result.get("stdout_tail", ""))
        if result.get("legacy") and match:
            sources.append((row, match.group(1)))
    counts = Counter((row["business_id"], identity) for row, identity in sources)
    changes = []
    for row, identity in sources:
        if counts[(row["business_id"], identity)] != 1 or row["state"] != "succeeded" or row["cost_json"].get("usd") != 0:
            continue
        raw = connection.execute("SELECT * FROM learning_runs WHERE business_id=? AND id=?", (row["business_id"], identity)).fetchone()
        if raw is None:
            continue
        learning = decode_row(raw)
        result = learning["result_json"]
        amount = result.get("usd")
        if (type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0 or result.get("errors")
                or result.get("start") != row["started_at"] or result.get("end") != row["finished_at"]):
            continue
        after = {**row["cost_json"], "usd": round(amount, 8)}
        for key in ("usage", "model"):
            if key in result:
                after[key] = result[key]
        changes.append({"routine_run_id": row["id"], "business_id": row["business_id"], "learning_run_id": identity,
            "routine_hash": canonical_hash(row), "learning_hash": canonical_hash(learning),
            "before": row["cost_json"], "after": after})
    return {"version": 1, "installation_id": installation[0], "changes": changes}


def preview(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return _plan(connection)
    finally:
        connection.close()


def apply(database, plan):
    with database.transaction() as uow:
        gate = uow.repository("maintenance_state").get("service")
        if not gate or gate["state"] != "closed":
            raise DomainError("maintenance_required", "Close maintenance and stop the service before applying this reviewed repair")
        if uow.connection.execute("SELECT 1 FROM jobs WHERE state IN ('running','applying','cancel_requested') LIMIT 1").fetchone():
            raise DomainError("busy", "An execution still needs reconciliation before maintenance")
        if canonical_hash(_plan(uow.connection)) != canonical_hash(plan):
            raise DomainError("revision_conflict", "Repair sources changed; generate and review a fresh preview")
        for change in plan["changes"]:
            repo = uow.repository("routine_runs", business_id=change["business_id"])
            row = repo.get(change["routine_run_id"], required=True)
            result = {**row["result_json"], "cost_reconciliation": {
                "reason": "Imported routine cost omitted the linked learning run's recorded usage",
                "at": utc_now(), "learning_run_id": change["learning_run_id"], "learning_hash": change["learning_hash"],
                "routine_hash": change["routine_hash"], "original_cost": change["before"]}}
            repo.update(row["id"], {"cost_json": change["after"], "result_json": result}, expected_revision=row["revision"])
    return {"repaired": len(plan["changes"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply-plan", type=Path, help="Apply this previously reviewed JSON preview during closed maintenance")
    args = parser.parse_args(argv)
    if args.apply_plan:
        result = apply(Database(args.database).open_existing(), json.loads(args.apply_plan.read_text()))
    else:
        result = preview(args.database)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
