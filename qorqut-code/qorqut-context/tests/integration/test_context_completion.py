"""Source versions, filings, the terminal-outcome outbox and its authorized application, over the synthetic Operations host.

Everything real is the engine: the queue and its ``transitions``, the ``context_filing`` contribution and its database
constraints, the program registry and background authority that prepare registered jobs, and the prepared-plan records
that apply a filing. The only test code is the stand-in for a filing adapter (``prepare_filing``): a prepared operation that
prepares the real registered job and calls ``admit``. No Context, model or provider runs."""

import json
import sqlite3

import pytest

from operational_support import AGENT, BUSINESS, COORDINATOR, OTHER, OperationalHost, WORKER
from qorqut_operations.application.plans import PlanBuilder, UUID, apply_records, schema
from qorqut_operations.background import BackgroundAuthority, Program, ProgramRegistry
from qorqut_context.integration.filing import admit, completion, envelope, new_filing_id
from qorqut_context.integration.queue import transitions
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id

META = {"kind": "document", "title": "Upload", "origin": "upload", "origin_ref": "document:x"}


@pytest.fixture
def host(tmp_path):
    host = OperationalHost(tmp_path)
    host.programs = ProgramRegistry()
    for identity, scopes in (("context.file", ("business", "global")), ("context.instruct", ("business", "global"))):
        host.programs.register(Program(identity, 1, schema({"filing_id": UUID}, ["filing_id"]), lambda control, args: None,
                                       {"context_filing_receipt": "allow"}, ("context_filing_receipt",), ("context_completions",),
                                       scope_kinds=scopes))
    host.authority = BackgroundAuthority(host.database, host.programs)
    with host.database.transaction() as uow:
        host.workers = host.authority.register_workers(uow, [BUSINESS, OTHER])
    return host


def source(body="Body", **metadata):
    return envelope.source_document(body, {"title": "T", **metadata})


def prepare_filing(host, *, kind="document", source_id=None, document=None, ctx=None, retracts=None, limit=None, program=None,
                   job_arguments=None, event=META):
    """The adapter stand-in: prepare the registered job and admit one version, then apply the plan in one transaction."""
    ctx = ctx or host.agent()
    source_id = source_id or new_id()
    with host.database.read() as uow:
        builder = PlanBuilder(ctx, "filing_test", {}, "Filing", clock=host.clock)
        filing_id = new_filing_id()
        program = program or ("context.instruct" if kind == "instruction" else "context.file")
        job_id = host.programs.prepare_job(builder, uow, program, job_arguments if job_arguments is not None else {"filing_id": filing_id})
        admission = admit(builder, uow, kind=kind, source_id=job_id if kind == "instruction" else source_id, filing_id=filing_id,
                          job_id=job_id, document=document if retracts is None else None, event_metadata=event, retracts=retracts,
                          max_content_bytes=limit, business_id=ctx.business_id)
        plan = builder.build()
    with host.database.transaction() as uow:
        apply_records(ctx, plan, uow)
    return admission, job_id


def drive(host, name, input_id, **kwargs):
    if name in ("record_published", "record_discarded"):
        kwargs["outcomes"] = host.outcomes
    with host.database.transaction() as uow:
        return getattr(transitions, name)(uow, driver_id=WORKER, input_id=input_id, **kwargs)


def publish(host, input_id, event="ab12c", commit="c0ffee"):
    drive(host, "start_attempt", input_id, context_event_id=event)
    drive(host, "record_published", input_id, ordubashy_id=COORDINATOR, result={"context_event_id": event, "published_commit": commit},
          handoff_note={"disposition": "empty", "text": ""})


def worker_context(host, job_id, *, business=BUSINESS):
    worker = host.query("SELECT principal_id, initiated_by_principal_id FROM jobs WHERE id=?", job_id)[0]
    return InvocationContext(Principal(worker["principal_id"], "system"), business_id=business, source="worker", job_id=job_id,
                             initiated_by_principal_id=worker["initiated_by_principal_id"])


def set_job(host, job_id, state):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))


def authorize(host, ctx, receipt_id):
    with host.database.read() as uow:
        return completion.authorize(uow, ctx, receipt_id)


def receipt_of(host, filing_id):
    with host.database.read() as uow:
        return completion.receipt_for_filing(uow, filing_id)


# -- admission ----------------------------------------------------------------------------------------------------------

def test_admission_records_one_version_filing_job_and_controlled_input_together(host):
    document = source("Hello")
    admission, job_id = prepare_filing(host, document=document, source_id="d" * 36)
    assert (admission.source_revision, admission.operation, admission.path) == (1, "add", "foundation/documents/" + "d" * 36 + ".txt")
    version = host.query("SELECT * FROM context_source_versions")[0]
    filing = host.query("SELECT * FROM context_filings")[0]
    row = host.query("SELECT * FROM context_inputs")[0]
    job = host.query("SELECT * FROM jobs")[0]
    assert version["source_digest"] == envelope.fingerprint(document) and version["business_id"] == BUSINESS
    assert (filing["id"], filing["job_id"], filing["input_id"], filing["source_version_id"]) == (admission.filing_id, job_id, row["id"], version["id"])
    assert (filing["worker_id"], filing["program_id"], filing["request_id"]) == (job["principal_id"], "context.file", filing["request_id"])
    assert job["arguments_json"] == {"filing_id": admission.filing_id}
    assert filing["definition_digest"] == job["definition_ref_json"]["digest"] and filing["program_version"] == 1
    payload = row["payload_json"]
    assert row["state"] == "queued" and row["business_id"] == BUSINESS and row["submitted_by"] == AGENT
    assert payload["content"] == document and payload["path"] == version["source_path"] and payload["metadata"]["origin"] == "upload"
    # Admission alone is not completion: nothing is published, no receipt exists, and the filing reads as queued.
    with host.database.read() as uow:
        shown = completion.describe(uow, admission.filing_id, current_digest=version["source_digest"])
    assert shown == {"filing_id": admission.filing_id, "source_revision": 1, "input_id": row["id"], "job_id": job_id, "state": "queued",
                     "published_commit": None, "completion_state": None, "source_current": True, "error": None, "queue_position": 0}
    assert receipt_of(host, admission.filing_id) is None


def test_the_same_source_gets_consecutive_versions_and_a_duplicate_is_a_replay_not_a_version(host):
    source_id = new_id()
    first, _ = prepare_filing(host, source_id=source_id, document=source("One"))
    with pytest.raises(DomainError) as replay:
        prepare_filing(host, source_id=source_id, document=source("One"))
    assert replay.value.code == "conflict" and replay.value.details["filing_id"] == first.filing_id
    second, _ = prepare_filing(host, source_id=source_id, document=source("Two"))
    metadata_only, _ = prepare_filing(host, source_id=source_id, document=envelope.source_document("Two", {"title": "Renamed"}))
    assert [(item.source_revision, item.operation) for item in (first, second, metadata_only)] == [(1, "add"), (2, "edit"), (3, "edit")]
    assert len({item.digest for item in (first, second, metadata_only)}) == 3        # a title change is a different source
    assert host.count("context_source_versions") == 3 and host.count("context_inputs") == 3 and host.count("jobs") == 3
    with host.database.read() as uow:
        views = [completion.describe(uow, item.filing_id, current_digest=metadata_only.digest) for item in (first, second, metadata_only)]
    assert [view["source_current"] for view in views] == [False, False, True]        # only the latest applicable version is current
    assert [view["queue_position"] for view in views] == [0, 1, 2]


def test_a_discarded_add_never_turns_a_later_version_into_an_add(host):
    source_id = new_id()
    first, _ = prepare_filing(host, source_id=source_id, document=source("One"))
    host.data("context_queue_discard", {"input_id": first.input_id, "expected_state": "queued"}, host.agent())
    later, _ = prepare_filing(host, source_id=source_id, document=source("One"))     # same bytes again: a new version
    assert (later.source_revision, later.operation) == (2, "edit")
    with host.database.transaction() as uow:
        with pytest.raises(DomainError) as unbound:
            from qorqut_context.integration.filing import reserve_source
            reserve_source(uow, driver_id=WORKER, input_id=later.input_id, exists=False)
    assert unbound.value.code == "source_unbound"                                     # the edit may block; it is not an add


def test_a_public_submission_cannot_take_a_controlled_path_and_the_database_agrees(host):
    for prefix in ("foundation/documents/", "foundation/instructions/"):
        assert host.error("context_queue_submit", {"operation": "add", "path": prefix + "x" * 36 + ".txt", "content": "Text",
                                                   "metadata": {"kind": "note", "title": "T", "origin": "agent"}}, host.agent()) == "validation_error"
    with pytest.raises(DomainError) as raw:
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO context_inputs(id,business_id,input_kind,operation,payload_json,submitted_by,created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id(), BUSINESS, "general", "add", json.dumps({"operation": "add", "path": "foundation/documents/" + "q" * 36 + ".txt",
                                                                     "content": "x", "metadata": None}), AGENT, "now"))
    assert "Controlled Context paths" in str(raw.value.__cause__)
    assert host.count("context_inputs") == 0
    # A filing that is not its own prepared job is refused before any record: a job with other arguments, another program.
    with pytest.raises(DomainError) as wrong:
        prepare_filing(host, document=source(), job_arguments={"filing_id": new_id()})
    assert wrong.value.code == "invalid_plan"
    with pytest.raises(DomainError):
        prepare_filing(host, kind="instruction", document=source(), program="context.file")
    assert host.count("context_filings") == 0 and host.count("jobs") == 0


def test_an_instruction_is_its_own_source_and_the_scope_and_size_are_enforced(host):
    admission, job_id = prepare_filing(host, kind="instruction", document=source("Correct the record"))
    assert admission.path == f"foundation/instructions/{job_id}.txt" and admission.operation == "add"
    with host.database.read() as uow:
        builder = PlanBuilder(host.agent(), "filing_test", {}, "x", clock=host.clock)
        filing_id = new_filing_id()
        job = host.programs.prepare_job(builder, uow, "context.file", {"filing_id": filing_id})
        with pytest.raises(DomainError) as scope:                                         # a source is filed in the caller's scope only
            admit(builder, uow, kind="document", source_id=new_id(), filing_id=filing_id, job_id=job, document=source(),
                  event_metadata=META, business_id=OTHER)
    assert scope.value.code == "forbidden"
    big = source("x" * 5000)
    with pytest.raises(DomainError) as large:
        prepare_filing(host, document=big, limit=1000)
    assert large.value.code == "source_too_large" and "1000" in large.value.message
    with pytest.raises(DomainError):
        prepare_filing(host, document="not an envelope")                              # only the canonical source document


# -- retraction ---------------------------------------------------------------------------------------------------------

def test_a_retraction_names_one_exact_published_version_and_never_a_pending_one(host):
    source_id = new_id()
    first, _ = prepare_filing(host, source_id=source_id, document=source("One"))
    with pytest.raises(DomainError) as pending:
        prepare_filing(host, source_id=source_id, retracts=1)
    assert pending.value.code == "invalid_transition"                                 # a pending input is discarded, not retracted
    publish(host, first.input_id)
    newer, _ = prepare_filing(host, source_id=source_id, document=source("Two"))
    with pytest.raises(DomainError) as busy:
        prepare_filing(host, source_id=source_id, retracts=1)
    assert busy.value.code == "invalid_transition" and "pending" in busy.value.message
    publish(host, newer.input_id, event="de34f", commit="beef01")
    with pytest.raises(DomainError):
        prepare_filing(host, source_id=source_id, retracts=1)                         # not the latest published version
    removal, _ = prepare_filing(host, source_id=source_id, retracts=2)
    row = host.query("SELECT operation, payload_json FROM context_inputs WHERE id=?", removal.input_id)[0]
    assert (removal.operation, removal.source_revision, row["operation"]) == ("remove", 3, "remove")
    assert row["payload_json"]["content"] is None and removal.digest == newer.digest


# -- terminal outcomes --------------------------------------------------------------------------------------------------

def test_publication_records_one_stable_pending_receipt_and_replays_return_it(host):
    admission, job_id = prepare_filing(host, document=source("Hello"))
    publish(host, admission.input_id)
    stored = receipt_of(host, admission.filing_id)
    assert stored["state"] == "pending" and stored["outcome"] == "published" and stored["job_id"] == job_id
    assert (stored["context_event_id"], stored["published_commit"], stored["input_id"]) == ("ab12c", "c0ffee", admission.input_id)
    assert stored["source_version_id"] == admission.version_id and stored["application_json"] is None
    with host.database.read() as uow:
        row = dict(uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (admission.input_id,)).fetchone())
    with host.database.transaction() as uow:
        again = host.outcomes.record(uow, row)
        assert again["id"] == stored["id"] and again["outcome_digest"] == stored["outcome_digest"]
        with pytest.raises(DomainError) as different:
            host.outcomes.record(uow, {**row, "result_json": json.dumps({**json.loads(row["result_json"]), "published_commit": "other"})})
    assert different.value.code == "completion_conflict" and host.count("context_completions") == 1
    with host.database.read() as uow:
        assert completion.describe(uow, admission.filing_id, current_digest=admission.digest) == {
            "filing_id": admission.filing_id, "source_revision": 1, "input_id": admission.input_id, "job_id": job_id, "state": "published",
            "published_commit": "c0ffee", "completion_state": "pending", "source_current": True, "error": None, "queue_position": None}


def test_an_unfiled_input_has_no_receipt(host):
    submitted = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body",
                                                    "metadata": {"kind": "note", "title": "A", "origin": "agent"}}, host.agent())["input"]
    publish(host, submitted["id"])
    assert host.count("context_completions") == 0


def test_an_unstarted_discard_control_records_its_receipt_before_any_event(host):
    admission, _ = prepare_filing(host, document=source("Hello"))
    result = host.data("context_queue_discard", {"input_id": admission.input_id, "expected_state": "queued"}, host.agent())["result"]
    assert result["state"] == "discarded"
    stored = receipt_of(host, admission.filing_id)
    assert stored["outcome"] == "discarded" and stored["context_event_id"] is None and stored["published_commit"] is None
    assert stored["reason_json"]["reason"] == "never_started" and stored["state"] == "pending"


def test_a_blocked_attempt_is_not_terminal_and_its_discard_closes_through_the_driver(host):
    admission, _ = prepare_filing(host, document=source("Hello"))
    drive(host, "start_attempt", admission.input_id, context_event_id="ab12c")
    drive(host, "record_blocked", admission.input_id, error={"code": "context_failed", "message": "refused"}, ordubashy_id=COORDINATOR)
    assert receipt_of(host, admission.filing_id) is None                              # blocked: no success, no receipt
    with host.database.read() as uow:
        shown = completion.describe(uow, admission.filing_id, current_digest=admission.digest)
    assert shown["state"] == "blocked" and shown["completion_state"] is None and shown["error"]["code"] == "context_failed"
    host.data("context_queue_discard", {"input_id": admission.input_id, "expected_state": "blocked"}, host.agent())
    assert receipt_of(host, admission.filing_id) is None                              # requested, not yet closed
    drive(host, "record_discarded", admission.input_id, result={"mechanical": True, "reason": "owner discard"})
    stored = receipt_of(host, admission.filing_id)
    assert stored["outcome"] == "discarded" and stored["context_event_id"] == "ab12c" and stored["reason_json"]["mechanical"] is True
    assert "request_evidence" not in stored["reason_json"] and "control_id" not in stored["reason_json"]       # no evidence was requested


def test_mechanical_recovery_records_a_missing_receipt_once(host):
    admission, _ = prepare_filing(host, document=source("Hello"))
    drive(host, "start_attempt", admission.input_id, context_event_id="ab12c")
    with host.database.transaction() as uow:                  # a gap: the input became final without its receipt
        uow.connection.execute("UPDATE context_inputs SET state='published', result_json=? WHERE id=?",
                               (json.dumps({"context_event_id": "ab12c", "published_commit": "c0ffee"}), admission.input_id))
    assert receipt_of(host, admission.filing_id) is None
    from qorqut_context.integration.filing import outcomes
    with host.database.transaction() as uow:
        recorded = outcomes.record_missing(uow, driver_id=WORKER)
    assert [item["filing_id"] for item in recorded] == [admission.filing_id]
    with host.database.transaction() as uow:
        assert outcomes.record_missing(uow, driver_id=WORKER) == []
        with pytest.raises(DomainError):
            outcomes.record_missing(uow, driver_id=AGENT)                              # system principal only
    assert host.count("context_completions") == 1


# -- authorized application ---------------------------------------------------------------------------------------------

def test_only_the_bound_claimed_job_can_apply_its_receipt(host):
    admission, job_id = prepare_filing(host, document=source("Hello"))
    other, other_job = prepare_filing(host, document=source("Other"))
    publish(host, admission.input_id)
    stored = receipt_of(host, admission.filing_id)
    set_job(host, job_id, "applying")
    set_job(host, other_job, "applying")
    ok = authorize(host, worker_context(host, job_id), stored["id"])
    assert ok["receipt"]["id"] == stored["id"] and ok["filing"]["job_id"] == job_id and ok["job"]["state"] == "applying"
    for label, ctx in (("another bound job", worker_context(host, other_job)),
                       ("an agent", host.agent()), ("the owner", host.owner()),
                       ("no job", InvocationContext(Principal(host.workers[0], "system"), business_id=BUSINESS, source="worker"))):
        with pytest.raises(DomainError) as refused:
            authorize(host, ctx, stored["id"])
        assert refused.value.code == "forbidden", label
    with pytest.raises(DomainError) as unknown:
        authorize(host, worker_context(host, job_id), new_id())
    assert unknown.value.code == "not_found"
    for state in ("cancel_requested", "cancelled", "queued", "failed", "succeeded"):
        set_job(host, job_id, state)
        with pytest.raises(DomainError) as stopped:
            authorize(host, worker_context(host, job_id), stored["id"])
        assert stopped.value.code == "forbidden", state                               # a cancelled job cannot impersonate a worker
    set_job(host, job_id, "applying")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?", (host.workers[0], BUSINESS))
    with pytest.raises(DomainError) as revoked:                                         # a revoked worker is no longer that worker
        authorize(host, worker_context(host, job_id), stored["id"])
    assert revoked.value.code == "forbidden"


def test_the_applied_marker_is_one_prepared_compare_and_set(host):
    admission, job_id = prepare_filing(host, document=source("Hello"))
    publish(host, admission.input_id)
    stored = receipt_of(host, admission.filing_id)
    ctx = worker_context(host, job_id)

    def plan():
        with host.database.read() as uow:
            builder = PlanBuilder(ctx, "context_filing_receipt", {"receipt_id": stored["id"]}, "Apply", clock=host.clock)
            completion.applied_marker(builder, uow, completion.receipt(uow, stored["id"]), {"source_current": True})
            return builder.build()
    first, second = plan(), plan()
    with host.database.transaction() as uow:
        apply_records(ctx, first, uow)
    assert receipt_of(host, admission.filing_id)["state"] == "applied"
    assert receipt_of(host, admission.filing_id)["application_json"] == {"source_current": True}
    with pytest.raises(Exception):                                                      # a second application loses the revision race
        with host.database.transaction() as uow:
            apply_records(ctx, second, uow)
    for statement in ("UPDATE context_completions SET state='pending', resolved_at=NULL WHERE id=?",   # an applied receipt is final
                      "UPDATE context_completions SET outcome='discarded' WHERE id=?"):             # and its outcome is immutable
        with pytest.raises(DomainError) as final:
            with host.database.transaction() as uow:
                uow.connection.execute(statement, (stored["id"],))
        assert isinstance(final.value.__cause__, sqlite3.IntegrityError)


def test_a_changed_program_definition_makes_the_unapplied_receipt_a_visible_conflict(host):
    admission, _ = prepare_filing(host, document=source("Hello"))
    publish(host, admission.input_id)
    stored = receipt_of(host, admission.filing_id)
    with host.database.transaction() as uow:
        assert completion.verify_pending(uow, driver_id=WORKER, programs=host.programs) == []     # intact: nothing changes
    changed = ProgramRegistry()
    changed.register(Program("context.file", 2, schema({"filing_id": UUID}, ["filing_id"]), lambda control, args: None,
                             {"context_filing_receipt": "allow"}, ("context_filing_receipt",), ("context_completions",),
                             scope_kinds=("business", "global")))
    with host.database.transaction() as uow:
        marked = completion.verify_pending(uow, driver_id=WORKER, programs=changed)
    assert [item["state"] for item in marked] == ["conflict"] and marked[0]["application_json"]["code"] == "binding_changed"
    with host.database.read() as uow:
        shown = completion.describe(uow, admission.filing_id, current_digest=admission.digest)
    assert shown["completion_state"] == "conflict" and shown["error"]["code"] == "binding_changed"
    assert receipt_of(host, admission.filing_id)["outcome"] == "published"             # the outcome itself is untouched
    with host.database.transaction() as uow:
        back = completion.verify_pending(uow, driver_id=WORKER, programs=host.programs)
    assert [item["state"] for item in back] == ["pending"] and receipt_of(host, admission.filing_id)["application_json"] is None


def test_the_current_filing_is_the_latest_version_matching_the_content_else_unfiled(host):
    source_id = new_id()
    first, _ = prepare_filing(host, source_id=source_id, document=source("One"))
    second, _ = prepare_filing(host, source_id=source_id, document=source("Two"))
    with host.database.read() as uow:
        current = completion.applicable(uow, "document", source_id, second.digest)
        edited = completion.applicable(uow, "document", source_id, envelope.fingerprint(source("Three")))
        removed = completion.applicable(uow, "document", source_id, None)
        older = completion.applicable(uow, "document", source_id, first.digest)
        none = completion.applicable(uow, "document", new_id(), second.digest)
    assert current["filing_id"] == second.filing_id and current["source_current"] is True
    assert edited["state"] == "unfiled" and edited["filing_id"] is None and edited["source_revision"] is None
    assert removed["state"] == "unfiled" and older["state"] == "unfiled" and none["state"] == "unfiled"


def test_a_cancelled_job_with_an_unfinished_input_is_listed_for_reconciliation_and_published_work_is_not(host):
    pending, pending_job = prepare_filing(host, document=source("Pending"))
    done, done_job = prepare_filing(host, document=source("Done"))
    set_job(host, pending_job, "cancelled")
    set_job(host, done_job, "cancel_requested")
    with host.database.read() as uow:
        stopped = completion.stopped_unfinished(uow)
    assert [(item["id"], item["job_state"]) for item in stopped] == [(pending.input_id, "cancelled"), (done.input_id, "cancel_requested")]
    drive(host, "start_attempt", pending.input_id, context_event_id="ab12c")
    drive(host, "record_published", pending.input_id, ordubashy_id=COORDINATOR, result={"context_event_id": "ab12c", "published_commit": "c0ffee"},
          handoff_note={"disposition": "empty", "text": ""})
    with host.database.read() as uow:
        assert [item["id"] for item in completion.stopped_unfinished(uow)] == [done.input_id]    # published stays published
    stored = receipt_of(host, pending.filing_id)
    assert stored["outcome"] == "published"                                              # cancellation cannot conceal it
    with host.database.read() as uow:
        shown = completion.describe(uow, pending.filing_id, current_digest=pending.digest)
    assert shown["state"] == "published" and shown["completion_state"] == "pending" and shown["error"]["code"] == "job_cancelled"


def test_reconciling_a_cancelled_job_discards_only_unfinished_work_through_the_queue_controls(host):
    published, published_job = prepare_filing(host, document=source("Published"))
    publish(host, published.input_id)
    blocked, blocked_job = prepare_filing(host, document=source("Blocked"))
    bystander, bystander_job = prepare_filing(host, document=source("Processing"))
    unstarted, unstarted_job = prepare_filing(host, document=source("Unstarted"))
    drive(host, "start_attempt", blocked.input_id, context_event_id="bb12c")
    drive(host, "record_blocked", blocked.input_id, error={"code": "context_failed", "message": "refused"}, ordubashy_id=COORDINATOR)
    for job in (published_job, blocked_job, unstarted_job):
        set_job(host, job, "cancelled")
    with host.database.transaction() as uow:
        handled = completion.reconcile_stopped(uow, driver_id=WORKER, outcomes=host.outcomes)
    assert {item["filing_id"]: item["state"] for item in handled} == {blocked.filing_id: "discard_requested",
                                                                       unstarted.filing_id: "discarded"}
    states = {row["id"]: row["state"] for row in host.query("SELECT id, state FROM context_inputs")}
    assert states[published.input_id] == "published" and states[bystander.input_id] == "queued"       # published work and an unrelated job stay
    assert states[unstarted.input_id] == "discarded" and states[blocked.input_id] == "discard_requested"
    stored = receipt_of(host, unstarted.filing_id)
    assert stored["outcome"] == "discarded" and stored["context_event_id"] is None
    assert stored["reason_json"]["reason"] == "job_cancelled" and stored["reason_json"]["job_id"] == unstarted_job
    assert stored["reason_json"]["job_state"] == "cancelled" and stored["reason_json"]["requested_by"] == WORKER
    control = host.query("SELECT * FROM context_input_controls WHERE input_id=?", unstarted.input_id)[0]
    assert (control["intent"], control["requested_by"], control["status"]) == ("discard", WORKER, "applied")
    pending = host.query("SELECT * FROM context_input_controls WHERE input_id=?", blocked.input_id)[0]
    assert (pending["status"], pending["requested_by"]) == ("pending", WORKER)
    assert receipt_of(host, blocked.filing_id) is None and receipt_of(host, published.filing_id)["outcome"] == "published"
    with host.database.transaction() as uow:                                           # idempotent: nothing is requested twice
        assert completion.reconcile_stopped(uow, driver_id=WORKER, outcomes=host.outcomes) == []
    drive(host, "record_discarded", blocked.input_id, result={"mechanical": True, "context_event_id": "bb12c"})
    assert receipt_of(host, blocked.filing_id)["outcome"] == "discarded"
    with host.database.transaction() as uow:
        marked = completion.verify_pending(uow, driver_id=WORKER, programs=host.programs)
    assert {item["filing_id"] for item in marked} == {published.filing_id, blocked.filing_id, unstarted.filing_id}
    assert {item["application_json"]["code"] for item in marked} == {"job_cancelled"}
    with host.database.transaction() as uow:
        with pytest.raises(DomainError):
            completion.reconcile_stopped(uow, driver_id=AGENT, outcomes=host.outcomes)     # the system principal only


# -- section A7 corrections ---------------------------------------------------------------------------------------------

def test_a_retraction_is_current_only_while_the_content_it_removes_is_unchanged(host):
    source_id = new_id()
    first, _ = prepare_filing(host, source_id=source_id, document=source("One"))
    publish(host, first.input_id)
    removal, _ = prepare_filing(host, source_id=source_id, retracts=1)
    changed = envelope.fingerprint(source("Changed"))
    with host.database.read() as uow:
        same = completion.applicable(uow, "document", source_id, first.digest)
        edited = completion.applicable(uow, "document", source_id, changed)
        missing = completion.applicable(uow, "document", source_id, None)
        bound = completion.filing(uow, removal.filing_id)
        verdicts = [completion.source_current(uow, bound, digest) for digest in (first.digest, changed, None)]
        shown = {name: completion.describe(uow, removal.filing_id, current_digest=digest)["source_current"]
                 for name, digest in (("same", first.digest), ("changed", changed), ("missing", None))}
    assert removal.digest == first.digest and same["filing_id"] == removal.filing_id and same["source_current"] is True
    assert edited["state"] == "unfiled" and edited["filing_id"] is None and missing["state"] == "unfiled"      # no stand-in for new content
    assert verdicts == [True, False, False] and shown == {"same": True, "changed": False, "missing": False}
    # The historical removal still records its actual outcome; it just no longer describes the source's current content.
    publish(host, removal.input_id, event="de34f", commit="beef01")
    stored = receipt_of(host, removal.filing_id)
    assert stored["outcome"] == "published" and stored["published_commit"] == "beef01"
    # The changed content is its own next version (an add after the accepted removal); the removal is no longer the latest.
    refiled, _ = prepare_filing(host, source_id=source_id, document=source("Changed"))
    assert (refiled.operation, refiled.source_revision) == ("add", 3) and refiled.filing_id != removal.filing_id
    with host.database.read() as uow:
        assert completion.source_current(uow, completion.filing(uow, removal.filing_id), first.digest) is False   # not the latest version
        assert completion.applicable(uow, "document", source_id, refiled.digest)["filing_id"] == refiled.filing_id
    assert host.query("SELECT COUNT(*) AS n FROM context_completions") == [{"n": 2}]


def blocked_with_event(host, document, event="bb12c"):
    admission, job_id = prepare_filing(host, document=document)
    drive(host, "start_attempt", admission.input_id, context_event_id=event)
    drive(host, "record_blocked", admission.input_id, error={"code": "context_failed", "message": "refused"}, ordubashy_id=COORDINATOR)
    return admission, job_id


def cancel_job(host, job_id):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE jobs SET state='cancelled', error_json=? WHERE id=?",
                               (json.dumps({"code": "cancelled", "message": "Owner stopped this job", "retryable": False}), job_id))


def reconcile(host):
    with host.database.transaction() as uow:
        return completion.reconcile_stopped(uow, driver_id=WORKER, outcomes=host.outcomes)


def controls_of(host, input_id):
    return host.query("SELECT * FROM context_input_controls WHERE input_id=? ORDER BY sequence", input_id)


def test_a_blocked_cancellation_freezes_its_evidence_in_the_pending_discard_and_keeps_it_through_resolution(host):
    admission, job_id = blocked_with_event(host, source("Blocked"))
    cancel_job(host, job_id)
    assert [item["state"] for item in reconcile(host)] == ["discard_requested"]
    (control,) = controls_of(host, admission.input_id)
    evidence = {"reason": "job_cancelled", "job_id": job_id, "job_state": "cancelled", "requested_by": WORKER,
                "job_error": {"code": "cancelled", "message": "Owner stopped this job", "retryable": False}}
    assert (control["intent"], control["status"], control["requested_by"]) == ("discard", "pending", WORKER)
    assert control["result_json"] == {"request_evidence": evidence}                    # frozen when it is created, not invented later
    assert reconcile(host) == [] and controls_of(host, admission.input_id) == [control]          # a repeat adds nothing
    assert receipt_of(host, admission.filing_id) is None
    drive(host, "record_discarded", admission.input_id, result={"context_event_id": "bb12c", "context": "draft_discarded", "mechanical": True})
    expected = {"context_event_id": "bb12c", "context": "draft_discarded", "mechanical": True, "outcome": "discarded",
                "request_evidence": evidence, "control_id": control["id"]}
    (resolved,) = controls_of(host, admission.input_id)
    row = host.query("SELECT result_json FROM context_inputs WHERE id=?", admission.input_id)[0]
    stored = receipt_of(host, admission.filing_id)
    assert resolved["status"] == "applied" and resolved["result_json"] == expected                  # the same object and control ID
    assert row["result_json"] == expected and stored["reason_json"] == expected and stored["outcome"] == "discarded"
    with host.database.read() as uow:
        shown = completion.describe(uow, admission.filing_id, current_digest=admission.digest)
    assert shown["state"] == "discarded" and shown["error"] == {"code": "discarded", "message": "job_cancelled"}
    assert reconcile(host) == [] and receipt_of(host, admission.filing_id) == stored


def test_publication_winning_a_requested_cancellation_keeps_the_evidence_on_the_rejected_control(host):
    admission, job_id = blocked_with_event(host, source("Blocked"))
    cancel_job(host, job_id)
    reconcile(host)
    (pending,) = controls_of(host, admission.input_id)
    drive(host, "record_published", admission.input_id, ordubashy_id=COORDINATOR,
          result={"context_event_id": "bb12c", "published_commit": "c0ffee"}, handoff_note={"disposition": "empty", "text": ""})
    (rejected,) = controls_of(host, admission.input_id)
    row = host.query("SELECT state, result_json FROM context_inputs WHERE id=?", admission.input_id)[0]
    assert rejected["status"] == "rejected" and rejected["result_json"] == {
        "outcome": "already_published", "context_event_id": "bb12c", "control_id": pending["id"],
        "request_evidence": pending["result_json"]["request_evidence"]}
    assert row["state"] == "published" and "request_evidence" not in row["result_json"]            # the input stays honestly published
    stored = receipt_of(host, admission.filing_id)
    assert stored["outcome"] == "published" and stored["published_commit"] == "c0ffee" and stored["reason_json"] is None
    assert reconcile(host) == [] and receipt_of(host, admission.filing_id) == stored


def test_cancellation_supersedes_a_pending_retry_before_any_new_attempt(host):
    admission, job_id = blocked_with_event(host, source("Blocked"))
    host.data("context_queue_retry", {"input_id": admission.input_id, "expected_state": "blocked"}, host.agent())
    (retry,) = controls_of(host, admission.input_id)
    assert (retry["intent"], retry["status"], retry["requested_by"]) == ("retry", "pending", AGENT)
    cancel_job(host, job_id)
    assert [item["state"] for item in reconcile(host)] == ["discard_requested"]
    rejected, discard = controls_of(host, admission.input_id)
    evidence = discard["result_json"]["request_evidence"]
    assert evidence["reason"] == "job_cancelled" and evidence["job_id"] == job_id and evidence["requested_by"] == WORKER
    assert (rejected["id"], rejected["intent"], rejected["status"], rejected["requested_by"]) == (retry["id"], "retry", "rejected", AGENT)
    assert rejected["result_json"] == {"outcome": "superseded_by_cancellation", "superseded_by": discard["id"], "request_evidence": evidence}
    assert (discard["intent"], discard["status"], discard["requested_by"]) == ("discard", "pending", WORKER)
    assert host.query("SELECT state, attempt FROM context_inputs WHERE id=?", admission.input_id) == [{"state": "discard_requested", "attempt": 1}]
    with host.database.transaction() as uow:                                          # no attempt can start for this input any more
        with pytest.raises(DomainError) as refused:
            transitions.start_attempt(uow, driver_id=WORKER, input_id=admission.input_id, context_event_id="cc12c")
    assert refused.value.code == "invalid_transition"
    assert reconcile(host) == [] and controls_of(host, admission.input_id) == [rejected, discard]       # repeated recovery is stable
    drive(host, "record_discarded", admission.input_id, result={"context_event_id": "bb12c", "context": "draft_discarded", "mechanical": True})
    stored = receipt_of(host, admission.filing_id)
    assert stored["outcome"] == "discarded" and stored["reason_json"]["request_evidence"] == evidence
    assert stored["reason_json"]["control_id"] == discard["id"]
    assert host.query("SELECT COUNT(*) AS n FROM context_input_attempts") == [{"n": 1}]               # still only the blocked attempt
    assert reconcile(host) == [] and receipt_of(host, admission.filing_id) == stored
