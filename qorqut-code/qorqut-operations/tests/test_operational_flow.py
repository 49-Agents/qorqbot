"""One small composed flow: Tasks, Actions, the tag catalog and the discussion in one engine, with Actions admitted to a test-owned port.

The Context queue half of this flow (one ordered queue across Actions and sources, driven in order) lives in qorqut-context's
integration tests."""

from __future__ import annotations

import pytest

from operational_support import AGENT2, BUSINESS, OTHER, OTHER_AGENT, OperationalHost


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def admitted(host):
    return host.query("SELECT action_key, revision, operation, business_id, submitted_by FROM admitted_revisions ORDER BY rowid")


def test_a_task_and_an_action_share_the_tag_catalog_and_the_signoff_commit_admits_the_revision_with_the_action(host):
    task = host.create_task("Prepare launch", assignee_ids=[AGENT2], tags=["launch", "Q4"])
    action = host.record_action("Launch checklist reviewed", ctx=host.agent(AGENT2), task_id=task["id"], tags=["Launch"])

    # The tag catalog holds each label once; neither it nor the task link widened anyone's access.
    assert [item["label"] for item in host.read("tags_list", {}, host.agent())["tags"]] == ["launch", "Q4"]
    outsider = host.agent(OTHER_AGENT, OTHER)
    assert host.read("tasks_list", {}, outsider)["tasks"] == [] and host.read("actions_list", {}, outsider)["actions"] == []

    # A Signoff-protected correction stages first and then commits revision, Action and the admitted revision together.
    host.agents.overrides["actions_correct"] = "signoff"
    signoff_id = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Launch checklist signed off"},
                            host.agent(AGENT2), expected_revision=1)
    assert [row["revision"] for row in admitted(host)] == [1]
    assert host.apply_approved(signoff_id)["state"] == "applied"
    rows = admitted(host)
    assert [(row["revision"], row["operation"]) for row in rows] == [(1, "add"), (2, "edit")]
    assert {row["business_id"] for row in rows} == {BUSINESS} and rows[1]["submitted_by"] == AGENT2
    assert [call[:2] for call in host.admissions.calls] == [(action["action_id"], 1), (action["action_id"], 2)]

    # The Task discussion has one writer: the assignee's completion is a required event for the creator's work and a thread entry.
    detail = host.read("tasks_get", {"task_id": task["id"]}, host.agent(AGENT2))["task"]
    host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "done"}, "note": "Launched"}, host.agent(AGENT2),
              expected_revision=detail["revision"])
    thread = host.read("tasks_thread_get", {"task_id": task["id"]}, host.agent())
    assert [entry["body"] for entry in thread["notes"] if entry["entry_kind"] == "note"] == ["Launched"]
