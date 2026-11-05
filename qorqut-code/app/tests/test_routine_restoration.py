from dataclasses import replace
from datetime import datetime, timezone

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.transport.routine_views import recent_runs, run_view
from foundation_support import BUSINESS, OTHER
from test_background_jobs import jobs, create_routine


def record(identity, business=BUSINESS, *, slot="2026-09-09T04:00:00Z", group=None, cost=.1):
    return {"id": identity, "namespace": "global", "routine_id": "evolution-extraction",
        "business_id": business, "trigger": "schedule" if slot else "manual", "slot": slot,
        "state": "succeeded", "started_at": "2026-09-09T04:00:01Z", "finished_at": "2026-09-09T04:00:03Z",
        "result_json": {"run_group_id": group, "definition": {"name": "Evolution extraction", "timezone": "America/New_York"},
            "data": {"processed": 1, "items": 2}, "job_id": new_id()},
        "cost_json": {"usd": cost, "calls": [{"model": "test-model", "usd": cost}]}}


def test_global_history_groups_all_targets_before_limit_and_keeps_manual_runs_separate():
    rows = [record("one"), record("two", OTHER, cost=.2),
        record("manual-a", slot=None, group="manual-1"), record("manual-b", OTHER, slot=None, group="manual-1"),
        record("manual-c", slot=None, group="manual-2")]
    views = recent_runs(rows, limit=20, businesses={BUSINESS: "ProjectA", OTHER: "ProjectB"})
    assert len(views) == 3
    scheduled = next(x for x in views if x["trigger"] == "schedule")
    assert scheduled["usd"] == .3
    assert {x["slug"] for x in scheduled["runs"]} == {"ProjectA", "ProjectB"}
    assert scheduled["name"] == "Evolution extraction"
    assert scheduled["zone"] == "America/New_York"
    assert len(recent_runs(rows[:2], limit=1)[0]["runs"]) == 2
    assert len(recent_runs([record("unknown-a", slot=None), record("unknown-b", OTHER, slot=None)])) == 2


def test_run_details_keep_structured_results_errors_and_known_usage():
    row = record("failed")
    row["state"] = "failed"
    row["result_json"]["error"] = {"code": "learning_extraction_failed", "message": "Provider unavailable"}
    view = run_view(row, businesses={BUSINESS: "ProjectA"})
    assert view["run"] == "failed"
    assert view["runs"][0]["model"] == "test-model"
    assert "Provider unavailable" in view["runs"][0]["stderr_tail"]
    assert '"processed": 1' in view["runs"][0]["stdout_tail"]
    assert view["usage_complete"] is True
    row["result_json"]["data"] = {"stdout": " M README.md\n", "returncode": 0}
    assert run_view(row)["runs"][0]["stdout_tail"] == " M README.md\n"
    assert run_view(row)["runs"][0]["rc"] == 0


def test_manual_global_runs_have_one_group_and_explicit_scope(jobs):
    db, app, _, _, _, _, service, _ = jobs
    ctx, routine = create_routine(jobs, scope="global", target="each-business")
    groups = []
    for _ in range(2):
        result = app.invoke(ctx, "routines_run", {"scope": "global", "routine_id": routine["id"]},
            {"expected_revision": 1, "idempotency_key": new_id()})
        assert result.status == "ok", result.to_dict()
        with db.read() as uow:
            values = [dict(uow.connection.execute("SELECT definition_ref_json FROM jobs WHERE id=?", (identity,)).fetchone())
                for identity in result.data["job_ids"]]
        import json
        definitions = [json.loads(x["definition_ref_json"]) for x in values]
        assert all(x["scope"] == "global" for x in definitions)
        assert len({x["run_group_id"] for x in definitions}) == 1
        groups.append(definitions[0]["run_group_id"])
        for identity in result.data["job_ids"]:
            assert service.run_one(identity)["state"] == "succeeded"
    assert groups[0] != groups[1]


def test_routine_status_does_not_inherit_other_program_errors_and_recovers_old_job_scope(jobs):
    db, app, programs, _, _, _, service, _ = jobs
    ctx, routine = create_routine(jobs, scope="global", target="each-business")
    saved = []
    with db.transaction() as uow:
        for business in (BUSINESS, OTHER):
            run_id = new_id()
            # Seed the actual pre-upgrade binding shape, without scope metadata.
            job = uow.repository("jobs", business_id=business).insert(programs.job_values(
                replace(ctx, business_id=business), "test.task", {"title": "Old queued run"}, uow,
                definition_extra={"routine_id": routine["id"], "routine_run_id": run_id, "skip_if_live": True}))
            uow.repository("routine_runs", business_id=business).insert({"id": run_id, "namespace": "global",
                "routine_id": routine["id"], "trigger": "manual", "state": "queued", "started_at": job["created_at"],
                "result_json": {"job_id": job["id"]}})
            saved.append(job)
    service.last_error = "forbidden"
    view = service.routine_status(ctx)
    assert view["error"] is None
    assert len(view["queued"]) == 2
    assert all(x["definition_ref_json"]["scope"] == "global" for x in view["queued"])
    with db.read() as uow:
        for job in saved:
            assert uow.repository("jobs", business_id=job["business_id"]).get(job["id"]) == job
    restricted = replace(ctx, global_capabilities=frozenset())
    assert service.routine_status(restricted)["queued"] == []
    service.scheduler.errors = [{"namespace": "global", "business_id": None, "routine_id": routine["id"], "error": "Bad schedule"}]
    assert service.routine_status(ctx)["invalid"]
    assert not service.routine_status(restricted)["invalid"]


def test_wait_option_is_rejected_and_old_queued_wait_flag_never_defers(jobs):
    db, app, programs, _, _, _, service, _ = jobs
    ctx, routine = create_routine(jobs)
    definition = {"name": "ProjectA extraction", "schedule": "0 0 * * *", "timezone": "America/New_York",
        "status": "active", "program": "test.task", "arguments": {"title": "Runs now"}, "skip_if_live": True}
    assert app.invoke(ctx, "routines_create", {"definition": definition}, {"idempotency_key": new_id()}).status == "error"
    with db.transaction() as uow:
        job = uow.repository("jobs", business_id=BUSINESS).insert(programs.job_values(ctx, "test.task", {"title": "Legacy queued work"}, uow,
            definition_extra={"skip_if_live": True}))
    service.is_live = lambda _: pytest.fail("Agent activity must never be consulted")
    service.scheduler.is_live = service.is_live
    assert service.run_one(job["id"])["state"] == "succeeded"
    service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc))
    assert len(service.scheduler.tick(datetime(2026, 9, 9, 10, tzinfo=timezone.utc))) == 1
