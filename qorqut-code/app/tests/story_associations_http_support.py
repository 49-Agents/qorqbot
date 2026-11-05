"""One inert synthetic Story-association composition for the real HTTP-to-dashboard acceptance check.

The composed host comes from the existing ``project_composition_support`` fixture: the real assembled schema and catalog,
the owner HTTP application with dashboard compatibility routes and the combined dashboard's own static files, running on
one disposable loopback server. This module adds only the records this scenario needs through the ordinary supported
operations (Task creation, Action recording, Signoff retention, relationship creation and private conversation sends).
Nothing here is a product handler: the browser check reads the real ``tasks_associated_records`` response from the real
dashboard, then opens the child record through the real record workspace.

The Action is attached through the canonical ``task_id`` argument, the Signoff through an ordinary ``concerns``
relationship, the same Action is reached through two duplicate ``concerns`` relationships, and one unrelated Task's
Action must stay out of the Story's associated history. All records are synthetic, disposable and scoped to one
pytest ``tmp_path`` instance. No live data, model, provider, native agent, routine/job execution or service is used.
"""

from __future__ import annotations

import copy
import json

from qorqut_operations.contracts import new_id

from project_composition_support import (DASHBOARD, SIGNOFF_PREVIEW, build_composed, call, insert_signoff, ok,
                                         write_fixture)

BROWSER_TEST = DASHBOARD / "tests" / "test_story_associations_http_browser.js"

STORY_TITLE = "Harbor rework story"
STORY_OVERVIEW = "Harbor rework story overview.\n\nThe nested checks are stored against its descendants."
CHILD_TITLE = "Draft the harbor rework"
CHILD_OVERVIEW = "The harbor rework draft is ready for its nested verification."
GRANDCHILD_TITLE = "Verify the nested harbor rework"
UNRELATED_TITLE = "Unrelated harbor errand"
ASSOCIATED_ACTION_TEXT = "Validated the nested harbor rework check"
UNRELATED_ACTION_TEXT = "Recorded an unrelated harbor errand result"
SIGNOFF_TITLE = "Approve the nested harbor rework"
PARENT_MESSAGE = "Parent Story private message for the acceptance check"
CHILD_MESSAGE = "Child Story private message for the acceptance check"


def _signoff_preview():
    """The retained canonical provider envelope, renamed for this scenario; no send or approval follows."""
    preview = copy.deepcopy(SIGNOFF_PREVIEW["whatsapp"])
    preview["title"] = SIGNOFF_TITLE
    preview["rationale"] = "The nested harbor rework needs the owner's decision."
    return preview


def _project_recorder(host, business):
    """One synthetic project owner for the Action writes (the composed fixture seeds project owners the same way).

    The installation owner's global capability makes ``actions_record`` store a Global Action, which cannot name a
    Project Task through its canonical ``task_id`` argument. A project-scoped owner records the Action in the Project's
    own storage, exactly as a project member does; the Action itself is created through the ordinary operation."""
    identity = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": "human",
                                             "display_name": "Harbor Rework Recorder"})
        uow.repository("business_memberships", business_id=business).insert(
            {"principal_id": identity, "is_owner": True})
    with host.database.read() as uow:
        return host.runtime.policy.context_for(identity, business_id=business, review_business_id=business,
                                               uow=uow, source="dashboard")


def build_story_associations(tmp_path):
    """The composed owner application plus the Story hierarchy and associations this check reads through HTTP."""
    host = build_composed(tmp_path)
    harbor = host.fixture["projects"]["harbor"]
    owner = host.context(harbor)
    recorder = _project_recorder(host, harbor)

    story = ok(call(host, owner, "tasks_create", {
        "title": STORY_TITLE, "task_type": "story", "overview": STORY_OVERVIEW}))["task"]
    child = ok(call(host, owner, "tasks_create", {
        "title": CHILD_TITLE, "task_type": "story", "parent_id": story["id"], "overview": CHILD_OVERVIEW}))["task"]
    grandchild = ok(call(host, owner, "tasks_create", {
        "title": GRANDCHILD_TITLE, "parent_id": child["id"]}))["task"]
    unrelated = ok(call(host, owner, "tasks_create", {"title": UNRELATED_TITLE}))["task"]

    # The associated Action is attached through the canonical task_id argument (stored as refs_json.task).
    associated = ok(call(host, recorder, "actions_record", {
        "text": ASSOCIATED_ACTION_TEXT, "category": "business-operational", "business_ids": [harbor],
        "task_id": grandchild["id"], "real_time": "2026-10-28"}))["action"]
    # The unrelated Task's Action is readable in the Project but is not part of this Story's associated history.
    unrelated_action = ok(call(host, recorder, "actions_record", {
        "text": UNRELATED_ACTION_TEXT, "category": "business-operational", "business_ids": [harbor],
        "task_id": unrelated["id"], "real_time": "2026-10-28"}))["action"]

    # The same Action is reached twice through the child Story's concerns relationship; the real read must dedupe.
    for _ in range(2):
        ok(call(host, owner, "relationships_create", {
            "source": {"type": "task", "id": child["id"]},
            "target": {"type": "action", "id": associated["action_id"]},
            "role": "concerns"}))

    # A retained canonical Signoff associated with the child Story through the same documented concerns relationship.
    signoff = insert_signoff(host, harbor, "story-rework-signoff", preview=_signoff_preview())
    ok(call(host, owner, "relationships_create", {
        "source": {"type": "task", "id": child["id"]},
        "target": {"type": "signoff", "id": signoff},
        "role": "concerns"}))

    # Each record keeps its own private conversation, so opening the child must retain the child's source.
    for task, text in ((story, PARENT_MESSAGE), (child, CHILD_MESSAGE)):
        ok(call(host, owner, "owner_conversation_send", {
            "source": {"type": "task", "id": task["id"], "business_id": harbor}, "text": text}))

    with host.database.read() as uow:
        slug = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (harbor,)).fetchone()["slug"]
        # The fixture states this check relies on are the ordinary supported creation paths' own stored shapes.
        associated_row = uow.connection.execute(
            "SELECT refs_json,business_id FROM action_logs WHERE action_key=?", (associated["action_id"],)).fetchone()
        unrelated_row = uow.connection.execute(
            "SELECT refs_json,business_id FROM action_logs WHERE action_key=?", (unrelated_action["action_id"],)).fetchone()
        assert json.loads(associated_row["refs_json"]).get("task") == grandchild["id"]
        assert json.loads(unrelated_row["refs_json"]).get("task") == unrelated["id"]
        assert associated_row["business_id"] == harbor
        assert unrelated_row["business_id"] == harbor
        duplicate_concerns = uow.connection.execute(
            "SELECT count(*) count FROM relationships WHERE business_id=? AND from_type='task' AND from_id=? "
            "AND to_type='action' AND to_id=? AND role='concerns'",
            (harbor, child["id"], associated["action_id"])).fetchone()["count"]
        signoff_concerns = uow.connection.execute(
            "SELECT count(*) count FROM relationships WHERE business_id=? AND from_type='task' AND from_id=? "
            "AND to_type='signoff' AND to_id=? AND role='concerns'",
            (harbor, child["id"], signoff)).fetchone()["count"]
        assert duplicate_concerns == 2
        assert signoff_concerns == 1
        parents = {row["id"]: row["parent_id"] for row in uow.connection.execute(
            "SELECT id,parent_id FROM tasks WHERE id IN (?,?,?)", (story["id"], child["id"], grandchild["id"]))}
        assert parents == {story["id"]: None, child["id"]: story["id"], grandchild["id"]: child["id"]}

    host.fixture["story_associations"] = {
        "project_id": harbor,
        "project_name": host.fixture["project_names"]["harbor"],
        "project_slug": slug,
        "story": {"id": story["id"], "title": STORY_TITLE, "message": PARENT_MESSAGE,
                  "overview": "Harbor rework story overview."},
        "child": {"id": child["id"], "title": CHILD_TITLE, "message": CHILD_MESSAGE},
        "grandchild": {"id": grandchild["id"], "title": GRANDCHILD_TITLE},
        "unrelated": {"id": unrelated["id"], "title": UNRELATED_TITLE},
        "action": {"id": associated["action_id"], "label": associated["summary"]},
        "unrelated_action": {"id": unrelated_action["action_id"], "label": unrelated_action["summary"]},
        "signoff": {"id": signoff, "label": SIGNOFF_TITLE},
    }
    return host