from pathlib import Path

import pytest
from starlette.testclient import TestClient
from qorqut_business_docs_versioning import HistoryStore

from bazarlyk.bootstrap.learning_applier import prepare_learning_applier
from qorqut_operations.contracts import Principal, new_id
from qorqut_operations.document_history.text import namespace
from test_background_programs import Model
from test_service_transports import ServiceFixture


@pytest.fixture(scope="module")
def companion(tmp_path_factory):
    model = Model()
    service = ServiceFixture(tmp_path_factory.mktemp("learning-decisions"), full=True,
                             model_client_factory=lambda business: model)
    service.learning_model = model
    fixture, runtime = service.fixture, service.runtime
    prepared = prepare_learning_applier(runtime, fixture.ctx, role_ref=fixture.refs["learning-applier"],
        allowed_business_ids=[fixture.business_id], endpoint="http://127.0.0.1:9988/mcp", claude_version="2.1.263")
    assert prepared["ready"], prepared
    agent = Principal(prepared["agent_id"], "agent")
    service.tokens[agent.id] = (Path(prepared["workspace"]) / ".bazarlyk/credential").read_text().strip()
    with TestClient(service.web) as client:
        headers = service.initialize(client, agent)
        yield service, client, headers, agent


def seed(service, bucket="delta", *, global_scope=False):
    business = None if global_scope else service.fixture.business_id
    identity = new_id()
    with service.runtime.database.transaction() as uow:
        uow.repository("learning_candidates", business_id=business).insert({"id": identity,
            "candidate_json": {"text": "Keep the corrected launch date", "scope": "global" if global_scope else "biz",
                               "bucket": bucket, "tag": "fact"}})
    return identity, business


def call(companion, identity, business, **changes):
    service, client, headers, _ = companion
    args = {"learning_id": identity, "state": "accepted", "owner_instruction": "Accept that launch-date proposal.",
            "expected_revision": 1, "idempotency_key": "decision-" + identity, **changes}
    if business:
        args["business_id"] = business
    return service.tool(client, headers, "learnings_decide", args)["decoded"]


@pytest.mark.parametrize("state", ["accepted", "rejected"])
@pytest.mark.parametrize("bucket", ["context", "delta"])
@pytest.mark.parametrize("global_scope", [False, True])
def test_instructed_decision_attribution_filing_and_retry(companion, state, bucket, global_scope):
    service, client, headers, agent = companion
    identity, business = seed(service, bucket, global_scope=global_scope)
    with service.runtime.database.read() as uow:
        jobs_before = len(uow.repository("jobs", business_id=business).list())
    quote = ("Accept" if state == "accepted" else "Reject") + " that launch-date proposal."
    result = call(companion, identity, business, state=state, owner_instruction=quote)
    assert result["status"] == "ok", result
    item = result["data"]["item"]
    assert item["state"] == state and item["decision"]["actor"] == agent.id
    assert item["decision"]["note"] is None
    filed = state == "accepted" and bucket == "context"
    assert bool(item["filing"]) == filed
    replay = call(companion, identity, business, state=state, owner_instruction=quote)
    assert replay["status"] == "ok" and replay["data"] == result["data"]
    assert call(companion, identity, business, state=state, owner_instruction=quote,
        expected_revision=2, idempotency_key=new_id())["status"] == "error"
    with service.runtime.database.read() as uow:
        events = [event for event in uow.repository("learning_history", business_id=business).list()
                  if event["candidate_id"] == identity]
        assert len(events) == 1 and events[0]["event"] == "learnings_decide"
        assert events[0]["actor_id"] == agent.id
        assert events[0]["after_json"]["decision"]["owner_instruction"] == quote
        assert events[0]["after_json"]["decision"]["instruction_source"] == "agent_reported_owner_conversation"
        jobs = uow.repository("jobs", business_id=business).list()
        assert len(jobs) == jobs_before + int(filed)
        # The decision is a versioned SQL document; the audit fact is summarised as business activity only.
        store = HistoryStore(uow.connection)
        decision = store.list_versions(namespace(business), "learning-decision:" + identity).versions
        assert len(decision) == 1 and decision[0].author_id == agent.id
        if business:
            facts = [e for e in store.list_events(business, limit=200).events if e.subject_id == identity]
            assert [e.kind for e in facts] == ["learning.changed"] and facts[0].actor_id == agent.id
            assert "launch date" not in repr(facts[0].data)
    for name in ("context_upload", "learnings_review", "learnings_edit", "learnings_keep_both", "signoffs_review"):
        denied = service.tool(client, headers, name, {"idempotency_key": new_id()})
        assert denied["isError"], (name, denied)


@pytest.mark.parametrize("changes", [
    {"owner_instruction": ""}, {"owner_instruction": " \t\n"}, {"owner_instruction": None},
    {"state": "open"}, {"expected_revision": None}, {"expected_revision": 2},
    {"changes": {"text": "Sneak in an edit"}},
])
def test_invalid_or_stale_decisions_have_no_effect(companion, changes):
    service, _, _, _ = companion
    identity, business = seed(service)
    result = call(companion, identity, business, **changes)
    assert result["status"] == "error", result
    with service.runtime.database.read() as uow:
        assert not uow.repository("learning_decisions", business_id=business).get(identity)
        assert not [event for event in uow.repository("learning_history", business_id=business).list()
                    if event["candidate_id"] == identity]


def test_other_agent_and_cross_business_are_denied(companion):
    service, client, _, _ = companion
    identity, business = seed(service)
    manager_headers = service.initialize(client, service.manager)
    result = service.tool(client, manager_headers, "learnings_decide", {"learning_id": identity,
        "business_id": business, "state": "accepted", "owner_instruction": "Accept this.",
        "expected_revision": 1, "idempotency_key": new_id()})
    assert result["isError"], result
    assert call(companion, identity, new_id())["error"]["code"] == "forbidden"


def test_failed_context_intake_can_resume_without_duplicate_filing(companion):
    service, _, _, _ = companion
    runtime = service.runtime
    identity, business = seed(service, "context")
    with runtime.database.read() as uow:
        jobs_before = len(uow.repository("jobs", business_id=business).list())
    runtime.file_applier.after_replace = lambda resource: (_ for _ in ()).throw(OSError("Injected interruption"))
    assert call(companion, identity, business)["status"] == "error"
    status = runtime.application.invoke(service.fixture.ctx, "learnings_intake_status", {"learning_id": identity})
    assert status.status == "ok" and status.data["pending"], status.to_dict()
    runtime.file_applier.after_replace = None
    resumed = call(companion, identity, business)
    assert resumed["status"] == "ok", resumed
    with runtime.database.read() as uow:
        assert len(uow.repository("jobs", business_id=business).list()) == jobs_before + 1
        assert len([event for event in uow.repository("learning_history", business_id=business).list()
                    if event["candidate_id"] == identity]) == 1


def test_omitted_feedback_preserves_owner_note(companion):
    service, _, _, _ = companion
    identity, business = seed(service)
    for revision, state in ((1, "rejected"), (2, "open")):
        result = service.runtime.application.invoke(service.fixture.ctx, "learnings_review",
            {"learning_id": identity, "state": state, "feedback": "Keep this narrow."},
            {"expected_revision": revision, "idempotency_key": new_id()})
        assert result.status == "ok", result.to_dict()
    result = call(companion, identity, business, expected_revision=3)
    assert result["status"] == "ok", result
    assert result["data"]["item"]["decision"]["note"] == "Keep this narrow."


def test_instructed_context_runs_existing_filer_with_fake_provider(companion):
    service, _, _, _ = companion
    identity, business = seed(service, "context")
    topic = service.runtime.application.invoke(service.fixture.ctx, "context_topics_create",
        {"title": "Launch", "slug": "launch"}, {"idempotency_key": new_id()})
    assert topic.status == "ok", topic.to_dict()
    accepted = call(companion, identity, business)
    assert accepted["status"] == "ok", accepted
    filing = accepted["data"]["item"]["filing"]
    service.learning_model.answers.append({"notes": [{"title": "Launch date",
        "topic_id": topic.data["document"]["id"], "paragraph_ids": [0]}]})
    result = service.runtime.background.run_one(filing["job_id"])
    assert result["state"] == "succeeded", result
    assert service.learning_model.calls
    with service.runtime.database.read() as uow:
        notes = [row for row in uow.repository("documents", business_id=business).list()
                 if row["kind"] == "context_note"]
        assert len(notes) == 1
        assert not uow.repository("tasks", business_id=business).list()
