from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.domains.common import page
from qorqut_operations.routines import reads as routine_reads
from qorqut_operations.routines.operations import RoutineOperations
from bazarlyk.transport import routine_views
from bazarlyk.transport.routine_views import hourly_runs, recent_runs
from foundation_support import BUSINESS, OTHER, context, database_at


def history_fixture(tmp_path):
    db = database_at(tmp_path)
    now = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)
    old = (now - timedelta(days=4)).isoformat().replace("+00:00", "Z")
    at = now.isoformat().replace("+00:00", "Z")
    boundary = (now.replace(minute=0) - timedelta(hours=47)).isoformat()
    with db.transaction() as uow:
        uow.repository("routines", business_id=None).insert({"namespace": "global", "id": "sync", "name": "Sync",
            "scope": "global", "target": "each-business", "schedule": "0 * * * *", "timezone": "UTC",
            "status": "paused", "job_kind": "test", "arguments_json": {}, "timeout_seconds": 60})
        records = []
        for identity, business, group, state, start, finish in [
            ("z-old", BUSINESS, "old", "succeeded", old, old),
            ("y-boundary", BUSINESS, "boundary", "succeeded", boundary, boundary),
            ("a-done", BUSINESS, "done", "succeeded", old, at),
            ("b-hidden", OTHER, "done", "failed", at, at),
            ("c-failed", BUSINESS, "failed", "failed", at, at),
            ("d-root", None, "failed", "succeeded", at, at),
            ("e-waiting", BUSINESS, "waiting", "running", old, None),
            ("f-finished", None, "waiting", "succeeded", at, at),
        ]:
            records.append(uow.repository("routine_runs", business_id=business).insert({
                "id": identity, "namespace": "global", "routine_id": "sync", "trigger": "manual",
                "state": state, "started_at": start, "finished_at": finish,
                "created_at": start, "result_json": {"run_group_id": group, "data": {"stdout": "output" * 100}},
                "cost_json": {"usd": .1}}))
    return db, now, [row for row in records if row["business_id"] != OTHER]


def test_sql_history_matches_legacy_order_and_scoped_cursors(tmp_path):
    db, now, records = history_fixture(tmp_path)
    ops = RoutineOperations(None, {}, None, clock=lambda: now)
    args = {"scope": "global", "limit": 2}
    with db.read() as uow:
        seen = []
        while True:
            result = ops.history(context(), args, None, uow)
            expected, cursor = page(records, args, None, "routines_runs")
            assert [r["id"] for r in result["items"]] == [r["id"] for r in expected]
            assert result["next_cursor"] == cursor
            seen.extend(r["id"] for r in result["items"])
            if not cursor:
                break
            args["cursor"] = cursor
        assert len(seen) == len(set(seen)) == len(records)
        with pytest.raises(DomainError):
            ops.history(context(), {**args, "routine_id": "other"}, None, uow)


def test_recent_view_keeps_insertion_order_on_a_large_out_of_order_fixture(tmp_path):
    """Migration 075 indexes routine_runs(business_id,...)/(namespace,...)/(state,...).
    activity()'s per-record query has no ORDER BY of its own; before any index it
    happens to return rows in table-scan (rowid/insertion) order, which is what
    every MCP client sees today. An index can change that scan order, so the
    query pins it explicitly. Five hundred rows with a full-cycle permutation
    between insertion order and started_at makes sure a regression here -- an
    index-driven reordering by started_at, or by any other indexed column --
    cannot coincidentally look right.
    """
    db = database_at(tmp_path)
    now = datetime(2026, 9, 14, 12, 30, tzinfo=timezone.utc)
    namespace = BUSINESS  # the routines/routine_runs CHECK constraint requires namespace == business_id for scope='business'
    total = 500
    with db.transaction() as uow:
        uow.repository("routines", business_id=BUSINESS).insert({"namespace": namespace, "id": "many", "name": "Many",
            "scope": "business", "target": "business", "schedule": "0 * * * *", "timezone": "UTC",
            "status": "paused", "job_kind": "test", "arguments_json": {}, "timeout_seconds": 60})
        inserted_ids = []
        for i in range(total):
            # 131 and 500 are coprime, so this is a full-cycle permutation of
            # 0..499: no row's started_at rank matches its insertion rank.
            shuffled = (i * 131) % total
            identity = f"run-{i:04d}"
            started = (now - timedelta(minutes=shuffled)).isoformat().replace("+00:00", "Z")
            uow.repository("routine_runs", business_id=BUSINESS).insert({
                "id": identity, "namespace": namespace, "routine_id": "many", "trigger": "manual",
                "state": "succeeded", "started_at": started, "finished_at": started,
                "created_at": started, "result_json": {}, "cost_json": {}})
            inserted_ids.append(identity)
    ops = RoutineOperations(None, {}, None, clock=lambda: now)
    with db.read() as uow:
        result = ops.history(context(), {"view": "recent", "limit": total}, None, uow)
    assert [row["id"] for row in result["items"]] == inserted_ids
    assert "_scan_rowid" not in result["items"][0]


def test_projection_preserves_complete_fanouts_and_completion_boundaries(tmp_path):
    db, now, records = history_fixture(tmp_path)
    ops = RoutineOperations(None, {}, None, clock=lambda: now)
    with db.read() as uow:
        args = {"scope": "global", "view": "recent", "limit": 2}
        compact = ops.history(context(), args, None, uow)["items"]
        assert recent_runs(compact, limit=2) == recent_runs(records, limit=2)
        summaries = ops.history(context(), {"scope": "global", "view": "hourly"}, None, uow)["items"]
        assert all("result_json" not in row for row in summaries)
        assert hourly_runs(summaries, now=now) == hourly_runs(recent_runs(records, limit=None), now=now)
        assert ops.history(context(), {"view": "recent"}, None, uow)["items"] == []


def test_routine_grouping_matches_between_sql_and_python(tmp_path):
    """routines/reads.py::activity() groups routine_runs SQL-side (its own GROUP_KEY, evaluated in a CTE) while
    transport/routine_views.py::recent_runs() groups the same kind of rows Python-side over already-fetched dicts;
    reads.py's own module comment claims the two "match". Call both real functions on the same rows and compare
    their independently computed groupings, at the domain level: no MCP, policy or agents involved."""
    db = database_at(tmp_path)
    at, later = "2026-09-08T12:00:00Z", "2026-09-08T13:00:00Z"
    # After every run timestamp, so activity(view="hourly")'s 48h completion window covers all of them.
    now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    failed_run_id, fanout_run_ids, fanout_group_id = new_id(), [], new_id()
    with db.transaction() as uow:
        uow.repository("routines", business_id=BUSINESS).insert({"namespace": BUSINESS, "id": "digest", "name": "Business Digest",
            "scope": "business", "target": "business", "schedule": "0 * * * *", "timezone": "UTC", "status": "active",
            "job_kind": "repository.status", "arguments_json": {}, "timeout_seconds": 60})
        uow.repository("routine_runs", business_id=BUSINESS).insert({"id": new_id(), "namespace": BUSINESS, "routine_id": "digest",
            "trigger": "manual", "state": "succeeded", "started_at": at, "finished_at": at, "created_at": at,
            "result_json": {}, "cost_json": {}})
        uow.repository("routine_runs", business_id=BUSINESS).insert({"id": failed_run_id, "namespace": BUSINESS, "routine_id": "digest",
            "trigger": "scheduled", "state": "failed", "started_at": later, "finished_at": later, "created_at": later,
            "result_json": {"error": "Injected fixture failure"}, "cost_json": {}})
        uow.repository("routines", business_id=None).insert({"namespace": "global", "id": "sync-all", "name": "Sync Every Business",
            "scope": "global", "target": "each-business", "schedule": "0 0 * * *", "timezone": "UTC", "status": "active",
            "job_kind": "repository.status", "arguments_json": {}, "timeout_seconds": 60})
        for business_id in (BUSINESS, OTHER):
            fanout_run_ids.append(new_id())
            uow.repository("routine_runs", business_id=business_id).insert({"id": fanout_run_ids[-1], "namespace": "global",
                "routine_id": "sync-all", "trigger": "scheduled", "state": "succeeded", "started_at": at, "finished_at": at,
                "created_at": at, "result_json": {"run_group_id": fanout_group_id}, "cost_json": {}})
    business_ctx = context()
    global_ctx = replace(context(), business_id=None, allowed_business_ids=frozenset({BUSINESS, OTHER}))

    with db.read() as uow:
        business_recent = routine_reads.activity(uow, business_ctx, {"view": "recent", "limit": 1000}, now)["items"]
        global_recent = routine_reads.activity(uow, global_ctx, {"view": "recent", "scope": "global", "limit": 1000}, now)["items"]
        business_hourly = routine_reads.activity(uow, business_ctx, {"view": "hourly"}, now)["items"]
        global_hourly = routine_reads.activity(uow, global_ctx, {"view": "hourly", "scope": "global"}, now)["items"]

    # SQL-side ground truth: activity(view="hourly") GROUP BYs GROUP_KEY and returns one pre-aggregated row per group
    # (namespace, routine_id, MIN(started_at), MAX(COALESCE(finished_at,started_at)) and a CASE state rollup) and never
    # exposes a group id, so compare the aggregated content both sides compute independently: which routine, which
    # scope, and the rolled-up start/end/state (the field names differ, the values are directly comparable).
    def sql_group_key(row):
        return ("global" if row["namespace"] == "global" else "biz", row["routine_id"], row["start"], row["end"], row["state"])
    sql_groups = {sql_group_key(row) for row in business_hourly} | {sql_group_key(row) for row in global_hourly}
    assert len(business_hourly) == 2 and len(global_hourly) == 1, (business_hourly, global_hourly)

    # Python-side: feed activity(view="recent")'s flat member rows (the business routine's 2 runs and the global fanout
    # group's 2 runs spanning both businesses) into routine_views.recent_runs(), which groups them by its own key logic.
    python_groups = routine_views.recent_runs(business_recent + global_recent, limit=1000)
    python_group_tuples = {(item["scope"], item["routine"], item["start"], item["end"], item["state"]) for item in python_groups}
    assert python_group_tuples == sql_groups, (python_group_tuples, sql_groups)

    # Not just the same group identities: the same membership. The business routine's succeeded and failed runs stay
    # their own singleton groups (business-scoped groups are always GROUP_KEY=json_array(id)); the fanout group keeps
    # both cross-business runs together.
    membership = {frozenset(member["id"] for member in item["runs"]) for item in python_groups}
    assert sorted(len(group) for group in membership) == [1, 1, 2]
    assert frozenset(fanout_run_ids) in membership
    assert frozenset({failed_run_id}) in membership
