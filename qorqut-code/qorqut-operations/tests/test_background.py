"""Registered programs, routines and the serial job service on the extracted engine, with synthetic programs only."""

from datetime import datetime, timezone
from dataclasses import replace
from uuid import UUID, uuid5

import pytest

from qorqut_operations.background import BackgroundAuthority, Program, ProgramRegistry, RoutineError, next_slot, parse_cron, prev_slot
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.storage.executor import application_claim
from qorqut_operations.storage.repositories import decode_row
from background_support import REPORT_PERMISSIONS, BackgroundHost, Programs
from signoff_support import INSTALLATION
from support import BUSINESS, HUMAN, Demo, database_at

UTC = timezone.utc


@pytest.fixture
def host(tmp_path):
    return BackgroundHost(tmp_path)


def utc(*parts):
    return datetime(*parts, tzinfo=UTC)


def drain(host, limit=10):
    states = []
    for _ in range(limit):
        outcome = host.service.run_one()
        if outcome is None:
            break
        states.append(outcome)
    return states


# -- program definitions and the module move: the digest is a function of the definition only ----------------------------

def test_definition_digests_are_unchanged_by_the_module_move():
    # Golden values computed from the deployed v1 ``bazarlyk.background.programs.Program`` for these definitions.
    full = Program("demo.report", 3, {"type": "object", "properties": {"count": {"type": "integer"},
        "label": {"type": "string"}}, "additionalProperties": False}, run=lambda control, args: None,
        permissions={"items_create": "allow"}, outputs=("items_create",), resource_types=("items",),
        discretionary=("items_touch",), global_capabilities=("manage_global_routines",), timeout_seconds=120,
        scope_kinds=("business", "global"))
    assert full.definition["digest"] == "7c69b0cfb7766190ec67617e02bee23ad15e71e6ab8b488184942862ad8f56d5"
    offline = Program("demo.unavailable", 1, {"type": "object"}, run=None, unavailable_reason="Not installed here")
    assert offline.definition["digest"] == "6efae4ce6d2637e880b7f6a264a36ad97b4d6a8660feced14c099a85fb8430b6"
    external = Program("demo.external", 2, {"type": "object"}, run=lambda c, a: None, external=True,
        recover=lambda c, a: None, outputs=("items_create",), resource_types=("items",), timeout_seconds=900)
    assert external.definition["digest"] == "31983ab0ae8cc7e8736d8de9564154b62ac95737ed6f30481c817e6030c781a0"
    # The run callable is not part of the definition; a changed definition field changes the digest.
    other = Program("demo.report", 3, full.input_schema, run=None, unavailable_reason="x", permissions=full.permissions)
    assert other.definition["digest"] != full.definition["digest"]


def test_worker_identities_and_serial_claim_derive_from_the_installation(host):
    expected = str(uuid5(UUID(INSTALLATION), "program:demo.report"))
    assert expected == "e6b3baf9-b0f3-5e40-b4c4-3e2e1ccdb53e" and expected in host.workers
    assert host.service._serial_id() == "3bece067-dbe6-58ae-a1ac-7c432febd908"
    with host.database.read() as uow:
        worker = uow.repository("principals").get(expected)
        assert worker["kind"] == "system" and worker["display_name"] == "demo.report worker"
        # Programs without an available implementation still get a deterministic identity, never a running one.
        assert host.authority.worker_id("demo.offline", uow)
        profile = host.authority.worker_profiles(expected, uow)
        assert dict(profile.operations) == REPORT_PERMISSIONS and profile.business_ids == frozenset({BUSINESS})
        assert host.authority.worker_profiles(HUMAN, uow) is None
        with pytest.raises(DomainError, match="not installed"):
            host.authority.worker_id("demo.missing", uow)


def test_program_registry_is_frozen_unique_and_bound_once(tmp_path):
    programs = ProgramRegistry()
    program = Program("p.one", 1, {"type": "object"}, run=lambda c, a: None)
    programs.register(program)
    with pytest.raises(ValueError, match="unique"):
        programs.register(program)
    with pytest.raises(ValueError):
        programs.register(object())
    database = database_at(tmp_path, demo=Demo())
    authority = BackgroundAuthority(database, programs)
    with pytest.raises(ValueError, match="already bound"):
        BackgroundAuthority(database, programs)
    programs.bind_authority(authority)        # re-binding the same authority is harmless
    assert programs.freeze() is programs and programs.frozen
    with pytest.raises(ValueError, match="frozen"):
        programs.register(Program("p.two", 1, {"type": "object"}, run=lambda c, a: None))
    assert programs.get("p.one")["id"] == "p.one" and programs.get("missing") is None
    with pytest.raises(DomainError, match="not installed"):
        programs.require("missing")
    with pytest.raises(ValueError, match="explicit reason"):
        Program("p.bad", 1, {"type": "object"}, run=None)
    with pytest.raises(ValueError, match="recovery"):
        Program("p.ext", 1, {"type": "object"}, run=lambda c, a: None, external=True)


def test_job_preparation_checks_scope_arguments_and_worker_registration(host):
    ctx = host.owner()
    with host.database.read() as uow:
        values = host.programs.job_values(ctx, "demo.report", {"label": "x"}, uow)
        assert values["principal_id"] == host.workers[0] and values["target_namespace"] == "business:" + BUSINESS
        assert values["initiated_by_principal_id"] == HUMAN and values["state"] == "queued"
        assert values["definition_ref_json"]["digest"] == host.programs.get("demo.report")["digest"]
        with pytest.raises(DomainError, match="label"):
            host.programs.job_values(ctx, "demo.report", {}, uow)
        with pytest.raises(DomainError, match="Provider is not installed"):
            host.programs.job_values(ctx, "demo.offline", {"label": "x"}, uow)
        global_ctx = InvocationContext(Principal(HUMAN, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
        with pytest.raises(DomainError, match="target scope"):
            host.programs.job_values(global_ctx, "demo.report", {"label": "x"}, uow)
        with pytest.raises(DomainError, match="replace a registered definition"):
            host.programs.job_values(ctx, "demo.report", {"label": "x"}, uow, definition_extra={"digest": "0"})


# -- 3. scheduling: first-seen, the repeated wall hour and manual-run independence -------------------------------------------

def slot_rows(host):
    with host.database.read() as uow:
        return [decode_row(row) for row in uow.connection.execute("SELECT * FROM routine_slots")]


def test_schedule_first_seen_then_exactly_one_run_for_a_repeated_wall_time(host):
    routine = host.routine(schedule="30 1 * * *", tz="America/New_York")
    # First sight establishes the cursor; it never back-fills a run for an earlier slot.
    assert host.scheduler.tick(utc(2026, 11, 1, 4, 0)) == []
    assert host.jobs() == [] and slot_rows(host)[0]["state_json"] == {"first_seen": True, "done": True}

    # 01:31 EDT on the night the clocks fall back: the first 01:30 fires once.
    queued = host.scheduler.tick(utc(2026, 11, 1, 5, 31))
    assert len(queued) == 1
    job = host.job(queued[0])
    assert job["definition_ref_json"]["trigger"] == "schedule" and job["state"] == "queued"
    assert job["principal_id"] == host.workers[0] and job["initiated_by_principal_id"] is None   # the scheduler is no person
    assert [state["state"] for state in drain(host)] == ["succeeded"]
    assert [item["id"] for item in host.items()] == ["report-daily"]
    assert host.items()[0]["created_by"] == host.workers[0]     # authority-bound: the program's worker principal
    runs = host_runs(host)
    assert len(runs) == 1 and runs[0]["trigger"] == "schedule" and runs[0]["state"] == "succeeded"

    # The repeated 01:30 (EST, the second pass of the same wall time) is suppressed, even though a new UTC slot exists.
    assert host.scheduler.tick(utc(2026, 11, 1, 6, 31)) == []
    assert len(host.jobs()) == 1
    # The next day's slot fires again, with no missed runs counted for the repeated hour.
    queued = host.scheduler.tick(utc(2026, 11, 2, 6, 31))
    assert len(queued) == 1 and host.job(queued[0])["definition_ref_json"]["missed"] == 0
    assert routine["id"] and len(slot_rows(host)) == 1


def host_runs(host):
    with host.database.read() as uow:
        return [decode_row(row) for row in uow.connection.execute("SELECT * FROM routine_runs ORDER BY created_at, rowid")]


def test_schedule_does_not_start_a_second_run_while_the_previous_slot_is_pending(host):
    host.routine(schedule="*/5 * * * *", tz="UTC")
    assert host.scheduler.tick(utc(2026, 10, 12, 12, 1)) == []
    first = host.scheduler.tick(utc(2026, 10, 12, 12, 6))
    assert len(first) == 1
    # The queued job has not run, so later slots wait instead of piling up.
    assert host.scheduler.tick(utc(2026, 10, 12, 12, 16)) == []
    assert len(host.jobs()) == 1
    assert [state["state"] for state in drain(host)] == ["succeeded"]
    resumed = host.scheduler.tick(utc(2026, 10, 12, 12, 17))
    assert len(resumed) == 1
    assert host.job(resumed[0])["definition_ref_json"]["missed"] == 1     # 12:10 was skipped; 12:15 is the slot that fires


def test_a_manual_run_is_independent_of_scheduling_and_goes_first(host):
    scheduled = host.routine(name="Scheduled", schedule="*/10 * * * *", tz="UTC", label="sched")
    manual = host.routine(name="Manual", schedule="0 3 * * *", tz="UTC", label="hand")
    assert host.scheduler.tick(utc(2026, 10, 12, 12, 1)) == []
    before = slot_rows(host)
    scheduled_jobs = host.scheduler.tick(utc(2026, 10, 12, 12, 11))
    assert len(scheduled_jobs) == 1
    manual_job = host.run_manual(manual)
    assert host.job(manual_job)["initiated_by_principal_id"] == HUMAN
    # A manual run neither creates nor advances a slot cursor, and runs ahead of older scheduled work.
    cursors = {row["routine_id"]: row for row in slot_rows(host)}
    assert set(cursors) == {scheduled["id"], manual["id"]} and cursors[manual["id"]]["state_json"] == {"first_seen": True, "done": True}
    assert before and host.job(manual_job)["definition_ref_json"]["trigger"] == "manual"
    first = host.service.run_one()
    assert first["job_id"] == manual_job and first["state"] == "succeeded"
    assert host.job(manual_job)["state"] == "succeeded" and host.job(scheduled_jobs[0])["state"] == "queued"
    assert host.service.run_one()["job_id"] == scheduled_jobs[0]
    assert sorted(item["id"] for item in host.items()) == ["report-hand", "report-sched"]
    # A routine already queued or running refuses a second manual run instead of doubling the work.
    again = host.run_manual(manual)
    refused = host.invoke("routines_run", {"routine_id": manual["id"]}, expected_revision=manual["revision"])
    assert refused.status == "error" and refused.error["code"] == "busy" and again


def test_unavailable_or_invalid_programs_cannot_be_scheduled_active(host):
    refused = host.invoke("routines_create", {"definition": {"name": "Off", "schedule": "0 3 * * *", "timezone": "UTC",
        "status": "active", "program": "demo.offline", "arguments": {"label": "x"}}})
    assert refused.error["code"] == "operation_unavailable"
    paused = host.routine("demo.offline", status="paused")
    assert paused["status"] == "paused"
    bad_cron = host.invoke("routines_create", {"definition": {"name": "Bad", "schedule": "61 * * * *", "timezone": "UTC",
        "status": "active", "program": "demo.report", "arguments": {"label": "x"}}})
    assert bad_cron.error["code"] == "validation_error"
    bad_zone = host.invoke("routines_create", {"definition": {"name": "Bad", "schedule": "0 3 * * *", "timezone": "Mars/Base",
        "status": "active", "program": "demo.report", "arguments": {"label": "x"}}})
    assert bad_zone.error["code"] == "validation_error"
    unknown = host.invoke("routines_create", {"definition": {"name": "Bad", "schedule": "0 3 * * *", "timezone": "UTC",
        "status": "active", "program": "demo.nothing", "arguments": {"label": "x"}}})
    assert unknown.error["code"] == "operation_unavailable"


def test_a_routine_has_no_department_grouping_in_its_definition_row_read_or_run_record(host):
    refused = host.invoke("routines_create", {"definition": {"name": "Grouped", "schedule": "0 3 * * *", "timezone": "UTC",
        "status": "active", "program": "demo.report", "arguments": {"label": "x"}, "departments": ["operations"]}})
    assert refused.status == "error" and refused.error["code"] == "validation_error"
    routine = host.routine(name="Plain", schedule="0 3 * * *", tz="UTC", label="plain")
    assert "departments" not in routine and "departments_json" not in routine
    read = host.invoke("routines_get", {"routine_id": routine["id"]}).data["routine"]
    assert "departments" not in read and "departments_json" not in read
    host.run_manual(routine)
    assert "departments" not in host_runs(host)[0]["result_json"]["definition"]
    with host.database.read() as uow:
        assert "departments_json" not in {column[1] for column in uow.connection.execute("PRAGMA table_info(routines)")}


def test_a_program_that_is_not_routine_allowed_cannot_be_scheduled_but_keeps_its_digest(host):
    for status in ("active", "paused"):
        refused = host.invoke("routines_create", {"definition": {"name": "Direct", "schedule": "0 3 * * *", "timezone": "UTC",
            "status": status, "program": "demo.direct", "arguments": {"label": "x"}}})
        assert refused.status == "error" and refused.error["code"] == "forbidden"
    plain = Program("demo.same", 1, {"type": "object"}, run=lambda c, a: None)
    direct = Program("demo.same", 1, {"type": "object"}, run=lambda c, a: None, routine_allowed=False)
    assert plain.definition["digest"] == direct.definition["digest"]
    assert host.routine("demo.report")["status"] == "active"


def test_cron_dst_helpers_keep_v1_wall_time_semantics():
    ny = "America/New_York"
    # The nonexistent 02:30 never matches on the night the clocks spring forward.
    assert next_slot("30 2 * * *", utc(2026, 3, 8, 6, 0), ny) == utc(2026, 3, 9, 6, 30)
    # The repeated 01:30 is visible twice in UTC order; both fold values are returned.
    first = prev_slot("30 1 * * *", utc(2026, 11, 1, 5, 40), ny)
    second = prev_slot("30 1 * * *", utc(2026, 11, 1, 6, 40), ny)
    assert (first, second) == (utc(2026, 11, 1, 5, 30), utc(2026, 11, 1, 6, 30))
    with pytest.raises(RoutineError):
        parse_cron("* * *")


# -- authority-bound outputs ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("program,code", [("demo.rogue", "invalid_result"), ("demo.overreach", "forbidden"),
                                          ("demo.ungranted", "policy_denied")])
def test_outputs_exceeding_the_declared_authority_fail_without_effect(host, program, code):
    routine = host.routine(program, schedule="0 3 * * *", tz="UTC")
    job_id = host.run_manual(routine)
    outcome = host.service.run_one(job_id)
    assert outcome["error"]["code"] == code, outcome
    job = host.job(job_id)
    assert job["state"] == "failed" and job["error_json"]["code"] == code
    assert host.items() == []
    run = host_runs(host)[0]
    assert run["state"] == "failed" and run["result_json"]["error"]["code"] == code
    # The failed slot is closed and the routine can be run again.
    assert host.jobs()[0]["id"] == job_id and host.service.run_one() is None


def test_a_frozen_output_is_applied_only_through_the_job_worker_identity(host):
    routine = host.routine(schedule="0 3 * * *", tz="UTC")
    job_id = host.run_manual(routine)
    assert host.service.run_one(job_id)["state"] == "succeeded"
    stored = host.job(job_id)
    assert stored["state"] == "succeeded" and stored["result_json"]["applied"]["0"]["status"] == "ok"
    assert stored["result_json"]["outputs_hash"] and stored["claimed_by"] is None
    # Even a context bound to this very job can only apply the exact frozen output: other arguments are refused.
    with host.database.read() as uow:
        context = host.policy.context_for(stored["principal_id"], business_id=BUSINESS, review_business_id=BUSINESS,
            initiated_by_principal_id=stored["initiated_by_principal_id"], job_id=job_id, uow=uow)
    other = host.app.invoke(context, "items_create", {"id": "not-the-frozen-output"}, {"idempotency_key": "tamper"})
    assert other.status == "error" and other.error["code"] == "invalid_plan"
    assert [item["id"] for item in host.items()] == ["report-daily"]
    # A job context for a job that does not exist or belongs to another worker is refused outright.
    forged = InvocationContext(Principal(host.workers[1], "system"), business_id=BUSINESS,
                               allowed_business_ids=frozenset({BUSINESS}), source="worker", job_id=job_id)
    refused = host.app.invoke(forged, "items_create", {"id": "report-daily"}, {"idempotency_key": "forged"})
    assert refused.status == "error" and refused.error["code"] in {"policy_denied", "forbidden"}


@pytest.mark.parametrize("declared,kind,business,allowed", [
    (True, "tags", None, True),
    (False, "tags", None, False),
    (True, "tags", "00000000-0000-4000-8000-000000000099", False),
    (True, "item_tags", BUSINESS, True),
    (True, "item_tags", None, False),
    (True, "item_tags", "00000000-0000-4000-8000-000000000099", False),
    (True, "items", None, False),
    (True, "items", "00000000-0000-4000-8000-000000000099", False),
])
def test_declared_shared_tags_do_not_expand_a_jobs_business_scope(tmp_path, monkeypatch, declared, kind, business, allowed):
    from qorqut_operations.contracts import ResourceRef

    registered = Programs.registered
    def programs(self):
        return tuple(replace(p, resource_types=("items", "item_tags", *(("tags",) if declared else ())))
                     if p.id == "demo.report" else p for p in registered(self))
    monkeypatch.setattr(Programs, "registered", programs)
    host = BackgroundHost(tmp_path)
    job_id = host.run_manual(host.routine())
    assert host.service.run_one(job_id)["state"] == "succeeded"
    stored = host.job(job_id)
    with host.database.read() as uow:
        context = host.policy.context_for(stored["principal_id"], business_id=BUSINESS, review_business_id=BUSINESS,
            initiated_by_principal_id=stored["initiated_by_principal_id"], job_id=job_id, uow=uow)
        descriptor = host.app.registry.get("items_create")
        targets = (ResourceRef(kind, "target", business),)
        if allowed:
            binding = host.authority.verify_consequence(context, descriptor, targets, uow)
            assert binding["definition_digest"] == stored["definition_ref_json"]["digest"]
        else:
            with pytest.raises(DomainError, match="declared resource kinds or project"):
                host.authority.verify_consequence(context, descriptor, targets, uow)


# -- ProgramPending, attempts and exact-once completion ---------------------------------------------------------------------

def test_a_pending_program_lets_another_job_run_and_then_completes_exactly_once(host):
    host.programs_impl.pending["slow"] = 1
    slow = host.run_manual(host.routine("demo.slow", name="Slow", schedule="0 3 * * *", tz="UTC", label="slow"))
    quick = host.run_manual(host.routine("demo.report", name="Quick", schedule="0 4 * * *", tz="UTC", label="quick"))
    first = host.service.run_one()
    assert first == {"job_id": slow, "state": "queued"}               # deferred, not failed
    deferred = host.job(slow)
    assert deferred["state"] == "queued" and deferred["attempts"] == 1 and deferred["next_retry_at"]
    assert deferred["claimed_by"] is None and deferred["lease_until"] is None
    second = host.service.run_one()                                   # the other job is not blocked behind it
    assert second["job_id"] == quick and second["state"] == "succeeded"
    assert host.service.run_one() is None                              # slow is not due yet
    assert [item["id"] for item in host.items()] == ["report-quick"]
    host.advance(seconds=121)
    third = host.service.run_one()
    assert third == {"job_id": slow, "state": "succeeded"}
    assert host.job(slow)["attempts"] == 2 and sorted(item["id"] for item in host.items()) == ["report-quick", "report-slow"]
    assert host.service.run_one() is None and host.programs_impl.calls.count(("slow", "slow")) == 2
    assert len(host.items()) == 2                                       # exactly one item per output


def test_an_interrupted_application_resumes_without_reapplying_finished_outputs(host):
    job_id = host.run_manual(host.routine("demo.many", schedule="0 3 * * *", tz="UTC", label="m"))
    real_invoke = host.app.invoke
    seen = []

    def stop_after_first(*args, **kwargs):
        result = real_invoke(*args, **kwargs)
        seen.append(result.status)
        if len(seen) == 1:
            host.service._stop.set()        # the service is asked to stop between two outputs
        return result
    host.app.invoke = stop_after_first
    outcome = host.service.run_one(job_id)
    host.app.invoke = real_invoke
    host.service._stop.clear()
    assert outcome == {"job_id": job_id, "state": "paused_for_restart"} and seen == ["ok"]
    paused = host.job(job_id)
    assert paused["state"] == "applying" and set(paused["result_json"]["applied"]) == {"0"} and paused["attempts"] == 1
    assert [item["id"] for item in host.items()] == ["report-m-a"]
    host.advance(seconds=5)
    assert host.service.run_one(job_id)["state"] == "succeeded"
    done = host.job(job_id)
    assert done["attempts"] == 1                                         # resuming frozen work is not a new attempt
    assert sorted(item["id"] for item in host.items()) == ["report-m-a", "report-m-b"]
    assert host.programs_impl.calls == [("many", "m")]                    # the program body ran once
    run = host_runs(host)[0]
    assert run["state"] == "succeeded" and run["finished_at"]


def test_a_serial_claim_held_elsewhere_defers_the_job_instead_of_failing_it(host):
    job_id = host.run_manual(host.routine(schedule="0 3 * * *", tz="UTC"))
    with application_claim(host.locks, host.service._serial_id()):
        assert host.service.run_one(job_id) is None
    assert host.job(job_id)["state"] == "queued"
    assert host.service.run_one(job_id)["state"] == "succeeded"


# -- the job observer reports the committed outcome ----------------------------------------------------------------------

class Recorder:
    """Records each notification with the job exactly as a separate read sees it at that moment."""

    def __init__(self, host, fail=False):
        self.host, self.fail, self.seen = host, fail, []

    def job_result(self, row, error=None):
        self.seen.append({"row": row, "error": error, "saved": self.host.job(row["id"])})
        if self.fail:
            raise RuntimeError("synthetic observer failure")


def observed(host, label, *, fail=False):
    host.service.job_observer = recorder = Recorder(host, fail=fail)
    job_id = host.run_manual(host.routine("demo.raises", schedule="0 3 * * *", tz="UTC", label=label))
    return recorder, job_id


@pytest.mark.parametrize("label", ["external_failed", "cancelled"])
def test_a_terminal_failure_is_observed_after_it_is_saved_with_the_saved_row(host, label):
    recorder, job_id = observed(host, label)
    assert host.service.run_one(job_id)["error"]["code"] == label
    (seen,) = recorder.seen
    state = "cancelled" if label == "cancelled" else "failed"
    assert seen["error"]["code"] == label
    assert seen["row"]["state"] == state and seen["row"]["error_json"]["code"] == label
    assert seen["row"]["claimed_by"] is None and seen["saved"]["state"] == state
    assert seen["row"]["attempts"] == 1


def test_an_unexpected_fault_is_observed_after_the_job_is_failed_and_still_propagates(host):
    recorder, job_id = observed(host, "boom")
    with pytest.raises(RuntimeError, match="synthetic program fault"):
        host.service.run_one(job_id)
    (seen,) = recorder.seen
    assert seen["error"] == {"code": "background_failed"}
    assert seen["row"]["state"] == "failed" and seen["saved"]["state"] == "failed"
    assert seen["row"]["error_json"]["code"] == "background_failed"


def test_a_completed_job_is_observed_with_its_saved_state_and_completion_error(host):
    host.service.job_observer = recorder = Recorder(host)
    done = host.run_manual(host.routine("demo.report", schedule="0 3 * * *", tz="UTC"))
    assert host.service.run_one(done)["state"] == "succeeded"
    failed = host.run_manual(host.routine("demo.incomplete", name="Incomplete", schedule="0 4 * * *", tz="UTC"))
    assert host.service.run_one(failed)["state"] == "failed"
    ok, bad = recorder.seen
    assert ok["error"] is None and ok["row"]["state"] == "succeeded" and ok["saved"]["state"] == "succeeded"
    assert bad["error"]["code"] == "synthetic_incomplete"
    assert bad["row"]["state"] == "failed" and bad["row"]["error_json"]["code"] == "synthetic_incomplete"


def test_observer_exceptions_change_nothing_about_the_saved_outcome_and_are_not_retried(host):
    recorder, job_id = observed(host, "external_failed", fail=True)
    assert host.service.run_one(job_id)["error"]["code"] == "external_failed"
    assert len(recorder.seen) == 1
    assert host.job(job_id)["state"] == "failed"
    assert host.service.run_one(job_id) is None and len(recorder.seen) == 1
    assert host.programs_impl.calls == [("raises", "external_failed")]


@pytest.mark.parametrize("label", ["busy", "service_stopping"])
def test_busy_and_stopping_stay_nonterminal_and_are_observed_before_deferral(host, label):
    recorder, job_id = observed(host, label)
    outcome = host.service.run_one(job_id)
    assert outcome is None or outcome["state"] == "paused_for_restart"
    (seen,) = recorder.seen
    assert seen["error"]["code"] == label
    assert seen["row"]["state"] == "running" and seen["saved"]["state"] == "running"
    job = host.job(job_id)
    assert job["state"] == "queued" and job["error_json"] is None
    assert host.service.last_error is None


def test_a_job_that_cannot_be_reread_is_not_reported_from_a_stale_snapshot(host):
    recorder, job_id = observed(host, "external_failed")
    host.service._committed = lambda row: None
    assert host.service.run_one(job_id)["error"]["code"] == "external_failed"
    assert recorder.seen == [] and host.job(job_id)["state"] == "failed"
