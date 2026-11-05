
import pytest

from qorqut_operations.application import Application, DelegatedCreationGroup, OperationDescriptor, OperationRegistry
from qorqut_operations.application.delegation import authorize_delegation
from qorqut_operations.contracts import (ArtifactRef, DomainError, ExpectedTarget, InvocationContext, PreparedChange,
    Principal, ResourceRef)
from qorqut_operations.application.plans import apply_records
from qorqut_operations.contracts import (ChangePreview, ChangeSet, FieldChange, PreviewResource, StructuredPreview,
    new_id)
from support import AGENT, BUSINESS, OTHER, FixedPolicy, database_at, item_plan

BINDING, ASSIGNMENT, LEASE = (f"00000000-0000-4000-8000-0000000000{n}" for n in ("b1", "a1", "c1"))
ROLE = ArtifactRef("global-peer", "1.0.0", "a" * 64)
SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}, "item_id": {"type": "string"},
                                          "thread_id": {"type": "string"}}}


class FakeDelegation:
    """The host's delegation rows and vocabulary, as a typed provider."""

    worker_operations = frozenset({"delegated_assignment_get", "delegated_assignment_checkpoint"})
    context_scoped_reads = frozenset({"agent_config_get"})

    def __init__(self, *, business_id=BUSINESS, allowed=("items_create", "items_get"), record_ids=None, scope_kind=None):
        self.binding_row = {"id": BINDING, "assignment_id": ASSIGNMENT, "parent_principal_id": AGENT,
                            "execution_generation": 1, "revoked_at": None, "credential_hash": "h"}
        self.assignment_row = {"id": ASSIGNMENT, "business_id": business_id, "lease_id": LEASE, "plan_revision": 4,
            "allowed_operations_json": list(allowed), "links_json": {"message_threads": ["t-linked"]},
            "scope_json": {"kind": scope_kind or ("business" if business_id else "global"),
                           "allowed_business_ids": [BUSINESS], "record_ids": record_ids if record_ids is not None else {}}}
        self.live, self.supported, self.required, self.thread_messages = True, True, {"manage_delegated_assignments"}, set()
        self.recorded = []

    def binding(self, uow, binding_id):
        return self.binding_row if binding_id == BINDING else None

    def authenticate_binding(self, uow, credential_hash):
        return {"id": BINDING} if self.live and credential_hash == "h" else None

    def assignment(self, uow, assignment_id):
        return self.assignment_row if assignment_id == ASSIGNMENT else None

    def supports_role(self, role):
        return self.supported

    def required_capabilities(self, role):
        return frozenset(self.required)

    def selector_kinds(self, operation_name):
        return {"item_id": "items", "thread_id": "message_threads", **({"id": "items"} if operation_name == "items_get" else {})}

    def thread_has_assignment_message(self, uow, thread_id, assignment_id):
        return thread_id in self.thread_messages

    def record_committed_action(self, uow, assignment_id, **action):
        self.recorded.append((assignment_id, action))


def delegated_context(**kwargs):
    values = dict(business_id=BUSINESS, home_business_id=None, allowed_business_ids=frozenset({BUSINESS}),
                  global_capabilities=frozenset({"manage_delegated_assignments"}), profile_revision=1, source="mcp",
                  role_artifact_ref=ROLE, delegation_binding_id=BINDING, delegation_assignment_id=ASSIGNMENT,
                  delegation_generation=1)
    return InvocationContext(Principal(AGENT, "agent"), **{**values, **kwargs})


def descriptor(name="items_create", **kwargs):
    options = dict(prepare=lambda *_: None, apply=lambda *_: None)
    if kwargs.get("effect_kind") == "read":
        options = dict(read=lambda *_: {}, supports_signoff=False)
    return OperationDescriptor(name, SCHEMA, **options, **kwargs)


def check(provider, ctx=None, op=None, targets=(), namespace="business:" + BUSINESS, arguments=None, uow=None, groups=()):
    return authorize_delegation(provider, ctx or delegated_context(), op or descriptor(), targets, uow,
                                namespace=namespace, arguments=arguments, creation_groups=groups)


def refused(match, **kwargs):
    with pytest.raises(DomainError, match=match) as raised:
        check(**kwargs)
    assert raised.value.code == "forbidden"


def test_absent_provider_fails_closed():
    with pytest.raises(DomainError, match="not installed") as raised:
        authorize_delegation(None, delegated_context(), descriptor(), (), None, namespace="business:" + BUSINESS)
    assert raised.value.code == "forbidden"


def test_an_exact_live_binding_assignment_and_operation_are_required():
    provider = FakeDelegation()
    assert check(provider)["id"] == ASSIGNMENT
    for change in ({"assignment_id": OTHER}, {"parent_principal_id": OTHER}, {"execution_generation": 2},
                   {"revoked_at": "2026-01-01T00:00:00Z"}):
        stale = FakeDelegation()
        stale.binding_row.update(change)
        refused("missing, revoked, or mismatched", provider=stale)
    refused("missing, revoked, or mismatched", provider=provider, ctx=delegated_context(delegation_binding_id=OTHER))
    gone = FakeDelegation()
    gone.live = False
    refused("no longer live", provider=gone)
    missing = FakeDelegation()
    missing.assignment_row = None
    refused("assignment is missing", provider=missing)
    refused("outside the approved assignment", provider=FakeDelegation(allowed=("items_get",)))


def test_the_peer_role_and_explicit_capabilities_are_required():
    unsupported = FakeDelegation()
    unsupported.supported = False
    refused("compatible global peer role", provider=unsupported)
    refused("compatible global peer role", provider=FakeDelegation(), ctx=delegated_context(role_artifact_ref=None))
    refused("compatible global peer role", provider=FakeDelegation(),
            ctx=delegated_context(global_capabilities=frozenset()))
    stricter = FakeDelegation()
    stricter.required = {"manage_delegated_assignments", "coordinate_businesses"}
    refused("compatible global peer role", provider=stricter)
    assert check(stricter, ctx=delegated_context(global_capabilities=frozenset(stricter.required)))
    # The host cannot waive the engine's own capability by omitting it from the role requirements.
    lenient = FakeDelegation()
    lenient.required = set()
    refused("compatible global peer role", provider=lenient, ctx=delegated_context(global_capabilities=frozenset()))


def test_owner_only_and_externally_consequential_operations_are_never_delegated():
    provider = FakeDelegation(allowed=("owner_op", "platform_op"))
    refused("owner-only or externally consequential", provider=provider,
            op=OperationDescriptor("owner_op", SCHEMA, owner_only=True, exposure=frozenset({"dashboard"}),
                                   supports_signoff=False, effect_kind="read", read=lambda *_: {}))
    refused("owner-only or externally consequential", provider=provider,
            op=descriptor("platform_op", signoff_category="platform_action"))


def test_business_and_global_assignment_scopes():
    refused("exact project scope", provider=FakeDelegation(), namespace="business:" + OTHER)
    refused("exact project scope", provider=FakeDelegation(), namespace="global")
    refused("exact project scope", provider=FakeDelegation(), ctx=delegated_context(business_id=None))
    wide = FakeDelegation(business_id=None, scope_kind="global")
    assert check(wide, namespace="global")
    assert check(wide, namespace="business:" + BUSINESS)
    refused("approved global project scope", provider=wide, namespace="business:" + OTHER)
    refused("approved global project scope", provider=wide, namespace="agent:" + AGENT)
    narrow = FakeDelegation(business_id=None, scope_kind="business")
    refused("cannot access global operations", provider=narrow, namespace="global")


def test_prepared_targets_stay_inside_the_assignment_record_and_business_boundary():
    provider = FakeDelegation(record_ids={"items": ["one"]})
    inside = (ExpectedTarget(ResourceRef("items", "one", BUSINESS), exists=True),)
    assert check(provider, targets=inside)
    refused("record boundary", provider=provider, targets=(ExpectedTarget(ResourceRef("items", "two", BUSINESS)),))
    refused("record boundary", provider=provider, targets=(ExpectedTarget(ResourceRef("documents", "one", BUSINESS)),))
    refused("assignment project scope", provider=provider, targets=(ExpectedTarget(ResourceRef("items", "one", OTHER)),))
    refused("record boundary", provider=FakeDelegation(), targets=inside)   # no record IDs granted at all


# -- one exact group of created records ------------------------------------------------------------------------------

GROUP = DelegatedCreationGroup("pair_create", frozenset({"items", "documents"}))
NEW, NEW_TOO = "00000000-0000-4000-8000-0000000000d1", "00000000-0000-4000-8000-0000000000d2"


def pair(first=NEW, second=NEW, *, business=BUSINESS, namespace=None, exists=False):
    return (ExpectedTarget(ResourceRef("items", first, business, namespace), exists=exists),
            ExpectedTarget(ResourceRef("documents", second, business, namespace), exists=exists))


def creates(provider=None, **kwargs):
    kwargs.setdefault("op", descriptor("pair_create"))
    kwargs.setdefault("targets", pair())
    return check(provider or FakeDelegation(allowed=("pair_create",)), groups=(GROUP,), **kwargs)


def test_a_complete_creation_group_alone_is_exempt_from_the_assignment_record_id_membership():
    provider = FakeDelegation(allowed=("pair_create",))   # no record IDs are granted at all
    refused("record boundary", provider=provider, op=descriptor("pair_create"), targets=pair())   # no rule: unchanged
    assert creates(provider)["id"] == ASSIGNMENT
    # The canonical admission namespace may be spelled on the resource; nothing else may.
    assert creates(provider, targets=pair(namespace="business:" + BUSINESS))
    for namespace in ("business:" + OTHER, "global", "agent:" + AGENT):
        refused("record boundary", provider=provider, op=descriptor("pair_create"), groups=(GROUP,),
                targets=pair(namespace=namespace))
    # Anything short of the whole group keeps the unchanged boundary.
    wrong = {
        "incomplete": pair()[:1],
        "extra target": pair() + (ExpectedTarget(ResourceRef("items", NEW_TOO, BUSINESS), exists=False),),
        "extra type": pair() + (ExpectedTarget(ResourceRef("demo_history", NEW, BUSINESS), exists=False),),
        "duplicate type": (pair()[0], pair()[0]),
        "another type": (pair()[0], ExpectedTarget(ResourceRef("demo_history", NEW, BUSINESS), exists=False)),
        "different identities": pair(second=NEW_TOO),
        "not a UUID": pair("one", "one"),
        "existing target": pair(exists=True),
        "one existing": (pair()[0], pair(exists=True)[1]),
        "Global scope under a business": pair(business=None),
        "bare references": tuple(target.resource for target in pair()),
    }
    for name, targets in wrong.items():
        with pytest.raises(DomainError, match="record boundary") as raised:
            creates(provider, targets=targets)
        assert raised.value.code == "forbidden", name
    # Another operation, or one with an existing expected record, never inherits the rule.
    other = FakeDelegation(allowed=("other_create",))
    refused("record boundary", provider=other, op=descriptor("other_create"), groups=(GROUP,), targets=pair())
    with pytest.raises(DomainError, match="record boundary"):
        creates(provider, targets=(ExpectedTarget(ResourceRef("items", "one", BUSINESS), exists=True), pair()[1]))


def test_a_global_assignment_admits_only_a_group_in_the_scope_of_its_actual_namespace():
    wide = FakeDelegation(business_id=None, scope_kind="global", allowed=("pair_create",))
    assert creates(wide, namespace="global", targets=pair(business=None))
    assert creates(wide, targets=pair())
    # A business-scoped group under the Global namespace (or the reverse) is not the admission's own scope.
    for namespace, business in (("global", BUSINESS), ("business:" + BUSINESS, None)):
        with pytest.raises(DomainError, match="record boundary"):
            creates(wide, namespace=namespace, targets=pair(business=business))
    with pytest.raises(DomainError, match="assignment project scope"):
        creates(wide, targets=pair(business=OTHER))


def test_a_creation_group_changes_none_of_the_mandatory_assignment_checks():
    refused("outside the approved assignment", provider=FakeDelegation(allowed=()), op=descriptor("pair_create"),
            groups=(GROUP,), targets=pair())
    refused("exact project scope", provider=FakeDelegation(allowed=("pair_create",)), op=descriptor("pair_create"),
            groups=(GROUP,), targets=pair(business=OTHER), namespace="business:" + OTHER)
    refused("exact project scope", provider=FakeDelegation(allowed=("pair_create",)), op=descriptor("pair_create"),
            groups=(GROUP,), targets=pair(business=None), namespace="global")
    gone = FakeDelegation(allowed=("pair_create",))
    gone.live = False
    refused("no longer live", provider=gone, op=descriptor("pair_create"), groups=(GROUP,), targets=pair())
    revoked = FakeDelegation(allowed=("pair_create",))
    revoked.binding_row["revoked_at"] = "2026-01-01T00:00:00Z"
    refused("missing, revoked, or mismatched", provider=revoked, op=descriptor("pair_create"), groups=(GROUP,), targets=pair())
    refused("missing, revoked, or mismatched", provider=FakeDelegation(allowed=("pair_create",)), op=descriptor("pair_create"),
            groups=(GROUP,), targets=pair(), ctx=delegated_context(delegation_generation=2))
    unsupported = FakeDelegation(allowed=("pair_create",))
    unsupported.supported = False
    refused("compatible global peer role", provider=unsupported, op=descriptor("pair_create"), groups=(GROUP,), targets=pair())
    refused("owner-only or externally consequential", provider=FakeDelegation(allowed=("pair_create",)),
            op=descriptor("pair_create", owner_only=True, exposure=frozenset({"dashboard"})), groups=(GROUP,), targets=pair())
    refused("owner-only or externally consequential", provider=FakeDelegation(allowed=("pair_create",)),
            op=descriptor("pair_create", signoff_category="platform_action"), groups=(GROUP,), targets=pair())


def test_creation_groups_are_validated_typed_assembly_input(tmp_path):
    for operation, types in (("Pair", {"items", "documents"}), ("", {"items", "documents"}), (None, {"items", "documents"}),
                             ("pair_create", {"items"}), ("pair_create", set()), ("pair_create", ["items", "items"]),
                             ("pair_create", {"items", "Documents"}), ("pair_create", {"items", 3}), ("pair_create", "items"),
                             ("pair_create", None)):
        with pytest.raises(ValueError):
            DelegatedCreationGroup(operation, types)
    assert DelegatedCreationGroup("pair_create", ["documents", "items"]).resource_types == frozenset({"items", "documents"})
    database = database_at(tmp_path)
    assert Application(database, OperationRegistry()).delegated_creation_groups == ()
    for bad in (({"operation_name": "pair_create"},), (("pair_create", "items", "documents"),)):
        with pytest.raises(TypeError, match="trusted assembly"):
            Application(database, OperationRegistry(), delegated_creation_groups=bad)
    with pytest.raises(ValueError, match="at most one"):
        Application(database, OperationRegistry(),
                    delegated_creation_groups=(GROUP, DelegatedCreationGroup("pair_create", {"items", "demo_history"})))
    assert Application(database, OperationRegistry(), delegated_creation_groups=[GROUP]).delegated_creation_groups == (GROUP,)


def test_reads_need_an_explicit_in_boundary_selector_and_worker_control_is_bound_to_its_own_assignment():
    provider = FakeDelegation(record_ids={"items": ["one"]}, allowed=("items_get",))
    read = descriptor("items_get", effect_kind="read")
    assert check(provider, op=read, arguments={"id": "one"})
    refused("explicit in-boundary record selector", provider=provider, op=read, arguments={})
    refused("explicit in-boundary record selector", provider=provider, op=read, arguments={"id": "two"})
    # Message threads are in-boundary when linked to the assignment or when it already has a message in them.
    assert check(provider, op=read, arguments={"thread_id": "t-linked"})
    refused("explicit in-boundary record selector", provider=provider, op=read, arguments={"thread_id": "t-new"})
    provider.thread_messages.add("t-new")
    assert check(provider, op=read, arguments={"thread_id": "t-new"})
    # An operation the host names as context-scoped needs no record selector.
    scoped = FakeDelegation(allowed=("agent_config_get",))
    assert check(scoped, op=descriptor("agent_config_get", effect_kind="read"), arguments={})
    refused("outside the assignment scope", provider=scoped, op=descriptor("agent_config_get", effect_kind="read"),
            arguments={"business_id": OTHER})
    # Worker control operations may only touch their own assignment.
    control = FakeDelegation(allowed=())
    get = descriptor("delegated_assignment_get", effect_kind="read")
    assert check(control, op=get, arguments={"assignment_id": ASSIGNMENT})
    refused("only its own assignment", provider=control, op=get, arguments={"assignment_id": OTHER})
    refused("bound to their own assignment", provider=control, op=descriptor("delegated_assignment_checkpoint"),
            targets=(ExpectedTarget(ResourceRef("operational_assignments", OTHER, BUSINESS)),))
    assert check(control, op=descriptor("delegated_assignment_checkpoint"),
                 targets=(ExpectedTarget(ResourceRef("operational_assignments", ASSIGNMENT, BUSINESS)),))


# -- through the Application -----------------------------------------------------------------------------------------

def delegated_application(tmp_path, provider, *, executor=None, with_file=False):
    database = database_at(tmp_path)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "agent"})
        # A delegating parent is a global (ecosystem) peer holding the explicit delegation capability.
        uow.repository("agent_profiles", business_id=None).insert({"agent_id": AGENT, "principal_id": AGENT,
            "namespace": "ecosystem", "name": "peer", "workspace": str(tmp_path / "peer"),
            "profile_json": {"status": "active", "role": {"id": "global-worker"}, "scope": {"kind": "ecosystem", "allowed_business_ids": [BUSINESS],
                                       "global_capabilities": ["manage_delegated_assignments"]}}})
    registry = OperationRegistry()
    def insert(ctx, plan, uow):
        return {"item": uow.repository("items", business_id=ctx.business_id).insert(dict(plan.changes[0].values))}
    registry.register(OperationDescriptor("items_create", SCHEMA, prepare=lambda ctx, args, options, uow: item_plan(ctx, args),
                                          apply=insert))
    return database, Application(database, registry, policy=FixedPolicy(), delegation=provider, file_executor=executor)


def test_delegated_calls_are_refused_without_a_provider_and_recorded_with_one(tmp_path):
    database, app = delegated_application(tmp_path, None)
    result = app.invoke(delegated_context(), "items_create", {"id": "one"}, {"idempotency_key": "k"})
    assert result.error["code"] == "forbidden" and "not installed" in result.error["message"]
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").list() == []

    provider = FakeDelegation(record_ids={"items": ["one"]})
    database, app = delegated_application(tmp_path / "second", provider)
    result = app.invoke(delegated_context(), "items_create", {"id": "one"}, {"idempotency_key": "k"})
    assert result.status == "ok"
    (assignment_id, action), = provider.recorded
    assert assignment_id == ASSIGNMENT and action["operation_name"] == "items_create"
    assert action["action_key"] == "k" and action["generation"] == 1 and action["lease_id"] == LEASE
    assert action["plan_revision"] == 4 and action["delegation_binding_id"] == BINDING
    assert action["target_namespace"] == "business:" + BUSINESS
    assert action["effect_ref"]["input_hash"] == action["payload_hash"]
    # A record outside the assignment boundary is refused before anything is applied or recorded.
    denied = app.invoke(delegated_context(), "items_create", {"id": "two"}, {"idempotency_key": "k2"})
    assert denied.error["code"] == "forbidden" and len(provider.recorded) == 1
    with database.read(business_id=BUSINESS) as uow:
        assert [row["id"] for row in uow.repository("items").list()] == ["one"]
    # Withdrawing the assignment's operation or the live binding stops the next call at once.
    provider.assignment_row["allowed_operations_json"] = []
    assert app.invoke(delegated_context(), "items_create", {"id": "one"}, {"idempotency_key": "k3"}).error["code"] == "forbidden"


def test_worker_control_operations_are_not_recorded_as_committed_actions(tmp_path):
    provider = FakeDelegation(record_ids={"items": ["one"]}, allowed=())
    database, app = delegated_application(tmp_path, provider)
    app.registry.register(OperationDescriptor("delegated_assignment_checkpoint", SCHEMA,
        prepare=lambda ctx, args, options, uow: item_plan(ctx, args, operation="delegated_assignment_checkpoint"),
        apply=lambda ctx, plan, uow: {}))
    # The control plan targets an item, which is not the worker's own assignment: refused, nothing recorded.
    result = app.invoke(delegated_context(), "delegated_assignment_checkpoint", {"id": "one"}, {"idempotency_key": "k"})
    assert result.error["code"] == "forbidden" and provider.recorded == []


def test_delegated_workers_cannot_stage_or_resume_file_effects(tmp_path):
    provider = FakeDelegation(record_ids={"items": ["one"]})
    database, app = delegated_application(tmp_path, provider)
    resource = ResourceRef("items", "one", BUSINESS)
    def prepare(ctx, args, options, uow):
        file_change = PreparedChange("file", resource, "write", {"content_b64": None, "before_hash": None, "after_hash": None})
        return item_plan(ctx, args, extra=(file_change,))
    app.registry.get("items_create")
    registry = OperationRegistry()
    registry.register(OperationDescriptor("items_create", SCHEMA, prepare=prepare, apply=lambda *_: {}))
    files = Application(database, registry, policy=FixedPolicy(), delegation=provider)
    result = files.invoke(delegated_context(), "items_create", {"id": "one"}, {"idempotency_key": "k"})
    assert result.error["code"] == "forbidden" and "transactionally receipted" in result.error["message"]

    class Pending:
        def pending(self, *args, **kwargs):
            return item_plan(delegated_context(), {"id": "one"})
        def execute(self, *args, **kwargs):
            raise AssertionError("A delegated worker must never resume out-of-transaction file effects")
    resuming = Application(database, app.registry, policy=FixedPolicy(), delegation=provider, file_executor=Pending())
    result = resuming.invoke(delegated_context(), "items_create", {"id": "one"}, {"idempotency_key": "k2"})
    assert result.error["code"] == "forbidden" and "resume out-of-transaction" in result.error["message"]


def pair_application(tmp_path, provider, *, groups=(GROUP,)):
    database, app = delegated_application(tmp_path, provider)
    def prepare(ctx, arguments, options, uow):
        identity = new_id()
        namespace = "business:" + ctx.business_id
        items, documents = ResourceRef("items", identity, ctx.business_id), ResourceRef("documents", identity, ctx.business_id)
        label = PreviewResource("items", identity, "Pair")
        return ChangeSet("pair_create", 1, ctx.principal_id, namespace, arguments, {"kind": "business", "business_id": ctx.business_id},
            (ExpectedTarget(items, exists=False), ExpectedTarget(documents, exists=False)),
            (PreparedChange("record", items, "insert", {"id": identity, "title": "Pair", "status": "todo", "created_by": ctx.principal_id}),
             PreparedChange("record", documents, "insert", {"id": identity, "kind": "note", "path": "pair.txt", "content_hash": "0" * 64})),
            ChangePreview("Create a pair", "", {"kind": "business", "business_id": ctx.business_id}, (label,),
                          (StructuredPreview(label, (FieldChange("title", None, "Pair"),)),)))
    app.registry.register(OperationDescriptor("pair_create", SCHEMA, prepare=prepare, apply=lambda ctx, plan, uow: {"created": len(apply_records(ctx, plan, uow))}))
    return database, Application(database, app.registry, policy=FixedPolicy(), delegation=provider, delegated_creation_groups=groups)


def test_a_delegated_worker_creates_the_pair_through_the_application_only_with_the_trusted_group(tmp_path):
    provider = FakeDelegation(allowed=("pair_create", "items_create"))
    database, app = pair_application(tmp_path / "grouped", provider)
    result = app.invoke(delegated_context(), "pair_create", {"id": "p"}, {"idempotency_key": "k"})
    assert result.status == "ok", result.error
    with database.read(business_id=BUSINESS) as uow:
        (item,), (document,) = uow.repository("items").list(), uow.repository("documents").list()
        assert item["id"] == document["id"]
    (assignment_id, action), = provider.recorded
    assert assignment_id == ASSIGNMENT and action["operation_name"] == "pair_create" and action["delegation_binding_id"] == BINDING
    # An exact replay is the same receipt: no second pair, no second record of the action.
    again = app.invoke(delegated_context(), "pair_create", {"id": "p"}, {"idempotency_key": "k"})
    assert again.status == "ok" and len(provider.recorded) == 1
    with database.read(business_id=BUSINESS) as uow:
        assert len(uow.repository("items").list()) == 1
    # An unrelated ordinary operation keeps the assignment's record boundary even though a group exists.
    denied = app.invoke(delegated_context(), "items_create", {"id": "two"}, {"idempotency_key": "k2"})
    assert denied.error["code"] == "forbidden" and "record boundary" in denied.error["message"] and len(provider.recorded) == 1
    # Without the trusted rule, the same worker and assignment are refused before anything is written.
    database, plain_app = pair_application(tmp_path / "plain", FakeDelegation(allowed=("pair_create",)), groups=())
    refused_result = plain_app.invoke(delegated_context(), "pair_create", {"id": "p"}, {"idempotency_key": "k"})
    assert refused_result.error["code"] == "forbidden" and "record boundary" in refused_result.error["message"]
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").list() == [] and uow.repository("documents").list() == []
    # Ending the assignment stops the next call with the unchanged live checks.
    provider.live = False
    ended = app.invoke(delegated_context(), "pair_create", {"id": "p"}, {"idempotency_key": "k3"})
    assert ended.error["code"] == "forbidden" and "no longer live" in ended.error["message"]
