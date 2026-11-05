"""Focused real-runtime checks for the retained factual learning paths.

The composed synthetic host is the real assembly over a real target store, the workspace role sources and the retained
Operations programs; model answers are frozen JSON objects and no provider, native session or live instance is touched.
These cover the acceptance paths the removed procedural engine used to own: owner-contribution extraction, the scan
checkpoint that deduplicates a repeated source, scope/privacy boundaries, procedural-item exclusion and the retained
extraction routine.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id

from host_foundation_support import owner_global_context
from learning_factual_support import (NOTE, NOW, business_context, candidates, composed, factual_item, invoke, ok,
                                      run_extraction, scans, task_with_note)


def test_extraction_records_one_factual_candidate_and_the_scan_checkpoint_deduplicates_the_source(tmp_path):
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(NOTE)]}])
    task_with_note(instance, ids["harbor"], NOTE)
    outcome = run_extraction(instance)
    assert outcome["state"] == "succeeded", outcome

    stored = candidates(instance)
    assert len(stored) == 1
    assert stored[0]["business_id"] is None, "extraction records the Global review queue"
    item = json.loads(stored[0]["candidate_json"])
    assert item["text"] == NOTE and item["bucket"] == "context" and item["tag"] == "fact"
    assert item["evidence"]["source"]["kind"] == "task_note"
    checkpoint = scans(instance)
    assert len(checkpoint) == 1 and checkpoint[0]["session_id"].startswith("business:" + ids["harbor"])
    assert checkpoint[0]["transcript_hash"] == item["evidence"]["source"]["hash"]
    state = json.loads(checkpoint[0]["state_json"])
    assert state["source_kind"] == "task_note" and state["source_id"] == item["evidence"]["source"]["resource_id"]
    assert len(model.calls) == 1

    # The same unchanged source is not extracted twice: the counted scan hash skips it before any model call.
    checkpoint_before = scans(instance)
    assert run_extraction(instance)["state"] == "succeeded"
    assert len(candidates(instance)) == 1
    assert scans(instance) == checkpoint_before, "the checkpoint (hash, state and revision) is untouched by the repeat"
    assert len(model.calls) == 1, "a repeated source never reaches the model again"

    # A second, distinct contribution is its own source with its own checkpoint; the first one stays counted.
    second = "Owner prefers the monthly summary on the first Monday."
    task_with_note(instance, ids["harbor"], second, summary="Monthly summary")
    model.answers.append({"items": [factual_item(second)]})
    model.answers.append({"duplicates": [], "contradictions": [], "merged": []})
    assert run_extraction(instance)["state"] in {"succeeded", "completed_with_warnings"}
    texts = [json.loads(row["candidate_json"])["text"] for row in candidates(instance)]
    assert texts.count(NOTE) == 1 and texts.count(second) == 1
    assert len(scans(instance)) == 2


def test_a_procedural_item_is_dropped_before_the_factual_queue(tmp_path):
    delta = factual_item("The harbour agent should always rename folders before work.", bucket="delta")
    instance, ids, model = composed(tmp_path, [{"items": [delta]}])
    task_with_note(instance, ids["harbor"], NOTE)
    outcome = run_extraction(instance)
    # The procedure-only answer is rejected before output: the job reports the factual extraction failure and
    # nothing procedural is stored in the retained queue.
    assert outcome["state"] == "failed", outcome
    with instance.database.read() as uow:
        error = json.loads(uow.connection.execute("SELECT error_json FROM jobs").fetchone()[0])
    assert error["code"] == "learning_extraction_failed"
    assert candidates(instance) == [], "the retained route records factual Context only"
    assert scans(instance) == []

    model.answers = [{"items": [factual_item(NOTE)]}]
    assert run_extraction(instance)["state"] in {"succeeded", "completed_with_warnings"}
    assert [json.loads(row["candidate_json"])["text"] for row in candidates(instance)] == [NOTE]


def test_extraction_is_global_and_business_notes_keep_their_own_scope(tmp_path):
    first = "The first project reports monthly instead."
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(NOTE)]}, {"items": [factual_item(first)]},
                                               {"duplicates": [], "contradictions": [], "merged": []}],
                                    businesses=("harbor", "first"))
    task_with_note(instance, ids["harbor"], NOTE)
    task_with_note(instance, ids["first"], first)
    run_extraction(instance)
    stored = candidates(instance)
    assert len(stored) == 2 and all(row["business_id"] is None for row in stored)
    # The Global review queue sees both contributions; a business-scoped listing never leaks the other business.
    global_items = instance.runtime.application.invoke(owner_global_context(instance), "learnings_list", {},
                                                       InvocationOptions(idempotency_key=new_id()))
    assert global_items.status == "ok", global_items.error
    assert len(global_items.data["items"]) == 2
    for slug in ("harbor", "first"):
        listing = instance.runtime.application.invoke(business_context(instance, ids[slug]), "learnings_list", {},
                                                      InvocationOptions(idempotency_key=new_id()))
        assert listing.status == "ok", listing.error
        assert list(listing.data["items"]) == [], "a business queue holds no Global review item"
    assert len(model.calls) >= 2, "each business contribution was its own extraction source"


def test_the_retained_extraction_routine_registers_and_runs(tmp_path):
    clock = [NOW]
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(NOTE)]}], clock=lambda: clock[0])
    task_with_note(instance, ids["harbor"], NOTE)
    created = instance.runtime.application.invoke(owner_global_context(instance), "routines_create", {
        "scope": "global", "definition": {
            "name": "Weekly learning", "why": "Collect owner lessons on schedule", "schedule": "0 3 * * *",
            "timezone": "UTC", "status": "active", "program": "learnings.extract",
            "arguments": {"passes": 1, "max_usd": 1}, "target": "root", "timeout_seconds": 600}},
        InvocationOptions(idempotency_key=new_id()))
    assert created.status == "ok", created.error
    record = created.data["records"][0]
    assert record["job_kind"] == "learnings.extract" and record["scope"] == "global"
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    # A routine's first observed slot is recorded without a backfill run; the next scheduled slot queues the
    # retained program and the service runs it to completion.
    assert instance.runtime.background.tick() is None
    clock[0] = datetime(2026, 10, 31, 3, 1, tzinfo=timezone.utc)
    outcome = instance.runtime.background.tick()
    assert outcome and outcome["state"] == "succeeded", outcome
    with instance.database.read() as uow:
        job = dict(uow.connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchone())
    assert job["kind"] == "learnings.extract" and job["state"] == "succeeded"
    assert [json.loads(row["candidate_json"])["text"] for row in candidates(instance)] == [NOTE]


def routine_definition(program, **overrides):
    return {"scope": "global", "definition": {
        "name": "Weekly learning", "why": "Collect owner lessons on schedule", "schedule": "0 3 * * *", "timezone": "UTC",
        "status": "active", "program": program, "arguments": {"passes": 1, "max_usd": 1}, "target": "root",
        "timeout_seconds": 600, **overrides}}


def test_a_manual_run_of_the_retained_routine_queues_and_completes_the_same_program(tmp_path):
    instance, ids, model = composed(tmp_path, [{"items": [factual_item(NOTE)]}])
    task_with_note(instance, ids["harbor"], NOTE)
    owner = owner_global_context(instance)
    record = ok(invoke(instance, owner, "routines_create", routine_definition("learnings.extract")))["records"][0]
    queued = ok(invoke(instance, owner, "routines_run", {"scope": "global", "routine_id": record["id"]},
                       revision=record["revision"]))
    assert len(queued["job_ids"]) == 1 and queued["records"][0]["trigger"] == "manual"
    assert instance.runtime.background.run_one(queued["job_ids"][0])["state"] == "succeeded"
    assert [json.loads(row["candidate_json"])["text"] for row in candidates(instance)] == [NOTE]
    with instance.database.read() as uow:
        job = dict(uow.connection.execute("SELECT kind, state, business_id FROM jobs").fetchone())
    assert job == {"kind": "learnings.extract", "state": "succeeded", "business_id": None}


@pytest.mark.parametrize("program", ["learnings.apply_accepted", "learnings.evolve"])
def test_a_retired_learning_program_cannot_be_registered_as_a_routine(tmp_path, program):
    instance, ids, model = composed(tmp_path, [])
    refused = invoke(instance, owner_global_context(instance), "routines_create", routine_definition(program))
    assert refused.status == "error" and refused.error["code"] == "operation_unavailable", refused.to_dict()
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM routines").fetchone()[0] == 0
        assert uow.connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
