"""Retained embedded factual reconciliation snapshots over the real composed Global runtime.

A fresh same-run earlier item is deliberately removed from the batch and kept only as a complete copy inside its surviving
candidate (``reconciliations[].earlier``). The shared factual guard must accept exactly that supported representation,
resolving its frozen run/core from the existing storage or from the same plan's own run effect, and must still refuse an
unknown or bare reference, a stub, procedural or engine content, unsupported or mismatched evidence, conflicting copies and
any attempt to override stored or prepared state. The frozen core must be complete evidence: a retained transcript core with
its Markdown, or a structured contribution core with well-formed blocks and the same actual source on both sides; a missing,
null, malformed or half-structured core is a deterministic refusal, never a validation without its quote gate. The genuine
flows run through the real extraction job, Application and durable file executor with frozen model answers; the
adversarial cases hit the public guard and the real Application.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationContext, InvocationOptions, PreparedChange,
                                         Principal, ResourceRef, canonical_hash, new_id, plain, utc_now)
from qorqut_operations.storage.executor import PreparedFileExecutor

from bazarlyk.domains.learnings import analysis
from bazarlyk.domains.learnings.validation import validate_factual_plan
from host_foundation_support import owner_global_context
from learning_factual_support import candidates, composed, factual_item, invoke, ok, run_extraction, scans, stored_owner_note

QUESTION = "Which refund amount applies to Karthik's contract?"
ANSWER = "The refund amount is 40 percent of the paid contract value."
DELTA_ID = "5b1d0e0e-0000-4000-8000-0000000000d1"


def owner(instance):
    return owner_global_context(instance)


def queue(instance, **arguments):
    return list(ok(invoke(instance, owner(instance), "learnings_list", arguments))["items"])


def collapse(tmp_path, *, kind="answered", earlier=(QUESTION, "question"), later=(ANSWER, "fact")):
    """One genuine extraction run whose two same-day sources are collapsed by the dedup resolution into one survivor."""
    def resolve(messages):
        fresh = sorted(json.loads(messages[1]["content"])["fresh"], key=lambda item: item["source_at"])
        assert len(fresh) == 2, "both sources are fresh in this one run"
        return {"resolutions": [{"earlier": fresh[0]["id"], "later": fresh[1]["id"], "kind": kind,
                                 "reason": "The later note settles the earlier one."}]}

    instance, ids, model = composed(tmp_path, [{"items": [factual_item(earlier[0], tag=earlier[1])]},
                                               {"items": [factual_item(later[0], tag=later[1])]}, resolve])
    stored_owner_note(instance, ids["harbor"], earlier[0], "2026-09-10T09:00:00Z")
    stored_owner_note(instance, ids["harbor"], later[0], "2026-09-10T10:00:00Z")
    outcome = run_extraction(instance)
    assert outcome.get("state") == "succeeded", outcome
    return instance, ids, model


def read_rows(instance, sql, *parameters):
    with instance.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(sql, parameters)]


# ------------------------------------------------------------------------------------- genuine same-run collapse

@pytest.mark.parametrize("kind, earlier, later", [
    ("answered", (QUESTION, "question"), (ANSWER, "fact")),
    ("updated", ("The refund amount is 30 percent of the paid contract value.", "fact"),
     ("The refund amount is now 40 percent of the paid contract value.", "fact")),
    ("duplicate", ("Karthik's refund is 40 percent of the contract value.", "fact"), (ANSWER, "fact")),
])
def test_a_same_run_collapse_is_recorded_with_its_full_earlier_snapshot_and_a_repeat_adds_nothing(tmp_path, kind, earlier, later):
    instance, _ids, model = collapse(tmp_path, kind=kind, earlier=earlier, later=later)

    stored = candidates(instance)
    assert len(stored) == 1, "exactly the intended survivor is stored; the collapsed earlier is never its own row"
    survivor = json.loads(stored[0]["candidate_json"])
    assert survivor["text"] == later[0] and survivor["bucket"] == "context"
    relation, = survivor["reconciliations"]
    assert relation["kind"] == kind and relation["automatic"] is True
    snapshot = relation["earlier"]
    assert snapshot["text"] == earlier[0] and snapshot["tag"] == earlier[1] and snapshot["bucket"] == "context"
    assert len(snapshot["id"]) == 36 and snapshot["id"] != survivor["id"]
    assert not read_rows(instance, "SELECT 1 FROM learning_candidates WHERE id=?", snapshot["id"])

    (run_row,) = read_rows(instance, "SELECT result_json FROM learning_runs")
    run = json.loads(run_row[0])
    assert survivor["run"] == snapshot["run"] == snapshot["evidence"]["run"] == run["id"]
    assert set(run["cores"]) == {survivor["evidence"]["session"], snapshot["evidence"]["session"]}
    assert run["processed"] == 2 and run["status"] == "complete" and run["dedup"]["resolved"] == 1
    checkpoint = scans(instance)
    assert {row["session_id"] for row in checkpoint} == set(run["cores"]) and len(checkpoint) == 2

    # The unchanged sources are counted: a repeat reaches no model, stores no duplicate and leaves the checkpoints alone.
    calls = len(model.calls)
    assert run_extraction(instance)["state"] == "succeeded"
    assert len(model.calls) == calls and len(candidates(instance)) == 1 and scans(instance) == checkpoint


def test_keep_both_restores_the_same_run_earlier_item_with_its_identity_and_evidence(tmp_path):
    instance, _ids, _model = collapse(tmp_path)
    survivor, = queue(instance)
    embedded = survivor["reconciliations"][0]["earlier"]

    ok(invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": survivor["id"]}, revision=survivor["revision"]))
    restored = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": embedded["id"]}))["item"]
    assert restored["id"] == embedded["id"] and restored["text"] == embedded["text"] and restored["tag"] == "question"
    assert restored["evidence"] == embedded["evidence"] and restored["state"] == "open"
    assert restored["reconciliation_reviewed"] is True and not restored.get("reconciled_into")
    assert {item["id"] for item in queue(instance)} == {survivor["id"], embedded["id"]}
    assert [row[0] for row in read_rows(
        instance, "SELECT event FROM learning_history WHERE candidate_id=? ORDER BY created_at, rowid", embedded["id"])] == ["learnings_keep_both"]

    again = invoke(instance, owner(instance), "learnings_keep_both", {"learning_id": survivor["id"]}, revision=survivor["revision"] + 1)
    assert again.status == "error" and again.error["code"] == "invalid_transition", again.to_dict()
    ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": embedded["id"], "state": "rejected",
                                                              "feedback": "Answered already."}, revision=restored["revision"]))


def test_ordinary_factual_mutation_and_intake_of_a_collapsed_survivor_keep_its_embedded_snapshot(tmp_path):
    instance, _ids, _model = collapse(tmp_path)
    survivor, = queue(instance)
    embedded = survivor["reconciliations"][0]["earlier"]
    edited = ok(invoke(instance, owner(instance), "learnings_edit",
                       {"learning_id": survivor["id"], "changes": {"text": "The refund is 40 percent of the paid value."}},
                       revision=survivor["revision"]))["item"]
    rejected = ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": survivor["id"], "state": "rejected",
                                                                       "feedback": "Not yet."}, revision=edited["revision"]))["item"]
    reopened = ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": survivor["id"], "state": "open"},
                         revision=rejected["revision"]))["item"]
    accepted = ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": survivor["id"], "state": "accepted"},
                         revision=reopened["revision"]))["item"]
    assert accepted["state"] == "accepted" and accepted["filing"]["upload_id"]
    assert accepted["reconciliations"][0]["earlier"]["id"] == embedded["id"], "the embedded earlier copy is untouched"
    assert [row[0] for row in read_rows(instance, "SELECT event FROM learning_history WHERE candidate_id=? ORDER BY created_at, rowid",
                                        survivor["id"])] == ["learnings_edit", "learnings_review", "learnings_review", "learnings_review"]
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(1,)]
    assert not read_rows(instance, "SELECT 1 FROM learning_candidates WHERE id=?", embedded["id"])


def application_options(key, revision):
    return InvocationOptions(idempotency_key=key, expected_revision=revision)


def interrupt_files(monkeypatch, instance):
    applier = instance.runtime.application.file_executor.file_applier

    def fail(*_args, **_kwargs):
        raise DomainError("operation_failed", "Synthetic interruption before any file was written", retryable=True)

    monkeypatch.setattr(applier, "apply", fail)


def review_receipts(instance):
    return read_rows(instance, "SELECT id, state FROM apply_receipts WHERE operation_name='learnings_review' ORDER BY created_at, id")


def test_the_saved_acceptance_of_a_collapsed_survivor_resumes_once_without_duplicate_filing(tmp_path, monkeypatch):
    instance, _ids, _model = collapse(tmp_path)
    survivor, = queue(instance)
    app, context, arguments = instance.runtime.application, owner(instance), {"learning_id": survivor["id"], "state": "accepted"}
    jobs = read_rows(instance, "SELECT COUNT(*) FROM jobs")[0][0]

    interrupt_files(monkeypatch, instance)
    first = app.invoke(context, "learnings_review", arguments, application_options("accept-collapsed", survivor["revision"]))
    assert first.status == "error" and first.error["code"] == "operation_failed", first.to_dict()
    (receipt,) = review_receipts(instance)
    assert receipt[1] in {"prepared", "failed"}
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(0,)]

    monkeypatch.undo()
    resumed = app.invoke(context, "learnings_review", arguments, application_options("accept-collapsed", survivor["revision"]))
    assert resumed.status == "ok", resumed.to_dict()
    assert resumed.data["item"]["state"] == "accepted" and resumed.data["item"]["filing"]["upload_id"]
    assert resumed.data["item"]["reconciliations"][0]["earlier"]["id"] == survivor["reconciliations"][0]["earlier"]["id"]
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(1,)]
    assert read_rows(instance, "SELECT COUNT(*) FROM jobs")[0][0] == jobs + 1
    assert [row[0] for row in read_rows(instance, "SELECT event FROM learning_history WHERE candidate_id=?", survivor["id"])] == ["learnings_review"]

    replay = app.invoke(context, "learnings_review", arguments, application_options("accept-collapsed", survivor["revision"]))
    assert replay.status == "ok" and replay.data == resumed.data
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(1,)]
    assert read_rows(instance, "SELECT COUNT(*) FROM jobs")[0][0] == jobs + 1


def forge_receipt(instance, plan, key, arguments, revision):
    """Insert a fabricated saved plan under its own key; receipt bindings are immutable, so a forged plan is a fabricated receipt."""
    app, context = instance.runtime.application, owner(instance)
    options = application_options(key, revision)
    with instance.database.read() as uow:
        resolved = app._options_context(app.current_context(context, uow), options)
        namespace = app.namespace(resolved, app.registry.get(plan.operation_name), arguments)
        bound = app.registry.bind_arguments(plan.operation_name, arguments)
    input_hash = canonical_hash({"operation": plan.operation_name, "version": 1, "arguments": bound,
                                 "options": {**plain(options), "business_id": resolved.business_id,
                                             "review_business_id": resolved.review_business_id, "idempotency_key": None}})
    now = utc_now()
    with instance.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,payload_hash,change_set,signoff_id,state,"
            "progress_json,revision,created_at,updated_at) VALUES (?,?,?,?,?,?,NULL,'prepared',?,1,?,?)",
            (PreparedFileExecutor._id(resolved, namespace, plan.operation_name, options), resolved.principal_id, namespace,
             plan.operation_name, plan.payload_hash, plan.canonical,
             json.dumps({"invocation": {"input_hash": input_hash, "idempotency_key": key, "options": plain(options)}}), now, now))


def tamper_quote(snapshot):
    snapshot["evidence"]["owner_quote"] = "A sentence the owner never wrote."


def nest_delta(snapshot):
    snapshot["reconciliations"] = [{"kind": "duplicate", "reason": "forged", "automatic": True,
                                    "earlier": {"id": DELTA_ID, "bucket": "delta"}}]


def stub(snapshot):
    for key in set(snapshot) - {"id"}:
        del snapshot[key]


def other_source(snapshot):
    snapshot["evidence"]["source"] = {**snapshot["evidence"]["source"], "hash": "0" * 64}


def in_place(tamper):
    """Rewrite the stored survivor's own embedded copy: it can no longer agree with the stored copy of the same identity."""
    return lambda candidate: tamper(candidate["reconciliations"][0]["earlier"])


def added(tamper):
    """Append one more relation whose earlier copy is a clone with a new identity, altered by ``tamper``."""
    def change(candidate):
        forged = deepcopy(candidate["reconciliations"][0]["earlier"])
        forged["id"] = new_id()
        tamper(forged)
        candidate["reconciliations"].append({"kind": "duplicate", "reason": "forged", "automatic": True, "earlier": forged})
    return change


@pytest.mark.parametrize("tamper, fragment", [
    (in_place(tamper_quote), "conflicting copies"),
    (added(tamper_quote), "not supported by its frozen evidence"),
    (added(nest_delta), "procedural or engine"),
    (added(stub), "complete factual Context item"),
    (added(other_source), "another source"),
], ids=["rewritten-stored-copy", "added-unsupported-quote", "added-nested-delta", "added-stub", "added-other-source"])
def test_a_saved_acceptance_with_a_tampered_embedded_snapshot_is_refused_before_any_file_effect(tmp_path, monkeypatch, tamper, fragment):
    instance, _ids, _model = collapse(tmp_path)
    survivor, = queue(instance)
    arguments = {"learning_id": survivor["id"], "state": "accepted"}
    interrupt_files(monkeypatch, instance)
    assert instance.runtime.application.invoke(owner(instance), "learnings_review", arguments,
                                               application_options("accept-warm", survivor["revision"])).status == "error"
    (row,) = [row for row in read_rows(instance, "SELECT change_set, payload_hash FROM apply_receipts WHERE operation_name='learnings_review'")]
    plan = ChangeSet.from_bytes(row[0], expected_hash=row[1])

    def forged(change):
        if change.resource.resource_type != "learning_candidates" or change.resource.resource_id != survivor["id"]:
            return change
        values = plain(change.values)
        tamper(values["candidate_json"])
        return replace(change, values=values)

    forge_receipt(instance, replace(plan, changes=tuple(forged(change) for change in plan.changes)), "accept-forged", arguments,
                  survivor["revision"])
    monkeypatch.undo()
    before = (candidates(instance), read_rows(instance, "SELECT COUNT(*) FROM documents"), read_rows(instance, "SELECT COUNT(*) FROM jobs"),
              read_rows(instance, "SELECT COUNT(*) FROM learning_history"), read_rows(instance, "SELECT COUNT(*) FROM context_filings"))
    refused = instance.runtime.application.invoke(owner(instance), "learnings_review", arguments,
                                                  application_options("accept-forged", survivor["revision"]))
    assert refused.status == "error" and refused.error["code"] == "forbidden", refused.to_dict()
    assert fragment in refused.error["message"], refused.error["message"]
    assert before == (candidates(instance), read_rows(instance, "SELECT COUNT(*) FROM documents"),
                      read_rows(instance, "SELECT COUNT(*) FROM jobs"), read_rows(instance, "SELECT COUNT(*) FROM learning_history"),
                      read_rows(instance, "SELECT COUNT(*) FROM context_filings"))
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(0,)]


# ------------------------------------------------------------------------------- public guard and Application cases

def synthetic_core(instance, business, text, at):
    comment, task = new_id(), new_id()
    session = f"business:{business}:task_note:{comment}"
    source = {"actor_id": instance.owner, "actor_name": "Owner", "actor_role": "owner", "at": at, "business_id": business,
              "hash": hashlib.sha256(text.encode()).hexdigest(), "id": comment, "kind": "task_note", "resource_id": task}
    return session, {"context": {"business": {"id": business, "name": "Harbor", "slug": "harbor"}, "id": task,
                                 "preceding_notes": [], "status": "open", "title": "Contract follow-up"},
                     "evidence_blocks": {"T1": {"role": "owner", "text": text}},
                     "header": {"agent": None, "agent_kind": None, "biz": None, "session": session, "slug": "Global"},
                     "markdown": "## T1  OWNER\n\n" + text + "\n", "source": source, "source_key": session,
                     "stats": {"core_bytes": len(text), "owner_messages": 1, "turns": 1}}


def synthetic_item(text, tag, core, run):
    session = core["header"]["session"]
    return {"v": analysis.SCHEMA_V, "id": new_id(), "bucket": "context", "tag": tag, "scope": "global",
            "scope_reason": "A durable owner statement.", "text": text, "said_by": "owner", "from_correction": False,
            "confidence": 0.9, "votes": 1, "passes": 1, "variants": [],
            "evidence": {"transcript": None, "run": run, "source_at": core["source"]["at"], "source": core["source"],
                         "session": session, "turns": ["T1"], "owner_quote": text, "agent_quote": None},
            "observed": {"agent": None, "agent_kind": None, "biz": None}, "run": run, "model": "synthetic-model",
            "created": "2026-10-29T10:00:00Z"}


def batch_arguments(instance, business):
    """A genuinely shaped same-run batch: the survivor carries the full earlier copy; the run carries both frozen cores."""
    run = new_id()
    first_session, first = synthetic_core(instance, business, QUESTION, "2026-09-10T09:00:00Z")
    second_session, second = synthetic_core(instance, business, ANSWER, "2026-09-10T10:00:00Z")
    earlier, survivor = synthetic_item(QUESTION, "question", first, run), synthetic_item(ANSWER, "fact", second, run)
    survivor["reconciliations"] = [{"kind": "answered", "reason": "The later note answers the earlier question.",
                                    "earlier": deepcopy(earlier), "automatic": True}]
    return {"items": [survivor], "evidence": [], "processed": [],
            "run": {"id": run, "cores": {first_session: first, second_session: second}}}


def earlier_of(arguments):
    return arguments["items"][0]["reconciliations"][0]["earlier"]


def transcript_core(text, session, at):
    """A retained transcript core exactly as the historical builder makes it for a Global agent conversation."""
    line = {"type": "user", "uuid": new_id(), "timestamp": at, "sessionId": session, "message": {"content": text}}
    return analysis.build_core({"source_id": f"agents/ordubashy/sessions/{session}.jsonl", "session_id": session,
                                "content": json.dumps(line)}, SimpleNamespace(id=None, slug="Global"), "agent-1", "ordubashy")


def transcript_item(text, tag, core, run):
    session = core["header"]["session"]
    return {"v": analysis.SCHEMA_V, "id": new_id(), "bucket": "context", "tag": tag, "scope": "global",
            "scope_reason": "A durable owner statement.", "text": text, "said_by": "owner", "from_correction": False,
            "confidence": 0.9, "votes": 1, "passes": 1, "variants": [],
            "evidence": {"transcript": core["header"]["transcript"], "run": run, "source_at": core["turns"][0]["ts"],
                         "session": session, "turns": ["T1"], "owner_quote": text, "agent_quote": None},
            "observed": {"agent": "agent-1", "agent_kind": "ordubashy", "biz": None}, "run": run, "model": "synthetic-model",
            "created": "2026-10-29T10:00:00Z"}


def transcript_batch(instance, business):
    """The same genuinely shaped same-run batch, but both frozen cores are retained transcript cores."""
    run = new_id()
    first = transcript_core(QUESTION, "transcript-" + new_id(), "2026-09-10T09:00:00Z")
    second = transcript_core(ANSWER, "transcript-" + new_id(), "2026-09-10T10:00:00Z")
    earlier, survivor = transcript_item(QUESTION, "question", first, run), transcript_item(ANSWER, "fact", second, run)
    survivor["reconciliations"] = [{"kind": "answered", "reason": "The later note answers the earlier question.",
                                    "earlier": deepcopy(earlier), "automatic": True}]
    return {"items": [survivor], "evidence": [], "processed": [],
            "run": {"id": run, "cores": {first["header"]["session"]: first, second["header"]["session"]: second}}}


def global_worker(instance):
    with instance.database.read() as uow:
        worker = instance.runtime.authority.worker_id("learnings.extract_global", uow)
    return InvocationContext(Principal(worker, "system"), source="worker")


def prepare_batch(instance, arguments, uow):
    return instance.runtime.registry.get("learnings_record_batch").prepare(
        owner(instance), arguments, InvocationOptions(idempotency_key=new_id()), uow)


def domain_rows(instance):
    return {table: read_rows(instance, f"SELECT COUNT(*) FROM {table}") for table in
            ("learning_candidates", "learning_decisions", "learning_history", "learning_runs", "learning_scan_state", "jobs")}


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    instance, ids, _model = composed(tmp_path_factory.mktemp("embedded"), [])
    return instance, ids["harbor"]


def test_the_public_guard_and_application_accept_a_genuine_same_run_batch_and_later_review(shared):
    instance, business = shared
    arguments = batch_arguments(instance, business)
    with instance.database.read() as uow:
        assert validate_factual_plan(owner(instance), prepare_batch(instance, arguments, uow), uow) is None
    applied = instance.runtime.application.invoke(global_worker(instance), "learnings_record_batch", arguments,
                                                  InvocationOptions(idempotency_key=new_id()))
    assert applied.status == "ok", applied.to_dict()
    survivor_id, earlier = arguments["items"][0]["id"], earlier_of(arguments)
    assert not read_rows(instance, "SELECT 1 FROM learning_candidates WHERE id=?", earlier["id"])
    # A later ordinary review of the stored survivor resolves the embedded earlier through the stored run.
    row = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": survivor_id}))["item"]
    reviewed = invoke(instance, owner(instance), "learnings_review", {"learning_id": survivor_id, "state": "rejected",
                                                                     "feedback": "Not now."}, revision=row["revision"])
    assert reviewed.status == "ok", reviewed.to_dict()


def mutate(function):
    return lambda arguments: function(earlier_of(arguments))


def conflicting_copy(arguments):
    relations = arguments["items"][0]["reconciliations"]
    other = deepcopy(relations[0])
    other["earlier"]["text"] = "Another wording of the same earlier note."
    other["kind"] = "duplicate"
    relations.append(other)


def bare_replacement(arguments):
    arguments["items"][0]["superseded_by"] = earlier_of(arguments)["id"]


def stub_of_known(arguments):
    arguments["items"][0]["reconciliations"][0]["earlier"] = {"id": earlier_of(arguments)["id"]}


def unknown_stub(arguments):
    arguments["items"][0]["reconciliations"][0]["earlier"] = {"id": new_id()}


def unknown_bare_link(arguments):
    arguments["items"][0]["reconciled_into"] = new_id()


def nested_unknown_stub(arguments):
    earlier_of(arguments)["reconciliations"] = [{"kind": "duplicate", "reason": "x", "automatic": True, "earlier": {"id": new_id()}}]


def different_run(snapshot):
    snapshot["run"] = snapshot["evidence"]["run"] = new_id()


def conflicting_runs(snapshot):
    snapshot["evidence"]["run"] = new_id()


def unknown_session(snapshot):
    snapshot["evidence"]["session"] = "business:x:task_note:" + new_id()


def unknown_turn(snapshot):
    snapshot["evidence"]["turns"] = ["T9"]


def business_scope(snapshot):
    snapshot["scope"] = "biz"


def engine_marker(snapshot):
    snapshot["observed"]["engine_id"] = "engine"


def missing_bucket(snapshot):
    del snapshot["bucket"]


def procedural_bucket(snapshot):
    snapshot["bucket"] = "delta"


def no_evidence(snapshot):
    snapshot["evidence"] = None


ADVERSARIAL = {
    "stub of the embedded identity": (stub_of_known, "complete factual Context item"),
    "unknown id-only stub": (unknown_stub, "complete factual Context item"),
    "unknown bare reconciled_into": (unknown_bare_link, "unknown learning record"),
    "bare link to an embedded identity": (bare_replacement, "unknown learning record"),
    "nested unknown stub": (nested_unknown_stub, "complete factual Context item"),
    "conflicting copies of one identity": (conflicting_copy, "conflicting copies"),
    "missing bucket": (mutate(missing_bucket), "complete factual Context item"),
    "procedural bucket": (mutate(procedural_bucket), "procedural or engine"),
    "nested procedural reference": (mutate(nest_delta), "procedural or engine"),
    "engine marker": (mutate(engine_marker), "engine projection"),
    "another scope": (mutate(business_scope), "another scope"),
    "no evidence": (mutate(no_evidence), "no evidence"),
    "quote outside its core": (mutate(tamper_quote), "not supported by its frozen evidence"),
    "turn outside its core": (mutate(unknown_turn), "not supported by its frozen evidence"),
    "run neither stored nor prepared": (mutate(different_run), "neither stored nor prepared"),
    "two different runs": (mutate(conflicting_runs), "two different runs"),
    "session without a frozen core": (mutate(unknown_session), "no frozen extraction core"),
    "another source than its core": (mutate(other_source), "another source"),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_an_unsupported_embedded_reference_is_refused_by_the_public_guard_and_the_application_before_any_effect(shared, name):
    instance, business = shared
    change, fragment = ADVERSARIAL[name]
    arguments = batch_arguments(instance, business)
    change(arguments)
    with instance.database.read() as uow:
        plan = prepare_batch(instance, arguments, uow)
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner(instance), plan, uow)
    assert refused.value.code == "forbidden" and fragment in refused.value.message, refused.value.message
    before = domain_rows(instance)
    result = instance.runtime.application.invoke(global_worker(instance), "learnings_record_batch", arguments,
                                                 InvocationOptions(idempotency_key=new_id()))
    assert result.status == "error" and result.error["code"] == "forbidden" and fragment in result.error["message"], result.to_dict()
    assert domain_rows(instance) == before


def test_a_factual_looking_copy_never_overrides_a_stored_historical_delta(shared):
    instance, business = shared
    arguments = batch_arguments(instance, business)
    identity = earlier_of(arguments)["id"]
    with instance.database.transaction() as uow:
        uow.repository("learning_candidates", business_id=None).insert({"id": identity, "candidate_json": {
            "bucket": "delta", "tag": "rule", "scope": "global", "text": "Old procedural rule", "run": "run-1",
            "evidence": {"session": "s-1"}}})
    with instance.database.read() as uow:
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner(instance), prepare_batch(instance, arguments, uow), uow)
    assert refused.value.code == "forbidden" and "historical procedural" in refused.value.message, refused.value.message
    before = domain_rows(instance)
    result = instance.runtime.application.invoke(global_worker(instance), "learnings_record_batch", arguments,
                                                 InvocationOptions(idempotency_key=new_id()))
    assert result.status == "error" and result.error["code"] == "forbidden", result.to_dict()
    assert domain_rows(instance) == before


def test_a_prepared_effect_for_an_embedded_identity_stays_authoritative(shared):
    instance, business = shared
    arguments = batch_arguments(instance, business)
    identity = earlier_of(arguments)["id"]
    with instance.database.read() as uow:
        plan = prepare_batch(instance, arguments, uow)
        delta = PreparedChange("record", ResourceRef("learning_candidates", identity), "insert", {
            "id": identity, "candidate_json": {"bucket": "delta", "tag": "rule", "scope": "global", "text": "Forged"}})
        with pytest.raises(DomainError) as procedural:
            validate_factual_plan(owner(instance), replace(plan, changes=(*plan.changes, delta)), uow)
        decision = PreparedChange("record", ResourceRef("learning_decisions", identity), "insert", {
            "candidate_id": identity, "state": "accepted", "feedback": None, "changes_json": {}, "decided_by": instance.owner})
        with pytest.raises(DomainError) as orphan:
            validate_factual_plan(owner(instance), replace(plan, changes=(*plan.changes, decision)), uow)
    assert procedural.value.code == "forbidden" and orphan.value.code == "forbidden"
    assert "unknown learning record" in orphan.value.message or "no resolvable candidate" in orphan.value.message, orphan.value.message


def test_a_prepared_same_plan_run_is_the_only_unstored_source_the_guard_accepts(shared):
    instance, business = shared
    arguments = batch_arguments(instance, business)
    with instance.database.read() as uow:
        plan = prepare_batch(instance, arguments, uow)
        without_run = replace(plan, changes=tuple(change for change in plan.changes
                                                  if change.resource.resource_type != "learning_runs"))
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner(instance), without_run, uow)
    assert "neither stored nor prepared" in refused.value.message, refused.value.message


# ------------------------------------------------------------------ complete frozen evidence is required of every core

def earlier_core(arguments):
    return arguments["run"]["cores"][earlier_of(arguments)["evidence"]["session"]]


def core_change(function):
    """Alter the earlier item's frozen core and its embedded copy together: ``function(core, snapshot)``."""
    return lambda arguments: function(earlier_core(arguments), earlier_of(arguments))


def header_only(core, snapshot):
    """The Manager's reproduction: only the correct header survives and the quote is one no frozen evidence contains."""
    header = core["header"]
    core.clear()
    core["header"] = header
    tamper_quote(snapshot)


def header_only_genuine_quote(core, _snapshot):
    header = core["header"]
    core.clear()
    core["header"] = header


def blocks_null(core, _snapshot):
    core["evidence_blocks"] = None


def blocks_empty(core, _snapshot):
    core["evidence_blocks"] = {}


def blocks_list(core, _snapshot):
    core["evidence_blocks"] = [{"role": "owner", "text": QUESTION}]


def block_missing_role(core, _snapshot):
    del core["evidence_blocks"]["T1"]["role"]


def block_missing_text(core, _snapshot):
    del core["evidence_blocks"]["T1"]["text"]


def block_text_number(core, _snapshot):
    core["evidence_blocks"]["T1"]["text"] = 5


def block_role_list(core, _snapshot):
    core["evidence_blocks"]["T1"]["role"] = ["owner"]


def block_not_object(core, _snapshot):
    core["evidence_blocks"]["T1"] = QUESTION


def source_absent_both(core, snapshot):
    del core["source"]
    del snapshot["evidence"]["source"]


def source_null_both(core, snapshot):
    core["source"] = snapshot["evidence"]["source"] = None


def source_empty_both(core, snapshot):
    core["source"] = snapshot["evidence"]["source"] = {}


def source_without_identity(core, snapshot):
    core["source"] = snapshot["evidence"]["source"] = {"hash": "0" * 64, "at": "2026-09-10T09:00:00Z"}


def source_only_in_core(core, snapshot):
    del snapshot["evidence"]["source"]


def source_only_in_item(core, _snapshot):
    del core["source"]


def downgraded_structure(core, _snapshot):
    """A contribution core stripped of its blocks must not fall through to the transcript path."""
    del core["evidence_blocks"]


def header_missing(core, _snapshot):
    del core["header"]


def header_text(core, _snapshot):
    core["header"] = "header"


def header_other_session(core, _snapshot):
    core["header"]["session"] = "business:x:task_note:" + new_id()


STRUCTURED_CORES = {
    "header only with a fabricated quote": (header_only, "incomplete structured source"),
    "header only with the genuine quote": (header_only_genuine_quote, "incomplete structured source"),
    "blocks null": (blocks_null, "malformed contribution blocks"),
    "blocks empty": (blocks_empty, "malformed contribution blocks"),
    "blocks a list": (blocks_list, "malformed contribution blocks"),
    "block without a role": (block_missing_role, "malformed contribution blocks"),
    "block without text": (block_missing_text, "malformed contribution blocks"),
    "block text a number": (block_text_number, "malformed contribution blocks"),
    "block role a list": (block_role_list, "malformed contribution blocks"),
    "block a bare string": (block_not_object, "malformed contribution blocks"),
    "source absent in the core and the item": (source_absent_both, "no actual source"),
    "source null in the core and the item": (source_null_both, "no actual source"),
    "source empty in the core and the item": (source_empty_both, "no actual source"),
    "source without an identity in both": (source_without_identity, "no actual source"),
    "source only in the core": (source_only_in_core, "another source"),
    "source only in the item": (source_only_in_item, "no actual source"),
    "blocks removed from a structured core": (downgraded_structure, "incomplete structured source"),
    "header missing": (header_missing, "another session"),
    "header not an object": (header_text, "another session"),
    "header of another session": (header_other_session, "another session"),
}


def refuse_everywhere(instance, arguments, fragment):
    """The public guard and the real Application both answer ``forbidden`` and write nothing."""
    with instance.database.read() as uow:
        plan = prepare_batch(instance, arguments, uow)
        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner(instance), plan, uow)
    assert refused.value.code == "forbidden" and fragment in refused.value.message, refused.value.message
    before = domain_rows(instance)
    result = instance.runtime.application.invoke(global_worker(instance), "learnings_record_batch", arguments,
                                                 InvocationOptions(idempotency_key=new_id()))
    assert result.status == "error" and result.error["code"] == "forbidden" and fragment in result.error["message"], result.to_dict()
    assert domain_rows(instance) == before


@pytest.mark.parametrize("name", sorted(STRUCTURED_CORES))
def test_an_incomplete_structured_frozen_core_is_refused_before_any_effect(shared, name):
    instance, business = shared
    change, fragment = STRUCTURED_CORES[name]
    arguments = batch_arguments(instance, business)
    core_change(change)(arguments)
    refuse_everywhere(instance, arguments, fragment)


def transcript_change(function):
    return lambda arguments: function(earlier_core(arguments), earlier_of(arguments))


def markdown_missing(core, _snapshot):
    del core["markdown"]


def markdown_null(core, _snapshot):
    core["markdown"] = None


def markdown_blank(core, _snapshot):
    core["markdown"] = " \n"


def markdown_number(core, _snapshot):
    core["markdown"] = 7


def markdown_without_the_quote(core, _snapshot):
    core["markdown"] = core["markdown"].replace(QUESTION, "A different owner sentence.")


def quote_only_in_the_agent_block(core, _snapshot):
    core["markdown"] = core["markdown"].replace("## T1", "## T1  2026-09-10 09:00  OWNER\nOwner words.\n\nAGENT\n" + QUESTION + "\n\n## T9", 1)


def transcript_header_only(core, snapshot):
    header_only(core, snapshot)


def transcript_with_source_key(core, _snapshot):
    core["source_key"] = core["header"]["session"]


def transcript_with_source(core, _snapshot):
    core["source"] = {"kind": "task_note", "id": new_id()}


def item_names_a_source(_core, snapshot):
    snapshot["evidence"]["source"] = {"kind": "task_note", "id": new_id()}


def header_of_a_business(core, _snapshot):
    core["header"]["biz"] = "business-1"


TRANSCRIPT_CORES = {
    "header only with a fabricated quote": (transcript_header_only, "no frozen transcript"),
    "markdown absent": (markdown_missing, "no frozen transcript"),
    "markdown null": (markdown_null, "no frozen transcript"),
    "markdown blank": (markdown_blank, "no frozen transcript"),
    "markdown a number": (markdown_number, "no frozen transcript"),
    "quote not in the frozen markdown": (markdown_without_the_quote, "not supported by its frozen evidence"),
    "quote only in an agent block": (quote_only_in_the_agent_block, "not supported by its frozen evidence"),
    "a structured source key on a transcript": (transcript_with_source_key, "incomplete structured source"),
    "a structured source on a transcript": (transcript_with_source, "incomplete structured source"),
    "an item naming a source for a transcript": (item_names_a_source, "incomplete structured source"),
    "a business header for a global item": (header_of_a_business, "not supported by its frozen evidence"),
}


@pytest.mark.parametrize("name", sorted(TRANSCRIPT_CORES))
def test_an_incomplete_transcript_frozen_core_is_refused_before_any_effect(shared, name):
    instance, business = shared
    change, fragment = TRANSCRIPT_CORES[name]
    arguments = transcript_batch(instance, business)
    transcript_change(change)(arguments)
    refuse_everywhere(instance, arguments, fragment)


def evidence_of(snapshot):
    return snapshot["evidence"]


MALFORMED_ITEMS = {
    "turns a string": lambda s: evidence_of(s).__setitem__("turns", "T1"),
    "turns a list of numbers": lambda s: evidence_of(s).__setitem__("turns", [1]),
    "turns empty": lambda s: evidence_of(s).__setitem__("turns", []),
    "owner quote a number": lambda s: evidence_of(s).__setitem__("owner_quote", 5),
    "owner quote missing": lambda s: evidence_of(s).pop("owner_quote"),
    "agent quote a number": lambda s: evidence_of(s).__setitem__("agent_quote", 5),
    "said by a list": lambda s: s.__setitem__("said_by", ["owner"]),
    "said by another role than its block": lambda s: s.__setitem__("said_by", "human"),
    "confidence a word": lambda s: s.__setitem__("confidence", "high"),
    "text a list": lambda s: s.__setitem__("text", [QUESTION]),
    "tag an object": lambda s: s.__setitem__("tag", {"a": 1}),
    "from correction a word": lambda s: s.__setitem__("from_correction", "no"),
    "scope reason missing": lambda s: s.pop("scope_reason"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_ITEMS))
def test_a_malformed_embedded_item_is_a_deterministic_refusal_for_either_core_shape(shared, name):
    instance, business = shared
    for build in (batch_arguments, transcript_batch):
        arguments = build(instance, business)
        MALFORMED_ITEMS[name](earlier_of(arguments))
        refuse_everywhere(instance, arguments, "not supported by its frozen evidence")


def test_genuine_structured_and_transcript_cores_are_both_accepted_and_reviewed_later(shared):
    instance, business = shared
    for build in (batch_arguments, transcript_batch):
        arguments = build(instance, business)
        with instance.database.read() as uow:
            assert validate_factual_plan(owner(instance), prepare_batch(instance, arguments, uow), uow) is None
        applied = instance.runtime.application.invoke(global_worker(instance), "learnings_record_batch", arguments,
                                                      InvocationOptions(idempotency_key=new_id()))
        assert applied.status == "ok", applied.to_dict()
        survivor_id = arguments["items"][0]["id"]
        assert not read_rows(instance, "SELECT 1 FROM learning_candidates WHERE id=?", earlier_of(arguments)["id"])
        row = ok(invoke(instance, owner(instance), "learnings_get", {"learning_id": survivor_id}))["item"]
        reviewed = invoke(instance, owner(instance), "learnings_review",
                          {"learning_id": survivor_id, "state": "rejected", "feedback": "Not now."}, revision=row["revision"])
        assert reviewed.status == "ok", reviewed.to_dict()


def test_a_cached_embedded_copy_is_checked_against_the_scope_of_every_owner(shared):
    instance, business = shared
    arguments = batch_arguments(instance, business)
    first = arguments["items"][0]
    other = deepcopy(first)
    first["id"], other["id"] = new_id(), new_id()
    arguments["items"].append(other)
    with instance.database.read() as uow:
        plan = prepare_batch(instance, arguments, uow)
        assert validate_factual_plan(owner(instance), plan, uow) is None, "the control: both owners are Global"

        def second_owner_in(scope):
            def alter(change):
                if change.resource.resource_type != "learning_candidates" or change.resource.resource_id != other["id"]:
                    return change
                values = plain(change.values)
                values["candidate_json"]["scope"] = scope
                return replace(change, values=values)
            return replace(plan, changes=tuple(alter(change) for change in plan.changes))

        with pytest.raises(DomainError) as refused:
            validate_factual_plan(owner(instance), second_owner_in("biz"), uow)
    assert refused.value.code == "forbidden" and "another scope" in refused.value.message, refused.value.message


# --------------------------------------------------- later genuine saved intake over a damaged stored frozen core

@pytest.fixture(scope="module")
def collapsed(tmp_path_factory):
    return collapse(tmp_path_factory.mktemp("collapsed"))


def tampered_store(instance, survivor_id, change):
    """Damage the stored run core (and the stored survivor's embedded copy) of a genuine collapse; return the restore step."""
    with instance.database.read() as uow:
        run_row = uow.connection.execute("SELECT id, result_json FROM learning_runs").fetchone()
        candidate_row = uow.connection.execute("SELECT candidate_json FROM learning_candidates WHERE id=?", (survivor_id,)).fetchone()
    original = (run_row["result_json"], candidate_row["candidate_json"])
    run, candidate = json.loads(original[0]), json.loads(original[1])
    snapshot = candidate["reconciliations"][0]["earlier"]
    change(run["cores"][snapshot["evidence"]["session"]], snapshot)
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE learning_runs SET result_json=? WHERE id=?", (json.dumps(run), run_row["id"]))
        uow.connection.execute("UPDATE learning_candidates SET candidate_json=? WHERE id=?", (json.dumps(candidate), survivor_id))

    def restore():
        with instance.database.transaction() as uow:
            uow.connection.execute("UPDATE learning_runs SET result_json=? WHERE id=?", (original[0], run_row["id"]))
            uow.connection.execute("UPDATE learning_candidates SET candidate_json=? WHERE id=?", (original[1], survivor_id))
    return restore


def effects(instance):
    return (candidates(instance), read_rows(instance, "SELECT COUNT(*) FROM documents"), read_rows(instance, "SELECT COUNT(*) FROM jobs"),
            read_rows(instance, "SELECT COUNT(*) FROM learning_history"), read_rows(instance, "SELECT COUNT(*) FROM context_filings"))


STORED_CORES = ("header only with a fabricated quote", "blocks null", "block without a role", "source absent in the core and the item",
                "source only in the core", "blocks removed from a structured core", "header of another session")


@pytest.mark.parametrize("name", STORED_CORES)
def test_a_damaged_stored_core_refuses_a_later_saved_intake_before_any_effect(collapsed, name):
    instance, _ids, _model = collapsed
    survivor, = queue(instance)
    change, fragment = STRUCTURED_CORES[name]
    restore = tampered_store(instance, survivor["id"], change)
    try:
        before = effects(instance)
        for operation, arguments in (("learnings_review", {"learning_id": survivor["id"], "state": "accepted"}),
                                     ("learnings_edit", {"learning_id": survivor["id"], "changes": {"text": "The refund is 40 percent."}})):
            result = invoke(instance, owner(instance), operation, arguments, revision=survivor["revision"])
            assert result.status == "error" and result.error["code"] == "forbidden", (name, operation, result.to_dict())
            assert fragment in result.error["message"], result.error["message"]
        assert effects(instance) == before
        assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(0,)]
    finally:
        restore()


def test_the_undamaged_stored_core_still_serves_the_later_intake(collapsed):
    instance, _ids, _model = collapsed
    survivor, = queue(instance)
    accepted = ok(invoke(instance, owner(instance), "learnings_review", {"learning_id": survivor["id"], "state": "accepted"},
                         revision=survivor["revision"]))["item"]
    assert accepted["state"] == "accepted" and accepted["filing"]["upload_id"]
    assert read_rows(instance, "SELECT COUNT(*) FROM documents WHERE kind='context_upload'") == [(1,)]
