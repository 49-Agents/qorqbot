"""One small composed flow: Tasks, Actions, the tag catalog, the discussion and the Context queue in one engine."""

from __future__ import annotations

import pytest

from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, OTHER, OTHER_AGENT, OperationalHost, WORKER
from qorqut_context.integration.queue import transitions

META = {"kind": "note", "title": "Launch note", "origin": "agent"}


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def inputs(host):
    return host.query("SELECT sequence, input_kind, item_id, revision, operation, state, business_id, submitted_by FROM context_inputs ORDER BY sequence")


def test_a_task_an_action_and_a_source_share_one_ordered_queue_in_their_own_namespace(host):
    task = host.create_task("Prepare launch", assignee_ids=[AGENT2], tags=["launch", "Q4"])
    action = host.record_action("Launch checklist reviewed", ctx=host.agent(AGENT2), task_id=task["id"], tags=["Launch"])
    submitted = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/launch.md", "content": "Plan", "metadata": META},
                          host.agent())["input"]

    # The tag catalog holds each label once; neither it nor the task link widened anyone's access.
    assert [item["label"] for item in host.read("tags_list", {}, host.agent())["tags"]] == ["launch", "Q4"]
    outsider = host.agent(OTHER_AGENT, OTHER)
    assert host.read("tasks_list", {}, outsider)["tasks"] == [] and host.read("actions_list", {}, outsider)["actions"] == []
    assert host.read("context_queue_list", {}, outsider)["items"] == []

    # A Signoff-protected correction stages first and then commits revision, Action and queued snapshot together.
    host.agents.overrides["actions_correct"] = "signoff"
    signoff_id = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Launch checklist signed off"},
                            host.agent(AGENT2), expected_revision=1)
    assert [row["input_kind"] for row in inputs(host)] == ["action", "general"]
    assert host.apply_approved(signoff_id)["state"] == "applied"
    rows = inputs(host)
    assert [(row["input_kind"], row["revision"], row["operation"]) for row in rows] == [("action", 1, "add"), ("general", None, "add"), ("action", 2, "edit")]
    assert [row["sequence"] for row in rows] == sorted(row["sequence"] for row in rows)
    assert {row["business_id"] for row in rows} == {BUSINESS} and rows[2]["submitted_by"] == AGENT2

    # The Task discussion has one writer: the assignee's completion is a required event for the creator's work and a thread entry.
    detail = host.read("tasks_get", {"task_id": task["id"]}, host.agent(AGENT2))["task"]
    host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "done"}, "note": "Launched"}, host.agent(AGENT2),
              expected_revision=detail["revision"])
    thread = host.read("tasks_thread_get", {"task_id": task["id"]}, host.agent())
    assert [entry["body"] for entry in thread["notes"] if entry["entry_kind"] == "note"] == ["Launched"]

    # The driver works the queue strictly in order, and the namespace owner sees every step of its own input.
    order = []
    for index, row in enumerate(rows):
        input_id = host.query("SELECT id FROM context_inputs WHERE sequence=?", row["sequence"])[0]["id"]
        event_id = f"ev{index}ab"
        with host.database.transaction() as uow:
            transitions.start_attempt(uow, driver_id=WORKER, input_id=input_id, context_event_id=event_id)
        with host.database.transaction() as uow:
            transitions.record_published(uow, driver_id=WORKER, input_id=input_id, ordubashy_id=COORDINATOR, outcomes=host.outcomes,
                                         result={"context_event_id": event_id, "published_commit": "c%d" % index},
                                         handoff_note={"disposition": "empty", "text": ""})
        order.append(input_id)
    states = [item["state"] for item in host.read("context_queue_list", {}, host.agent())["items"]]
    assert states == ["published"] * 3 and submitted["id"] in order
    assert host.read("context_queue_list", {}, outsider)["items"] == [] and host.read("context_queue_list", {}, outsider)["queue_blocked"] is False
