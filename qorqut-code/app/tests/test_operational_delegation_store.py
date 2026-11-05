"""Focused contracts for persisted assignment transitions and stale-worker fencing."""
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from bazarlyk.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import (ArtifactRef, ChangePreview, ChangeSet, DomainError, ExpectedTarget,
    InvocationContext, PolicyDecision, PreparedChange, Principal, ResourceRef, new_id)
from bazarlyk.application.engine import Application
from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.storage import Database
from bazarlyk.transport.auth import Authentication


def setup_store(tmp_path):
    database = Database(tmp_path / "delegation.sqlite3").initialize()
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    store = OperationalDelegation(clock=lambda: now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                                  dispatch_reason=None)
    parent_id, business_id = new_id(), new_id()
    with database.transaction() as uow:
        now_text = store.clock()
        uow.repository("principals").insert({"id": parent_id, "kind": "agent", "display_name": "Astra",
            "active": 1, "created_at": now_text, "updated_at": now_text})
        uow.repository("businesses").insert({"id": business_id, "slug": "demo", "workspace": str(tmp_path),
            "hq_path": str(tmp_path), "name": "Demo", "created_at": now_text, "updated_at": now_text})
        uow.repository("agent_profiles", business_id=None).insert({"agent_id": parent_id, "principal_id": parent_id,
            "namespace": "ecosystem", "name": "Astra", "workspace": str(tmp_path / "parent"),
            "profile_json": {"scope": {"kind": "ecosystem", "allowed_business_ids": [business_id],
                "global_capabilities": ["manage_delegated_assignments"]}}, "status": "active",
            "created_at": now_text, "updated_at": now_text})
    return database, store, parent_id, business_id, now


def create_assignment(database, store, parent_id, business_id):
    task_id = new_id()
    with database.transaction() as uow:
        return store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id,
                   "record_ids": {"tasks": [task_id]}, "source_ids": {}},
            objective="Prepare the approved briefing", plan={"steps": ["collect", "draft"]},
            allowed_operations=["tasks_get", "tasks_update"], links={"tasks": [task_id]},
            resource_bounds={"max_turns": 20})


def test_waiting_assignment_releases_lease_and_reclaim_fences_old_generation(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    assignment = create_assignment(database, store, parent_id, business_id)
    with database.transaction() as uow:
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        waiting = store.checkpoint(uow, assignment["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], checkpoint={"completed_steps": ["collect"]},
            next_step="Draft after source reply", state="waiting",
            wake_condition={"kind": "message_thread", "thread_id": new_id()})
    assert waiting["state"] == "waiting" and waiting["lease_id"] is None
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="not queued or due"):
            store.claim(uow, assignment["id"], actor_principal_id=parent_id)

    due = (now + timedelta(minutes=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as uow:
        # An explicit parent resume is required for event-driven waits; a timer
        # wake is claimable only when the persisted due timestamp has arrived.
        resumed = store.control(uow, assignment["id"], parent_principal_id=parent_id,
            expected_revision=waiting["revision"], action="resume")
        reclaimed = store.claim(uow, assignment["id"], actor_principal_id=parent_id, now=due)
        assert reclaimed["execution_generation"] > running["execution_generation"]
        assert reclaimed["state"] == "running"
        with pytest.raises(DomainError, match="no longer current"):
            store.checkpoint(uow, assignment["id"], actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                expected_revision=resumed["revision"], checkpoint={}, state="completed",
                result={"summary": "stale completion"}, now=due)


def test_expired_worker_recovery_uses_persisted_bounded_backoff(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    assignment = create_assignment(database, store, parent_id, business_id)
    with database.transaction() as uow:
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
    lease_expiry = datetime.fromisoformat(running["lease_until"].replace("Z", "+00:00"))
    too_early = (lease_expiry + timedelta(seconds=4)).isoformat().replace("+00:00", "Z")
    recovered_at = (lease_expiry + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="not queued or due"):
            store.claim(uow, assignment["id"], actor_principal_id=parent_id, now=too_early)
        recovered = store.claim(uow, assignment["id"], actor_principal_id=parent_id, now=recovered_at)
    assert recovered["execution_generation"] == running["execution_generation"] + 1
    assert recovered["attempt_count"] == 2


def test_unavailable_dispatch_leaves_assignment_queued_with_visible_reason(tmp_path):
    database, enabled_store, parent_id, business_id, _ = setup_store(tmp_path)
    assignment = create_assignment(database, enabled_store, parent_id, business_id)
    disabled_store = OperationalDelegation(clock=enabled_store.clock)
    with database.transaction() as uow:
        queued = disabled_store.claim(uow, assignment["id"], actor_principal_id=parent_id)
    assert queued["state"] == "queued"
    assert queued["execution_generation"] == 0 and queued["attempt_count"] == 0
    assert "compatible parent dispatcher" in queued["dispatch_reason"].lower()


def test_cancel_fences_live_worker_and_old_generation_cannot_complete(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    assignment = create_assignment(database, store, parent_id, business_id)
    with database.transaction() as uow:
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        stopped = store.control(uow, assignment["id"], parent_principal_id=parent_id,
            expected_revision=running["revision"], action="cancel")
        assert stopped["state"] == "cancelled"
        assert stopped["execution_generation"] > running["execution_generation"]
        with pytest.raises(DomainError, match="no longer current"):
            store.checkpoint(uow, assignment["id"], actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                expected_revision=stopped["revision"], checkpoint={}, state="completed",
                result={"summary": "stale completion"})


def test_create_rejects_scope_expansion_and_parent_only_capabilities(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    with pytest.raises(DomainError, match="must match exactly"):
        with database.transaction() as uow:
            store.create(uow, parent_principal_id=parent_id, business_id=business_id,
                scope={"kind": "business", "business_id": new_id()}, objective="Do scoped work",
                plan={"steps": ["go"]}, allowed_operations=["tasks_get"])
    with pytest.raises(DomainError, match="parent-only"):
        with database.transaction() as uow:
            store.create(uow, parent_principal_id=parent_id, business_id=business_id,
                scope={"kind": "business", "business_id": business_id}, objective="Change grants",
                plan={"steps": ["go"]}, allowed_operations=["permissions_set"])


def test_binding_auth_and_application_gate_enforce_assignment_records(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assignment = create_assignment(database, store, parent_id, business_id)
    task_id = assignment["links_json"]["tasks"][0]
    with database.transaction() as uow:
        uow.repository("machine_registrations").insert({"id": new_id(), "principal_id": parent_id,
            "workspace": str(tmp_path / "parent"), "service_url": "http://127.0.0.1:9000",
            "profile_revision": 1, "created_at": store.clock(), "updated_at": store.clock()})
        uow.repository("tasks", business_id=business_id).insert({"business_id": business_id,
            "id": task_id, "title": "Briefing task", "status": "open", "created_by": parent_id,
            "created_at": store.clock(), "updated_at": store.clock()})
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        issued = store.issue_worker_binding(uow, assignment["id"], parent_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=str(tmp_path / "worker"),
            service_url="http://127.0.0.1:9000", expires_at=running["lease_until"],
            runtime_thread_id="thread-synthetic")
        binding, binding_token = issued["binding"], issued["worker_token"]
        registration_id = issued["registration_id"]
        action_key = "task-step-committed"
        payload_hash = store.hash_payload({"task_id": task_id, "status": "in_progress"})
        uow.repository("idempotency_receipts").insert({
            "principal_id": parent_id, "target_namespace": "business:" + business_id,
            "operation_name": "tasks_update", "idempotency_key": action_key,
            "input_hash": payload_hash, "result_json": {"status": "ok", "data": {"task_id": task_id}},
        })
        action_receipt = store.record_committed_action(uow, assignment["id"],
            actor_principal_id=parent_id, generation=running["execution_generation"],
            lease_id=running["lease_id"], target_namespace="business:" + business_id,
            action_key=action_key, operation_name="tasks_update", payload_hash=payload_hash,
            effect_ref={"task_id": task_id})
        assert action_receipt["state"] == "applied"
        assert store.record_committed_action(uow, assignment["id"],
            actor_principal_id=parent_id, generation=running["execution_generation"],
            lease_id=running["lease_id"], target_namespace="business:" + business_id,
            action_key=action_key, operation_name="tasks_update", payload_hash=payload_hash,
            effect_ref={"task_id": task_id})["id"] == action_receipt["id"]
        with pytest.raises(DomainError, match="committed application receipt"):
            store.record_committed_action(uow, assignment["id"], actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                target_namespace="business:" + business_id, action_key="forged-key",
                operation_name="tasks_update", payload_hash=payload_hash, effect_ref={"task_id": task_id})
        context = InvocationContext(Principal(parent_id, "agent", "Astra"), business_id=business_id,
            home_business_id=None, allowed_business_ids=frozenset({business_id}), profile_revision=1, source="mcp",
            global_capabilities=frozenset({"manage_delegated_assignments"}),
            role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
            session_registration_id=registration_id, delegation_binding_id=binding["id"],
            delegation_assignment_id=assignment["id"], delegation_generation=running["execution_generation"])
        accepted = Application.authorize_delegation(context, "tasks_update",
            [ResourceRef("tasks", task_id, business_id, "business:" + business_id)], uow,
            namespace="business:" + business_id)
        assert accepted["id"] == assignment["id"]
        with pytest.raises(DomainError, match="record boundary"):
            Application.authorize_delegation(context, "tasks_update",
                [ResourceRef("tasks", new_id(), business_id, "business:" + business_id)], uow,
                namespace="business:" + business_id)
    registry = builtin_registry()
    def prepare_task_update(ctx, args, options, uow):
        task = uow.repository("tasks", business_id=business_id).get(args["task_id"])
        target = ResourceRef("tasks", task["id"], business_id, "business:" + business_id)
        return ChangeSet("tasks_update", 1, ctx.principal_id, "business:" + business_id, args,
            {"kind": "business", "business_id": business_id},
            (ExpectedTarget(target, revision=task["revision"]),),
            (PreparedChange("record", target, "update_task", {"status": args["status"]}),),
                ChangePreview("Update task", "Synthetic delegated action", {"business_id": business_id}, (), ()))
    def apply_task_update(ctx, plan, uow):
        target = plan.expected_targets[0]
        task = uow.repository("tasks", business_id=business_id).update(target.resource.resource_id,
            {"status": plan.changes[0].values["status"]}, expected_revision=target.revision)
        return {"task": task}
    registry.register(OperationDescriptor("tasks_update",
        {"type": "object", "properties": {"task_id": {"type": "string", "format": "uuid"},
            "status": {"type": "string"}}, "required": ["task_id", "status"]},
        effect_kind="records", scope_kinds=frozenset({"business"}),
        prepare=prepare_task_update, apply=apply_task_update))
    OperationalDelegationOperations(store).register(registry, None)
    class AllowPolicy:
        @staticmethod
        def resolve(ctx, descriptor, targets, uow):
            return PolicyDecision("allow", "delegation-contract")
    application = Application(database, registry, policy=AllowPolicy())
    updated = application.invoke(context, "tasks_update", {"task_id": task_id, "status": "in_progress"},
        {"business_id": business_id, "idempotency_key": "stable-task-step"})
    assert updated.status == "ok"
    with database.read() as uow:
        task = uow.repository("tasks", business_id=business_id).get(task_id)
        receipt = uow.connection.execute("SELECT * FROM operational_action_receipts WHERE assignment_id=? AND action_key=?",
            (assignment["id"], "stable-task-step")).fetchone()
    assert task["status"] == "in_progress"
    assert receipt["state"] == "applied" and receipt["operation_name"] == "tasks_update"
    with database.read() as uow:
        assignment_revision = store.get(uow, assignment["id"])["revision"]
    checkpointed = application.invoke(context, "delegated_assignment_checkpoint", {
        "checkpoint": {"completed_steps": ["update task"]}, "next_step": "Review the reply",
        "state": "running",
    }, {"expected_revision": assignment_revision, "idempotency_key": "worker-checkpoint-1"})
    assert checkpointed.status == "ok"
    assert checkpointed.data["assignment"]["checkpoint_json"]["completed_steps"] == ("update task",)
    denied = application.invoke(context, "tasks_update", {"task_id": new_id(), "status": "closed"},
        {"business_id": business_id, "idempotency_key": "outside-task-step"})
    assert denied.status == "error" and denied.error["code"] == "forbidden"
    authentication = Authentication(database, tmp_path, clock=lambda: datetime.now(timezone.utc).timestamp())
    actor = authentication.agent("Bearer " + binding_token)
    assert actor.principal.id == parent_id
    assert actor.delegation_assignment_id == assignment["id"]
    assert actor.delegation_generation == running["execution_generation"]
    with database.transaction() as uow:
        current = store.get(uow, assignment["id"])
        store.control(uow, assignment["id"], parent_principal_id=parent_id,
            expected_revision=current["revision"], action="cancel")
    with pytest.raises(DomainError, match="expired, revoked, or stale"):
        authentication.agent("Bearer " + binding_token)


def test_application_descriptors_register_only_the_narrow_assignment_surface():
    registry = builtin_registry()
    operations = OperationalDelegationOperations(OperationalDelegation()).register(registry, object())
    assert operations.CAPABILITY == "manage_delegated_assignments"
    assert {"delegated_assignment_get", "delegated_assignments_list", "delegated_assignments_create",
            "delegated_assignments_control", "delegated_assignments_revise",
            "delegated_assignment_checkpoint"} <= {item.name for item in registry.installed()}
    assert registry.get("delegated_assignment_checkpoint").effect_kind == "records"
    assert registry.get("delegated_assignments_list").effect_kind == "read"


def test_parent_application_can_create_and_inspect_an_approved_assignment(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    registry = builtin_registry()
    uuid_schema = {"type": "string", "format": "uuid"}
    registry.register(OperationDescriptor("tasks_get",
        {"type": "object", "properties": {"task_id": uuid_schema}, "required": ["task_id"]},
        effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"business"}),
        read=lambda ctx, args, options, uow: {"task": {"id": args["task_id"]}}))
    registry.register(OperationDescriptor("tasks_update",
        {"type": "object", "properties": {"task_id": uuid_schema}, "required": ["task_id"]},
        effect_kind="records", scope_kinds=frozenset({"business"}),
        prepare=lambda *args: None, apply=lambda *args: None))
    OperationalDelegationOperations(store).register(registry, None)

    class AllowPolicy:
        @staticmethod
        def resolve(context, descriptor, targets, uow):
            return PolicyDecision("allow", "delegation-contract")

    application = Application(database, registry, policy=AllowPolicy())
    parent = InvocationContext(Principal(parent_id, "agent", "Astra"),
        allowed_business_ids=frozenset({business_id}), profile_revision=1,
        global_capabilities=frozenset({"manage_delegated_assignments"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64))
    task_id = new_id()
    created = application.invoke(parent, "delegated_assignments_create", {
        "business_id": business_id,
        "scope": {"kind": "business", "business_id": business_id,
                  "record_ids": {"tasks": [task_id]}, "source_ids": {}},
        "objective": "Prepare the approved briefing", "plan": {"steps": ["collect", "draft"]},
        "allowed_operations": ["tasks_get", "tasks_update"],
        "links": {"tasks": [task_id]},
    }, {"idempotency_key": new_id()})
    assert created.status == "ok", created.error
    assignment_id = created.data["assignment"]["id"]
    inspected = application.invoke(parent, "delegated_assignment_get", {"assignment_id": assignment_id})
    assert inspected.status == "ok"
    assert inspected.data["objective"] == "Prepare the approved briefing"
    assert inspected.data["allowed_operations_json"] == ("tasks_get", "tasks_update")


def test_worker_action_receipt_is_bound_to_worker_registration_and_plan_revision(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    assignment = create_assignment(database, store, parent_id, business_id)
    with database.transaction() as uow:
        uow.repository("machine_registrations").insert({"id": new_id(), "principal_id": parent_id,
            "workspace": str(tmp_path / "parent"), "service_url": "http://127.0.0.1:9000",
            "profile_revision": 1, "created_at": store.clock(), "updated_at": store.clock()})
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        issued = store.issue_worker_binding(uow, assignment["id"], parent_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=str(tmp_path / "worker"),
            service_url="http://127.0.0.1:9000", expires_at=running["lease_until"],
            runtime_thread_id="thread-attributed")
        payload = {"task_id": assignment["links_json"]["tasks"][0], "status": "in_progress"}
        payload_hash = store.hash_payload(payload)
        uow.repository("idempotency_receipts").insert({
            "principal_id": parent_id, "target_namespace": "business:" + business_id,
            "operation_name": "tasks_update", "idempotency_key": "attributed-action",
            "input_hash": payload_hash, "result_json": {"status": "ok", "data": {"task_id": payload["task_id"]}},
        })
        receipt = store.record_committed_action(uow, assignment["id"],
            actor_principal_id=parent_id, generation=running["execution_generation"],
            lease_id=running["lease_id"], target_namespace="business:" + business_id,
            action_key="attributed-action", operation_name="tasks_update",
            payload_hash=payload_hash, effect_ref={"task_id": payload["task_id"]},
            delegation_binding_id=issued["binding"]["id"], plan_revision=assignment["plan_revision"])
        assert receipt["parent_principal_id"] == parent_id
        assert receipt["delegation_binding_id"] == issued["binding"]["id"]
        assert receipt["plan_revision"] == assignment["plan_revision"]
        with pytest.raises(DomainError, match="binding is missing, revoked, or mismatched"):
            store.record_committed_action(uow, assignment["id"],
                actor_principal_id=parent_id, generation=running["execution_generation"],
                lease_id=running["lease_id"], target_namespace="business:" + business_id,
                action_key="untrusted-binding", operation_name="tasks_update",
                payload_hash=payload_hash, effect_ref={"task_id": payload["task_id"]},
                delegation_binding_id=new_id(), plan_revision=assignment["plan_revision"])


@pytest.mark.parametrize('state', ['queued', 'waiting', 'running'])
def test_legacy_luna_upgrades_only_on_next_eligible_claim(tmp_path, state):
    db, store, parent, business, now = setup_store(tmp_path)
    a = create_assignment(db, store, parent, business)
    assert a['selected_model'] == 'gpt-6-luna'
    thread = new_id()
    past = (now - timedelta(minutes=10)).isoformat().replace('+00:00', 'Z')
    with db.transaction() as u:
        legacy = u.repository('operational_assignments').update(a['id'], {
            'selected_model': 'gpt-5.6-luna', 'state': state, 'runtime_thread_id': thread,
            'wake_at': past if state == 'waiting' else None,
            'lease_id': new_id() if state == 'running' else None,
            'lease_until': past if state == 'running' else None,
        }, expected_revision=a['revision'])
        claimed = store.claim(u, a['id'], actor_principal_id=parent)
        assert claimed['selected_model'] == 'gpt-6-luna'
        assert claimed['runtime_thread_id'] == thread
        assert claimed['execution_generation'] == legacy['execution_generation'] + 1
        for field in ['scope_json', 'allowed_operations_json', 'checkpoint_json', 'resource_bounds_json']:
            assert claimed[field] == legacy[field]
        events = u.connection.execute("select payload_json from operational_assignment_events where assignment_id=? and event_type='model_upgraded'", (a['id'],)).fetchall()
        assert len(events) == 1 and 'gpt-5.6-luna' in events[0][0] and 'gpt-6-luna' in events[0][0]


@pytest.mark.parametrize('state', ['running', 'paused', 'blocked', 'completed', 'cancelled'])
def test_legacy_luna_live_or_ineligible_history_is_not_rewritten(tmp_path, state):
    db, store, parent, business, now = setup_store(tmp_path)
    a = create_assignment(db, store, parent, business)
    with db.transaction() as u:
        before = u.repository('operational_assignments').update(a['id'], {
            'selected_model':'gpt-5.6-luna', 'state':state,
            'lease_id':new_id() if state == 'running' else None,
            'lease_until':(now + timedelta(minutes=5)).isoformat().replace('+00:00','Z') if state == 'running' else None,
        }, expected_revision=a['revision'])
        with pytest.raises(DomainError, match='not queued or due'):
            store.claim(u, a['id'], actor_principal_id=parent)
        assert store.get(u,a['id']) == before


def test_worker_configuration_sets_luna_and_effort_by_browser_grant(tmp_path):
    db, store, parent, business, _ = setup_store(tmp_path)
    scope = {'kind':'business','business_id':business,'record_ids':{},'source_ids':{}}
    with db.transaction() as u:
        plain = store.create(u, parent_principal_id=parent, business_id=business, scope=scope,
            objective='bounded work', plan={'steps':['read']}, allowed_operations=['tasks_get'])
        browser = store.create(u, parent_principal_id=parent, business_id=business, scope=scope,
            objective='browser work', plan={'steps':['open']}, allowed_operations=['browser_use'])
        with pytest.raises(TypeError):
            store.create(u, parent_principal_id=parent, business_id=business, scope=scope,
                objective='chosen model', plan={'steps':['read']}, allowed_operations=[], selected_model='gpt-6-astra')
    assert (plain['selected_model'], plain['reasoning_effort']) == ('gpt-6-luna', 'medium')
    assert (browser['selected_model'], browser['reasoning_effort']) == ('gpt-6-luna', 'high')
    with db.transaction() as u:
        revised = store.revise(u, plain['id'], parent_principal_id=parent, expected_revision=plain['revision'],
            expected_plan_revision=1, objective='now in the browser', plan={'steps':['open']},
            allowed_operations=['browser_use'])
    assert revised['reasoning_effort'] == 'high'
