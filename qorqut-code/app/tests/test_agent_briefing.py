"""Exercise briefing selection through real profiles, policy and domain storage."""

import io
import json
from pathlib import Path
import time

import pytest
from starlette.testclient import TestClient

from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from bazarlyk.bootstrap.claude_session import run
from qorqut_operations.contracts import InvocationOptions, new_id
from test_service_transports import ServiceFixture


@pytest.fixture
def service(tmp_path):
    return ServiceFixture(tmp_path, full=True)


def call(service, name, args=None, *, principal=None, revision=None):
    ctx = (service.runtime.resolver.invocation_context(principal) if principal else service.fixture.ctx)
    result = service.runtime.application.invoke(ctx, name, args or {},
        InvocationOptions(expected_revision=revision, idempotency_key=new_id())).to_dict()
    assert result["status"] in {"ok", "accepted"}, result
    return result["data"]


def briefing(service, principal=None):
    return call(service, "agent_config_get", principal=principal or service.agent)


def link(service, source, target, role="concerns", context="Relevant evidence"):
    return call(service, "relationships_create", {"source": source, "target": target, "role": role, "context": context})["relationship"]


def item(kind, identity):
    return {"type": kind, "id": identity}


def test_current_work_neighborhood_and_explicit_responsibility(service):
    # The business agent opens the task with its comment; the installation owner's own
    # comments belong to the private channel.
    task = call(service, "tasks_create", {"title": "Interview trial users", "note": "Ask about the missing onboarding step exactly.",
        "assignee_ids": [service.agent.id, service.manager.id]}, principal=service.agent)["task"]
    story = call(service, "stories_create", {"title": "Trial activation", "purpose": "Reduce early churn",
        "details": "Measure whether the second session occurs.", "situation": "Three interviews remain."})["story"]
    topic = call(service, "context_topics_create", {"title": "Activation evidence", "slug": "activation-evidence"})["document"]
    call(service, "context_document_update", {"document_id": topic["id"], "body": "Users cannot find their saved draft."}, revision=topic["revision"])
    edge = link(service, item("task", task["id"]), item("story", story["id"]), "part_of")
    link(service, item("topic", topic["id"]), item("story", story["id"]), "informs")
    note = call(service, "notes_create", {"title": "Interview note", "body": "The customer requested a simpler first session."})["document"]
    link(service, item("material", note["id"]), item("story", story["id"]))
    unrelated = call(service, "stories_create", {"title": "Unrelated legal work", "purpose": "Not this agent's work"})["story"]
    raw = call(service, "context_upload", {"title": "Mixed source", "body": "RAW UPLOAD MUST NOT APPEAR"})["document"]
    link(service, item("material", raw["id"]), item("story", story["id"]))
    service.fixture.update(service.agent, {"tool_policy": {"overrides": {"tables_rows_write": "signoff"}}})
    start = time.monotonic()
    data = briefing(service)
    print("Briefing seconds:", round(time.monotonic() - start, 3))
    text = data["work_briefing"]["rendered"]
    for expected in (task["title"], "Ask about the missing onboarding step exactly.", story["details"], story["situation"],
                     "Users cannot find their saved draft.", "The customer requested a simpler first session.", edge["id"], "part_of", "informs", "Needs Context Review", "tasks_assign [allow]", "tables_rows_write [signoff]"):
        assert expected in text
    assert "RAW UPLOAD MUST NOT APPEAR" not in text
    assert raw["id"] not in text
    # The business agent manages its business: other work is labelled as overview, not assignment.
    assert "explicitly assigned to you" in text and unrelated["title"] in text
    assert "project overview for your manager role" in text
    assert service.agent.id in text and service.manager.id in text
    assert data["work_briefing"]["read_only"]
    assert len(text) + len(data["effective_config"]["mandatory_context"]["rendered"]) + 2 <= MAX_CONTEXT_CHARACTERS
    with service.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM context_reviews").fetchone()[0] == 0
    queue = call(service, "context_reviews_list", {"item": item("story", story["id"])})
    observed = next(r for r in queue["items"] if r["source"]["id"] == topic["id"])
    decision = {**{key: observed[key] for key in ("relationship_id", "relationship_revision", "review_revision", "source_token")},
                "result": "no_change", "explanation": "Already reflected in the plan"}
    call(service, "context_review", {"item": item("story", story["id"]), "items": [decision]}, revision=queue["item_revision"])
    briefing(service)
    after = call(service, "context_reviews_list", {"item": item("story", story["id"]), "include_reviewed": True})
    assert not next(r for r in after["items"] if r["source"]["id"] == topic["id"])["pending"]
    with service.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM context_reviews").fetchone()[0] == 1


def test_dynamic_refresh_does_not_change_configuration_or_prepare(service):
    before = briefing(service)
    task = call(service, "tasks_create", {"title": "Brand new assignment", "assignee_ids": [service.agent.id]})["task"]
    after = briefing(service)
    assert before["effective_config"]["effective_config_id"] == after["effective_config"]["effective_config_id"]
    assert after["effective_config"]["preparation"]["ready"]
    assert task["title"] not in before["work_briefing"]["rendered"]
    assert task["title"] in after["work_briefing"]["rendered"]
    call(service, "tasks_update", {"task_id": task["id"], "changes": {"title": "Updated assignment"}}, revision=task["revision"])
    assert "Updated assignment" in briefing(service)["work_briefing"]["rendered"]
    call(service, "tasks_complete", {"task_id": task["id"]}, revision=2)
    assert "Updated assignment" not in briefing(service)["work_briefing"]["rendered"]


def test_denied_reads_and_other_business_never_leak(service):
    task = call(service, "tasks_create", {"title": "DENIED TASK SECRET", "assignee_ids": [service.agent.id]})["task"]
    story = call(service, "stories_create", {"title": "Visible story", "purpose": "Visible"})["story"]
    link(service, item("agent", service.agent.id), item("story", story["id"]), "manages")
    link(service, item("task", task["id"]), item("story", story["id"]), "part_of")
    with service.fixture.db.transaction() as uow:
        row = uow.repository("agent_profiles", business_id=service.fixture.business_id).get(service.agent.id)
        profile = row["profile_json"]
        profile["tool_policy"]["overrides"].update(tasks_list="deny", tasks_get="deny")
        profile["profile_revision"] = row["revision"] + 1
        uow.repository("agent_profiles", business_id=service.fixture.business_id).update(service.agent.id, {"profile_json": profile}, expected_revision=row["revision"])
        foreign = new_id()
        uow.repository("businesses").insert({"id": foreign, "name": "Other", "slug": "other", "workspace": "/other", "hq_path": "/other/hq"})
        uow.repository("stories", business_id=foreign).insert({"id": new_id(), "title": "FOREIGN SECRET", "purpose": "Secret", "state": "planned", "created_by": service.fixture.owner.id})
    text = briefing(service)["work_briefing"]["rendered"]
    assert "DENIED TASK SECRET" not in text and task["id"] not in text
    assert "FOREIGN SECRET" not in text
    assert "tasks_list: not available" in text


def test_hook_lifecycle_emits_briefing_and_keeps_config_receipt(service):
    task = call(service, "tasks_create", {"title": "Warmup integration", "assignee_ids": [service.agent.id]})["task"]
    config = service.runtime.resolver.resolve_agent_config(service.agent).to_dict()
    path = Path(config["workspace"]) / ".bazarlyk/registration.json"
    class Client:
        def __init__(self, registration): pass
        def post(self, route, values):
            resolver = service.runtime.resolver
            return (resolver.record_delivery if route.endswith("receipt") else resolver.bootstrap)(service.agent, **values)
    for event in ("startup", "resume", "clear", "compact", "fork"):
        output, diagnostics = io.StringIO(), io.StringIO()
        run({"hook_event_name": "SessionStart", "source": event, "session_id": "briefing-test"}, path,
            stream=output, diagnostics=diagnostics, client_factory=Client)
        text = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
        assert "Universal Foundation" in text and task["title"] in text
        assert len(text) <= MAX_CONTEXT_CHARACTERS
        assert "receipt could not" not in diagnostics.getvalue()
    output = io.StringIO()
    run({"hook_event_name": "UserPromptSubmit", "session_id": "briefing-test"}, path,
        stream=output, diagnostics=io.StringIO(), client_factory=Client)
    assert "INBOX WATCHER CHECK" in json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
    with service.fixture.db.read() as uow:
        row = uow.repository("session_deliveries").list(limit=1)[0]
        assert row["bundle_hash"] == config["mandatory_context"]["bundle_hash"]
        assert row["metadata_json"]["briefing_hash"]


def test_agents_can_use_the_documented_read_edit_and_relationship_interfaces(service):
    agent = service.agent
    story = call(service, "stories_create", {"title": "Agent-authored campaign", "purpose": "Exercise real grants"}, principal=agent)["story"]
    task = call(service, "tasks_create", {"title": "Agent-authored task", "assignee_ids": [agent.id], "story_ids": [story["id"]]}, principal=agent)["task"]
    edge = call(service, "relationships_create", {"source": item("agent", agent.id), "target": item("story", story["id"]),
        "role": "manages", "context": "Responsible for planning"}, principal=agent)["relationship"]
    call(service, "relationships_update", {"relationship_id": edge["id"], "changes": {"context": "Responsible for planning and evidence"}},
         principal=agent, revision=edge["revision"])
    call(service, "stories_update", {"story_id": story["id"], "changes": {"details": "Read evidence before preparing outreach."}}, principal=agent, revision=story["revision"])
    call(service, "tasks_comment", {"task_id": task["id"], "text": "Checked the latest brief."}, principal=agent, revision=task["revision"])
    text = briefing(service)["work_briefing"]["rendered"]
    assert "Responsible for planning and evidence" in text
    assert "Read evidence before preparing outreach." in text
    assert "Checked the latest brief." in text
    call(service, "relationships_remove", {"relationship_id": edge["id"]}, principal=agent, revision=2)
    assert edge["id"] not in briefing(service)["work_briefing"]["rendered"]


def test_large_briefing_is_bounded_and_reports_excerpts_and_pagination(service):
    with service.fixture.db.transaction() as uow:
        for index in range(45):
            identity = new_id()
            uow.repository("tasks", business_id=service.fixture.business_id).insert({"id": identity, "title": f"Active task {index}", "status": "open", "created_by": service.fixture.owner.id})
            uow.repository("relationships", business_id=service.fixture.business_id).insert({"id": new_id(), "from_type": "task", "from_id": identity,
                "to_type": "agent", "to_id": service.agent.id, "role": "assigned_to"})
            uow.repository("task_notes", business_id=service.fixture.business_id).insert({"id": new_id(), "task_id": identity,
                "body": "Detailed working instruction. " * 500, "created_by": service.fixture.owner.id})
    data = briefing(service)
    text = data["work_briefing"]["rendered"]
    assert len(text) > 9000
    assert len(text) + len(data["effective_config"]["mandatory_context"]["rendered"]) + 2 <= MAX_CONTEXT_CHARACTERS
    assert "Excerpt ends here" in text
    assert "tasks_list has more results" in text
    assert data["work_briefing"]["omitted_counts"]["Current Work"] > 0


def test_global_briefing_does_not_aggregate_business_work(service):
    principal = service.fixture.create("chief", "ordubashy")
    call(service, "stories_create", {"title": "Business-only campaign", "purpose": "Business detail"})
    global_data = briefing(service, principal)
    assert "Business-only campaign" not in global_data["work_briefing"]["rendered"]
    assert "does not aggregate project work" in global_data["work_briefing"]["rendered"]
    selected = call(service, "agent_config_get", {"business_id": service.fixture.business_id}, principal=principal)
    assert "Business-only campaign" in selected["work_briefing"]["rendered"]


def test_http_and_mcp_deliver_same_live_work(service):
    task = call(service, "tasks_create", {"title": "Visible over the wire", "assignee_ids": [service.agent.id]})["task"]
    headers = {"Authorization": "Bearer " + service.tokens[service.agent.id]}
    body = {"registration_id": service.registrations[service.agent.id], "session_id": new_id(), "event": "startup"}
    with TestClient(service.web) as client:
        response = client.post("/api/agent/bootstrap", headers=headers, json=body)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert task["title"] in data["work_briefing"]["rendered"]
        receipt = client.post("/api/agent/bootstrap/receipt", headers=headers, json={**body,
            "effective_config_id": data["effective_config_id"], "bundle_hash": data["mandatory_context"]["bundle_hash"],
            "briefing_hash": data["work_briefing"]["bundle_hash"], "emitted_at": "2026-09-09T00:00:00+00:00"})
        assert receipt.status_code == 200, receipt.text
        mcp_headers = service.initialize(client, service.agent)
        result = service.tool(client, mcp_headers, "agent_config_get", {"response_detail": "full"})
        assert task["title"] in json.dumps(result)


def test_rejection_feedback_is_injected_without_reapplying_the_proposal(service):
    ctx = service.runtime.resolver.invocation_context(service.agent)
    result = service.runtime.application.invoke(ctx, "context_people_create", {"name": "Proposed contact"},
        InvocationOptions(idempotency_key=new_id()))
    assert result.status == "signoff_required", result.to_dict()
    proposal = dict(result.signoff)
    service.runtime.signoffs.review(service.fixture.ctx, proposal["id"], decision="reject",
        proposal_revision=proposal["proposal_revision"], payload_hash=proposal["payload_hash"],
        expected_status_revision=proposal["status_revision"], idempotency_key=new_id(),
        feedback="Do not create this contact; verify the organization first.")
    text = briefing(service)["work_briefing"]["rendered"]
    assert "Do not create this contact; verify the organization first." in text
    assert '"state": "rejected"' in text
    assert not call(service, "context_items_list", {"type": "person", "query": "Proposed contact"})["items"]


def test_repeated_briefings_do_not_reload_published_packages(service, monkeypatch):
    import bazarlyk.catalog.packages as packages
    task = call(service, "tasks_create", {"title": "Reuse pinned packages", "assignee_ids": [service.agent.id]})["task"]
    story = call(service, "stories_create", {"title": "Package reuse", "purpose": "Keep reads cheap"})["story"]
    link(service, item("task", task["id"]), item("story", story["id"]))
    briefing(service)
    loads = []
    original = packages.load_package
    monkeypatch.setattr(packages, "load_package", lambda *args: (loads.append(args[0]), original(*args))[1])
    rendered = briefing(service)["work_briefing"]["rendered"]
    assert "Reuse pinned packages" in rendered and "Package reuse" in rendered
    assert loads == []
