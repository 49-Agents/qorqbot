"""In-process acceptance path through real application operations and routing.

This deliberately uses deterministic worker actions and no model provider. It
proves the service lifecycle and receipts, not model quality or provider-turn
counts, which still need an approved runtime trial.
"""

from datetime import datetime, timedelta, timezone

from bazarlyk.agents.operational_delegation import OperationalDelegationOperations
from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import ArtifactRef, InvocationContext, PolicyDecision, Principal, new_id
from bazarlyk.domains.tasks import TaskOperations
from bazarlyk.inbox.service import InboxService
from bazarlyk.transport.auth import Authentication
from test_operational_delegation_store import setup_store


ROLE = ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64)
CAPABILITIES = frozenset({"manage_delegated_assignments", "read_global_context"})


def _text(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _invoke(application, context, operation, arguments=None, *, key=None, revision=None):
    options = {}
    if key is not None:
        options["idempotency_key"] = key
    if revision is not None:
        options["expected_revision"] = revision
    result = application.invoke(context, operation, arguments or {}, options)
    assert result.status == "ok", result.to_dict()
    return result.data


def test_assignment_waits_routes_two_replies_and_recovers_without_parent_intervention(
        tmp_path, monkeypatch):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    clock = [datetime.now(timezone.utc)]
    store.clock = lambda: _text(clock[0])
    parent_workspace = (tmp_path / "parent").resolve()
    parent_workspace.mkdir()
    peer_id, task_id = new_id(), new_id()
    parent_registration_id, consumer_id = new_id(), new_id()
    state_root = tmp_path / "codex-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    with database.transaction() as uow:
        profiles = uow.repository("agent_profiles", business_id=None)
        profile = profiles.get(parent_id)
        profile_json = dict(profile["profile_json"])
        profile_json["scope"] = {**profile_json["scope"],
            "global_capabilities": sorted(CAPABILITIES)}
        updated_profile = profiles.update(parent_id, {"profile_json": profile_json},
            expected_revision=profile["revision"])
        profile_revision = updated_profile["revision"]
        uow.repository("principals").insert({"id": peer_id, "kind": "agent",
            "display_name": "Onbashy", "active": 1,
            "created_at": _text(clock[0]), "updated_at": _text(clock[0])})
        uow.repository("agent_profiles", business_id=business_id).insert({
            "agent_id": peer_id, "principal_id": peer_id, "namespace": business_id,
            "name": "Onbashy", "workspace": str((tmp_path / "peer").resolve()),
            "home_business_id": business_id,
            "profile_json": {"role": {"id": "onbashy"}, "scope": {
                "kind": "business", "allowed_business_ids": [business_id],
                "global_capabilities": []}},
            "status": "active", "created_at": _text(clock[0]), "updated_at": _text(clock[0])})
        uow.repository("tasks", business_id=business_id).insert({
            "business_id": business_id, "id": task_id,
            "title": "Coordinate the approved outreach launch", "status": "open",
            "created_by": parent_id, "created_at": _text(clock[0]), "updated_at": _text(clock[0])})
        uow.repository("machine_registrations").insert({
            "id": parent_registration_id, "principal_id": parent_id,
            "workspace": str(parent_workspace), "service_url": "http://127.0.0.1:9876",
            "profile_revision": profile_revision,
            "compatibility_json": {"durable_delegation_enabled": True},
            "created_at": _text(clock[0]), "updated_at": _text(clock[0])})

    class AllowPolicy:
        @staticmethod
        def resolve(context, descriptor, targets, uow):
            return PolicyDecision("allow", "synthetic acceptance")

        @staticmethod
        def context_for(principal_id, *, business_id, review_business_id, uow):
            row = uow.repository("principals").get(principal_id, required=True)
            return InvocationContext(Principal(row["id"], row["kind"], row["display_name"]),
                business_id=business_id,
                allowed_business_ids=frozenset({business_id}) if business_id else frozenset())

    registry = builtin_registry()
    application = Application(database, registry, policy=AllowPolicy())
    TaskOperations(clock=lambda: clock[0]).register(registry, application)
    delegation = OperationalDelegationOperations(store).register(registry, application)
    inbox = InboxService(application, None, clock=lambda: clock[0]).register()
    delegation.inbox = inbox
    application.operational_delegation = store

    parent = InvocationContext(Principal(parent_id, "agent", "Astra"),
        allowed_business_ids=frozenset({business_id}), global_capabilities=CAPABILITIES,
        profile_revision=profile_revision, role_artifact_ref=ROLE,
        session_registration_id=parent_registration_id)
    peer = InvocationContext(Principal(peer_id, "agent", "Onbashy"),
        business_id=business_id, home_business_id=business_id,
        allowed_business_ids=frozenset({business_id}), profile_revision=1)
    inbox.start_session(parent, parent_registration_id, consumer_id, "startup")

    allowed = ["agents_message_send", "agents_messages_list", "tasks_config_get",
        "tasks_get", "tasks_update"]
    created = _invoke(application, parent, "delegated_assignments_create", {
        "business_id": business_id,
        "scope": {"kind": "business", "business_id": business_id,
            "record_ids": {"tasks": [task_id], "business_configs": ["tasks"]},
            "source_ids": {}},
        "objective": "Coordinate the approved launch, resolve two replies, and update its task",
        "plan": {"steps": ["inspect task", "confirm launch", "wait for replies", "record completion"]},
        "allowed_operations": allowed, "links": {"tasks": [task_id]},
        "internal_recipients": [peer_id], "max_attempts": 4,
    }, key="acceptance-plan")
    assignment_id = created["assignment"]["id"]

    parent_application_calls = []
    original_invoke = application.invoke

    def track_parent_calls(context, operation, arguments=None, options=None):
        if context.principal_id == parent_id and context.delegation_assignment_id is None:
            parent_application_calls.append(operation)
        return original_invoke(context, operation, arguments, options)

    application.invoke = track_parent_calls
    authentication = Authentication(database, tmp_path,
        clock=lambda: clock[0].timestamp())

    def worker_context(claim):
        authenticated = authentication.agent("Bearer " + claim["worker_token"])
        return InvocationContext(authenticated.principal, business_id=business_id,
            allowed_business_ids=frozenset({business_id}), global_capabilities=CAPABILITIES,
            profile_revision=profile_revision, role_artifact_ref=ROLE,
            session_registration_id=authenticated.registration_id,
            delegation_binding_id=authenticated.delegation_binding_id,
            delegation_assignment_id=authenticated.delegation_assignment_id,
            delegation_generation=authenticated.delegation_generation)

    def assignment_revision(worker):
        return _invoke(application, worker, "delegated_assignment_get",
            {"assignment_id": assignment_id})["revision"]

    first = delegation.claim_next_for_dispatch(parent, parent_registration_id, consumer_id)
    assert first["assignment"]["id"] == assignment_id
    assert first["generation"] == 1
    worker1 = worker_context(first)
    assert _invoke(application, worker1, "tasks_config_get", {"scope": "business"})["config"]
    task = _invoke(application, worker1, "tasks_get", {"task_id": task_id})["task"]
    assert task["title"] == "Coordinate the approved outreach launch"
    _invoke(application, worker1, "tasks_update", {"task_id": task_id,
        "changes": {"title": "Launch checked with Onbashy"}}, key="stable-task-step-1", revision=1)
    message1 = _invoke(application, worker1, "agents_message_send", {
        "to_agent_id": peer_id, "message": "Please confirm the approved launch window.",
        "summary": "Confirm launch window"}, key="stable-message-step-1")["message"]

    # The child dies after its task and message effects commit, before checkpointing.
    reply1 = _invoke(application, peer, "agents_message_send", {
        "reply_to": message1["id"], "message": "The approved launch window is confirmed."},
        key="peer-reply-1")["message"]
    failed = delegation.dispatch_failed(parent, parent_registration_id, consumer_id,
        assignment_id, first["generation"], reason="synthetic child interruption")
    duplicate_failure = delegation.dispatch_failed(parent, parent_registration_id, consumer_id,
        assignment_id, first["generation"], reason="duplicate failure notification")
    assert failed["retry_count"] == duplicate_failure["retry_count"] == 1
    assert failed["checkpoint_json"] == {}

    clock[0] += timedelta(seconds=5)
    second = delegation.claim_next_for_dispatch(parent, parent_registration_id, consumer_id)
    assert second["generation"] == 2
    assert second["worker_registration_id"] == first["worker_registration_id"]
    assert any(event["source_type"] == "agent_messages" for event in second["events"])
    worker2 = worker_context(second)
    replayed_task = _invoke(application, worker2, "tasks_update", {"task_id": task_id,
        "changes": {"title": "Launch checked with Onbashy"}}, key="stable-task-step-1", revision=1)
    assert replayed_task["task"]["revision"] == 2
    replayed_message = _invoke(application, worker2, "agents_message_send", {
        "to_agent_id": peer_id, "message": "Please confirm the approved launch window.",
        "summary": "Confirm launch window"}, key="stable-message-step-1")["message"]
    assert replayed_message["id"] == message1["id"]
    history = _invoke(application, worker2, "agents_messages_list", {
        "thread_id": message1["thread_id"]})["items"]
    assert [message["id"] for message in history] == [message1["id"], reply1["id"]]
    routed1 = next(event for event in second["events"] if event["source_type"] == "agent_messages")
    _invoke(application, worker2, "delegated_assignment_event_settle", {
        "event_id": routed1["id"], "outcome": "Confirmed the launch window"},
        key="settle-reply-1", revision=assignment_revision(worker2))
    message2 = _invoke(application, worker2, "agents_message_send", {
        "reply_to": reply1["id"], "message": "Thanks; I have recorded the first confirmation."},
        key="helper-reply-1")["message"]
    waiting = _invoke(application, worker2, "delegated_assignment_checkpoint", {
        "checkpoint": {"phase": "first-reply-handled", "thread_id": message1["thread_id"]},
        "next_step": "Resume when the second confirmation arrives", "state": "waiting",
        "wake_condition": {"kind": "message_thread", "thread_id": message1["thread_id"]}},
        key="checkpoint-after-reply-1", revision=assignment_revision(worker2))["assignment"]
    assert waiting["state"] == "waiting"

    reply2 = _invoke(application, peer, "agents_message_send", {
        "reply_to": message2["id"], "message": "Second confirmation: the launch is ready."},
        key="peer-reply-2")["message"]
    third = delegation.claim_next_for_dispatch(parent, parent_registration_id, consumer_id)
    assert third["generation"] == 3
    assert third["worker_registration_id"] == first["worker_registration_id"]
    routed2 = next(event for event in third["events"] if event["source_type"] == "agent_messages")
    worker3 = worker_context(third)
    final_history = _invoke(application, worker3, "agents_messages_list", {
        "thread_id": message1["thread_id"]})["items"]
    assert [item["id"] for item in final_history] == [
        message1["id"], reply1["id"], message2["id"], reply2["id"]]
    _invoke(application, worker3, "tasks_update", {"task_id": task_id,
        "changes": {"title": "Launch ready after two confirmations"}},
        key="stable-task-step-2", revision=2)
    _invoke(application, worker3, "delegated_assignment_event_settle", {
        "event_id": routed2["id"], "outcome": "Recorded second launch confirmation"},
        key="settle-reply-2", revision=assignment_revision(worker3))
    completed = _invoke(application, worker3, "delegated_assignment_checkpoint", {
        "checkpoint": {"phase": "completed", "thread_id": message1["thread_id"]},
        "next_step": None, "state": "completed",
        "result": {"summary": "Launch task updated after two internal confirmations"}},
        key="complete-after-reply-2", revision=assignment_revision(worker3))["assignment"]

    with database.read() as uow:
        saved = store.get(uow, assignment_id)
        task = uow.repository("tasks", business_id=business_id).get(task_id)
        messages = uow.connection.execute(
            "SELECT id FROM agent_messages WHERE thread_id=? ORDER BY event_id",
            (message1["thread_id"],)).fetchall()
        claims = uow.connection.execute(
            "SELECT state,outcome FROM operational_event_claims WHERE assignment_id=? ORDER BY event_id",
            (assignment_id,)).fetchall()
        action_count = uow.connection.execute(
            "SELECT count(*) FROM operational_action_receipts WHERE assignment_id=?",
            (assignment_id,)).fetchone()[0]

    assert completed["state"] == saved["state"] == "completed"
    assert saved["attempt_count"] == 3 and saved["retry_count"] == 1
    assert task["title"] == "Launch ready after two confirmations" and task["revision"] == 3
    assert [row["id"] for row in messages] == [message1["id"], reply1["id"], message2["id"], reply2["id"]]
    assert len(claims) == 2 and all(row["state"] == "handled" for row in claims)
    assert [row["outcome"] for row in claims] == [
        "Confirmed the launch window", "Recorded second launch confirmation"]
    assert action_count == 4
    assert parent_application_calls == []
