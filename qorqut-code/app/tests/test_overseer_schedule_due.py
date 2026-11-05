"""A018 scheduled extraction: real scheduler, program output, due records, inbox and admission.

Synthetic only: a temporary instance, a synthetic registered Learning Applier/Overseer, the real routine scheduler,
background service and program registry, the real host contribution and inbox, and the real portable module. No native
product session, provider call, live data or deployment is used.
"""
from datetime import datetime, timedelta, timezone

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id

from bazarlyk.overseer.due import coalescing_key, reconcile, request_visible
from bazarlyk.overseer.invocation import start_from_owner
from bazarlyk.background.integration import attach_background
from bazarlyk.native_chat import store
from host_foundation_support import create_registered_global, grant_capabilities, owner_global_context
from native_chat_support import Native
from test_agentic_access import registration

UTC = timezone.utc


def conversation(native):
    with native.runtime.database.read() as uow:
        return store.live_session(uow.connection, native.agent)["conversation_id"]


def schedule(native):
    """One Global/root routine of the bounded scheduled program, created through the real operation."""
    definition = {"name": "Daily Overseer extraction", "schedule": "0 9 * * *", "timezone": "UTC",
                  "status": "active", "program": "overseer.extraction_due", "target": "root",
                  "arguments": {"agent_id": native.agent, "conversation_id": conversation(native)}}
    created = native.runtime.application.invoke(owner_global_context(native.instance), "routines_create",
        {"scope": "global", "definition": definition}, InvocationOptions(idempotency_key="schedule-due"))
    assert created.status == "ok", created.to_dict()
    return created.data["records"][0]


@pytest.fixture
def overseer(tmp_path):
    native = Native(tmp_path, "claude", role="learning-applier")
    native.publish()
    native.pin()
    grant_capabilities(native.instance, native.agent, "manage_agentic_layer")
    background = attach_background(native.runtime, native.runtime.programs)
    native.runtime.freeze_registry()
    with native.runtime.database.transaction() as uow:
        native.runtime.authority.register_workers(uow, [native.instance.business])
    # The fresh synthetic installation seeds the packaged Overseer procedures; nothing is imported by the check.
    native.runtime.agentic_layer.catalog.read_entry("overseer/extract")
    native.runtime._test_agent = native.agent
    return native, background


def run_scheduled_occurrence(native, background, *, first_hour=9, days=1):
    routine = schedule(native)
    now = datetime(2026, 10, 28, first_hour, 0, tzinfo=UTC)
    background.scheduler.tick(now)              # first-seen suppression: the slot is recorded, not queued
    queued = background.scheduler.tick(now + timedelta(days=days))
    assert len(queued) == 1
    assert background.run_one(queued[0])["state"] == "succeeded"
    with native.runtime.database.read() as uow:
        occurrence = dict(uow.connection.execute("SELECT * FROM overseer_due_occurrences").fetchone())
    return routine, occurrence


def test_scheduled_occurrence_coalesces_into_one_due_request_and_inbox_event(overseer):
    native, background = overseer
    routine, occurrence = run_scheduled_occurrence(native, background)
    assert occurrence["routine_id"] == routine["id"]
    assert occurrence["program"] == "overseer.extraction_due"
    assert occurrence["missed_count"] == 0
    assert occurrence["scheduled_slot"] == "2026-10-29T09:00:00Z"
    with native.runtime.database.transaction() as uow:
        summary = reconcile(native.runtime, uow)
    assert summary["assigned"] == 1 and summary["requests"] == 1
    with native.runtime.database.read() as uow:
        rows = [dict(row) for row in uow.connection.execute("SELECT * FROM overseer_due_requests").fetchall()]
        events = [dict(row) for row in uow.connection.execute(
            "SELECT * FROM inbox_events WHERE source_type='overseer_extraction_due'").fetchall()]
        deliveries = [dict(row) for row in uow.connection.execute(
            "SELECT * FROM inbox_deliveries WHERE event_id=?", (events[0]["id"],)).fetchall()]
    assert len(rows) == 1 and rows[0]["state"] == "pending"
    assert rows[0]["coalescing_key"] == coalescing_key(occurrence)
    assert events[0]["source_id"] == rows[0]["id"] and events[0]["source_revision"] == 1
    assert events[0]["event_type"] == "overseer_extraction_due"
    assert deliveries[0]["agent_id"] == native.agent
    assert "extraction is due" in events[0]["summary"]

    # A second missed slot before admission joins the same pending request, not a second one.
    background.scheduler.tick(datetime(2026, 10, 30, 9, 0, tzinfo=UTC))
    with native.runtime.database.read() as uow:
        queued = uow.connection.execute("SELECT id FROM jobs WHERE kind='overseer.extraction_due' "
                                        "AND state='queued'").fetchone()
    assert background.run_one(queued[0])["state"] == "succeeded"
    with native.runtime.database.transaction() as uow:
        summary = reconcile(native.runtime, uow)
    assert summary["assigned"] == 1 and summary["requests"] == 0
    with native.runtime.database.read() as uow:
        request = uow.connection.execute("SELECT * FROM overseer_due_requests").fetchone()
        members = uow.connection.execute("SELECT COUNT(*) FROM overseer_due_request_occurrences "
                                         "WHERE request_id=?", (request["id"],)).fetchone()[0]
    assert members == 2


def test_due_request_is_read_and_admitted_with_its_exact_reference(overseer):
    native, background = overseer
    routine, occurrence = run_scheduled_occurrence(native, background)
    with native.runtime.database.transaction() as uow:
        reconcile(native.runtime, uow)
    with native.runtime.database.read() as uow:
        request = dict(uow.connection.execute("SELECT * FROM overseer_due_requests").fetchone())
    reference = {"provider": "qorqut", "kind": "extraction_due", "id": request["id"],
                 "revision": request["source_hash"]}
    registration_id = registration(native.instance, native.agent)
    ctx = native.runtime.resolver.runtime_context(Principal(native.agent, "agent"),
                                                  session_registration_id=registration_id)
    listed = native.runtime.application.invoke(ctx, "overseer_due_list", {})
    assert listed.status == "ok", listed.to_dict()
    item = listed.data["items"][0]
    assert item["reference"] == reference and item["state"] == "pending"
    assert item["occurrence_count"] == 1
    assert item["eligibility"] == {"eligible": True, "reason": None}
    fetched = native.runtime.application.invoke(ctx, "overseer_due_get", {"reference": reference})
    assert fetched.status == "ok", fetched.to_dict()
    from qorqut_operations.contracts import plain
    arguments = plain(fetched.data["extraction_arguments"])
    assert arguments == {}
    assert fetched.data["target"]["conversation_id"] == conversation(native)

    # Forged source/hash and a review start are refused before any workflow exists.
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, ctx, "extraction", {"reference": {**reference, "revision": "0" * 64}},
                         {"kind": "extraction", "arguments": arguments,
                          "rationale": "The owner asked for the scheduled extraction."}, "due-forged")
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, ctx, "review", {"reference": reference},
                         {"kind": "review", "arguments": {}, "rationale": "A due reference cannot start review."},
                         "due-review")
    assert native.runtime.agentic_layer.workflows.list()["total"] == 0

    interpretation = {"kind": "extraction", "arguments": arguments,
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    started = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                               "due-start-1")
    assert started["state"] == "linked" and started["run_id"]
    run = native.runtime.agentic_layer.workflows.get(started["run_id"])["run"]
    assert run["workflow_ref"] == "overseer/extract"
    assert run["state"] in {"running", "waiting_arguments"}
    # A retry returns the one linked run and does not create a second workflow or event.
    assert start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                            "due-start-1")["run_id"] == started["run_id"]
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM inbox_events WHERE source_type="
                                      "'overseer_extraction_due'").fetchone()[0] == 1
        linked = uow.connection.execute("SELECT state,linked_run_id FROM overseer_due_requests WHERE id=?",
                                        (request["id"],)).fetchone()
    assert tuple(linked) == ("linked", started["run_id"])
    # A different interpretation for the same linked request is refused.
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference},
                         {**interpretation, "rationale": "Another reading of the same request."}, "due-start-2")

def _registered_ctx(native):
    ctx = native.runtime.resolver.runtime_context(Principal(native.agent, "agent"),
                                                  session_registration_id=registration(native.instance, native.agent))
    return ctx


def _pending(native, background):
    run_scheduled_occurrence(native, background)
    with native.runtime.database.transaction() as uow:
        reconcile(native.runtime, uow)
    with native.runtime.database.read() as uow:
        request = dict(uow.connection.execute("SELECT * FROM overseer_due_requests").fetchone())
    reference = {"provider": "qorqut", "kind": "extraction_due", "id": request["id"],
                 "revision": request["source_hash"]}
    return request, reference


def test_missed_slots_coalesce_with_their_bounded_count(overseer):
    native, background = overseer
    _, occurrence = run_scheduled_occurrence(native, background, days=3)
    assert occurrence["missed_count"] == 2 and not occurrence["missed_capped"]
    assert occurrence["scheduled_slot"] == "2026-10-31T09:00:00Z"
    with native.runtime.database.transaction() as uow:
        summary = reconcile(native.runtime, uow)
    assert summary["requests"] == 1
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM overseer_due_requests").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM inbox_events WHERE source_type="
                                      "'overseer_extraction_due'").fetchone()[0] == 1


def test_paused_chat_keeps_the_request_pending_and_resume_admits_the_same_conversation(overseer):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    arguments = {}
    interpretation = {"kind": "extraction", "arguments": arguments,
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) "
                               "VALUES(?,?,?,1,'paused') ON CONFLICT(agent_id) DO UPDATE SET desired_state='paused'",
                               (native.agent, new_id(), '{"fixture":true}'))
    from bazarlyk.overseer.due import get_request
    with native.runtime.database.read() as uow:
        view = get_request(native.runtime, ctx, uow, reference)
    assert view["eligibility"] == {"eligible": False, "reason": "conversation_paused", "blocked_occurrences": []}
    with pytest.raises(DomainError) as paused:
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-paused")
    assert paused.value.code == "owner_input_required", paused.value.to_dict()
    with native.runtime.database.read() as uow:
        row = uow.connection.execute("SELECT state,linked_run_id FROM overseer_due_requests WHERE id=?",
                                     (request["id"],)).fetchone()
    assert row["state"] == "pending" and row["linked_run_id"] is None
    assert native.runtime.agentic_layer.workflows.list()["total"] == 0
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE native_session_owners SET desired_state='running' WHERE agent_id=?",
                               (native.agent,))
    started = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-paused")
    assert started["state"] == "linked"
    assert start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                            "due-paused")["run_id"] == started["run_id"]
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1


def test_a_crash_between_reserving_starting_and_linking_recovers_the_same_run(overseer, monkeypatch):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    interpretation = {"kind": "extraction", "arguments": {},
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    real = native.runtime.database.transaction

    def crash(operation=None, **options):
        if operation == "overseer.due-link":
            raise RuntimeError("synthetic crash between the module run and the request link")
        return real(operation=operation, **options)

    monkeypatch.setattr(native.runtime.database, "transaction", crash)
    with pytest.raises(RuntimeError):
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-crash")
    with native.runtime.database.read() as uow:
        row = uow.connection.execute("SELECT state,linked_run_id FROM overseer_due_requests WHERE id=?",
                                     (request["id"],)).fetchone()
    assert row["state"] == "reserved" and row["linked_run_id"] is None
    runs = native.runtime.agentic_layer.workflows.list()
    assert runs["total"] == 1                                            # the module run exists, unlinked
    pre_crash = runs["runs"][0].get("run_id") or runs["runs"][0]["id"]
    monkeypatch.setattr(native.runtime.database, "transaction", real)
    recovered = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-crash")
    assert recovered["state"] == "linked" and recovered["run_id"] == pre_crash
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1
    with native.runtime.database.read() as uow:
        assert tuple(uow.connection.execute("SELECT state,linked_run_id FROM overseer_due_requests WHERE id=?",
                                            (request["id"],)).fetchone()) == ("linked", recovered["run_id"])


def test_due_delivery_never_claims_analysis_or_coverage(overseer):
    import sqlite3
    native, background = overseer
    _, reference = _pending(native, background)
    started = start_from_owner(native.runtime, _registered_ctx(native), "extraction", {"reference": reference},
                               {"kind": "extraction", "arguments": {},
                                "rationale": "The scheduled extraction is due for this continuing conversation."},
                               "due-analysis")
    run = native.runtime.agentic_layer.workflows.get(started["run_id"])["run"]
    assert run["state"] != "completed"
    # The module records coverage only when extraction actually analyzes sources; delivery writes none.
    connection = sqlite3.connect((native.runtime.agentic_layer.state / "state.sqlite3").absolute().as_uri() + "?mode=ro",
                                 uri=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM overseer_coverage").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM overseer_coverage_sources").fetchone()[0] == 0
    finally:
        connection.close()


def test_only_the_overseer_role_and_its_own_conversation_see_or_start_a_due_request(overseer):
    native, background = overseer
    request, reference = _pending(native, background)
    other = create_registered_global(native.instance, "ordubashy")
    grant_capabilities(native.instance, other, "manage_agentic_layer")
    other_ctx = native.runtime.resolver.runtime_context(Principal(other, "agent"),
                                                       session_registration_id=registration(native.instance, other))
    listed = native.runtime.application.invoke(other_ctx, "overseer_due_list", {})
    assert listed.status == "error" and listed.error["code"] in {"forbidden", "policy_denied"}, listed.to_dict()
    with pytest.raises(DomainError):
        start_from_owner(native.runtime, other_ctx, "extraction", {"reference": reference},
                         {"kind": "extraction", "arguments": {}, "rationale": "Not this agent's request."},
                         "due-other")
    assert native.runtime.agentic_layer.workflows.list()["total"] == 0
    # The inbox/nudge reader has no MCP registration: the stored request, role, capability and saved conversation decide.
    reader = native.runtime.resolver.runtime_context(Principal(native.agent, "agent"))
    with native.runtime.database.read() as uow:
        assert request_visible(native.runtime, reader, request["id"], uow) is True
        row = uow.connection.execute("SELECT * FROM inbox_events WHERE source_type='overseer_extraction_due'").fetchone()
        item = {}
        native.runtime.inbox.host.decorate_before_extensions(reader, row, item, uow)
        assert item["overseer_extraction_due"] == reference
        assert request_visible(native.runtime, other_ctx, request["id"], uow) is False
    # A manual routine run is not an owner chat invocation and records no due occurrence.
    routine = native.runtime.application.invoke(owner_global_context(native.instance), "routines_list",
                                                {"scope": "global"}).data["items"][0]
    manual = native.runtime.application.invoke(owner_global_context(native.instance), "routines_run",
        {"routine_id": routine["id"], "scope": "global"},
        InvocationOptions(idempotency_key="manual-due", expected_revision=routine["revision"]))
    assert manual.status == "ok", manual.to_dict()
    outcome = background.run_one(manual.data["job_ids"][0])
    assert outcome["error"]["code"] == "forbidden"
    assert "scheduled occurrence" in outcome["error"]["message"]
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT state FROM jobs WHERE id=?", (manual.data["job_ids"][0],)).fetchone()[0] == "failed"
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM overseer_due_occurrences").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM overseer_due_requests").fetchone()[0] == 1


def _owner_routine(native):
    owner = owner_global_context(native.instance)
    items = native.runtime.application.invoke(owner, "routines_list", {"scope": "global"}).data["items"]
    return owner, items[0]


def _update_routine(native, definition, key):
    owner, routine = _owner_routine(native)
    result = native.runtime.application.invoke(owner, "routines_update",
        {"scope": "global", "routine_id": routine["id"], "definition": definition},
        InvocationOptions(idempotency_key=key, expected_revision=routine["revision"]))
    assert result.status == "ok", result.to_dict()


def _set_owner_state(native, state):
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) "
            "VALUES(?,?,?,1,?) ON CONFLICT(agent_id) DO UPDATE SET desired_state=?",
            (native.agent, new_id(), '{"fixture":true}', state, state))


def _definition(native, **changes):
    return {"name": "Daily Overseer extraction", "schedule": "0 9 * * *", "timezone": "UTC",
            "status": "active", "program": "overseer.extraction_due", "target": "root",
            "arguments": {"agent_id": native.agent, "conversation_id": conversation(native)}, **changes}


@pytest.mark.parametrize("change", ["pause", "retarget", "arguments"])
def test_current_routine_authority_blocks_admission_and_restores(overseer, change):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    owner, routine = _owner_routine(native)
    if change == "pause":
        result = native.runtime.application.invoke(owner, "routines_pause",
            {"scope": "global", "routine_id": routine["id"]},
            InvocationOptions(idempotency_key="due-pause", expected_revision=routine["revision"]))
        assert result.status == "ok", result.to_dict()
        original = None
    elif change == "retarget":
        _update_routine(native, _definition(native, arguments={
            "agent_id": native.agent, "conversation_id": "another-saved-chat"}), "due-retarget")
        original = _definition(native)
    else:
        _update_routine(native, _definition(native, arguments={
            "agent_id": native.agent, "conversation_id": conversation(native), "selection": {"scope": "biz"}}),
            "due-arguments")
        original = _definition(native)
    interpretation = {"kind": "extraction", "arguments": {},
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    with pytest.raises(DomainError) as refused:
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-refused")
    assert refused.value.code == "owner_input_required"
    assert native.runtime.agentic_layer.workflows.list()["total"] == 0
    with native.runtime.database.read() as uow:
        stored = uow.connection.execute("SELECT state FROM overseer_due_requests WHERE id=?",
                                        (request["id"],)).fetchone()[0]
        blocked = uow.connection.execute("SELECT blocked_reason FROM overseer_due_occurrences").fetchone()[0]
    assert stored == "pending" and blocked in {"routine_inactive", "routine_arguments_changed"}
    # The read projection explains the same block and never claims eligibility.
    with native.runtime.database.read() as uow:
        from bazarlyk.overseer.due import get_request
        view = get_request(native.runtime, ctx, uow, reference)
    assert view["eligibility"]["eligible"] is False
    assert view["eligibility"]["reason"] in {"routine_inactive", "routine_arguments_changed"}
    if change == "pause":
        resumed = native.runtime.application.invoke(owner, "routines_resume",
            {"scope": "global", "routine_id": routine["id"]},
            InvocationOptions(idempotency_key="due-resume", expected_revision=routine["revision"] + 1))
        assert resumed.status == "ok", resumed.to_dict()
    else:
        _update_routine(native, original, "due-restore-" + change)
    started = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                               "due-restored")
    assert started["state"] == "linked" and native.runtime.agentic_layer.workflows.list()["total"] == 1
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT blocked_reason FROM overseer_due_occurrences").fetchone()[0] is None


def test_time_only_routine_edit_keeps_the_recorded_reference_eligible(overseer):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    _update_routine(native, _definition(native, schedule="30 10 * * *", timezone="America/New_York"), "due-time-only")
    started = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference},
        {"kind": "extraction", "arguments": {},
         "rationale": "The scheduled occurrence predates only a time-of-day edit."}, "due-time-only-start")
    assert started["state"] == "linked" and native.runtime.agentic_layer.workflows.list()["total"] == 1


def test_linked_replay_revalidates_live_owner_authority(overseer):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    interpretation = {"kind": "extraction", "arguments": {},
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    started = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                               "due-linked")
    _set_owner_state(native, "paused")
    with pytest.raises(DomainError) as paused:
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-linked")
    assert paused.value.code == "owner_input_required"
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1
    _set_owner_state(native, "running")
    assert start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation,
                            "due-linked")["run_id"] == started["run_id"]


def test_reserved_admission_survives_a_crash_and_a_later_due_occurrence(overseer, monkeypatch):
    native, background = overseer
    request, reference = _pending(native, background)
    ctx = _registered_ctx(native)
    interpretation = {"kind": "extraction", "arguments": {},
                      "rationale": "The scheduled extraction is due for this continuing conversation."}
    original = native.runtime.agentic_layer.overseer.start_run

    def crash_after_start(*args, **kwargs):
        crash_after_start.result = original(*args, **kwargs)
        raise OSError("synthetic crash after the module created its workflow")

    monkeypatch.setattr(native.runtime.agentic_layer.overseer, "start_run", crash_after_start)
    with pytest.raises(OSError):
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-crash")
    monkeypatch.setattr(native.runtime.agentic_layer.overseer, "start_run", original)
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1
    with native.runtime.database.read() as uow:
        reserved = uow.connection.execute(
            "SELECT state,admission_hash,linked_run_id FROM overseer_due_requests WHERE id=?",
            (request["id"],)).fetchone()
    assert reserved["state"] == "reserved" and reserved["admission_hash"] and reserved["linked_run_id"] is None
    _set_owner_state(native, "paused")
    with pytest.raises(DomainError) as refused:
        start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-crash")
    assert refused.value.code == "owner_input_required"
    with native.runtime.database.read() as uow:
        assert tuple(uow.connection.execute(
            "SELECT state,admission_hash,linked_run_id FROM overseer_due_requests WHERE id=?",
            (request["id"],)).fetchone()) == ("reserved", reserved["admission_hash"], None)
    # A later scheduled slot creates a distinct pending request; it does not erase or join the reserved admission.
    queued = background.scheduler.tick(datetime(2026, 10, 30, 9, 0, tzinfo=UTC))
    assert len(queued) == 1 and background.run_one(queued[0])["state"] == "succeeded"
    with native.runtime.database.transaction() as uow:
        reconcile(native.runtime, uow)
    with native.runtime.database.read() as uow:
        requests = [dict(row) for row in uow.connection.execute(
            "SELECT * FROM overseer_due_requests ORDER BY created_at,id").fetchall()]
    assert len(requests) == 2 and requests[0]["id"] == request["id"] and requests[0]["state"] == "reserved"
    assert requests[1]["state"] == "pending" and requests[1]["id"] != request["id"]
    # Restoring authority recovers the original single workflow with its same invocation key.
    _set_owner_state(native, "running")
    recovered = start_from_owner(native.runtime, ctx, "extraction", {"reference": reference}, interpretation, "due-crash")
    assert recovered["state"] == "linked" and recovered["run_id"] == crash_after_start.result["run_id"]
    assert native.runtime.agentic_layer.workflows.list()["total"] == 1
    with native.runtime.database.read() as uow:
        assert uow.connection.execute("SELECT state FROM overseer_due_requests WHERE id=?",
                                      (request["id"],)).fetchone()[0] == "linked"
