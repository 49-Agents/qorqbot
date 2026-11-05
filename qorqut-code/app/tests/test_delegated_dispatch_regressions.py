"""New regressions for scoped worker reads, retries, and runtime-limit blocking."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bazarlyk.agents.operational_delegation import OperationalDelegationOperations
from bazarlyk.application.engine import Application
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from bazarlyk.inbox.service import InboxService
from test_operational_delegation_store import setup_store


def _text(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def test_worker_context_reads_are_grant_and_record_scoped(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    now = datetime.now(timezone.utc)
    store.clock = lambda: _text(now)
    focus_id = new_id()
    registration_id = new_id()
    allowed = {"agent_config_get", "tasks_config_get", "focus_get"}
    scope = {"kind": "global", "allowed_business_ids": [business_id],
        "record_ids": {"focus_cards": [focus_id]}, "source_ids": {}}
    with database.transaction() as uow:
        registration = uow.repository("machine_registrations").insert({"id": registration_id,
            "principal_id": parent_id, "workspace": str(tmp_path / "parent"),
            "service_url": "http://127.0.0.1:9000", "profile_revision": 1,
            "created_at": store.clock(), "updated_at": store.clock()})
        assignment = store.create(uow, parent_principal_id=parent_id, business_id=None,
            scope=scope, objective="Read only the assigned operating context",
            plan={"steps": ["refresh context"]}, allowed_operations=sorted(allowed))
        running = store.claim(uow, assignment["id"], actor_principal_id=parent_id)
        issued = store.issue_worker_binding(uow, assignment["id"], parent_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=str(tmp_path / "worker"),
            service_url=registration["service_url"], expires_at=running["lease_until"])

    worker = InvocationContext(Principal(parent_id, "agent", "Astra"),
        allowed_business_ids=frozenset({business_id}), profile_revision=1, source="mcp",
        global_capabilities=frozenset({"manage_delegated_assignments", "read_global_context"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
        session_registration_id=issued["registration_id"], delegation_binding_id=issued["binding"]["id"],
        delegation_assignment_id=assignment["id"], delegation_generation=running["execution_generation"])

    def authorize(name, arguments, namespace):
        descriptor = SimpleNamespace(name=name, effect_kind="read", owner_only=False,
            signoff_category="internal_change")
        return Application.authorize_delegation(worker, descriptor, (), uow,
            namespace=namespace, arguments=arguments)

    with database.read() as uow:
        assert authorize("agent_config_get", {"business_id": business_id},
            "agent:" + parent_id)["id"] == assignment["id"]
        assert authorize("tasks_config_get", {"scope": "business"},
            "business:" + business_id)["id"] == assignment["id"]
        assert authorize("focus_get", {"scope": "global", "item_id": focus_id},
            "global")["id"] == assignment["id"]
        with pytest.raises(DomainError, match="configuration business is outside"):
            authorize("agent_config_get", {"business_id": new_id()}, "agent:" + parent_id)
        with pytest.raises(DomainError, match="in-boundary record selector"):
            authorize("focus_get", {"scope": "global"}, "global")
        with pytest.raises(DomainError, match="in-boundary record selector"):
            authorize("focus_get", {"scope": "global", "item_id": new_id()}, "global")


def test_successful_wait_resumptions_do_not_consume_failure_retry_budget(tmp_path):
    database, store, parent_id, business_id, start = setup_store(tmp_path)
    store.clock = lambda: _text(start)
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id,
                   "record_ids": {}, "source_ids": {}},
            objective="Continue through several expected replies", plan={"steps": ["wait", "continue"]},
            allowed_operations=["tasks_get"], resource_bounds={"max_runtime_seconds": 120}, max_attempts=4)
        running = store.claim(uow, saved["id"], actor_principal_id=parent_id,
            lease_seconds=60, dispatcher_ready=True, now=_text(start))

    clock = start
    for index in range(6):
        checkpoint_at = clock + timedelta(seconds=1)
        wake_at = checkpoint_at + timedelta(seconds=1)
        with database.transaction() as uow:
            waiting = store.checkpoint(uow, saved["id"], actor_principal_id=parent_id,
                generation=running["execution_generation"], lease_id=running["lease_id"],
                expected_revision=running["revision"], checkpoint={"completed_cycles": index + 1},
                next_step="Continue after the next reply", state="waiting",
                wake_condition={"kind": "timer"}, wake_at=_text(wake_at), now=_text(checkpoint_at))
        clock = wake_at
        with database.transaction() as uow:
            running = store.claim(uow, saved["id"], actor_principal_id=parent_id,
                lease_seconds=60, dispatcher_ready=True, now=_text(clock))
        assert running["state"] == "running"

    assert running["attempt_count"] == 7
    assert running["retry_count"] == 0
    lease_until = datetime.fromisoformat(running["lease_until"].replace("Z", "+00:00"))
    with database.transaction() as uow:
        with pytest.raises(DomainError, match="not queued or due"):
            store.claim(uow, saved["id"], actor_principal_id=parent_id,
                dispatcher_ready=True, now=_text(lease_until + timedelta(seconds=4)))
        recovered = store.claim(uow, saved["id"], actor_principal_id=parent_id,
            dispatcher_ready=True, now=_text(lease_until + timedelta(seconds=5)))
    assert recovered["state"] == "running"
    assert recovered["attempt_count"] == 8
    assert recovered["retry_count"] == 1


def _dispatch_service(tmp_path, *, max_runtime_seconds=30):
    database, store, parent_id, business_id, start = setup_store(tmp_path)
    clock = [start]
    store.clock = lambda: _text(clock[0])
    registration_id, consumer_id = new_id(), new_id()
    workspace = tmp_path / "parent"
    workspace.mkdir()
    with database.transaction() as uow:
        uow.repository("machine_registrations").insert({"id": registration_id,
            "principal_id": parent_id, "workspace": str(workspace),
            "service_url": "http://127.0.0.1:9000", "profile_revision": 1,
            "compatibility_json": {"durable_delegation_enabled": True},
            "created_at": store.clock(), "updated_at": store.clock()})
        assignment = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id,
                   "record_ids": {}, "source_ids": {}},
            objective="Stop at the approved runtime limit", plan={"steps": ["execute"]},
            allowed_operations=["tasks_get"],
            resource_bounds={"max_turns": 5, "max_runtime_seconds": max_runtime_seconds})
        uow.connection.execute("INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) "
            "VALUES(?,?,?,?)", (parent_id, consumer_id, store.clock(),
                _text(start + timedelta(minutes=15))))

    class App:
        def __init__(self):
            self.database = database

        @staticmethod
        def current_context(context, _uow):
            return context

    app = App()
    inbox = InboxService(app, None, clock=lambda: clock[0])
    operations = OperationalDelegationOperations(store)
    operations.application, operations.inbox = app, inbox
    context = InvocationContext(Principal(parent_id, "agent", "Astra"),
        allowed_business_ids=frozenset({business_id}), profile_revision=1,
        global_capabilities=frozenset({"manage_delegated_assignments"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
        session_registration_id=registration_id)
    return database, store, operations, context, registration_id, consumer_id, assignment, clock


def test_poll_fences_and_notifies_when_runtime_budget_is_exhausted(tmp_path):
    database, store, operations, context, registration_id, consumer_id, saved, clock = _dispatch_service(tmp_path)
    claimed = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    generation = claimed["generation"]
    clock[0] += timedelta(seconds=30)

    status = operations.poll_worker(context, registration_id, consumer_id, saved["id"], generation)

    assignment = status["assignment"]
    assert assignment["state"] == "blocked"
    assert assignment["execution_generation"] == generation + 1
    assert assignment["runtime_used_seconds"] == 30
    assert "execution time limit" in assignment["next_step"]
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM operational_assignment_events "
            "WHERE assignment_id=? AND event_type='runtime_limit_reached'", (saved["id"],)).fetchone()[0] == 1
        assert uow.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='operational_assignments' "
            "AND source_id=? AND event_type='blocked'", (saved["id"],)).fetchone()[0] == 1
        assert uow.connection.execute("SELECT revoked_at FROM operational_delegation_bindings "
            "WHERE assignment_id=? AND execution_generation=?", (saved["id"], generation)).fetchone()[0] is not None
