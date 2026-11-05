from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from bazarlyk.application import Application, ApprovalBinding, OperationDescriptor, OperationRegistry
from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, OperationResult,
    PreparedChange, Principal, ResourceRef, canonical_bytes, new_id, plain)
from bazarlyk.storage.receipts import IdempotencyReceipts
from foundation_support import AGENT, BUSINESS, HUMAN, OTHER, FixedPolicy, context, database_at, task_plan

TASK_SCHEMA = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "title": {"type": "string"}}}


def make_application(tmp_path, *, policy=None, prepare=None, apply=None, signoffs=None, descriptor_options=None):
    database = database_at(tmp_path)
    registry = OperationRegistry()
    def insert(ctx, plan, uow):
        return {"task": uow.repository("tasks", business_id=ctx.business_id).insert(plain(plan.changes[0].values))}
    registry.register(OperationDescriptor("tasks_create", TASK_SCHEMA, aliases=("task_add",),
        prepare=prepare or (lambda ctx, args, options, uow: task_plan(ctx, args)), apply=apply or insert,
        **(descriptor_options or {})))
    app = Application(database, registry, policy=policy if policy is not None else FixedPolicy(), signoffs=signoffs)
    return database, app


def test_alias_replay_and_changed_input_conflict(tmp_path):
    database, app = make_application(tmp_path)
    ctx = context()
    first = app.invoke(ctx, "task_add", {"id": "1"}, {"idempotency_key": "same"})
    second = app.invoke(replace(ctx, request_id="request-two"), "tasks_create", {"id": "1"}, {"idempotency_key": "same"})
    assert first.status == second.status == "ok"
    assert first.data == second.data
    assert second.request_id == "request-two"
    changed = app.invoke(ctx, "tasks_create", {"id": "2"}, {"idempotency_key": "same"})
    assert changed.error["code"] == "idempotency_conflict"
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("tasks").list()) == 1
        assert len(uow.repository("idempotency_receipts").list()) == 1


def test_concurrent_identical_calls_apply_once(tmp_path):
    barrier = Barrier(2)
    def prepare(ctx, args, options, uow):
        plan = task_plan(ctx, args)
        barrier.wait(timeout=5)
        return plan
    database, app = make_application(tmp_path, prepare=prepare)
    def invoke(_):
        return app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "concurrent"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert [result.status for result in results] == ["ok", "ok"]
    assert results[0].data == results[1].data
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("tasks").list()) == 1


@pytest.mark.parametrize("case,code", [("missing_key", "validation_error"), ("unknown_input", "validation_error"),
    ("cross_business", "forbidden"), ("unknown_operation", "operation_unavailable"), ("missing_policy", "policy_denied"),
    ("inactive", "invalid_principal"), ("null_context", "invalid_principal")])
def test_invalid_invocations_do_not_apply(tmp_path, case, code):
    database, app = make_application(tmp_path)
    ctx, arguments, operation, options = context(), {"id": "one"}, "tasks_create", {"idempotency_key": "one"}
    if case == "missing_key": options = None
    if case == "unknown_input": arguments["actor"] = HUMAN
    if case == "cross_business": options["business_id"] = OTHER
    if case == "unknown_operation": operation = "arbitrary_sql"
    if case == "missing_policy": app.policy = None
    if case == "null_context": ctx = None
    if case == "inactive":
        with database.transaction() as uow:
            uow.repository("principals").update(HUMAN, {"active": False}, expected_revision=1)
    result = app.invoke(ctx, operation, arguments, options)
    assert result.status == "error" and result.error["code"] == code
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("tasks").list() == []


def test_context_revalidation_rejects_inactive_or_expanded_agent_scope(tmp_path):
    database, app = make_application(tmp_path)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "agent"})
        uow.repository("agent_profiles", business_id=BUSINESS).insert({"agent_id": AGENT, "principal_id": AGENT,
            "namespace": BUSINESS, "name": "worker", "workspace": str(tmp_path / "worker"),
            "profile_json": {"scope": {"kind": "business", "business_id": BUSINESS}}})
    ctx = InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS, home_business_id=BUSINESS,
        allowed_business_ids=frozenset({BUSINESS, OTHER}), profile_revision=1)
    result = app.invoke(ctx, "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "forbidden"
    result = app.invoke(replace(ctx, allowed_business_ids=frozenset({BUSINESS}), profile_revision=2),
                        "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "context_refresh_required"


def test_composed_effect_cannot_bypass_denial(tmp_path):
    def prepare(ctx, args, options, uow):
        effect = PreparedChange("record", ResourceRef("tasks", args["id"], ctx.business_id), "assign", {}, operation_name="tasks_assign")
        return task_plan(ctx, args, extra=(effect,))
    database, app = make_application(tmp_path, prepare=prepare, policy=FixedPolicy(overrides={"tasks_assign": "deny"}))
    app.registry.register(OperationDescriptor("tasks_assign", {"type": "object"}, prepare=lambda *_: None, apply=lambda *_: {}))
    result = app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "policy_denied"
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("tasks").list() == []


def test_prepare_cannot_write_and_bad_output_rolls_back(tmp_path):
    def bad_prepare(ctx, args, options, uow):
        uow.repository("principals").update(HUMAN, {"display_name": "Side effect"}, expected_revision=1)
    database, app = make_application(tmp_path, prepare=bad_prepare)
    result = app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "read_only"
    with database.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"


def test_invalid_apply_output_rolls_back_domain_and_receipt(tmp_path):
    def bad_apply(ctx, plan, uow):
        uow.repository("tasks", business_id=ctx.business_id).insert(plain(plan.changes[0].values))
        return ["not the declared object"]
    database, app = make_application(tmp_path, apply=bad_apply)
    result = app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "invalid_result"
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("tasks").list() == []
        assert uow.repository("idempotency_receipts").list() == []


class StoredProposal:
    def stage(self, ctx, descriptor, plan, options, uow):
        identity = new_id()
        uow.repository("signoffs", business_id=ctx.review_business_id).insert({
            "id": identity, "proposer_principal_id": ctx.principal_id, "operation_name": descriptor.name,
            "operation_version": descriptor.version, "proposal_revision": 1, "payload_hash": plan.payload_hash,
            "change_set": plan.canonical, "preview_json": plan.preview, "expected_targets_json": plain(plan.expected_targets),
            "policy_binding_json": plan.policy_binding, "affected_scope_json": plan.affected_scope,
            "state": "pending", "updated_sequence": uow.next_sequence("signoffs")})
        return OperationResult("", "signoff_required", signoff={"id": identity, "payload_hash": plan.payload_hash})


def test_stage_has_no_domain_effect_and_approval_binds_exact_bytes(tmp_path):
    database, app = make_application(tmp_path, policy=FixedPolicy("signoff"), signoffs=StoredProposal())
    result = app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.status == "signoff_required"
    from qorqut_operations.contracts import ChangeSet
    with database.read(business_id=BUSINESS) as uow:
        assert not uow.repository("tasks").list()
        row = uow.repository("signoffs").get(result.signoff["id"])
        plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
        app.validate_prepared(context(), plan, uow, require_approval=False)
    with pytest.raises(DomainError, match="approval"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow)
    with database.transaction() as uow:
        uow.repository("signoffs", business_id=BUSINESS).update(row["id"], {"state": "approved", "updated_sequence": uow.next_sequence("signoffs")}, expected_revision=1)
        applied = app.apply_prepared(context(), plan, uow, approval=ApprovalBinding(row["id"], plan.payload_hash))
        assert applied.data["task"]["id"] == "one"
    with pytest.raises(DomainError):
        with database.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET change_set=? WHERE id=?", (canonical_bytes({"changed": True}), row["id"]))


def test_policy_change_conflicts_before_approved_effect(tmp_path):
    policy = FixedPolicy("signoff")
    database, app = make_application(tmp_path, policy=policy, signoffs=StoredProposal())
    staged = app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"})
    from qorqut_operations.contracts import ChangeSet
    policy.mode = "allow"
    with database.read(business_id=BUSINESS) as uow:
        row = uow.repository("signoffs").get(staged.signoff["id"])
        plan = ChangeSet.from_bytes(row["change_set"])
    with pytest.raises(DomainError, match="Authority configuration changed"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow, approval=ApprovalBinding(row["id"], row["payload_hash"]))


def test_maintenance_gate_blocks_writes_without_disabling_reads(tmp_path):
    database, app = make_application(tmp_path)
    app.registry.register(OperationDescriptor("tasks_list", {"type": "object"}, effect_kind="read", supports_signoff=False,
        read=lambda ctx, args, options, uow: {"tasks": uow.repository("tasks", business_id=ctx.business_id).list()}))
    with database.transaction() as uow:
        uow.repository("maintenance_state").insert({"id": "service", "state": "closed", "message": "Restoring"})
    assert app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "maintenance"
    assert app.invoke(context(), "tasks_list").status == "ok"


def test_owner_only_is_not_mcp_exposed_or_delegated_to_policy(tmp_path):
    database, app = make_application(tmp_path)
    app.registry.register(OperationDescriptor("owner_decision", {"type": "object"}, owner_only=True,
        exposure=frozenset({"dashboard"}), supports_signoff=False, effect_kind="read", read=lambda *_: {"owner": True}))
    app.policy = None
    assert app.invoke(context(), "owner_decision").status == "ok"
    assert app.invoke(replace(context(), source="mcp"), "owner_decision").error["code"] == "forbidden"


def test_registered_global_namespace_requires_specific_capability(tmp_path):
    database, app = make_application(tmp_path)
    app.registry.register(OperationDescriptor("global_read", {"type": "object"}, effect_kind="read", supports_signoff=False,
        scope_kinds=frozenset({"global"}), global_capability="read_global_context", read=lambda *_: {}))
    assert app.invoke(context(), "global_read").error["code"] == "forbidden"
    assert app.invoke(replace(context(), global_capabilities=frozenset({"read_global_context"})), "global_read").status == "ok"


def test_external_phase_runs_before_and_outside_the_write_transaction(tmp_path, monkeypatch):
    events = []
    database, app = make_application(tmp_path, apply=lambda ctx, plan, uow: events.append("apply") or {},
        descriptor_options={"supports_signoff": False, "external": lambda ctx, plan: events.append(("external", open_writes[0]))})
    open_writes, transaction = [0], database.transaction
    def tracked(*args, **kwargs):
        manager = transaction(*args, **kwargs)
        class Tracked:
            def __enter__(self):
                open_writes[0] += 1
                return manager.__enter__()
            def __exit__(self, *exc):
                open_writes[0] -= 1
                return manager.__exit__(*exc)
        return Tracked()
    monkeypatch.setattr(database, "transaction", tracked)
    assert app.invoke(context(), "tasks_create", {"id": "one"}, {"idempotency_key": "one"}).status == "ok"
    assert events == [("external", 0), "apply"]
    with pytest.raises(ValueError, match="signoff"):
        OperationDescriptor("staged", TASK_SCHEMA, prepare=lambda *_: None, apply=lambda *_: {}, external=lambda *_: None)
