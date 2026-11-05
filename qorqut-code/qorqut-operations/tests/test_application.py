from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier

import pytest

from qorqut_operations.application import (Application, ApprovalBinding, OperationDescriptor, OperationRegistry,
    ReviewScopeException)
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision
from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationContext, OperationResult, PreparedChange,
    Principal, ResourceRef, canonical_bytes, new_id, plain)
from support import AGENT, BUSINESS, HUMAN, OTHER, FixedPolicy, context, database_at, item_plan

ITEM_SCHEMA = {"type": "object", "required": ["id"], "properties": {
    "id": {"type": "string"}, "title": {"type": "string"}, "expected_revision": {"type": "integer"}}}


def insert(ctx, plan, uow):
    return {"item": uow.repository("items", business_id=ctx.business_id).insert(plain(plan.changes[0].values))}


def make_application(tmp_path, *, policy=None, prepare=None, apply=None, signoffs=None, descriptor_options=None, **options):
    database = database_at(tmp_path)
    registry = OperationRegistry()
    registry.register(OperationDescriptor("items_create", ITEM_SCHEMA, aliases=("item_add",),
        prepare=prepare or (lambda ctx, args, opts, uow: item_plan(ctx, args)), apply=apply or insert,
        **(descriptor_options or {})))
    app = Application(database, registry, policy=policy if policy is not None else FixedPolicy(), signoffs=signoffs, **options)
    return database, app


def add_update_operation(app):
    """A mutation prepared with the portable PlanBuilder and applied by the portable record application."""
    def prepare(ctx, args, opts, uow):
        before = uow.repository("items", business_id=ctx.business_id).get(args["id"], required=True)
        require_revision(before, args.get("expected_revision"))
        builder = PlanBuilder(ctx, "items_update", args, "Update item")
        builder.record("items", args["id"], before, {"title": args["title"]})
        return builder.build()
    app.registry.register(OperationDescriptor("items_update", ITEM_SCHEMA, prepare=prepare,
        apply=lambda ctx, plan, uow: {"item": apply_records(ctx, plan, uow)[0]}))


def test_alias_replay_and_changed_input_conflict(tmp_path):
    database, app = make_application(tmp_path)
    ctx = context()
    first = app.invoke(ctx, "item_add", {"id": "1"}, {"idempotency_key": "same"})
    second = app.invoke(replace(ctx, request_id="request-two"), "items_create", {"id": "1"}, {"idempotency_key": "same"})
    assert first.status == second.status == "ok"
    assert first.data == second.data
    assert second.request_id == "request-two"
    changed = app.invoke(ctx, "items_create", {"id": "2"}, {"idempotency_key": "same"})
    assert changed.error["code"] == "idempotency_conflict"
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("items").list()) == 1
        assert len(uow.repository("idempotency_receipts").list()) == 1


def test_an_idempotency_key_is_scoped_to_its_owning_namespace(tmp_path):
    database, app = make_application(tmp_path)
    ctx = replace(context(), allowed_business_ids=frozenset({BUSINESS, OTHER}))
    first = app.invoke(ctx, "items_create", {"id": "1"}, {"idempotency_key": "same"})
    other = app.invoke(ctx, "items_create", {"id": "1"}, {"idempotency_key": "same", "business_id": OTHER})
    assert first.status == other.status == "ok"
    for business in (BUSINESS, OTHER):
        with database.read(business_id=business) as uow:
            assert [row["id"] for row in uow.repository("items").list()] == ["1"]
    # The same key and namespace with a changed option-scope is a conflict, never a silent second apply.
    changed = app.invoke(ctx, "items_create", {"id": "1"}, {"idempotency_key": "same", "review_business_id": OTHER})
    assert changed.error["code"] in ("idempotency_conflict", "validation_error")
    with database.read() as uow:
        assert len(uow.repository("idempotency_receipts").list()) == 2


def test_concurrent_identical_calls_apply_once(tmp_path):
    barrier = Barrier(2)
    def prepare(ctx, args, options, uow):
        plan = item_plan(ctx, args)
        barrier.wait(timeout=5)
        return plan
    database, app = make_application(tmp_path, prepare=prepare)
    def invoke(_):
        return app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "concurrent"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert [result.status for result in results] == ["ok", "ok"]
    assert results[0].data == results[1].data
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("items").list()) == 1


@pytest.mark.parametrize("case,code", [("missing_key", "validation_error"), ("unknown_input", "validation_error"),
    ("cross_business", "forbidden"), ("unknown_operation", "operation_unavailable"), ("missing_policy", "policy_denied"),
    ("inactive", "invalid_principal"), ("null_context", "invalid_principal")])
def test_invalid_invocations_do_not_apply(tmp_path, case, code):
    database, app = make_application(tmp_path)
    ctx, arguments, operation, options = context(), {"id": "one"}, "items_create", {"idempotency_key": "one"}
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
        assert uow.repository("items").list() == []


def test_context_revalidation_admits_only_active_global_agents_within_the_active_directory(tmp_path):
    database, app = make_application(tmp_path)
    global_agent, unknown = "00000000-0000-4000-8000-0000000000c4", "00000000-0000-4000-8000-0000000000b9"
    with database.transaction() as uow:
        for identity, home, scope in ((AGENT, BUSINESS, {"kind": "business", "business_id": BUSINESS}),
                                      (global_agent, None, {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []})):
            uow.repository("principals").insert({"id": identity, "kind": "agent"})
            uow.repository("agent_profiles", business_id=home).insert({"agent_id": identity, "principal_id": identity,
                "namespace": home or "ecosystem", "name": "worker-" + identity[-2:], "workspace": str(tmp_path / identity),
                "profile_json": {"status": "active", "role": {"id": "global-worker"}, "scope": scope}})

    def code(ctx, key):
        result = app.invoke(ctx, "items_create", {"id": key}, {"idempotency_key": key})
        return result.error["code"] if result.status == "error" else result.status

    # A business-home registration is history, never an actor, even within exactly its own business.
    home = InvocationContext(Principal(AGENT, "agent"), business_id=BUSINESS, home_business_id=BUSINESS,
        allowed_business_ids=frozenset({BUSINESS}), profile_revision=1)
    assert code(home, "home") == code(replace(home, home_business_id=None), "home") == "forbidden"
    # A global agent reaches every active business with no stored list or membership row, and nothing beyond.
    ctx = InvocationContext(Principal(global_agent, "agent"), business_id=OTHER, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                            profile_revision=1)
    assert code(ctx, "one") == "ok"
    assert code(replace(ctx, allowed_business_ids=frozenset({BUSINESS, OTHER, unknown})), "two") == "forbidden"
    assert code(replace(ctx, home_business_id=OTHER), "two") == "forbidden"
    assert code(replace(ctx, global_capabilities=frozenset({"read_global_context"})), "two") == "forbidden"
    assert code(replace(ctx, profile_revision=2), "two") == "context_refresh_required"
    with database.transaction() as uow:
        uow.repository("agent_profiles", business_id=None).update(global_agent, {"status": "retired"}, expected_revision=1)
    assert code(replace(ctx, profile_revision=2), "two") == "inactive_agent"


def test_context_resolver_cannot_replace_authenticated_provenance(tmp_path):
    database, app = make_application(tmp_path)
    ctx = context()
    app.context_resolver = lambda current, uow: replace(current, source="worker")
    assert app.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "invalid_principal"
    app.context_resolver = lambda current, uow: replace(current, principal=Principal(OTHER, "human"))
    assert app.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "invalid_principal"


def test_composed_effect_cannot_bypass_denial(tmp_path):
    def prepare(ctx, args, options, uow):
        effect = PreparedChange("record", ResourceRef("items", args["id"], ctx.business_id), "assign", {}, operation_name="items_assign")
        return item_plan(ctx, args, extra=(effect,))
    database, app = make_application(tmp_path, prepare=prepare, policy=FixedPolicy(overrides={"items_assign": "deny"}))
    app.registry.register(OperationDescriptor("items_assign", {"type": "object"}, prepare=lambda *_: None, apply=lambda *_: {}))
    result = app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "policy_denied"
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").list() == []


def test_prepare_cannot_write_and_bad_output_rolls_back(tmp_path):
    def bad_prepare(ctx, args, options, uow):
        uow.repository("principals").update(HUMAN, {"display_name": "Side effect"}, expected_revision=1)
    database, app = make_application(tmp_path, prepare=bad_prepare)
    result = app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "read_only"
    with database.read() as uow:
        assert uow.repository("principals").get(HUMAN)["display_name"] == "Owner"


def test_invalid_apply_output_rolls_back_domain_and_receipt(tmp_path):
    def bad_apply(ctx, plan, uow):
        uow.repository("items", business_id=ctx.business_id).insert(plain(plan.changes[0].values))
        return ["not the declared object"]
    database, app = make_application(tmp_path, apply=bad_apply)
    result = app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"})
    assert result.error["code"] == "invalid_result"
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").list() == []
        assert uow.repository("idempotency_receipts").list() == []


# -- owning-scope mutation and exact revision CAS -------------------------------------------------------------------

def test_owning_scope_mutation_with_revision_cas_through_portable_plan_helpers(tmp_path):
    database, app = make_application(tmp_path)
    add_update_operation(app)
    ctx = context()
    assert app.invoke(ctx, "items_create", {"id": "one", "title": "First"}, {"idempotency_key": "create"}).status == "ok"
    updated = app.invoke(ctx, "items_update", {"id": "one", "title": "Second", "expected_revision": 1}, {"idempotency_key": "u1"})
    assert updated.status == "ok" and updated.data["item"]["revision"] == 2 and updated.data["item"]["title"] == "Second"
    # A stale pin is refused at prepare, before any plan exists.
    stale = app.invoke(ctx, "items_update", {"id": "one", "title": "Third", "expected_revision": 1}, {"idempotency_key": "u2"})
    assert stale.error["code"] == "revision_conflict" and stale.error["current_revision"] == 2
    # The saved receipt is replayed, never reapplied, even though the pinned revision is no longer current.
    again = app.invoke(ctx, "items_update", {"id": "one", "title": "Second", "expected_revision": 1}, {"idempotency_key": "u1"})
    assert again.status == "ok" and again.data == updated.data
    with database.read(business_id=BUSINESS) as uow:
        row = uow.repository("items").get("one")
        assert (row["title"], row["revision"]) == ("Second", 2)
    # The domain's history hook saw the tracked update with its before/after revisions.
    assert database.demo.hooks.applied == [("items", 1, 2)]


def test_a_versioned_operation_binds_its_plan_to_the_registered_version(tmp_path):
    def versioned(builder_version):
        def prepare(ctx, args, opts, uow):
            builder = PlanBuilder(ctx, "items_revise", args, "Revise item", **({} if builder_version is None else {"version": builder_version}))
            builder.record("items", args["id"], None, {"id": args["id"], "title": "Revised"})
            return builder.build()
        return prepare
    outcomes = {}
    for label, builder_version in (("matching", 2), ("default", None), ("older", 1)):
        database, app = make_application(tmp_path / label)
        app.registry.register(OperationDescriptor("items_revise", ITEM_SCHEMA, version=2, prepare=versioned(builder_version),
                                                  apply=lambda ctx, plan, uow: {"item": apply_records(ctx, plan, uow)[0]}))
        outcomes[label] = app.invoke(context(), "items_revise", {"id": label}, {"idempotency_key": label})
    assert outcomes["matching"].status == "ok", outcomes["matching"].error
    assert outcomes["matching"].data["item"]["title"] == "Revised"
    for label in ("default", "older"):  # a plan prepared under any other version is never applied for this descriptor
        assert outcomes[label].status == "error" and outcomes[label].error["code"] == "invalid_plan", label


def test_a_write_between_prepare_and_apply_conflicts_on_the_prepared_revision(tmp_path):
    database, app = make_application(tmp_path)
    add_update_operation(app)
    ctx = context()
    app.invoke(ctx, "items_create", {"id": "one", "title": "First"}, {"idempotency_key": "create"})
    def prepare(ctx, args, opts, uow):
        before = uow.repository("items", business_id=ctx.business_id).get(args["id"], required=True)
        builder = PlanBuilder(ctx, "items_update", args, "Update item")
        builder.record("items", args["id"], before, {"title": args["title"]})
        plan = builder.build()
        with database.transaction(business_id=BUSINESS) as other:  # a concurrent writer wins after the snapshot
            other.repository("items").update("one", {"title": "Raced"}, expected_revision=1)
        return plan
    racing_registry = OperationRegistry()
    racing_registry.register(OperationDescriptor("items_update", ITEM_SCHEMA, prepare=prepare,
        apply=lambda c, plan, uow: {"item": apply_records(c, plan, uow)[0]}))
    racing_app = Application(database, racing_registry, policy=FixedPolicy())
    result = racing_app.invoke(ctx, "items_update", {"id": "one", "title": "Mine"}, {"idempotency_key": "race"})
    assert result.error["code"] == "revision_conflict"
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").get("one")["title"] == "Raced"
        assert uow.repository("idempotency_receipts").list(filters={"operation_name": "items_update"}) == []


def test_prepared_targets_must_belong_to_the_selected_owning_scope(tmp_path):
    def prepare(ctx, args, options, uow):
        builder = PlanBuilder(ctx, "items_create", args, "Create elsewhere")
        builder.record("items", args["id"], None, {"id": args["id"], "title": "Hijack", "created_by": ctx.principal_id},
                       business_id=OTHER)
        return builder.build()
    database, app = make_application(tmp_path, prepare=prepare)
    # Another allowed business is still not the invocation's owning scope for a write.
    ctx = replace(context(), allowed_business_ids=frozenset({BUSINESS, OTHER}))
    assert app.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "forbidden"
    # And a business outside the principal's authority is refused outright.
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "two"}).error["code"] == "forbidden"
    for business in (BUSINESS, OTHER):
        with database.read(business_id=business) as uow:
            assert uow.repository("items").list() == []


def test_record_application_hooks_cover_domain_effects_forced_revisions_and_history(tmp_path):
    database, app = make_application(tmp_path)
    hooks = database.demo.hooks
    def prepare(ctx, args, opts, uow):
        before = uow.repository("items", business_id=ctx.business_id).get(args["id"])
        builder = PlanBuilder(ctx, "items_touch", args, "Touch")
        if args.get("op") == "stamp":
            builder.record("items", args["id"], None, {"id": args["id"], "title": "stamped"}, action="stamp")
        elif args.get("op") == "delete":
            builder.record("items", args["id"], before, {}, action="delete")
        else:
            builder.record("items", args["id"], before, {"title": before["title"]})
        return builder.build()
    schema = {"type": "object", "required": ["id", "op"], "properties": {"id": {"type": "string"}, "op": {"type": "string"}}}
    app.registry.register(OperationDescriptor("items_touch", schema, prepare=prepare,
        apply=lambda c, plan, uow: {"rows": apply_records(c, plan, uow)}))
    ctx = context()
    stamped = app.invoke(ctx, "items_touch", {"id": "s", "op": "stamp"}, {"idempotency_key": "stamp"})
    assert stamped.data["rows"][0]["title"] == "STAMPED"  # the domain's own effect wrote it, not the generic insert
    assert ("items", "stamp") in hooks.offered
    app.invoke(ctx, "items_create", {"id": "plain", "title": "Plain"}, {"idempotency_key": "create"})
    # Identical values would be a no-op update; the domain's force_revision hook advances the revision anyway.
    touched = app.invoke(ctx, "items_touch", {"id": "plain", "op": "update"}, {"idempotency_key": "touch"})
    assert touched.data["rows"][0]["revision"] == 2
    removed = app.invoke(ctx, "items_touch", {"id": "plain", "op": "delete"}, {"idempotency_key": "delete"})
    assert removed.data["rows"][0]["id"] == "plain"
    # The hook-handled stamp wrote its own row; the generic update and delete reported before/after images.
    assert hooks.applied == [("items", 1, 2), ("items", 2, None)]
    with database.read(business_id=BUSINESS) as uow:
        assert [row["id"] for row in uow.repository("items").list()] == ["s"]
        assert uow.connection.execute("SELECT count(*) FROM demo_history WHERE kind='record'").fetchone()[0] == len(hooks.applied)


def test_review_scope_requires_a_trusted_exception_for_business_content_in_a_global_plan(tmp_path):
    def prepare(ctx, args, options, uow):
        resource = ResourceRef("items", args["id"], BUSINESS)
        plan = item_plan(replace(ctx, business_id=BUSINESS), args)
        return replace(plan, target_namespace="global", affected_scope={"kind": "global"}, review_business_id=None,
                       changes=(PreparedChange("record", resource, "insert", {"id": args["id"], "title": "T"}),))
    made = []
    def make(exceptions=()):
        made.append(None)
        database, app = make_application(tmp_path / str(len(made)), prepare=prepare, review_exceptions=exceptions,
            apply=lambda c, plan, uow: {"item": apply_records(c, plan, uow)[0]},
            descriptor_options={"scope_kinds": frozenset({"global"}), "global_capability": "demo_global"})
        return database, app
    ctx = replace(context(), business_id=None, allowed_business_ids=frozenset({BUSINESS}),
                  global_capabilities=frozenset({"demo_global"}))
    _, refused = make()
    assert refused.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "invalid_plan"
    other = ReviewScopeException(frozenset({"items_other"}), frozenset({"items"}))
    _, still_refused = make((other,))
    assert still_refused.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "invalid_plan"
    exception = ReviewScopeException(frozenset({"items_create"}), frozenset({"items"}), principal_kinds=frozenset({"human"}),
                                     allow_delegated=False)
    database, allowed = make((exception,))
    assert allowed.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"}).status == "ok"
    with pytest.raises(TypeError, match="typed contributions"):
        Application(database, OperationRegistry(), review_exceptions=({"operations": ["items_create"]},))


# -- Signoff staging: the exact prepared plan reaches an injected stager -----------------------------------------------

class StoredProposal:
    """An injected test stager; the real Signoff lifecycle is a later extraction step."""

    def __init__(self):
        self.staged = []

    def stage(self, ctx, descriptor, plan, options, uow):
        self.staged.append((descriptor.name, plan, options))
        identity = new_id()
        uow.repository("signoffs", business_id=ctx.review_business_id).insert({
            "id": identity, "proposer_principal_id": ctx.principal_id, "operation_name": descriptor.name,
            "operation_version": descriptor.version, "proposal_revision": 1, "payload_hash": plan.payload_hash,
            "change_set": plan.canonical, "preview_json": plan.preview, "expected_targets_json": plain(plan.expected_targets),
            "policy_binding_json": plan.policy_binding, "affected_scope_json": plan.affected_scope,
            "state": "pending", "updated_sequence": uow.next_sequence("signoffs")})
        return OperationResult("", "signoff_required", signoff={"id": identity, "payload_hash": plan.payload_hash})


def test_policy_protected_mutation_stages_the_exact_prepared_plan_without_domain_effect(tmp_path):
    stager = StoredProposal()
    database, app = make_application(tmp_path, policy=FixedPolicy("signoff"), signoffs=stager)
    result = app.invoke(context(), "items_create", {"id": "one", "title": "Held"}, {"idempotency_key": "one"})
    assert result.status == "signoff_required"
    name, plan, options = stager.staged[0]
    assert name == "items_create" and options.idempotency_key == "one"
    # The staged value is the engine-bound plan: v2 encoding, review destination, authority binding of the decisions.
    assert plan.encoding_version == 2 and plan.review_business_id == BUSINESS
    assert plan.review_scope == {"kind": "business", "business_id": BUSINESS}
    assert plan.policy_binding["operations"]["items_create"]["mode"] == "signoff"
    assert plan.target_namespace == "business:" + BUSINESS and plan.principal_id == HUMAN
    with database.read(business_id=BUSINESS) as uow:
        assert not uow.repository("items").list()
        row = uow.repository("signoffs", business_id=BUSINESS).get(result.signoff["id"])
        assert row["change_set"] == plan.canonical and row["payload_hash"] == plan.payload_hash
        assert ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"]) == plan
    # A retry with the same key replays the staged reference instead of staging a second proposal.
    again = app.invoke(context(), "items_create", {"id": "one", "title": "Held"}, {"idempotency_key": "one"})
    assert again.signoff == result.signoff and len(stager.staged) == 1


def test_staging_failures_leave_no_receipt_or_proposal(tmp_path):
    database, app = make_application(tmp_path, policy=FixedPolicy("signoff"))
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "operation_unavailable"
    class RefusingStage(StoredProposal):
        def stage(self, *args):
            raise DomainError("forbidden", "Staging refused")
    app.signoffs = RefusingStage()
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "forbidden"
    class WrongResult(StoredProposal):
        def stage(self, *args):
            return OperationResult("", "ok", data={})
    app.signoffs = WrongResult()
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "invalid_result"
    with database.read() as uow:
        assert uow.repository("signoffs", business_id=BUSINESS).list() == []
        assert uow.repository("idempotency_receipts").list() == []
        assert uow.repository("items", business_id=BUSINESS).list() == []


def staged_plan(app, database, ctx=None):
    ctx = ctx or context()
    staged = app.invoke(ctx, "items_create", {"id": "one"}, {"idempotency_key": "one"})
    with database.read(business_id=BUSINESS) as uow:
        row = uow.repository("signoffs").get(staged.signoff["id"])
        return row, ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])


def test_approval_binds_exact_bytes_and_is_required_to_apply(tmp_path):
    database, app = make_application(tmp_path, policy=FixedPolicy("signoff"), signoffs=StoredProposal())
    row, plan = staged_plan(app, database)
    with database.read() as uow:
        app.validate_prepared(context(), plan, uow, require_approval=False)
        with pytest.raises(DomainError, match="approval"):
            app.validate_prepared(context(), plan, uow)
    with pytest.raises(DomainError, match="approval"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow)
    with database.transaction() as uow:
        uow.repository("signoffs", business_id=BUSINESS).update(
            row["id"], {"state": "approved", "updated_sequence": uow.next_sequence("signoffs")}, expected_revision=1)
    for wrong in (ApprovalBinding(row["id"], "0" * 64), ApprovalBinding(new_id(), plan.payload_hash), None):
        with pytest.raises(DomainError, match="approval|Exact owner"):
            with database.transaction() as uow:
                app.apply_prepared(context(), plan, uow, approval=wrong)
    with database.transaction() as uow:
        applied = app.apply_prepared(context(), plan, uow, approval=ApprovalBinding(row["id"], plan.payload_hash))
        assert applied.data["item"]["id"] == "one"
    with pytest.raises(DomainError):
        with database.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET change_set=? WHERE id=?", (canonical_bytes({"changed": True}), row["id"]))


def test_changed_or_revoked_grants_are_never_bypassed_by_a_stored_approval(tmp_path):
    policy = FixedPolicy("signoff")
    database, app = make_application(tmp_path, policy=policy, signoffs=StoredProposal())
    row, plan = staged_plan(app, database)
    with database.transaction() as uow:
        uow.repository("signoffs", business_id=BUSINESS).update(
            row["id"], {"state": "approved", "updated_sequence": uow.next_sequence("signoffs")}, expected_revision=1)
    approval = ApprovalBinding(row["id"], row["payload_hash"])
    # A current grant that now allows immediate execution changes the authority binding: the proposal is stale.
    policy.mode = "allow"
    with pytest.raises(DomainError, match="Authority configuration changed"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow, approval=approval)
    # Revocation after approval denies, whatever the owner already approved.
    policy.mode = "deny"
    with pytest.raises(DomainError, match="denied"):
        with database.transaction() as uow:
            app.apply_prepared(context(), plan, uow, approval=approval)
    # A deny on a composed effect is honoured the same way.
    policy.mode, policy.overrides = "signoff", {"items_create": "signoff", "items_extra": "deny"}
    app.registry.register(OperationDescriptor("items_extra", {"type": "object"}, prepare=lambda *_: None, apply=lambda *_: {}))
    extra = PreparedChange("record", ResourceRef("items", "one", BUSINESS), "assign", {}, operation_name="items_extra")
    composed = replace(plan, changes=(*plan.changes, extra))
    with pytest.raises(DomainError):
        with database.read() as uow:
            app.validate_prepared(context(), composed, uow, require_approval=False)
    with database.read(business_id=BUSINESS) as uow:
        assert not uow.repository("items").list()


def test_maintenance_gate_blocks_writes_without_disabling_reads(tmp_path):
    database, app = make_application(tmp_path)
    app.registry.register(OperationDescriptor("items_list", {"type": "object"}, effect_kind="read", supports_signoff=False,
        read=lambda ctx, args, options, uow: {"items": uow.repository("items", business_id=ctx.business_id).list()}))
    with database.transaction() as uow:
        uow.repository("maintenance_state").insert({"id": "service", "state": "closed", "message": "Restoring"})
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "maintenance"
    assert app.invoke(context(), "items_list").status == "ok"


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
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).status == "ok"
    assert events == [("external", 0), "apply"]
    with pytest.raises(ValueError, match="signoff"):
        OperationDescriptor("staged", ITEM_SCHEMA, prepare=lambda *_: None, apply=lambda *_: {}, external=lambda *_: None)


def test_writer_admission_gates_mutations_but_not_reads(tmp_path):
    entered = []
    class Admission:
        @contextmanager
        def writer(self):
            entered.append("writer")
            yield
    database, app = make_application(tmp_path, writer_admission=Admission())
    app.registry.register(OperationDescriptor("items_list", {"type": "object"}, effect_kind="read", supports_signoff=False,
                                              read=lambda *_: {}))
    assert app.invoke(context(), "items_list").status == "ok" and entered == []
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).status == "ok"
    assert entered == ["writer"]


def test_replay_validator_can_only_refuse_a_saved_receipt(tmp_path):
    allowed = [True]
    def validator(ctx, arguments, result, uow):
        if not allowed[0]:
            raise DomainError("forbidden", "Authority for this receipt was withdrawn")
    database, app = make_application(tmp_path, descriptor_options={"replay_validator": validator})
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).status == "ok"
    allowed[0] = False
    assert app.invoke(context(), "items_create", {"id": "one"}, {"idempotency_key": "one"}).error["code"] == "forbidden"
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("items").list()) == 1


def test_target_resolvers_register_once(tmp_path):
    database, app = make_application(tmp_path)
    app.register_target_resolver("blobs", lambda ctx, ref, uow: None)
    with pytest.raises(ValueError, match="already registered"):
        app.register_target_resolver("blobs", lambda ctx, ref, uow: None)


def test_bound_review_exceptions_match_only_the_exact_argument_bound_resource():
    """The v1 owner-journal rule, expressed as data: one document selected by argument, from Global, by its human owner."""
    from qorqut_operations.application import BoundResource
    from qorqut_operations.application.review import review_scope
    exception = ReviewScopeException(frozenset({"notes_update", "notes_delete"}), principal_kinds=frozenset({"human"}),
        allow_delegated=False, bound_resources={"documents": BoundResource("document_id"),
                                                "managed_document_files": BoundResource("document_id", "document_id")})
    document = "00000000-0000-4000-8000-0000000000d1"
    def plan_for(*resources, operation="notes_update", arguments=None):
        changes = tuple(PreparedChange("record", resource, "update", {}) for resource in resources)
        base = item_plan(context(), {"id": "x"})
        return replace(base, operation_name=operation, target_namespace="global", changes=changes,
                       arguments={"document_id": document} if arguments is None else arguments)
    exact = ResourceRef("documents", document, BUSINESS)
    managed = ResourceRef("managed_document_files", '{"document_id": "%s"}' % document, BUSINESS)
    ctx = replace(context(), business_id=None)
    assert review_scope(ctx, plan_for(exact, managed), (exception,)) == {"kind": "global"}
    for refused in (plan_for(ResourceRef("documents", "other", BUSINESS)),
                    plan_for(ResourceRef("managed_document_files", '{"document_id": "other"}', BUSINESS)),
                    plan_for(ResourceRef("managed_document_files", "not json", BUSINESS)),
                    plan_for(ResourceRef("items", document, BUSINESS)),
                    plan_for(exact, operation="notes_create"), plan_for(exact, arguments={})):
        with pytest.raises(DomainError, match="separate business-owned proposal"):
            review_scope(ctx, refused, (exception,))
    with pytest.raises(DomainError, match="separate business-owned proposal"):
        review_scope(ctx, plan_for(exact), ())
    worker = replace(ctx, principal=Principal(AGENT, "agent"), source="mcp")
    with pytest.raises(DomainError, match="separate business-owned proposal"):
        review_scope(worker, plan_for(exact), (exception,))
    # A business-namespace plan is simply reviewed by that business and needs no exception.
    own = replace(item_plan(context(), {"id": "x"}), changes=(PreparedChange("record", exact, "update", {}),))
    assert review_scope(context(), own, ()) == {"kind": "business", "business_id": BUSINESS}
