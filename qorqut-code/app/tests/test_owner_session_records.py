"""Owner sessions change tasks and Stories on explicit request, inside one business, without taking part."""

import json

import pytest

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.owner_bootstrap import NAME
from test_owner_session_final_model import business_agent
from test_owner_session_mcp import call, code, connect, enable, issue, ok, tool_names
from test_service_transports import ORIGIN, ServiceFixture


@pytest.fixture
def full(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    enable(service.fixture)
    service.token = issue(service.fixture)
    service.agent_id, _ = business_agent(service)
    return service


def second_business(fixture):
    other = new_id()
    (fixture.root / "second/hq").mkdir(parents=True)
    with fixture.db.transaction() as uow:
        uow.repository("businesses").insert({"id": other, "slug": "second", "name": "Second",
            "workspace": str(fixture.root / "second"), "hq_path": str(fixture.root / "second/hq"),
            "stage": "validating"})
        uow.repository("business_memberships", business_id=other).insert(
            {"principal_id": fixture.owner.id, "is_owner": True})
    enable(fixture, other)
    return other, issue(fixture, other, label="second")


def counts(fixture):
    with fixture.db.read() as uow:
        tables = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: uow.connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}


def test_explicit_task_create_edit_close_and_reopen(full):
    fixture = full.fixture
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        task = ok(call(client, headers, "tasks_create", {
            "title": "Publish pricing page", "assignee_ids": [full.agent_id], "idempotency_key": new_id()}))["task"]
        assert task["status"] == "open" and task["assignee_ids"] == [full.agent_id]
        edited = ok(call(client, headers, "tasks_update", {
            "task_id": task["id"], "changes": {"title": "Publish the pricing page"},
            "expected_revision": task["revision"], "idempotency_key": new_id()}))["task"]
        assert edited["title"] == "Publish the pricing page"
        stale = {"task_id": task["id"], "expected_revision": task["revision"], "idempotency_key": new_id()}
        assert code(call(client, headers, "tasks_complete", stale)) == "revision_conflict"
        assert code(call(client, headers, "tasks_complete", {**stale, "expected_revision": edited["revision"],
                                                              "status": "doing"})) == "validation_error"
        closed = ok(call(client, headers, "tasks_complete", {
            "task_id": task["id"], "expected_revision": edited["revision"], "idempotency_key": new_id()}))["task"]
        assert closed["status"] == "done"
        assert code(call(client, headers, "tasks_update", {
            "task_id": task["id"], "changes": {"status": "shipped"}, "expected_revision": closed["revision"],
            "idempotency_key": new_id()})) == "validation_error"
        reopened = ok(call(client, headers, "tasks_update", {
            "task_id": task["id"], "changes": {"status": "open"}, "expected_revision": closed["revision"],
            "idempotency_key": new_id()}))["task"]
        assert reopened["status"] == "open"
        dropped = ok(call(client, headers, "tasks_update", {
            "task_id": task["id"], "changes": {"status": "dropped"}, "expected_revision": reopened["revision"],
            "idempotency_key": new_id()}))["task"]
        assert dropped["status"] == "dropped"
    with fixture.db.read() as uow:
        row = uow.connection.execute("SELECT created_by, status FROM tasks WHERE id=?", (task["id"],)).fetchone()
        assert (row["created_by"], row["status"]) == (fixture.owner.id, "dropped")
        # An ordinary visible change: the assigned business agent is notified as usual.
        deliveries = uow.connection.execute(
            "SELECT DISTINCT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
            "WHERE e.source_id=?", (task["id"],)).fetchall()
        assert full.agent_id in {row["agent_id"] for row in deliveries}


def test_explicit_story_create_edit_close_and_reopen(full):
    fixture = full.fixture
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        story = ok(call(client, headers, "stories_create", {
            "title": "Pricing test", "purpose": "Learn willingness to pay", "idempotency_key": new_id()}))["story"]
        edited = ok(call(client, headers, "stories_update", {
            "story_id": story["id"], "changes": {"title": "Pricing test 2"},
            "expected_revision": story["revision"], "idempotency_key": new_id()}))["story"]
        assert edited["title"] == "Pricing test 2"
        assert code(call(client, headers, "stories_close", {
            "story_id": story["id"], "outcome": "success", "expected_revision": story["revision"],
            "idempotency_key": new_id()})) == "revision_conflict"
        closed = ok(call(client, headers, "stories_close", {
            "story_id": story["id"], "outcome": "success", "expected_revision": edited["revision"],
            "idempotency_key": new_id()}))["story"]
        assert (closed["state"], closed["outcome"]) == ("closed", "success")
        assert code(call(client, headers, "stories_close", {
            "story_id": story["id"], "outcome": "failed", "expected_revision": closed["revision"],
            "idempotency_key": new_id()})) == "conflict"
        assert code(call(client, headers, "stories_update", {
            "story_id": story["id"], "changes": {"state": "working"}, "expected_revision": closed["revision"],
            "idempotency_key": new_id()})) == "forbidden"
        reopened = ok(call(client, headers, "stories_reopen", {
            "story_id": story["id"], "state": "working", "expected_revision": closed["revision"],
            "idempotency_key": new_id()}))["story"]
        assert (reopened["state"], reopened["outcome"]) == ("working", None)
        assert code(call(client, headers, "stories_reopen", {
            "story_id": story["id"], "state": "planned", "expected_revision": reopened["revision"],
            "idempotency_key": new_id()})) == "conflict"
        # Transition reasons stay off the shared timeline for owner sessions.
        assert code(call(client, headers, "stories_close", {
            "story_id": story["id"], "outcome": "success", "note": "Done", "expected_revision": reopened["revision"],
            "idempotency_key": new_id()})) == "channel_required"
    with fixture.db.read() as uow:
        actors = {row["actor_id"] for row in uow.connection.execute(
            "SELECT actor_id FROM story_events WHERE story_id=?", (story["id"],))}
        assert actors == {fixture.owner.id}


def test_other_business_records_stay_out_of_reach(full):
    fixture = full.fixture
    other, other_token = second_business(fixture)
    with TestClient(full.web) as client:
        foreign = connect(client, other_token)
        task = ok(call(client, foreign, "tasks_create", {"title": "Second task", "idempotency_key": new_id()}))["task"]
        story = ok(call(client, foreign, "stories_create", {
            "title": "Second story", "purpose": "Stay private", "idempotency_key": new_id()}))["story"]
        before = counts(fixture)
        headers = connect(client, full.token)
        for operation, arguments in (
            ("tasks_update", {"task_id": task["id"], "changes": {"title": "Taken"}, "expected_revision": 1}),
            ("tasks_complete", {"task_id": task["id"], "expected_revision": 1}),
            ("stories_close", {"story_id": story["id"], "outcome": "success", "expected_revision": 1}),
            ("stories_update", {"story_id": story["id"], "changes": {"title": "Taken"}, "expected_revision": 1}),
        ):
            assert code(call(client, headers, operation, {**arguments, "idempotency_key": new_id()})) == "not_found"
            assert code(call(client, headers, operation, {**arguments, "business_id": other,
                                                         "idempotency_key": new_id()})) == "forbidden"
        assert code(call(client, headers, "tasks_create", {
            "title": "Global", "scope": "global", "idempotency_key": new_id()})) == "forbidden"
        assert code(call(client, headers, "tasks_create", {
            "title": "Linked", "story_ids": [story["id"]], "idempotency_key": new_id()})) in {"not_found", "forbidden"}
    after = counts(fixture)
    assert {table: count for table, count in after.items() if count != before.get(table)} == {}


def test_starting_and_stopping_a_session_writes_nothing_and_adds_no_participant(full):
    fixture = full.fixture
    with TestClient(full.web) as client:
        roster = client.post("/api/biz/widget/operations/agents_list", headers=ORIGIN, json={})
        assert roster.status_code == 200, roster.text
        # Settle the signoff notifier's per-owner schedule row so background timing cannot differ.
        full.runtime.signoffs.notifications.tick()
        before, agents = counts(fixture), roster.json()["data"]["agents"]
        headers = connect(client, full.token)
        assert "agents_list" not in tool_names(client, headers)
        ok(call(client, headers, NAME, {}))
        assignees = ok(call(client, headers, "assignees_list", {}))["assignees"]
        ok(call(client, headers, "tasks_list", {}))
        ok(call(client, headers, "agents_messages_list", {}))
        ended = client.delete("/mcp/owner", headers=headers)
        assert ended.status_code in {200, 202, 204, 405}, ended.text
        after = client.post("/api/biz/widget/operations/agents_list", headers=ORIGIN, json={})
        assert after.json()["data"]["agents"] == agents
    assert counts(fixture) == before
    with fixture.db.read() as uow:
        members = {row["principal_id"] for row in uow.connection.execute(
            "SELECT principal_id FROM business_memberships WHERE business_id=? AND active=1", (fixture.business_id,))}
    # The only human assignee is the owner; the session itself has no identity to assign.
    assert {item["id"] for item in assignees} <= members
    assert [item["id"] for item in assignees if item["kind"] == "human"] == [fixture.owner.id]
    assert fixture.owner.id not in json.dumps(agents)


def test_business_notes_are_discoverable_through_the_session(full):
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        assert {"notes_list", "notes_get"} <= tool_names(client, headers)
        # Business-owned note storage and categories belong to the notes contract; the route reaches them.
        assert isinstance(ok(call(client, headers, "notes_list", {}))["items"], list)
