"""The shared factual saved-plan guard on every retained learning mutator.

The subject is the real ``PreparedFileExecutor`` path over a synthetic host: a saved plan that claims factual arguments
but carries a procedural or engine effect is refused before any prepared file is applied, and a compatible interrupted
factual intake still resumes and applies exactly once. Bookkeeping rows may change; prepared files, learning rows and
retained historical records must not.
"""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import (ChangeSet, DomainError, ExpectedTarget, InvocationOptions, PreparedChange,
                                         ResourceRef, canonical_hash, new_id, plain, utc_now)
from qorqut_operations.storage.executor import PreparedFileExecutor
from host_foundation_support import build_instance

BUSINESS = "first"
DELTA_ID = "5b1d0e0e-0000-4000-8000-0000000000d1"
CONTEXT_ID = "5b1d0e0e-0000-4000-8000-0000000000c1"
ARGUMENTS = {"learning_id": CONTEXT_ID, "state": "accepted"}


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=(BUSINESS,))
    runtime = instance.runtime
    (tmp_path / "instance/operations/projects" / BUSINESS / "hq").mkdir(parents=True, exist_ok=True)
    from bazarlyk.background.integration import attach_background
    attach_background(runtime, runtime.programs, model_client_factory=lambda _business: None,
                      transcript_source=lambda business_id, agent_id=None: iter(()))
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [instance.business])
    runtime.freeze_registry()
    now = utc_now()
    with instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (instance.business, CONTEXT_ID, json.dumps({"text": "Keep the launch date", "scope": "biz", "bucket": "context",
                                                        "tag": "decision", "run": "run-1", "evidence": {"session": "s-1"}}), now, now))
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (instance.business, DELTA_ID, json.dumps({"text": "Old procedural rule", "scope": "biz", "bucket": "delta",
                                                      "tag": "rule", "run": "run-1", "evidence": {"session": "s-2"}}), now, now))
        uow.connection.execute("INSERT INTO learning_runs(business_id,id,result_json,created_at) VALUES (?,?,?,?)",
                               (instance.business, "run-1",
                                json.dumps({"cores": {"s-1": {"markdown": "core"}, "s-2": {"markdown": "old"}}}), now))
    yield SimpleNamespace(instance=instance, database=instance.database, runtime=runtime, tmp=tmp_path,
                          owner=instance.human(instance.business))
    runtime.release_instance()


def invoke(host, arguments, key, *, revision=1):
    options = InvocationOptions(idempotency_key=key, **({"expected_revision": revision} if revision else {}))
    return host.runtime.application.invoke(host.owner, "learnings_review", arguments, options)


def receipt_rows(host):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute("SELECT * FROM apply_receipts ORDER BY created_at, id")]


def tables(host):
    """Every mutable/prepared row a refusal must leave alone (bookkeeping rows are the documented exception)."""
    with host.database.read() as uow:
        engine = ("learning_issues" if uow.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='learning_issues'").fetchone()[0] else None)
        rows = {
            "learning_candidates": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,candidate_json,revision,work_state,filing_json FROM learning_candidates ORDER BY id")],
            "learning_decisions": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,candidate_id,state,feedback,changes_json FROM learning_decisions ORDER BY candidate_id")],
            "learning_history": uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0],
            "documents": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,kind,revision FROM documents ORDER BY id")],
            "jobs": [tuple(row) for row in uow.connection.execute("SELECT business_id,id,kind,state FROM jobs ORDER BY id")],
            "context_filings": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,source_version_id,job_id,input_id FROM context_filings ORDER BY id")],
            "context_inputs": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,input_kind,operation FROM context_inputs ORDER BY id")],
            "context_source_versions": [tuple(row) for row in uow.connection.execute(
                "SELECT business_id,id,source_id,source_revision,operation FROM context_source_versions ORDER BY id")],
        }
        if engine:
            rows["engine_issues"] = uow.connection.execute("SELECT COUNT(*) FROM learning_issues").fetchone()[0]
        return rows


def hq(host):
    return host.instance.paths.business_hq(BUSINESS)


def file_tree(host):
    root = hq(host)
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")) if root.is_dir() else []


def interrupt(monkeypatch, host):
    """Fail the prepared-file apply once, leaving a real durable receipt the way an interrupted intake does."""
    applier = host.runtime.application.file_executor.file_applier

    def fail(*_args, **_kwargs):
        raise DomainError("operation_failed", "Synthetic interruption before any file was written", retryable=True)

    monkeypatch.setattr(applier, "apply", fail)


def resume(monkeypatch, host):
    """Restore the real applier; an interrupted compatible receipt begins exactly here."""
    monkeypatch.undo()


def bound_plan(host):
    """The engine-bound plan of the interrupted invocation, read back from its immutable durable receipt."""
    rows = receipt_rows(host)
    assert len(rows) == 1, rows
    return ChangeSet.from_bytes(rows[0]["change_set"], expected_hash=rows[0]["payload_hash"])


def insert_receipt(host, plan, key):
    """Insert a fabricated saved plan under its own idempotency key.

    Receipt bindings are immutable by trigger, so a forged plan is a fabricated receipt, exactly like a tampered store.
    The plan keeps the factual arguments (and therefore the same input hash); only its prepared effects differ.
    """
    app, options = host.runtime.application, InvocationOptions(idempotency_key=key, expected_revision=1)
    with host.database.read() as uow:
        context = app._options_context(app.current_context(host.owner, uow), options)
        namespace = app.namespace(context, app.registry.get(plan.operation_name), ARGUMENTS)
        bound = app.registry.bind_arguments(plan.operation_name, ARGUMENTS)
    input_hash = canonical_hash({"operation": plan.operation_name, "version": 1, "arguments": bound,
                                 "options": {**plain(options), "business_id": context.business_id,
                                             "review_business_id": context.review_business_id, "idempotency_key": None}})
    receipt_id = PreparedFileExecutor._id(context, namespace, plan.operation_name, options)
    now = utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,payload_hash,change_set,signoff_id,state,"
            "progress_json,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,NULL,'prepared',?,1,?,?)",
            (receipt_id, context.principal_id, namespace, plan.operation_name, plan.payload_hash, plan.canonical,
             json.dumps({"invocation": {"input_hash": input_hash, "idempotency_key": key, "options": plain(options)}}), now, now))
    return receipt_id


def candidate_change(host, identity, values, *, action="insert", exists=False):
    resource = ResourceRef("learning_candidates", identity, business_id=host.instance.business)
    return (PreparedChange("record", resource, action, values),
            ExpectedTarget(resource, exists=exists, revision=1 if exists else None))


def delta_values(host, **overrides):
    now = utc_now()
    return {"business_id": host.instance.business,
            "candidate_json": {"text": "Forged procedural delta", "scope": "biz", "bucket": "delta", "tag": "rule"},
            "created_at": now, "updated_at": now, **overrides}


def unbound_plan(host, changes):
    """A prepared plan whose effects alone are inspected: the direct guard does not need engine binding."""
    descriptor = host.runtime.registry.get("learnings_review")
    with host.database.read() as uow:
        real = descriptor.prepare(host.owner, {"learning_id": CONTEXT_ID, "state": "rejected"},
                                 InvocationOptions(idempotency_key=new_id(), expected_revision=1), uow)
    return replace(real, changes=tuple(changes), expected_targets=())


# --------------------------------------------------------------------------------------------------- forged plans

def test_a_forged_plan_with_a_procedural_insert_is_refused_before_any_file_effect(host, monkeypatch):
    before_files = file_tree(host)
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-interrupted").status == "error"
    plan = bound_plan(host)
    change, target = candidate_change(host, "forged-delta", delta_values(host))
    insert_receipt(host, replace(plan, changes=(*plan.changes, change), expected_targets=(*plan.expected_targets, target)),
                   "review-forged-insert")
    resume(monkeypatch, host)
    before = tables(host)
    refused = invoke(host, ARGUMENTS, "review-forged-insert")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert "procedural" in refused.error["message"] or "factual" in refused.error["message"]
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_candidates WHERE id='forged-delta'").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0
    assert [row["state"] for row in receipt_rows(host)] == ["failed", "failed"]


def test_a_forged_plan_that_mutates_a_historical_delta_is_refused(host, monkeypatch):
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-interrupted").status == "error"
    plan = bound_plan(host)
    with host.database.read() as uow:
        stored = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (DELTA_ID,)).fetchone()[0])
    change, target = candidate_change(host, DELTA_ID, {"candidate_json": {**stored, "text": "rewritten"},
                                                       "updated_at": utc_now()}, action="update", exists=True)
    insert_receipt(host, replace(plan, changes=(*plan.changes, change), expected_targets=(*plan.expected_targets, target)),
                   "review-forged-update")
    resume(monkeypatch, host)
    before = tables(host)
    refused = invoke(host, ARGUMENTS, "review-forged-update")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    with host.database.read() as uow:
        assert json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (DELTA_ID,)).fetchone()[0]) == stored
    assert tables(host) == before


def test_a_forged_plan_that_links_a_delta_into_a_factual_reconciliation_is_refused(host, monkeypatch):
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-interrupted").status == "error"
    plan = bound_plan(host)
    with host.database.transaction() as write:
        write.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (host.instance.business, "engine-target", json.dumps({"bucket": "context", "scope": "biz", "tag": "fact",
                                                                  "engine": {"issue_id": "x"}}), utc_now(), utc_now()))
    with host.database.read() as uow:
        stored = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0])
    linked = {**stored, "reconciliations": [{"kind": "duplicate", "reason": "forged", "automatic": True,
                                             "earlier": {"id": DELTA_ID}}]}
    change, target = candidate_change(host, CONTEXT_ID, {"candidate_json": linked, "updated_at": utc_now()},
                                      action="update", exists=True)
    # Replace the plan's own candidate effect with the forged link so this candidate has exactly one prepared change.
    def replaced(item):
        return item.resource.resource_type == "learning_candidates" and item.resource.resource_id == CONTEXT_ID

    changes = tuple(change if replaced(item) else item for item in plan.changes)
    targets = tuple(target if replaced(item) else item for item in plan.expected_targets)
    insert_receipt(host, replace(plan, changes=changes, expected_targets=targets), "review-forged-link")
    resume(monkeypatch, host)
    refused = invoke(host, ARGUMENTS, "review-forged-link")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    with host.database.read() as uow:
        assert json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0]) == stored
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 0


def test_the_original_saved_receipt_carrying_a_removed_table_effect_still_refuses(host, monkeypatch):
    """The exact removed-table case the review reproduced: a saved receipt whose effect names a retired engine table.

    The target has no resolver for that resource type, so the combined Application refuses the plan with ``invalid_plan``
    and the executor's own message, before any prepared file or retained row changes. No production resolver or
    compatibility adapter is expected for the removed tables.
    """
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-removed-table-warm").status == "error"
    plan = bound_plan(host)
    values = {"business_id": host.instance.business, "id": "removed-" + new_id(), "title": "Retired engine issue",
              "state": "open", "created_at": utc_now(), "updated_at": utc_now()}
    resource = ResourceRef("learning_issues", values["id"], business_id=host.instance.business)
    change = PreparedChange("record", resource, "insert", values)
    target = ExpectedTarget(resource, exists=False, revision=None)
    insert_receipt(host, replace(plan, changes=tuple([*plan.changes, change]),
                                 expected_targets=tuple([*plan.expected_targets, target])), "review-removed-table")
    resume(monkeypatch, host)
    before_files, before_rows = file_tree(host), tables(host)
    refused = invoke(host, ARGUMENTS, "review-removed-table")
    assert refused.status == "error" and refused.error["code"] == "invalid_plan", refused.to_dict()
    assert "no registered resolver" in refused.error["message"].lower(), refused.error["message"]
    assert file_tree(host) == before_files and tables(host) == before_rows
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 0


def test_a_forged_plan_with_an_engine_effect_is_refused(host, monkeypatch):
    """A saved plan may not smuggle a procedural engine projection into a factual candidate record.

    The seven removed engine tables have no live resolver after the schema transition, so their refusal is asserted on the
    public guard directly (see the direct-guard test below). Here the forged effect must still resolve for the executor, so
    it updates the factual candidate with the engine marker a retired projection used to carry.
    """
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-interrupted").status == "error"
    plan = bound_plan(host)
    with host.database.read() as uow:
        stored = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0])
    forged = {**stored, "engine": {"engine_id": "engine", "engine_scope_id": "global", "issue_id": "iss_" + new_id()}}
    change, target = candidate_change(host, CONTEXT_ID, {"candidate_json": forged, "updated_at": utc_now()},
                                      action="update", exists=True)

    def replaced(item):
        return item.resource.resource_type == "learning_candidates" and item.resource.resource_id == CONTEXT_ID

    changes = tuple(change if replaced(item) else item for item in plan.changes)
    targets = tuple(target if replaced(item) else item for item in plan.expected_targets)
    insert_receipt(host, replace(plan, changes=changes, expected_targets=targets), "review-forged-engine")
    resume(monkeypatch, host)
    before = tables(host)
    refused = invoke(host, ARGUMENTS, "review-forged-engine")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert "procedural" in refused.error["message"].lower() or "engine" in refused.error["message"].lower()
    assert tables(host) == before
    with host.database.read() as uow:
        assert json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0]) == stored


def test_a_stale_saved_plan_is_refused_when_its_effective_candidate_became_procedural(host, monkeypatch):
    now = utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO learning_decisions(business_id,candidate_id,state,changes_json,decided_by,"
                               "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                               (host.instance.business, CONTEXT_ID, "open", "{}", host.instance.owner, now, now))
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-stale-plan").status == "error"
    # Between preparation and resumption the retained decision now classifies this record as a historical delta, without
    # touching the candidate revision or any prepared target: only the effective-state guard can see it.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE learning_decisions SET changes_json=? WHERE candidate_id=?",
                               (json.dumps({"bucket": "delta"}), CONTEXT_ID))
    resume(monkeypatch, host)
    before_files = file_tree(host)
    refused = invoke(host, ARGUMENTS, "review-stale-plan")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 0
        assert uow.connection.execute("SELECT filing_json FROM learning_candidates WHERE id=?",
                                      (CONTEXT_ID,)).fetchone()[0] == "{}"
        assert json.loads(uow.connection.execute(
            "SELECT changes_json FROM learning_decisions WHERE candidate_id=?", (CONTEXT_ID,)).fetchone()[0]) == {"bucket": "delta"}


# -------------------------------------------------------------------------------------------- compatible resumption

def test_compatible_interrupted_factual_intake_resumes_and_applies_exactly_once(host, monkeypatch):
    with host.database.read() as uow:
        jobs = uow.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    interrupt(monkeypatch, host)
    first = invoke(host, ARGUMENTS, "review-resume", revision=1)
    assert first.status == "error" and first.error["code"] == "operation_failed", first.to_dict()
    (receipt,) = receipt_rows(host)
    assert receipt["state"] in {"prepared", "failed"}
    resume(monkeypatch, host)
    resumed = invoke(host, ARGUMENTS, "review-resume", revision=1)
    assert resumed.status == "ok", resumed.to_dict()
    assert resumed.data["item"]["state"] == "accepted" and resumed.data["item"]["filing"]["upload_id"]
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT state FROM learning_decisions WHERE candidate_id=?",
                                      (CONTEXT_ID,)).fetchone()[0] == "accepted"
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs + 1
        assert uow.connection.execute("SELECT COUNT(*) FROM documents WHERE kind='context_upload'").fetchone()[0] == 1
        upload = uow.connection.execute("SELECT path FROM documents WHERE kind='context_upload'").fetchone()[0]
    assert (hq(host) / upload).is_file() and "Keep the launch date" in (hq(host) / upload).read_text()
    # Already applied idempotency returns the saved result without a second intake, decision, history row or job.
    replay = invoke(host, ARGUMENTS, "review-resume", revision=1)
    assert replay.status == "ok" and replay.data == resumed.data
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_history").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs + 1


# --------------------------------------------------------------------------------------------------- direct guard

def test_the_public_factual_plan_guard_refuses_removed_engine_and_procedural_effects(host):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    with host.database.read() as uow:
        # A decision edit that would convert a factual record into a delta is refused on its effective state.
        converting = PreparedChange("record", ResourceRef("learning_decisions", CONTEXT_ID, business_id=host.instance.business),
                                    "insert", {"candidate_id": CONTEXT_ID, "state": "accepted", "changes_json": {"bucket": "delta"},
                                               "decided_by": host.instance.owner, "created_at": utc_now(), "updated_at": utc_now()})
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (converting,)), uow)
        assert refused.value.code == "forbidden"
        # A record effect on any removed engine table is refused as an engine write.
        for table in ("learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                      "learning_engine_packages", "learning_engine_enrollments", "learning_engine_state"):
            engine = PreparedChange("record", ResourceRef(table, new_id(), business_id=host.instance.business), "insert", {})
            with pytest.raises(DomainError) as refused:
                validate_factual_plan(host.owner, unbound_plan(host, (engine,)), uow)
            assert refused.value.code == "forbidden" and "removed procedural engine" in refused.value.message
        # A plan whose factual arguments carry a job effect outside its contract is refused too.
        job = PreparedChange("record", ResourceRef("jobs", new_id(), business_id=host.instance.business), "insert", {})
        plan = replace(unbound_plan(host, (job,)), operation_name="learnings_keep_both")
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, plan, uow)
        assert refused.value.code == "forbidden"
        # The real factual prepared plan of a retained mutator passes unchanged.
        real = host.runtime.registry.get("learnings_review").prepare(
            host.owner, {"learning_id": CONTEXT_ID, "state": "rejected"},
            InvocationOptions(idempotency_key=new_id(), expected_revision=1), uow)
        assert validate_factual_plan(host.owner, real, uow) is None


def test_every_retained_learning_mutator_is_registered_with_the_shared_guard(host):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    names = ("learnings_review", "learnings_edit", "learnings_decide", "learnings_keep_both",
             "learnings_record_batch", "learnings_extract")
    assert {name for name in names if host.runtime.registry.get(name).validate_targets is not None} == set(names)
    # Every retained mutator wraps the shared entry point with the current configured Context limit; the intake
    # mutators additionally compose the existing document-target validation.
    for name in names:
        assert host.runtime.registry.get(name).validate_targets is not None
    assert host.runtime.registry.get("learnings_review").validate_targets is not None

# ---------------------------------------------------------------------------------------- manager review corrections

def global_worker(host):
    with host.database.read() as uow:
        worker = host.runtime.authority.worker_id("learnings.extract_global", uow)
    from qorqut_operations.contracts import InvocationContext, Principal
    return InvocationContext(Principal(worker, "system"), source="worker")


def same_batch_arguments(host, old, *, relation_kind="reconciled", bucket="context"):
    """One real Global batch: a new factual item reconciled into an existing unresolved factual record."""
    core = {"markdown": "## T1  OWNER\n\nkeep invoices short\n", "header": {"biz": None},
            "evidence_blocks": {"T1": {"role": "owner", "text": "keep invoices short"}}}
    new, run = new_id(), new_id()
    relation = [{"automatic": True, "earlier": {"id": old}, "reason": "same fact", "kind": "duplicate",
                 "assessment": {}, "evidence": {"turns": ["T1"]}}]
    item = {"id": new, "bucket": bucket, "tag": "fact", "scope": "global", "scope_reason": "global fact",
            "text": "a factual item", "said_by": "owner", "from_correction": False, "confidence": 0.9,
            "evidence": {"session": "s", "turns": ["T1"], "owner_quote": "keep invoices short", "agent_quote": None},
            "reconciliations": relation}
    arguments = {"items": [item], "evidence": [{"of": old, "kind": relation_kind, "into": new, "revision": 1,
                                                "reconciliations": relation}],
                 "run": {"id": run, "cores": {"s": core}}}
    return arguments, new, run


def test_a_saved_plan_that_smuggles_a_procedural_link_through_decision_changes_is_refused(host, monkeypatch):
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, "review-interrupted").status == "error"
    plan = bound_plan(host)
    changed = []
    for change in plan.changes:
        if change.resource.resource_type == "learning_decisions":
            values = plain(change.values)
            values["changes_json"] = {"reconciliations": [{"earlier": {"id": DELTA_ID, "bucket": "delta"}}]}
            change = replace(change, values=values)
        changed.append(change)
    insert_receipt(host, replace(plan, changes=tuple(changed)), "review-forged-decision-link")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    refused = invoke(host, ARGUMENTS, "review-forged-decision-link")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions").fetchone()[0] == 0


def test_the_public_guard_refuses_nested_engine_jobs_deletes_and_history_bypasses(host):
    from bazarlyk.domains.learnings.validation import validate_factual_plan

    with host.database.transaction() as write:
        write.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (host.instance.business, "engine-target", json.dumps({"bucket": "context", "scope": "biz", "tag": "fact",
                                                                  "engine": {"issue_id": "x"}}), utc_now(), utc_now()))
    with host.database.read() as uow:
        stored = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (CONTEXT_ID,)).fetchone()[0])
        nested = {**stored, "reconciliations": [{"earlier": {"id": "embedded", "bucket": "context",
                                                            "reconciliations": [{"earlier": {"id": DELTA_ID,
                                                                                             "bucket": "delta"}}]}}]}
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (PreparedChange(
                "record", ResourceRef("learning_candidates", CONTEXT_ID, business_id=host.instance.business),
                "update", {"candidate_json": nested}),)), uow)
        assert refused.value.code == "forbidden" and "procedural" in refused.value.message
        converting = PreparedChange("record", ResourceRef("learning_decisions", CONTEXT_ID, business_id=host.instance.business),
                                    "insert", {"candidate_id": CONTEXT_ID, "state": "accepted",
                                               "changes_json": {"reconciliations": [{"earlier": {"id": DELTA_ID}}]}})
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (converting,)), uow)
        assert "unsupported change fields" in refused.value.message
        # A complete engine job is refused by its actual program identity, not only by a missing field.
        from host_foundation_support import owner_global_context
        global_ctx = owner_global_context(host.instance)
        now = utc_now()
        job = {"id": "engine-job", "business_id": None, "target_namespace": "global", "kind": "learnings.engine_extract",
               "principal_id": host.instance.owner, "initiated_by_principal_id": host.instance.owner,
               "arguments_json": {}, "definition_ref_json": {"id": "learnings.engine_extract"},
               "dedupe_key": "engine-job", "state": "queued"}
        engine_plan = unbound_plan(host, (PreparedChange("record", ResourceRef("jobs", "engine-job"), "insert", job),))
        engine_plan = replace(engine_plan, operation_name="learnings_extract")
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(global_ctx, engine_plan, uow)
        assert refused.value.code == "forbidden"
        # Unsupported deletes and a history row aimed at an existing delta are refused on their actual target.
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, replace(
                unbound_plan(host, (PreparedChange("record", ResourceRef("learning_runs", "run-1", business_id=host.instance.business),
                                                  "delete", {}),)), operation_name="learnings_record_batch"), uow)
        assert "delete" in refused.value.message
        history = {"id": new_id(), "candidate_id": DELTA_ID, "event": "learnings_review",
                   "actor_id": host.instance.owner, "before_json": {"bucket": "context"},
                   "after_json": {"bucket": "context", "state": "accepted"}, "created_at": utc_now()}
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (PreparedChange(
                "record", ResourceRef("learning_history", history["id"], business_id=host.instance.business),
                "insert", history),)), uow)
        assert "procedural" in refused.value.message
        # A currently engine-linked stored target is refused even when its outer candidate is factual.
        linked = {**stored, "reconciliations": [{"earlier": {"id": "engine-target", "bucket": "context"}}]}
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (PreparedChange(
                "record", ResourceRef("learning_candidates", CONTEXT_ID, business_id=host.instance.business),
                "update", {"candidate_json": linked}),)), uow)
        assert "engine-linked" in refused.value.message


def test_a_same_batch_factual_reconciliation_reaches_application(host):
    from host_foundation_support import owner_global_context
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    old, now = "9a0c1e00-0000-4000-8000-000000000001", utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (NULL,?,?,?,?)",
            (old, json.dumps({"text": "old global fact", "scope": "global", "bucket": "context", "tag": "fact"}), now, now))
    arguments, new, run = same_batch_arguments(host, old)
    owner = owner_global_context(host.instance)
    with host.database.read() as uow:
        plan = host.runtime.registry.get("learnings_record_batch").prepare(
            owner, arguments, InvocationOptions(idempotency_key=new_id()), uow)
        assert validate_factual_plan(owner, plan, uow) is None
    applied = host.runtime.application.invoke(global_worker(host), "learnings_record_batch", arguments,
                                              InvocationOptions(idempotency_key="batch-reconciliation"))
    assert applied.status == "ok", applied.to_dict()
    with host.database.read() as uow:
        restored = json.loads(uow.connection.execute(
            "SELECT candidate_json FROM learning_candidates WHERE id=?", (old,)).fetchone()[0])
        assert restored["reconciled_into"] == new and restored["bucket"] == "context"
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_candidates WHERE id=?", (new,)).fetchone()[0] == 1
        history = [dict(row) for row in uow.connection.execute(
            "SELECT candidate_id,event FROM learning_history WHERE candidate_id=?", (old,))]
        assert [(row["candidate_id"], row["event"]) for row in history] == [(old, "learnings_record_batch")]
        stored_run = json.loads(uow.connection.execute(
            "SELECT result_json FROM learning_runs WHERE id=?", (run,)).fetchone()[0])
        assert stored_run["id"] == run and "s" in stored_run["cores"]


def test_same_plan_procedural_targets_are_refused_and_factual_supersession_is_supported(host):
    from host_foundation_support import owner_global_context
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    old, now = "9a0c1e00-0000-4000-8000-000000000002", utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (NULL,?,?,?,?)",
            (old, json.dumps({"text": "old global fact", "scope": "global", "bucket": "context", "tag": "fact"}), now, now))
    arguments, new, _ = same_batch_arguments(host, old)
    owner = owner_global_context(host.instance)
    with host.database.read() as uow:
        plan = host.runtime.registry.get("learnings_record_batch").prepare(
            owner, arguments, InvocationOptions(idempotency_key=new_id()), uow)
        forged = []
        for change in plan.changes:
            if change.resource.resource_id == new and change.resource.resource_type == "learning_candidates":
                values = plain(change.values)
                values["candidate_json"] = {**values["candidate_json"], "bucket": "delta"}
                change = replace(change, values=values)
            forged.append(change)
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner, replace(plan, changes=tuple(forged)), uow)
        assert refused.value.code == "forbidden"
        # The complete plan itself is accepted; adding a same-plan factual supersession stays supported.
        assert validate_factual_plan(owner, plan, uow) is None
        superseded = []
        for change in plan.changes:
            if change.resource.resource_id == old and change.resource.resource_type == "learning_candidates":
                values = plain(change.values)
                values["candidate_json"] = {**values["candidate_json"], "superseded_by": new,
                                            "supersession": {"reason": "newer", "kind": "duplicate", "run": arguments["run"]["id"],
                                                             "replacement_id": new, "assessment": {}}}
                change = replace(change, values=values)
            superseded.append(change)
        superseded.append(PreparedChange("record", ResourceRef("learning_decisions", old, business_id=None), "insert", {
            "candidate_id": old, "state": "rejected", "feedback": "Superseded", "changes_json": {},
            "decided_by": owner.principal_id}))
        assert validate_factual_plan(owner, replace(plan, changes=tuple(superseded)), uow) is None


def test_a_real_extract_plan_with_its_job_passes_the_shared_guard(host):
    from host_foundation_support import owner_global_context
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    owner = owner_global_context(host.instance)
    with host.database.read() as uow:
        plan = host.runtime.registry.get("learnings_extract").prepare(
            owner, {"max_usd": 1}, InvocationOptions(idempotency_key=new_id()), uow)
        assert [change.resource.resource_type for change in plan.changes] == ["jobs"]
        assert validate_factual_plan(owner, plan, uow) is None


# --------------------------------------------------------------------------------------- round 2 review corrections

def interrupted_plan(host, monkeypatch, key):
    interrupt(monkeypatch, host)
    assert invoke(host, ARGUMENTS, key).status == "error"
    return bound_plan(host)


def test_a_saved_decision_cannot_be_written_under_a_different_candidate_key(host, monkeypatch):
    plan = interrupted_plan(host, monkeypatch, "round2-decision-key-interruption")
    changed = []
    for change in plan.changes:
        if change.resource.resource_type == "learning_decisions":
            values = plain(change.values)
            values["candidate_id"] = DELTA_ID
            change = replace(change, values=values)
        changed.append(change)
    insert_receipt(host, replace(plan, changes=tuple(changed)), "round2-decision-key")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    refused = invoke(host, ARGUMENTS, "round2-decision-key")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.repository("learning_decisions", business_id=host.instance.business).get(DELTA_ID) is None


def test_a_saved_auxiliary_filing_action_is_refused_before_any_file_or_domain_write(host, monkeypatch):
    plan = interrupted_plan(host, monkeypatch, "round2-filing-action-interruption")
    changed = [replace(change, action="unsupported-action")
               if change.kind == "record" and change.resource.resource_type == "context_filings" else change
               for change in plan.changes]
    insert_receipt(host, replace(plan, changes=tuple(changed)), "round2-filing-action")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    refused = invoke(host, ARGUMENTS, "round2-filing-action")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM context_filings").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_a_saved_plan_without_its_filing_association_is_refused_before_files(host, monkeypatch):
    plan = interrupted_plan(host, monkeypatch, "round2-unbound-filing-interruption")
    changed = [change for change in plan.changes
               if not (change.kind == "record" and change.resource.resource_type == "context_filings")]
    insert_receipt(host, replace(plan, changes=tuple(changed)), "round2-unbound-filing")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    refused = invoke(host, ARGUMENTS, "round2-unbound-filing")
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM context_inputs").fetchone()[0] == 0


def test_duplicate_effects_and_every_stored_table_key_are_bound_to_the_declared_resource(host):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    with host.database.read() as uow:
        fact = plain(uow.repository("learning_candidates", business_id=host.instance.business).get(CONTEXT_ID)["candidate_json"])
        duplicate = (PreparedChange("record", ResourceRef("learning_candidates", CONTEXT_ID,
                                                          business_id=host.instance.business), "update",
                                    {"candidate_json": {**fact, "bucket": "delta"}}),
                     PreparedChange("record", ResourceRef("learning_candidates", CONTEXT_ID,
                                                          business_id=host.instance.business), "update",
                                    {"candidate_json": fact}))
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, duplicate), uow)
        assert "same stored record" in refused.value.message
        for operation, table, resource_id, values in (
                ("learnings_keep_both", "learning_candidates", "declared-candidate", {"id": "forged-candidate"}),
                ("learnings_review", "learning_decisions", "declared-decision", {"candidate_id": "forged-decision"}),
                ("learnings_record_batch", "learning_runs", "declared-run", {"id": "forged-run"}),
                ("learnings_review", "learning_history", "declared-history", {"id": "forged-history"}),
                ("learnings_record_batch", "learning_scan_state", "declared-session", {"session_id": "forged-session"}),
                ("learnings_review", "documents", "declared-document", {"id": "forged-document"}),
                ("learnings_extract", "jobs", "declared-job", {"id": "forged-job"})):
            resource = ResourceRef(table, resource_id, business_id=host.instance.business)
            change = PreparedChange("record", resource, "insert", values)
            with pytest.raises(DomainError) as refused:
                validate_factual_plan(host.owner, replace(unbound_plan(host, (change,)), operation_name=operation), uow)
            assert "names another stored record" in refused.value.message, (table, refused.value.message)
    for operation, table, key, resource_id in (
            ("learnings_review", "learning_candidates", "id", CONTEXT_ID),
            ("learnings_review", "learning_decisions", "candidate_id", CONTEXT_ID),
            ("learnings_record_batch", "learning_runs", "id", "run-1"),
            ("learnings_review", "learning_history", "id", "history-1"),
            ("learnings_record_batch", "learning_scan_state", "session_id", "session-1"),
            ("learnings_review", "documents", "id", "document-1"),
            ("learnings_extract", "jobs", "id", "job-1")):
        resource = ResourceRef(table, resource_id, business_id=host.instance.business)
        change = PreparedChange("record", resource, "update", {key: resource_id})
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, replace(unbound_plan(host, (change,)), operation_name=operation), uow)
        assert "may not mutate its primary key" in refused.value.message, (table, refused.value.message)
    with host.database.read() as uow:
        scope_change = PreparedChange("record", ResourceRef("documents", "document-1",
                                                           business_id=host.instance.business), "update",
                                      {"business_id": host.instance.business})
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (scope_change,)), uow)
        assert "may not mutate its stored scope" in refused.value.message
    # A direct unbound filing effect is refused too, not accepted by table membership.
    with host.database.read() as uow:
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, unbound_plan(host, (PreparedChange(
                "record", ResourceRef("context_filings", "unknown-filing", business_id=host.instance.business),
                "unsupported-action", {"unexpected": "value"}),)), uow)
        assert "never prepares the action unsupported-action" in refused.value.message


# --------------------------------------------------------------------------------------- round 3 review corrections

def test_a_context_job_must_name_its_exact_filing_before_files(host, monkeypatch):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round3-job-binding-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control before mutation
    state_changed = []
    for change in plan.changes:
        if change.kind == "record" and change.resource.resource_type == "jobs":
            values = plain(change.values)
            values["state"] = "applied"
            change = replace(change, values=values)
        state_changed.append(change)
    with host.database.read() as uow:
        assert "queued filing job" in _guard_refusal(validate_factual_plan, host,
                                              replace(plan, changes=tuple(state_changed)), uow)
    changed = []
    for change in plan.changes:
        if change.kind == "record" and change.resource.resource_type == "jobs":
            values = plain(change.values)
            values["arguments_json"] = {"filing_id": "00000000-0000-4000-8000-000000000099"}
            change = replace(change, values=values)
        changed.append(change)
    with host.database.read() as uow:
        refused = _guard_refusal(validate_factual_plan, host, replace(plan, changes=tuple(changed)), uow)
    assert "not bound to the exact filing" in refused
    insert_receipt(host, replace(plan, changes=tuple(changed)), "round3-job-binding")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    applied = invoke(host, ARGUMENTS, "round3-job-binding")
    assert applied.status == "error" and applied.error["code"] == "forbidden", applied.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM context_filings").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_a_queued_context_input_must_carry_the_exact_accepted_snapshot(host, monkeypatch):
    from qorqut_context.integration.filing import envelope
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round3-source-content-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control before mutation
    original = next(plain(change.values) for change in plan.changes if change.kind == "record"
                    and change.resource.resource_type == "context_inputs")["payload_json"]["content"]
    metadata = envelope.parse(original)["metadata"]
    changed = []
    for change in plan.changes:
        if change.kind == "record" and change.resource.resource_type == "context_inputs":
            values = plain(change.values)
            values["payload_json"]["content"] = envelope.source_document("A different source.\n", metadata)
            change = replace(change, values=values)
        changed.append(change)
    with host.database.read() as uow:
        refused = _guard_refusal(validate_factual_plan, host, replace(plan, changes=tuple(changed)), uow)
    assert "not the exact accepted learning source" in refused
    digest_changed = []
    for change in plan.changes:
        if change.kind == "record" and change.resource.resource_type == "context_source_versions":
            values = plain(change.values)
            values["source_digest"] = "0" * 64
            change = replace(change, values=values)
        digest_changed.append(change)
    with host.database.read() as uow:
        assert "exact accepted source digest" in _guard_refusal(
            validate_factual_plan, host, replace(plan, changes=tuple(digest_changed)), uow)
    insert_receipt(host, replace(plan, changes=tuple(changed)), "round3-source-content")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    applied = invoke(host, ARGUMENTS, "round3-source-content")
    assert applied.status == "error" and applied.error["code"] == "forbidden", applied.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM context_source_versions").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM context_inputs").fetchone()[0] == 0


def test_an_unknown_record_table_is_a_deterministic_domain_refusal(host, monkeypatch):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round3-unknown-table-interruption")
    unrelated = PreparedChange("record", ResourceRef("tasks", "unknown-task", business_id=host.instance.business),
                               "update", {"title": "Unrelated effect"})
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control before mutation
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(host.owner, replace(plan, changes=(*plan.changes, unrelated)), uow)
    assert refused.value.code == "forbidden" and "outside the supported learning contract" in refused.value.message
    assert not isinstance(refused.value.__cause__, KeyError)


def guard_limit(host):
    """The actual configured limit of the synthetic composed Context, read fresh for each direct public-guard call."""
    return host.runtime.domains.documents.filing.ports.source_bytes


def _guard_refusal(validate, host, plan, uow, *, ctx=None):
    from qorqut_operations.contracts import DomainError as GuardError
    kwargs = {"source_bytes": guard_limit(host)} if any(
        change.kind == "record" and change.resource.resource_type in ("documents", "jobs", "context_source_versions",
                                                                     "context_filings", "context_inputs")
        for change in plan.changes) else {}
    with pytest.raises(GuardError) as refused:
        validate(ctx or host.owner, plan, uow, **kwargs)
    assert refused.value.code == "forbidden", refused.value.to_dict()
    return refused.value.message


def test_prepared_context_file_content_is_bound_to_the_document_and_learning_snapshot(host, monkeypatch):
    import base64
    import hashlib
    from qorqut_operations.documents.serialization import document_bytes
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round3-file-content-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control before mutation
    filing = next(plain(change.values)["filing_json"] for change in plan.changes if change.kind == "record"
                  and change.resource.resource_type == "learning_candidates")
    upload_id = filing["upload_id"]
    document = next(plain(change.values) for change in plan.changes if change.kind == "record"
                    and change.resource.resource_type == "documents" and change.resource.resource_id == upload_id)
    data = document_bytes(document["metadata_json"], "A different accepted body\n")
    changed = []
    for change in plan.changes:
        if change.kind == "file" and json.loads(change.resource.resource_id)["document_id"] == upload_id:
            values = plain(change.values)
            values.update(content_b64=base64.b64encode(data).decode("ascii"),
                          after_hash=hashlib.sha256(data).hexdigest())
            change = replace(change, values=values)
        elif change.kind == "record" and change.resource.resource_type == "documents" \
                and change.resource.resource_id == upload_id:
            values = plain(change.values)
            values["content_hash"] = hashlib.sha256(data).hexdigest()
            change = replace(change, values=values)
        changed.append(change)
    with host.database.read() as uow:
        refused = _guard_refusal(validate_factual_plan, host, replace(plan, changes=tuple(changed)), uow)
    assert "exact accepted learning snapshot" in refused or "exact registered serialization" in refused


def test_a_global_factual_acceptance_plan_passes_the_context_guard(host):
    from host_foundation_support import owner_global_context
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    identity, now = "9b0c1e00-0000-4000-8000-000000000001", utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (NULL,?,?,?,?)",
            (identity, json.dumps({"text": "Global fact", "scope": "global", "bucket": "context", "tag": "fact",
                                   "run": "run-1", "evidence": {"session": "s-1"}}), now, now))
    ctx = owner_global_context(host.instance)
    with host.database.read() as uow:
        plan = host.runtime.registry.get("learnings_review").prepare(
            ctx, {"learning_id": identity, "state": "accepted"},
            InvocationOptions(idempotency_key=new_id(), expected_revision=1), uow)
        assert validate_factual_plan(ctx, plan, uow, source_bytes=guard_limit(host)) is None
    kinds = [change.resource.resource_type for change in plan.changes if change.kind == "record"]
    assert "context_filings" in kinds and "context_source_versions" in kinds and "learning_decisions" in kinds
    from qorqut_context.integration.filing import envelope
    content = next(plain(change.values)["payload_json"]["content"] for change in plan.changes
                   if change.kind == "record" and change.resource.resource_type == "context_inputs")
    body = envelope.parse(content)["body"]
    assert 'scope="global"' in body and 'learning-id="' + identity + '"' in body


# --------------------------------------------------------------------------------------- round 4 review corrections

def _change(plan, table, **updates):
    changes = []
    for change in plan.changes:
        if change.kind == "record" and change.resource.resource_type == table:
            values = plain(change.values)
            values.update(updates)
            change = replace(change, values=values)
        changes.append(change)
    return replace(plan, changes=tuple(changes))


def test_source_version_lineage_and_creator_are_refused_before_files(host, monkeypatch):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round4-lineage-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control
        assert "first revision" in _guard_refusal(validate_factual_plan, host,
                                                  _change(plan, "context_source_versions", source_revision=2), uow)
        assert "source creator is not a non-empty string" in _guard_refusal(
            validate_factual_plan, host, _change(plan, "context_source_versions", created_by=None), uow)
    changed = _change(plan, "context_source_versions", source_revision=2)
    insert_receipt(host, changed, "round4-lineage")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    applied = invoke(host, ARGUMENTS, "round4-lineage")
    assert applied.status == "error" and applied.error["code"] == "forbidden", applied.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM context_source_versions").fetchone()[0] == 0


def test_context_records_must_be_ordered_job_version_filing_input_before_files(host, monkeypatch):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round4-order-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # valid control
    changes = list(plan.changes)
    job_position = next(index for index, change in enumerate(changes)
                        if change.kind == "record" and change.resource.resource_type == "jobs")
    filing_position = next(index for index, change in enumerate(changes)
                           if change.kind == "record" and change.resource.resource_type == "context_filings")
    changes[job_position], changes[filing_position] = changes[filing_position], changes[job_position]
    changed = replace(plan, changes=tuple(changes))
    with host.database.read() as uow:
        assert "unsupported record order" in _guard_refusal(validate_factual_plan, host, changed, uow)
    insert_receipt(host, changed, "round4-order")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    applied = invoke(host, ARGUMENTS, "round4-order")
    assert applied.status == "error" and applied.error["code"] == "forbidden", applied.to_dict()
    assert tables(host) == before and file_tree(host) == before_files


def test_context_required_values_attribution_shape_and_admission_limits(host, monkeypatch):
    from qorqut_context.integration.filing import envelope
    from qorqut_context.integration.queue.queue import general_arguments
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round4-shape-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None      # known-valid control

    def without(table, field):
        def mutate(candidate):
            changes = []
            for change in candidate.changes:
                if change.kind == "record" and change.resource.resource_type == table:
                    values = plain(change.values)
                    values.pop(field, None)
                    change = replace(change, values=values)
                changes.append(change)
            return replace(candidate, changes=tuple(changes))
        return mutate

    def nested(table, outer, field, value):
        def mutate(candidate):
            changes = []
            for change in candidate.changes:
                if change.kind == "record" and change.resource.resource_type == table:
                    values = plain(change.values)
                    values[outer] = {**values[outer], field: value}
                    change = replace(change, values=values)
                changes.append(change)
            return replace(candidate, changes=tuple(changes))
        return mutate

    cases = [
        ("missing document content hash", without("documents", "content_hash")(plan), "omits required fields"),
        ("missing filing request", without("context_filings", "request_id")(plan), "omits required fields"),
        ("missing input submitter", without("context_inputs", "submitted_by")(plan), "omits required fields"),
        ("source revision", _change(plan, "context_source_versions", source_revision=2), "first revision"),
        ("source creator", _change(plan, "context_source_versions", created_by="another-principal"), "another creator"),
        ("upload author", nested("documents", "metadata_json", "by", "another-principal")(plan), "authored by another"),
        ("filing initiator", _change(plan, "context_filings", initiated_by="another-principal"), "attributed caller"),
        ("job initiator", _change(plan, "jobs", initiated_by_principal_id="another-principal"), "attributed queued filing job"),
        ("input submitter", _change(plan, "context_inputs", submitted_by="another-principal"), "attributed accepted general source"),
        ("payload submitter", nested("context_inputs", "payload_json", "submitted_by", "another-principal")(plan), "attributed accepted source"),
        ("document availability", _change(plan, "documents", availability="pending"), "live file state"),
        ("document namespace", _change(plan, "documents", namespace="another-namespace"), "another scope or namespace"),
        ("document owner", _change(plan, "documents", owning_agent_id="another-agent"), "unsupported owning agent"),
        ("payload shape", nested("context_inputs", "payload_json", "extra", "unexpected")(plan), "unsupported shape"),
        ("source kind", _change(plan, "context_source_versions", source_kind="instruction"), "admit the accepted upload"),
    ]
    for label, changed, expected in cases:
        with host.database.read() as uow:
            message = _guard_refusal(validate_factual_plan, host, changed, uow)
        assert expected in message, (label, message)

    limit = guard_limit(host)
    assert type(limit) is int and limit > 0
    with pytest.raises(DomainError):
        envelope.check_size("x" * (limit + 1), limit)
    with pytest.raises(DomainError):
        general_arguments("add", "foundation/documents/oversized.txt", "x" * (limit + 1),
                          {"kind": "document", "title": "oversized", "origin": "upload"},
                          limit, controlled=True)


# --------------------------------------------------------------------------------------- round 5 review corrections

SECOND_ID = "5b1d0e0e-0000-4000-8000-0000000000c2"


def add_second_candidate(host, identity=SECOND_ID, text="Keep the second launch date"):
    now = utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO learning_candidates(business_id,id,candidate_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (host.instance.business, identity,
             json.dumps({"text": text, "scope": "biz", "bucket": "context", "tag": "decision",
                         "run": "run-1", "evidence": {"session": "s-1"}}), now, now))


def test_two_ordinary_factual_acceptances_update_the_existing_context_index(host):
    first = invoke(host, ARGUMENTS, "round5-first-acceptance")
    assert first.status == "ok", first.to_dict()
    add_second_candidate(host)
    second = invoke(host, {"learning_id": SECOND_ID, "state": "accepted"}, "round5-second-acceptance")
    assert second.status == "ok", second.to_dict()
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions WHERE state='accepted'").fetchone()[0] == 2
        assert uow.connection.execute("SELECT COUNT(*) FROM documents WHERE kind='context_index'").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM context_filings").fetchone()[0] == 2
    assert (hq(host) / "context/INDEX.md").is_file()


def test_an_interrupted_second_acceptance_updates_the_existing_index_once(host, monkeypatch):
    assert invoke(host, ARGUMENTS, "round5-first-acceptance-interrupted").status == "ok"
    add_second_candidate(host)
    interrupt(monkeypatch, host)
    first = invoke(host, {"learning_id": SECOND_ID, "state": "accepted"}, "round5-second-interrupted")
    assert first.status == "error" and first.error["code"] == "operation_failed", first.to_dict()
    assert sum(row["state"] != "applied" for row in receipt_rows(host)) == 1
    resume(monkeypatch, host)
    resumed = invoke(host, {"learning_id": SECOND_ID, "state": "accepted"}, "round5-second-interrupted")
    assert resumed.status == "ok", resumed.to_dict()
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM learning_decisions WHERE state='accepted'").fetchone()[0] == 2
        assert uow.connection.execute("SELECT COUNT(*) FROM documents WHERE kind='context_index'").fetchone()[0] == 1
        assert uow.connection.execute("SELECT COUNT(*) FROM documents WHERE kind='context_upload'").fetchone()[0] == 2
        assert uow.connection.execute("SELECT COUNT(*) FROM context_filings").fetchone()[0] == 2


def test_the_configured_context_limit_is_required_for_a_filing_and_binds_the_live_wrapper(host, monkeypatch):
    from dataclasses import replace as dataclass_replace
    from qorqut_context.integration.filing.filing import FilingPorts
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round5-limit-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None
        for invalid in (None, 0, -1, True, "128", 1.5):
            with pytest.raises(DomainError) as refused:
                validate_factual_plan(host.owner, plan, uow, source_bytes=invalid)
            assert "configured Context source limit" in refused.value.message, invalid
    # A non-filing plan keeps the original three-argument interface.
    with host.database.read() as uow:
        rejected = host.runtime.registry.get("learnings_review").prepare(
            host.owner, {"learning_id": CONTEXT_ID, "state": "rejected"},
            InvocationOptions(idempotency_key=new_id(), expected_revision=1), uow)
        assert validate_factual_plan(host.owner, rejected, uow) is None
    # A lower current configured limit refuses the already prepared source before files.
    filing = host.runtime.domains.documents.filing
    original = filing.ports
    try:
        filing.ports = dataclass_replace(original, source_bytes=128)
        resume(monkeypatch, host)
        before, before_files = tables(host), file_tree(host)
        refused = invoke(host, ARGUMENTS, "round5-limit-interruption")
        assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
        assert "configured Context source limit" in refused.error["message"] or "not admissible" in refused.error["message"]
        assert tables(host) == before and file_tree(host) == before_files
    finally:
        filing.ports = original


def test_a_valid_non_default_configured_limit_still_admits_an_intake(host):
    from dataclasses import replace as dataclass_replace
    filing = host.runtime.domains.documents.filing
    original = filing.ports
    try:
        filing.ports = dataclass_replace(original, source_bytes=4096)
        result = invoke(host, ARGUMENTS, "round5-non-default-limit")
        assert result.status == "ok", result.to_dict()
    finally:
        filing.ports = original
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM documents WHERE kind='context_upload'").fetchone()[0] == 1


def test_context_program_version_and_request_length_are_checked_before_files(host, monkeypatch):
    from bazarlyk.domains.learnings.validation import validate_factual_plan
    plan = interrupted_plan(host, monkeypatch, "round5-values-interruption")
    with host.database.read() as uow:
        assert validate_factual_plan(host.owner, plan, uow, source_bytes=guard_limit(host)) is None

    def version_zero(candidate):
        changes = []
        for change in candidate.changes:
            if change.kind == "record" and change.resource.resource_type == "context_filings":
                values = plain(change.values)
                values["program_version"] = 0
                change = replace(change, values=values)
            elif change.kind == "record" and change.resource.resource_type == "jobs":
                values = plain(change.values)
                values["definition_ref_json"] = {**values["definition_ref_json"], "version": 0}
                change = replace(change, values=values)
            changes.append(change)
        return replace(candidate, changes=tuple(changes))

    zero = version_zero(plan)
    with host.database.read() as uow:
        assert "positive integer" in _guard_refusal(validate_factual_plan, host, zero, uow)
        request_too_long = _change(plan, "context_filings", request_id="r" * 201)
        assert "longer than its stored contract" in _guard_refusal(validate_factual_plan, host, request_too_long, uow)
    insert_receipt(host, zero, "round5-values")
    resume(monkeypatch, host)
    before, before_files = tables(host), file_tree(host)
    applied = invoke(host, ARGUMENTS, "round5-values")
    assert applied.status == "error" and applied.error["code"] == "forbidden", applied.to_dict()
    assert tables(host) == before and file_tree(host) == before_files
