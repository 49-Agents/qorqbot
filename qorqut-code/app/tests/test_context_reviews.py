"""Uniform review behavior for related items, not only Story uploads."""

import sqlite3

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from test_context_relationships import call, item, ok, service, story


def pending(service, target, **options):
    return ok(call(service, "context_reviews_list", {"item": target, **options}))


def decision(status, result="incorporated"):
    return {**{key: status[key] for key in ("relationship_id", "relationship_revision", "review_revision", "source_token")},
            "result": result, "explanation": "Accounted for this evidence in the next step"}


def note(service, body="The investor requested a product demonstration."):
    return ok(call(service, "notes_create", {"title": "Call notes", "body": body}))["document"]


def link(service, source, target):
    return ok(call(service, "relationships_create", {"source": source, "target": target, "role": "concerns"}))["relationship"]


def test_immutable_activity_can_review_related_evidence(service):
    source = note(service)
    activity = ok(call(service, "logs_add", {"tag": "operations", "summary": "Recorded the experiment"}))["entry"]
    focus = item("action", activity["id"])
    link(service, item("material", source["id"]), focus)
    queue = pending(service, focus)
    assert queue["item_revision"] == 1
    ok(call(service, "context_review", {"item": focus, "items": [decision(queue["items"][0], "no_change")]}, queue["item_revision"]))
    assert not pending(service, focus)["items"]


def test_shared_explicit_reviews_are_independent_for_each_item(service):
    source = note(service)
    task = ok(call(service, "tasks_create", {"title": "Prepare demonstration"}))["task"]
    undertaking = story(service)
    source_item, task_item, story_item = item("material", source["id"]), item("task", task["id"]), item("story", undertaking["id"])
    edge = link(service, source_item, task_item)
    link(service, source_item, story_item)
    status = pending(service, task_item)["items"][0]
    ok(call(service, "notes_get", {"document_id": source["id"]}))
    assert pending(service, task_item)["items"][0]["pending"]
    key = new_id()
    args = {"item": task_item, "items": [decision(status)], "edit": {"operation": "tasks_update", "arguments": {
        "task_id": task["id"], "changes": {"title": "Prepare the requested product demonstration"}}}}
    result = ok(call(service, "context_review", args, task["revision"], key))
    assert ok(call(service, "context_review", args, task["revision"], key)) == result
    assert not pending(service, task_item)["items"]
    assert len(pending(service, story_item)["items"]) == 1
    assert len(pending(service, source_item)["items"]) == 2
    current = ok(call(service, "tasks_get", {"task_id": task["id"]}))["task"]
    assert current["title"] == "Prepare the requested product demonstration"
    after = ok(call(service, "relationships_get", {"relationship_id": edge["id"]}))["relationship"]
    assert after["revision"] == edge["revision"] and after["updated_at"] == edge["updated_at"]
    reviewed = pending(service, task_item, include_reviewed=True)["items"][0]
    assert reviewed["latest_review"]["actor"]["id"] == service.fixture.owner.id
    reopened = {"item": task_item, **{key: reviewed[key] for key in ("relationship_id", "relationship_revision", "review_revision", "source_token")}, "explanation": "Reconsider after the planning meeting"}
    ok(call(service, "context_review_reopen", reopened, current["revision"]))
    assert pending(service, task_item)["items"][0]["pending"]
    history = ok(call(service, "context_reviews_history", {"item": task_item, "relationship_id": edge["id"]}))["reviews"]
    assert [entry["result"] for entry in history] == ["pending", "incorporated"]
    with service.fixture.db.transaction() as uow:
        with pytest.raises(DomainError, match="append-only"):
            uow.repository("context_reviews", business_id=service.fixture.business_id).update(history[0]["id"], {"explanation": "Rewrite history"})
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            uow.connection.execute("DELETE FROM context_reviews WHERE id=?", (history[0]["id"],))


def test_changed_source_or_edge_meaning_reopens_context(service):
    source, target = note(service), story(service)
    source_item, target_item = item("material", source["id"]), item("story", target["id"])
    edge = link(service, source_item, target_item)
    first = pending(service, target_item)["items"][0]
    ok(call(service, "context_review", {"item": target_item, "items": [decision(first, "no_change")]}, target["revision"]))
    assert not pending(service, target_item)["items"]
    ok(call(service, "notes_update", {"document_id": source["id"], "body": "The investor now requests financial projections."}, source["revision"]))
    changed = pending(service, target_item)["items"][0]
    assert changed["source_token"] != first["source_token"]
    rejected = call(service, "context_review", {"item": target_item, "items": [decision(first)]}, target["revision"])
    assert rejected["error"]["code"] == "revision_conflict"
    ok(call(service, "context_review", {"item": target_item, "items": [decision(changed)]}, target["revision"]))
    ok(call(service, "relationships_update", {"relationship_id": edge["id"], "changes": {"context": "Now urgent for investor follow-up"}}, edge["revision"]))
    assert pending(service, target_item)["items"][0]["pending"]


def test_stale_shared_review_and_invalid_edit_are_atomic(service):
    source, target = note(service), story(service)
    target_item = item("story", target["id"])
    link(service, item("material", source["id"]), target_item)
    status = pending(service, target_item)["items"][0]
    args = {"item": target_item, "items": [decision(status)]}
    invalid = call(service, "context_review", {**args, "edit": {"operation": "stories_update", "arguments": {
        "story_id": target["id"], "changes": {"state": "waiting"}}}}, target["revision"])
    assert invalid["status"] == "error"
    assert pending(service, target_item)["items"][0]["review_revision"] == 0
    with service.fixture.db.read() as uow:
        plan = service.runtime.domains.relationships.reviews.prepare(service.fixture.ctx, args, InvocationOptions(expected_revision=target["revision"]), uow)
    ok(call(service, "context_review", args, target["revision"]))
    stale = call(service, "context_review", args, target["revision"])
    assert stale["error"]["code"] == "revision_conflict"
    # The plan observed an absent review state; another participant created it.
    state_targets = [value for value in plan.expected_targets if value.resource.resource_type == "context_review_states"]
    assert len(state_targets) == 1 and state_targets[0].exists is False
    assert "context_review" not in service.runtime.background.programs.require("context.file").outputs


def test_remove_edge_keeps_history_but_relink_starts_pending(service):
    source, target = note(service), story(service)
    source_item, target_item = item("material", source["id"]), item("story", target["id"])
    edge = link(service, source_item, target_item)
    ok(call(service, "context_review", {"item": target_item, "items": [decision(pending(service, target_item)["items"][0])]}, target["revision"]))
    ok(call(service, "relationships_remove", {"relationship_id": edge["id"]}, edge["revision"]))
    assert not pending(service, target_item)["items"]
    assert len(ok(call(service, "context_reviews_history", {"item": target_item, "relationship_id": edge["id"]}))["reviews"]) == 1
    link(service, source_item, target_item)
    assert pending(service, target_item)["items"][0]["review_revision"] == 0


def test_document_edit_and_review_commit_with_exact_file_receipt(service):
    source, target = note(service), note(service, "Current analysis")
    target_item = item("material", target["id"])
    link(service, item("material", source["id"]), target_item)
    status = pending(service, target_item)["items"][0]
    args = {"item": target_item, "items": [decision(status)], "edit": {"operation": "notes_update", "arguments": {
        "document_id": target["id"], "body": "Analysis now includes the demonstration request."}}}
    ok(call(service, "context_review", args, target["revision"]))
    assert not pending(service, target_item)["items"]
    assert ok(call(service, "notes_get", {"document_id": target["id"]}))["document"]["body"] == "Analysis now includes the demonstration request."


def test_table_edit_and_review_preserve_normal_snapshot_history(service):
    source = note(service)
    table = ok(call(service, "tables_create", {"title": "Prospects", "columns": [{"id": "name", "name": "Name", "type": "text"}]}))["table"]
    target = item("table", table["id"])
    link(service, item("material", source["id"]), target)
    args = {"item": target, "items": [decision(pending(service, target)["items"][0])], "edit": {"operation": "tables_rows_write", "arguments": {
        "table_id": table["id"], "rows": [{"cells": {"name": "Investor demonstration"}}]}}}
    ok(call(service, "context_review", args, table["revision"]))
    assert not pending(service, target)["items"]
    assert ok(call(service, "tables_rows_list", {"table_id": table["id"]}))["items"][0]["cells"]["name"] == "Investor demonstration"
    with service.fixture.db.read() as uow:
        versions = uow.repository("table_versions", business_id=service.fixture.business_id).list()
        assert len(versions) == 2 and {row["table_revision"] for row in versions} == {1, 2}
    archived = ok(call(service, "context_items_archive", {"item": target, "archived": True}, 2))["item"]
    assert archived["archived"]


def test_immutable_history_remains_readable_while_documents_are_applying(service):
    source, target = note(service), note(service, "Current analysis")
    focus = item("material", target["id"])
    edge = link(service, item("material", source["id"]), focus)
    ok(call(service, "context_review", {"item": focus,
        "items": [decision(pending(service, focus)["items"][0])]}, target["revision"]))
    with service.fixture.db.transaction() as uow:
        repo = uow.repository("documents", business_id=service.fixture.business_id)
        for document in (source, target):
            repo.update(document["id"], {"availability": "applying"}, expected_revision=document["revision"])
    assert call(service, "notes_get", {"document_id": source["id"]})["error"]["code"] == "busy"
    history = ok(call(service, "context_reviews_history", {"item": focus, "relationship_id": edge["id"]}))["reviews"]
    assert len(history) == 1 and history[0]["result"] == "incorporated"
