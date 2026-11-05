"""The installation owner's ordinary team comments are refused with channel_required.

Agents, system principals and humans who are not the installation owner keep
their shared writes, formal Signoff decisions keep their shared decision note,
and historical owner rows stay exactly where they were written.
"""
from dataclasses import replace

import pytest

from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, OperationResult,
                                Principal, canonical_hash, new_id, plain)
from bazarlyk.transport.results import http_status
from foundation_support import AGENT, BUSINESS, HUMAN, context
from test_domain_documents import setup_documents
from test_signoffs import scenario
from test_signoff_inputs import FORM, binding
from test_management_backend import install_coordinator

TEAMMATE = "00000000-0000-4000-8000-000000000011"
WORKER = "00000000-0000-4000-8000-000000000012"
CHANNEL = {"channel": "owner_ordubashy", "operation": "owner_conversation_send"}


def details(operation):
    """The private channel to use instead, plus the team write that was refused."""
    return {**CHANNEL, "refused_operation": operation}


def refused(result, operation):
    """Assert the contract's exact channel_required outcome for `operation`."""
    assert result.status == "error", result.to_dict()
    assert result.error["code"] == "channel_required", result.to_dict()
    assert plain(result.error["details"]) == details(operation)
    assert result.error["retryable"] is False
    return result


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result


def call(app, ctx, operation, arguments, *, revision=None, key=None):
    return app.invoke(ctx, operation, arguments, InvocationOptions(expected_revision=revision,
        idempotency_key=key or canonical_hash([operation, arguments, revision, ctx.principal_id])))


def actor(identity, kind, **kwargs):
    return InvocationContext(Principal(identity, kind), business_id=BUSINESS,
                             allowed_business_ids=frozenset({BUSINESS}), **kwargs)


def workspace(tmp_path):
    """Tasks and Stories with an owner, an agent, a teammate and a system worker."""
    database, app, _ = setup_documents(tmp_path)
    with database.transaction() as uow:
        for identity, kind, name in ((AGENT, "agent", "Agent"), (TEAMMATE, "human", "Teammate"),
                                     (WORKER, "system", "Worker")):
            uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": name})
            uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": identity})
        uow.repository("agent_profiles", business_id=BUSINESS).insert({"agent_id": AGENT, "principal_id": AGENT,
            "namespace": BUSINESS, "name": "agent-one", "workspace": str(tmp_path / "agent-one"),
            "profile_json": {"scope": {"kind": "business", "business_id": BUSINESS}}})
    return database, app


def agent_context():
    return actor(AGENT, "agent", home_business_id=BUSINESS, source="mcp", profile_revision=1)


def task_revision(app, ctx, task_id, **args):
    return call(app, ctx, "tasks_get", {"task_id": task_id, **args}).data["task"]["revision"]


def story_revision(app, ctx, story_id):
    return call(app, ctx, "stories_get", {"story_id": story_id}).data["story"]["revision"]


def register_owner(database):
    with database.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                               "owner_principal_id": HUMAN})


def notes_of(app, ctx, task_id, **args):
    """Authored comments in the shared task thread, without semantic change entries."""
    notes = call(app, ctx, "tasks_thread_get", {"task_id": task_id, **args}).data["notes"]
    return [note["body"] for note in notes if note.get("entry_kind") != "system"]


def test_owner_task_comments_are_allowed_but_embedded_notes_keep_their_guard(tmp_path):
    database, app = workspace(tmp_path)
    owner = context()
    ok(call(app, owner, "tasks_create", {"id": "launch", "title": "Launch"}))
    # A comment written before the installation was registered stays readable afterwards.
    ok(call(app, owner, "tasks_comment", {"task_id": "launch", "text": "Historical owner note"},
            revision=task_revision(app, owner, "launch")))
    register_owner(database)
    assert notes_of(app, owner, "launch") == ["Historical owner note"]
    revision = task_revision(app, owner, "launch")
    ok(call(app, owner, "tasks_comment", {"task_id": "launch", "text": "Ordinary owner comment"}, revision=revision))
    revision = task_revision(app, owner, "launch")
    refused(call(app, owner, "tasks_update", {"task_id": "launch", "changes": {"title": "Renamed"},
                                              "note": "Why I renamed it"}, revision=revision), "tasks_update")
    refused(call(app, owner, "tasks_complete", {"task_id": "launch", "note": "Finished"}, revision=revision),
            "tasks_complete")
    refused(call(app, owner, "tasks_create", {"id": "second", "title": "Second", "note": "Opening comment"}),
            "tasks_create")
    # Ordinary task mutations without a note remain the owner's to make.
    assert task_revision(app, owner, "launch") == revision
    ok(call(app, owner, "tasks_update", {"task_id": "launch", "changes": {"title": "Renamed"}}, revision=revision))
    ok(call(app, owner, "tasks_create", {"id": "third", "title": "Third"}))
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM task_notes").fetchone()[0] == 2
        assert uow.connection.execute("SELECT count(*) FROM tasks WHERE id='second'").fetchone()[0] == 0
    assert notes_of(app, owner, "launch") == ["Ordinary owner comment", "Historical owner note"]


def test_agents_system_principals_and_other_humans_keep_their_team_writes(tmp_path):
    database, app = workspace(tmp_path)
    register_owner(database)
    owner = context()
    ok(call(app, owner, "tasks_create", {"id": "shared", "title": "Shared"}))
    for ctx in (agent_context(), actor(TEAMMATE, "human", source="dashboard"),
                actor(WORKER, "system", source="worker")):
        ok(call(app, ctx, "tasks_comment", {"task_id": "shared", "text": "Team note from " + ctx.principal_kind},
                revision=task_revision(app, owner, "shared")))
    ok(call(app, agent_context(), "tasks_update", {"task_id": "shared", "changes": {"status": "done"},
            "note": "Agent closing note"}, revision=task_revision(app, owner, "shared")))
    assert len(notes_of(app, owner, "shared")) == 4


def test_guard_needs_a_registered_installation_owner(tmp_path):
    _, app = workspace(tmp_path)
    owner = context()
    ok(call(app, owner, "tasks_create", {"id": "unregistered", "title": "Unregistered"}))
    ok(call(app, owner, "tasks_comment", {"task_id": "unregistered", "text": "No installation yet"},
            revision=task_revision(app, owner, "unregistered")))


def test_owner_global_task_comments_are_refused_in_global_scope(tmp_path):
    database, app = workspace(tmp_path)
    register_owner(database)
    owner = replace(context(), business_id=None, review_business_id=None,
                    global_capabilities=frozenset({"read_global_context"}))
    ok(call(app, owner, "tasks_create", {"scope": "global", "id": "global-task", "title": "Global"}))
    revision = task_revision(app, owner, "global-task", scope="global")
    refused(call(app, owner, "tasks_comment", {"scope": "global", "task_id": "global-task", "text": "Owner comment"},
                 revision=revision), "tasks_comment")
    refused(call(app, owner, "tasks_create", {"scope": "global", "id": "global-two", "title": "Two", "note": "Note"}),
            "tasks_create")
    ok(call(app, owner, "tasks_update", {"scope": "global", "task_id": "global-task",
                                         "changes": {"title": "Global renamed"}}, revision=revision))
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM task_notes").fetchone()[0] == 0


def test_owner_story_notes_and_drafts_are_refused_while_state_changes_and_history_remain(tmp_path):
    database, app = workspace(tmp_path)
    owner = context()
    story = ok(call(app, owner, "stories_create", {"title": "Launch", "purpose": "Publish the launch"})).data["story"]
    ok(call(app, owner, "stories_note_add", {"story_id": story["id"], "body": "Historical owner note"}))
    register_owner(database)
    ok(call(app, owner, "stories_note_add", {"story_id": story["id"], "body": "Ordinary owner note"}))
    # A draft exists only to become a committed discussion note, so both halves are refused.
    draft = new_id()
    refused(call(app, owner, "stories_draft_save", {"story_id": story["id"], "draft_id": draft, "body": "Drafted"}),
            "stories_draft_save")
    ok(call(app, agent_context(), "stories_note_add", {"story_id": story["id"], "body": "Agent discussion note"}))
    teammate = actor(TEAMMATE, "human", source="dashboard")
    # Another human still drafts and commits an ordinary discussion note.
    teammate_draft = new_id()
    ok(call(app, teammate, "stories_draft_save",
            {"story_id": story["id"], "draft_id": teammate_draft, "body": "Teammate discussion note"}))
    ok(call(app, teammate, "stories_draft_commit", {"draft_id": teammate_draft}, revision=1))
    # A reason inseparable from a formal state change stays shared.
    ok(call(app, owner, "stories_close", {"story_id": story["id"], "outcome": "success", "note": "Why we closed it"},
            revision=story_revision(app, owner, story["id"])))
    events = ok(call(app, owner, "stories_timeline", {"story_id": story["id"]})).data["events"]
    assert [event["body"] for event in events if event["kind"] == "close"] == ["Why we closed it"]
    timeline = ok(call(app, owner, "stories_timeline", {"story_id": story["id"], "notes_only": True})).data["events"]
    assert [event["body"] for event in timeline] == ["Teammate discussion note", "Agent discussion note",
                                                     "Ordinary owner note", "Historical owner note"]
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM story_drafts").fetchone()[0] == 0


def test_owner_story_draft_commit_of_an_existing_draft_is_refused(tmp_path):
    database, app = workspace(tmp_path)
    owner = context()
    story = ok(call(app, owner, "stories_create", {"title": "Launch", "purpose": "Publish the launch"})).data["story"]
    draft = new_id()
    ok(call(app, owner, "stories_draft_save", {"story_id": story["id"], "draft_id": draft, "body": "Drafted earlier"}))
    register_owner(database)
    # A draft written before the guard existed cannot become a shared note now.
    refused(call(app, owner, "stories_draft_commit", {"draft_id": draft}, revision=1), "stories_draft_commit")
    # Discarding it, which writes no discussion row, remains available.
    ok(call(app, owner, "stories_draft_discard", {"draft_id": draft}, revision=1))
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM story_events WHERE kind='note'").fetchone()[0] == 0


def test_owner_context_review_cannot_carry_an_embedded_note(tmp_path):
    database, app = workspace(tmp_path)
    owner = context()
    story = ok(call(app, owner, "stories_create", {"title": "Launch", "purpose": "Publish the launch"})).data["story"]
    task = ok(call(app, owner, "tasks_create", {"id": "supplier", "title": "Choose a supplier"})).data["task"]
    ok(call(app, owner, "stories_link_add", {"story_id": story["id"], "resource_type": "tasks",
            "resource_id": task["id"]}, revision=story_revision(app, owner, story["id"])))
    register_owner(database)
    listed = ok(call(app, owner, "stories_context_list", {"story_id": story["id"]})).data["items"][0]
    review = {"story_id": story["id"], "items": [{"link_id": listed["link_id"], "link_revision": listed["link_revision"],
              "review_revision": listed["review_revision"], "source_token": listed["source_token"],
              "result": "incorporated", "explanation": "The supplier task is reflected in the plan."}]}
    refused(call(app, owner, "stories_context_review", {**review, "note": {"body": "Owner review commentary"}},
                 revision=story_revision(app, owner, story["id"])), "stories_note_add")
    # The same acknowledgement without a discussion note is still the owner's to make.
    ok(call(app, owner, "stories_context_review", review, revision=story_revision(app, owner, story["id"])))
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM story_events WHERE kind='note'").fetchone()[0] == 0


def test_a_stored_owner_plan_is_refused_again_when_it_is_applied(tmp_path):
    """Apply re-checks the prepared rows, so a plan stored earlier cannot be replayed."""
    database, app = workspace(tmp_path)
    owner = context()
    ok(call(app, owner, "tasks_create", {"id": "replay", "title": "Replay"}))
    story = ok(call(app, owner, "stories_create", {"title": "Replay", "purpose": "Replay"})).data["story"]
    with database.read() as uow:
        plans = {
            "tasks_comment": app.registry.get("tasks_comment").prepare(owner,
                {"task_id": "replay", "text": "Prepared before the owner was registered"},
                InvocationOptions(expected_revision=task_revision(app, owner, "replay"),
                                  idempotency_key=new_id()), uow),
            "stories_note_add": app.registry.get("stories_note_add").prepare(owner,
                {"story_id": story["id"], "body": "Prepared before the owner was registered"},
                InvocationOptions(idempotency_key=new_id()), uow)}
    register_owner(database)
    for operation, plan in plans.items():
        with pytest.raises(DomainError) as refusal:
            with database.transaction() as uow:
                app.registry.get(operation).apply(owner, plan, uow)
        assert refusal.value.code == "channel_required"
        assert plain(refusal.value.details) == details(operation)
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM task_notes").fetchone()[0] == 0
        assert uow.connection.execute("SELECT count(*) FROM story_events WHERE kind='note'").fetchone()[0] == 0


def test_owner_signoff_comment_is_refused_but_decision_feedback_stays_shared(scenario):
    s = scenario
    install_coordinator(s)
    s.change_override("signoffs_comment", "allow")
    proposal = s.propose()
    with s.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at,refs_json,summary)"
            " VALUES(?,?,?,?,?,'[]',NULL)",
            (new_id(), proposal["id"], "Historical owner note", s.owner.id, "2026-09-08T11:00:00.000000Z"))
    refused(s.app.invoke(s.owner_context, "signoffs_comment",
        {"signoff_id": proposal["id"], "text": "Ordinary owner comment"}, {"idempotency_key": "owner-note"}),
        "signoffs_comment")
    ok(s.app.invoke(s.agent_context(), "signoffs_comment",
        {"signoff_id": proposal["id"], "text": "Agent comment"}, {"idempotency_key": "agent-note"}))
    rejected = s.decide(proposal, "reject", feedback="Please revise the supplier")
    assert rejected.status == "ok", rejected.to_dict()
    notes = ok(s.app.invoke(s.owner_context, "signoffs_notes_list", {"signoff_id": proposal["id"]})).data["notes"]
    assert [note["body"] for note in notes] == ["Historical owner note", "Agent comment", "Please revise the supplier"]
    assert notes[2]["created_by"] == s.owner.id
    assert notes[2]["id"] == rejected.data["signoff"]["decision"]["message_id"]


def test_owner_approval_feedback_and_input_answers_remain_shared(scenario):
    s = scenario
    install_coordinator(s)
    s.change_override("signoffs_input_create", "allow")
    proposal = s.propose("Approved work")
    approved = s.decide(proposal, "approve", feedback="Approved with this context")
    assert approved.status in ("ok", "accepted"), approved.to_dict()
    request = ok(s.app.invoke(s.agent_context(), "signoffs_input_create",
        {"request_key": "supplier", "form": FORM}, {"idempotency_key": new_id()})).data["signoff"]
    answered = s.service.respond(s.owner_context, request["id"], notes="Use the first supplier",
                                 selected_choices=["a"], **binding(request))
    assert answered.status == "ok", answered.to_dict()
    bodies = [note["body"] for note in ok(s.app.invoke(s.owner_context, "signoffs_notes_list",
        {"signoff_id": proposal["id"]})).data["notes"]]
    assert bodies == ["Approved with this context"]
    answers = [note["body"] for note in ok(s.app.invoke(s.owner_context, "signoffs_notes_list",
        {"signoff_id": request["id"]})).data["notes"]]
    assert answers == ["Use the first supplier"]


def test_owner_global_signoff_comment_is_refused(tmp_path):
    from test_service_transports import ServiceFixture

    service = ServiceFixture(tmp_path, full=True)
    app = service.runtime.application
    coordinator = service.fixture.create("ordu", "ordubashy")
    service.fixture.update(coordinator, {"tool_policy": {"overrides": {"tasks_create": "signoff"}}})
    proposer = service.runtime.resolver.invocation_context(coordinator)
    owner = replace(service.fixture.ctx, business_id=None, review_business_id=None)
    proposed = app.invoke(proposer, "tasks_create", {"scope": "global", "title": "Global approved task"},
                          {"idempotency_key": new_id()})
    assert proposed.status == "signoff_required", proposed.to_dict()
    row = ok(app.signoffs.get(owner, proposed.signoff["id"])).data["signoff"]
    assert row["review_scope"]["kind"] == "global"
    refused(app.invoke(owner, "signoffs_comment",
        {"scope": "global", "signoff_id": row["id"], "text": "Owner comment"}, {"idempotency_key": new_id()}),
        "signoffs_comment")


def test_channel_required_is_an_ordinary_outcome_with_a_request_content_status():
    from bazarlyk.incidents import _NORMAL
    from qorqut_operations.team_channel import channel_required

    assert plain(channel_required().details) == CHANNEL
    error = channel_required("tasks_comment")
    assert plain(error.details) == details("tasks_comment")
    assert http_status(OperationResult.failure(error, request_id=new_id())) == 422
    assert http_status(OperationResult.failure(DomainError("validation_error", "x"), request_id=new_id())) == 422
    assert "channel_required" in _NORMAL


def test_legacy_task_note_adapter_surfaces_the_refusal_over_http(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from test_service_transports import ServiceFixture, ORIGIN

    service = ServiceFixture(tmp_path, full=True)
    monkeypatch.setattr(service.runtime.background, "start", lambda: None)
    slug = "widget"
    with TestClient(service.web) as client:
        created = client.post("/api/biz/%s/task" % slug, json={"title": "Legacy route task"}, headers=ORIGIN)
        assert created.status_code == 201, created.text
        task = created.json()["task"]
        for payload, operation in (({"note": "Legacy owner comment"}, "tasks_comment"),
                                   ({"expected_revision": task["revision"], "set": {"status": "done"},
                                     "note": "Legacy owner note"}, "tasks_update")):
            if operation == "tasks_update": payload["expected_revision"] = task["revision"]
            response = client.post("/api/biz/%s/task/%s" % (slug, task["id"]), json=payload, headers=ORIGIN)
            if operation == "tasks_comment":
                assert response.status_code == 200, response.text
                task = client.get("/api/biz/%s/task/%s" % (slug, task["id"])).json()["task"]
                continue
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "channel_required", response.text
            assert plain(response.json()["error"]["details"]) == details(operation)
        done = client.post("/api/biz/%s/task/%s/done" % (slug, task["id"]),
                           json={"note": "Legacy completion note"}, headers=ORIGIN)
        assert done.status_code == 422, done.text
        assert done.json()["error"]["code"] == "channel_required", done.text
        current = client.get("/api/biz/%s/task/%s" % (slug, task["id"])).json()["task"]
        assert current["status"] != "done" and current["notes"]
