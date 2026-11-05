from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.background.authority import BackgroundAuthority
from qorqut_operations.background.programs import Program, ProgramRegistry, ProgramResult
from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.service import BackgroundService
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, canonical_hash, new_id
from bazarlyk.domains.common import schema
from qorqut_operations.routines import register_operations
from bazarlyk.domains.tasks.operations import TaskOperations
from bazarlyk.policy import PolicyResolver
from qorqut_operations.signoffs import SignoffService
from foundation_support import BUSINESS, HUMAN, OTHER, context, database_at


@pytest.fixture
def jobs(tmp_path):
    db = database_at(tmp_path)
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
    registry = builtin_registry()
    programs = ProgramRegistry()
    calls = []
    def compute(control, args):
        calls.append(args)
        return ProgramResult(outputs=({"operation": "tasks_create", "arguments": {"title": args["title"]}},))
    programs.register(Program("test.task", 1, schema({"title": {"type": "string"}}, ["title"]), compute,
        permissions={"tasks_create": "allow"}, outputs=("tasks_create",), resource_types=("tasks", "business_configs")))
    authority = BackgroundAuthority(db, programs)
    with db.transaction() as uow:
        authority.register_workers(uow, [BUSINESS, OTHER])
    policy = PolicyResolver(registry, worker_profiles=authority.worker_profiles,
        job_consequence_verifier=authority.verify_consequence, job_plan_validator=authority.validate_plan)
    app = Application(db, registry, policy=policy, context_resolver=policy.refresh_context)
    TaskOperations().register(registry, app)
    routines = register_operations(app, programs, authority)
    signoffs = SignoffService(app).register_operations()
    service = BackgroundService(app, programs, authority)
    service.scheduler = RoutineScheduler(routines)
    return db, app, programs, authority, routines, signoffs, service, calls


def create_routine(jobs, *, scope="business", status="active", schedule="0 9 * * *", target=None, zone="UTC"):
    _, app, _, _, _, _, _, _ = jobs
    ctx = replace(context(), allowed_business_ids=frozenset({BUSINESS, OTHER}), global_capabilities=frozenset({"manage_global_routines"}))
    definition = {"name": "Registered work", "schedule": schedule, "timezone": zone, "status": status,
        "program": "test.task", "arguments": {"title": "Exact job output"}}
    if target:
        definition["target"] = target
    result = app.invoke(ctx, "routines_create", {"scope": scope, "definition": definition}, {"idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    return ctx, result.data["records"][0]


def test_manual_job_uses_worker_and_applies_once(jobs):
    db, app, _, authority, _, _, service, calls = jobs
    ctx, routine = create_routine(jobs, status="paused")
    result = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    job_id = result.data["job_ids"][0]
    with db.read() as uow:
        row = uow.repository("jobs", business_id=BUSINESS).get(job_id)
        assert row["principal_id"] == authority.worker_id("test.task", uow)
        assert row["initiated_by_principal_id"] == HUMAN
    assert service.run_one(job_id)["state"] == "succeeded"
    assert service.run_one(job_id) is None
    with db.read() as uow:
        assert len(uow.repository("tasks", business_id=BUSINESS).list()) == 1
        assert uow.repository("routine_runs", business_id=BUSINESS).list()[0]["state"] == "succeeded"
    assert len(calls) == 1


def test_nightly_worker_creates_real_generation_without_domain_outputs(tmp_path, monkeypatch):
    import json
    import subprocess
    from bazarlyk.background.integration import register_programs
    from bazarlyk.storage.versioning import NightlyMaintenance
    from test_storage_maintenance import configured

    root, db, app, document = configured(tmp_path)
    document_path = root / "first/hq" / document["path"]
    original_document = document_path.read_bytes()
    maintenance = NightlyMaintenance(db, root, git_enabled=False)
    programs = register_programs(ProgramRegistry(), maintenance=maintenance)
    authority = BackgroundAuthority(db, programs)
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})
        authority.register_workers(uow, [BUSINESS, OTHER])
    policy = PolicyResolver(app.registry, worker_profiles=authority.worker_profiles,
        job_consequence_verifier=authority.verify_consequence, job_plan_validator=authority.validate_plan)
    app.policy, app.context_resolver = policy, policy.refresh_context
    service = BackgroundService(app, programs, authority)

    def refused(*args, **kwargs):
        raise AssertionError("Git-disabled maintenance must not launch an executable")

    monkeypatch.setattr(subprocess, "Popen", refused)
    with db.transaction() as uow:
        owner = replace(context(), business_id=None, review_business_id=BUSINESS)
        job = uow.repository("jobs", business_id=None).insert(programs.job_values(owner, "maintenance.nightly", {}, uow))
    outcome = service.run_one(job["id"])
    assert outcome["state"] == "succeeded", outcome
    with db.read() as uow:
        stored = uow.repository("jobs", business_id=None).get(job["id"], required=True)
    assert stored["business_id"] is None and stored["state"] == "succeeded"
    assert stored["result_json"]["outputs"] == []
    assert stored["result_json"]["data"]["generation"] == job["id"]
    assert stored["result_json"]["data"]["versioning"] == {"enabled": False}
    metadata = maintenance.backups.verify(job["id"])
    assert metadata["generation_id"] == job["id"] and metadata["state"] == "complete"
    assert metadata["database"]["checksum"] == stored["result_json"]["data"]["checksum"]
    exported = json.loads((root / "first/hq/exports/tasks.json").read_text())
    assert exported["records"][0]["title"] == "Retain customer evidence"
    assert document_path.read_bytes() == original_document
    assert service.run_one(job["id"]) is None
    assert maintenance.backups.verify(job["id"]) == metadata
    with db.read() as uow:
        assert uow.repository("routine_runs", business_id=None).list() == []
        assert uow.repository("signoffs", business_id=None).list() == []


def test_frozen_outputs_reject_different_arguments_and_business(jobs):
    db, app, programs, authority, _, _, service, _ = jobs
    with db.transaction() as uow:
        values = programs.job_values(context(), "test.task", {"title": "Allowed"}, uow)
        outputs = [{"operation": "tasks_create", "arguments": {"title": "Allowed"}}]
        row = uow.repository("jobs", business_id=BUSINESS).insert({**values, "state": "applying",
            "result_json": {"outputs": outputs, "outputs_hash": canonical_hash(outputs), "applied": {}}})
    with db.read() as uow:
        worker = service.context_for(row, uow)
    altered = app.invoke(worker, "tasks_create", {"title": "Injected"}, {"idempotency_key": new_id()})
    assert altered.status == "error" and altered.error["code"] == "invalid_plan", altered.to_dict()
    other = app.invoke(replace(worker, business_id=OTHER), "tasks_create", {"title": "Allowed"}, {"idempotency_key": new_id()})
    assert other.status == "error"


def test_recovery_never_recomputes_frozen_outputs(jobs):
    db, _, programs, _, _, _, service, calls = jobs
    with db.transaction() as uow:
        values = programs.job_values(context(), "test.task", {"title": "Before"}, uow)
        outputs = [{"operation": "tasks_create", "arguments": {"title": "Frozen"}}]
        row = uow.repository("jobs", business_id=BUSINESS).insert({**values, "state": "applying",
            "result_json": {"outputs": outputs, "outputs_hash": canonical_hash(outputs), "applied": {}}})
    assert service.run_one(row["id"])["state"] == "succeeded"
    assert not calls
    with db.read() as uow:
        assert uow.repository("tasks", business_id=BUSINESS).list()[0]["title"] == "Frozen"


def test_job_completion_publishes_the_coarse_jobs_routines_kind(jobs):
    """jobs/routine_runs have no capture trigger and BackgroundService writes
    their state transitions outside Application.invoke
    (bazarlyk/background/service.py), so without _run_one_admitted's own
    ambient operation tag the change feed (bazarlyk/service/changes.py)
    would never learn a job actually finished; PR3's /live and
    /context+processes routes depend on exactly this.
    """
    from bazarlyk.service.changes import ChangeFeed
    db, app, _, _, _, _, service, _ = jobs
    changes = ChangeFeed()
    db.change_feed = changes
    ctx, routine = create_routine(jobs, status="paused")
    result = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    job_id = result.data["job_ids"][0]
    # Global, not BUSINESS: bazarlyk/service/changes.py bumps a coarse kind
    # at global scope only (see its own comment) since no reliable
    # per-business signal is available here -- safe to over-bump, so every
    # business's dependency on "jobs_routines" sees this too.
    before = changes.version("jobs_routines", "global")
    assert service.run_one(job_id)["state"] == "succeeded"
    assert changes.version("jobs_routines", "global") > before


def test_idle_job_admission_publishes_nothing(jobs):
    """Review fix 1: `_run_admitted_job`'s own transaction (tagged ambiently
    as `jobs_run` by `observe`, see the sibling test above) does only a
    SELECT when nothing is queued. Before the fix, the coarse kind published
    on the operation name alone regardless of whether anything was found --
    every job-admission poll would bump it, forever, even fully idle.
    """
    from bazarlyk.service.changes import ChangeFeed
    db, app, _, _, _, _, service, _ = jobs
    changes = ChangeFeed()
    db.change_feed = changes
    assert service.run_one() is None
    assert changes.replay_since(0) == []


def test_routine_scheduler_tick_publishes_the_coarse_jobs_routines_kind(jobs):
    from bazarlyk.service.changes import ChangeFeed
    db, app, _, _, _, _, service, _ = jobs
    changes = ChangeFeed()
    db.change_feed = changes
    ctx, routine = create_routine(jobs)
    first = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    service.scheduler.tick(first)
    manual = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    service.run_one(manual.data["job_ids"][0])
    before = changes.version("jobs_routines", "global")  # global-only; see the sibling test above.
    later = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
    scheduled = service.scheduler.tick(later)
    assert len(scheduled) == 1
    assert changes.version("jobs_routines", "global") > before


def test_idle_routine_scheduler_tick_publishes_nothing(jobs):
    """Review fix 1: `tick()` always opens a `jobs_routines_tick` coarse-
    tagged transaction (see routines.py's own comment on why), whether or
    not a routine is due -- with zero routines registered, its SELECT finds
    none and nothing is written. Before the fix, the coarse kind published on
    the operation name alone regardless, every 5 seconds, forever.
    """
    from bazarlyk.service.changes import ChangeFeed
    db, app, _, _, _, _, service, _ = jobs
    changes = ChangeFeed()
    db.change_feed = changes
    assert service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc)) == []
    assert changes.replay_since(0) == []


def test_first_seen_latest_slot_and_manual_do_not_advance_schedule(jobs):
    db, app, _, _, _, _, service, _ = jobs
    ctx, routine = create_routine(jobs)
    first = datetime(2026, 9, 8, 10, tzinfo=timezone.utc)
    assert service.scheduler.tick(first) == []
    manual = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    service.run_one(manual.data["job_ids"][0])
    assert service.scheduler.tick(first) == []
    later = datetime(2026, 9, 11, 10, tzinfo=timezone.utc)
    scheduled = service.scheduler.tick(later)
    assert len(scheduled) == 1
    assert service.scheduler.tick(later) == []
    with db.read() as uow:
        row = uow.repository("jobs", business_id=BUSINESS).get(scheduled[0])
        assert row["definition_ref_json"]["missed"] == 2
        assert row["initiated_by_principal_id"] is None


@pytest.mark.parametrize("progress", ["unavailable", "recovery_required"])
def test_imported_missing_progress_blocks_schedule_without_blocking_manual_run(jobs, progress):
    db, app, _, _, _, _, service, calls = jobs
    ctx, routine = create_routine(jobs)
    with db.transaction() as uow:
        repo = uow.repository("routines", business_id=BUSINESS)
        key = {"namespace": BUSINESS, "id": routine["id"]}
        row = repo.get(key)
        repo.update(key, {"metadata_json": {**row["metadata_json"], "legacy_progress": progress}}, expected_revision=1)
    assert service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc)) == []
    assert service.scheduler.errors[0]["routine_id"] == routine["id"]
    with db.read() as uow:
        assert uow.repository("routine_slots").list() == []
        assert uow.repository("jobs", business_id=BUSINESS).list() == []
    result = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 2, "idempotency_key": new_id()})
    assert result.status == "ok", result.to_dict()
    assert service.run_one(result.data["job_ids"][0])["state"] == "succeeded"
    assert len(calls) == 1
    with db.read() as uow:
        assert uow.repository("routine_slots").list() == []


def test_each_business_fanout_remains_scoped(jobs):
    db, _, _, _, _, _, service, _ = jobs
    create_routine(jobs, scope="global", target="each-business")
    assert service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc)) == []
    ids = service.scheduler.tick(datetime(2026, 9, 9, 10, tzinfo=timezone.utc))
    assert len(ids) == 2
    for identity in ids:
        assert service.run_one(identity)["state"] == "succeeded"
    with db.read() as uow:
        assert len(uow.repository("tasks", business_id=BUSINESS).list()) == 1
        assert len(uow.repository("tasks", business_id=OTHER).list()) == 1


def test_repeated_wall_hour_is_not_fired_twice(jobs):
    _, _, _, _, _, _, service, _ = jobs
    create_routine(jobs, schedule="30 1 * * *", zone="America/New_York")
    service.scheduler.tick(datetime(2026, 10, 31, 12, tzinfo=timezone.utc))
    ids = service.scheduler.tick(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    assert len(ids) == 1
    service.run_one(ids[0])
    assert service.scheduler.tick(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == []


def test_stop_cancels_queued_work_without_computation(jobs):
    db, app, _, _, _, _, service, calls = jobs
    ctx, routine = create_routine(jobs)
    result = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    job_id = result.data["job_ids"][0]
    stopped = app.invoke(ctx, "routines_stop", {"routine_id": routine["id"], "job_id": job_id},
        {"expected_revision": 1, "idempotency_key": new_id()})
    assert stopped.status == "ok", stopped.to_dict()
    service.run_one(job_id)
    assert not calls
    with db.read() as uow:
        assert uow.repository("jobs", business_id=BUSINESS).get(job_id)["state"] == "cancelled"


def test_declared_effect_survives_inactive_agent_but_discretionary_edit_does_not(jobs):
    db, app, programs, _, _, _, service, _ = jobs
    agent = new_id()
    with db.transaction() as uow:
        uow.repository("principals").insert({"id": agent, "kind": "agent", "display_name": "Initiator"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": agent})
        initiated = replace(context(), principal=Principal(agent, "agent"), source="mcp")
        row = uow.repository("jobs", business_id=BUSINESS).insert(programs.job_values(initiated, "test.task", {"title": "Receipt"}, uow))
        actor = uow.repository("principals").get(agent)
        uow.repository("principals").update(agent, {"active": False}, expected_revision=actor["revision"])
    assert service.run_one(row["id"])["state"] == "succeeded"
    current = programs.require("test.task")
    programs._programs["test.task"] = replace(current, version=2, discretionary=("tasks_create",))
    with db.transaction() as uow:
        row = uow.repository("jobs", business_id=BUSINESS).insert(programs.job_values(initiated, "test.task", {"title": "New discretion"}, uow))
    result = service.run_one(row["id"])
    assert result["error"]["code"] == "invalid_principal", result
    with db.read() as uow:
        assert [item["title"] for item in uow.repository("tasks", business_id=BUSINESS).list()] == ["Receipt"]


def test_service_stop_keeps_frozen_work_recoverable(jobs):
    db, _, programs, _, _, _, service, calls = jobs
    current = programs.require("test.task")
    def compute(control, args):
        calls.append(args)
        service._stop.set()
        return ProgramResult(outputs=({"operation": "tasks_create", "arguments": {"title": args["title"]}},))
    programs._programs["test.task"] = replace(current, run=compute)
    with db.transaction() as uow:
        row = uow.repository("jobs", business_id=BUSINESS).insert(programs.job_values(context(), "test.task", {"title": "After restart"}, uow))
    assert service.run_one(row["id"])["state"] == "paused_for_restart"
    with db.read() as uow:
        assert not uow.repository("tasks", business_id=BUSINESS).list()
        assert uow.repository("jobs", business_id=BUSINESS).get(row["id"])["state"] == "applying"
    service._stop.clear()
    assert service.run_one(row["id"])["state"] == "succeeded"
    assert len(calls) == 1


def test_old_activity_flag_does_not_defer_either_job(jobs):
    db, _, programs, _, _, _, service, _ = jobs
    with db.transaction() as uow:
        first = uow.repository("jobs", business_id=BUSINESS).insert(programs.job_values(context(), "test.task", {"title": "Deferred"}, uow,
            definition_extra={"skip_if_live": True}))
        second_context = replace(context(), business_id=OTHER, allowed_business_ids=frozenset({OTHER}))
        second = uow.repository("jobs", business_id=OTHER).insert(programs.job_values(second_context, "test.task", {"title": "Available"}, uow))
    service.is_live = lambda business: business == BUSINESS
    assert service.run_one()["job_id"] == first["id"]
    assert service.run_one()["job_id"] == second["id"]
    with db.read() as uow:
        assert uow.repository("jobs", business_id=BUSINESS).get(first["id"])["state"] == "succeeded"


def test_invalid_definition_does_not_stop_other_schedules(jobs):
    db, _, _, _, _, _, service, _ = jobs
    _, valid = create_routine(jobs)
    with db.transaction() as uow:
        values = {key: value for key, value in valid.items() if key not in {"created_at", "updated_at", "revision"}}
        values.update(id="unavailable-imported-routine", job_kind="not.installed")
        uow.repository("routines", business_id=BUSINESS).insert(values)
    assert service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc)) == []
    assert service.scheduler.errors[0]["routine_id"] == "unavailable-imported-routine"
    assert len(service.scheduler.tick(datetime(2026, 9, 9, 10, tzinfo=timezone.utc))) == 1


def test_cost_history_retains_known_usage_and_definition(jobs):
    db, app, programs, _, _, _, service, _ = jobs
    current = programs.require("test.task")
    def compute(control, args):
        control.record_usage("deepseek-v4-flash", {"hit": 1, "miss": 2, "out": 3}, 0.125)
        return ProgramResult(data={"computed": True})
    programs._programs["test.task"] = replace(current, run=compute)
    ctx, routine = create_routine(jobs)
    queued = app.invoke(ctx, "routines_run", {"routine_id": routine["id"]}, {"expected_revision": 1, "idempotency_key": new_id()})
    assert service.run_one(queued.data["job_ids"][0])["state"] == "succeeded"
    result = app.invoke(ctx, "routines_costs", {"routine_id": routine["id"]})
    assert result.data["usd"] == 0.125 and result.data["runs"] == 1
    with db.read() as uow:
        record = uow.repository("routine_runs", business_id=BUSINESS).list()[0]
        assert record["result_json"]["definition"]["program"] == "test.task"
        assert record["cost_json"]["calls"][0]["usage"]["out"] == 3


def test_imported_unfinished_fanout_finishes_old_slot_before_new_slot(jobs):
    db, _, _, _, _, _, service, _ = jobs
    _, routine = create_routine(jobs, scope="global", target="each-business")
    service.scheduler.tick(datetime(2026, 9, 8, 10, tzinfo=timezone.utc))
    with db.transaction() as uow:
        repo = uow.repository("routine_slots")
        for business in (BUSINESS, OTHER):
            repo.update({"namespace": "global", "routine_id": routine["id"], "target_namespace": business},
                {"slot": "2026-09-09T09:00:00Z", "wall_slot": "2026-09-09T09:00", "state_json": {"done": business == BUSINESS}},
                expected_revision=1)
    rest = service.scheduler.tick(datetime(2026, 9, 10, 10, tzinfo=timezone.utc))
    assert len(rest) == 1
    with db.read() as uow:
        run = next(row for row in uow.repository("routine_runs", business_id=OTHER).list() if row["result_json"]["job_id"] == rest[0])
        assert run["slot"] == "2026-09-09T09:00:00Z"
    service.run_one(rest[0])
    assert len(service.scheduler.tick(datetime(2026, 9, 10, 10, tzinfo=timezone.utc))) == 2
