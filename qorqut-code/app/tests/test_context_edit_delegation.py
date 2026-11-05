"""A delegated worker's ``context_edit``, end to end (FEATURE_COMPLETION D2, delegated caller continuation).

Real: the retained ``OperationalDelegation`` store (assignment, claim, lease, worker binding, revocation, committed-action receipts)
and the host ``HostDelegationProvider``, the host resolver and ``Application`` with its one trusted ``context_edit`` creation group,
the actual ``context_edit`` operation, the Context integration's queue and ``context_graph_edits`` binding, its ``ContextQueueDriver`` with its
typed authority port, the role sources of the committed prerequisite and a disposable real Context. Faked: only the model runner
(it fails the test if called), the injected clock and the synthetic rows under a pytest ``tmp_path``. Every assignment here has EMPTY
``record_ids``: the edit's random queue/binding identities cannot be named in advance, and no case weakens a permission, patches an
allow or lets the unrestricted parent stand in for the worker.

Every controlled case also asserts that no model started and no published event carries a usage receipt."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.graph_edits")

from qorqut_operations.contracts import DomainError, ExpectedTarget, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.operational_delegation import OperationalDelegation
from context_edit_support import Interrupted, build, put_edge, put_node
from test_context_edit_acceptance import graph, head_error, ordubashy, retry


@pytest.fixture
def host(tmp_path):
    built = build(tmp_path)
    built.models = False
    yield built
    if not built.models:
        built.no_model()


def iso(moment):
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


class Live:
    """One live delegated assignment of a registered parent: its worker binding and the invocation context the worker's calls carry."""

    def __init__(self, host, parent, store, assignment_id, binding, registration_id, parent_registration_id, workspace, business):
        self.host, self.parent, self.store, self.assignment_id = host, parent, store, assignment_id
        self.binding, self.registration_id, self.business = binding, registration_id, business
        self.parent_registration_id, self.workspace = parent_registration_id, workspace

    @property
    def generation(self) -> int:
        return self.binding["execution_generation"]

    @property
    def triplet(self) -> dict:
        return {"binding_id": self.binding["id"], "assignment_id": self.assignment_id, "generation": self.generation}

    def context(self, business=None):
        """The authenticated worker call: the parent's current context plus exactly this binding, assignment and generation."""
        ctx = self.host.runtime.agent_context(Principal(self.parent, "agent"), business_id=self.host.ids[business or self.business],
                                              session_registration_id=self.registration_id)
        return replace(ctx, delegation_binding_id=self.binding["id"], delegation_assignment_id=self.assignment_id,
                       delegation_generation=self.generation)

    def edit(self, plan, *, key=None, business=None):
        return self.host.call(self.context(business), "context_edit", {"edit": plan}, key=key)

    def run(self, action):
        with self.host.database.transaction() as uow:
            return action(uow)

    # -- the ways an assignment ends or changes, through the retained store or the stored rows it keeps --------------------

    def expire_lease(self):
        self.run(lambda uow: uow.connection.execute("UPDATE operational_assignments SET lease_until=? WHERE id=?",
                                                    ("2020-01-01T00:00:00.000000Z", self.assignment_id)))

    def revoke_binding(self):
        self.run(lambda uow: self.store.revoke_binding(uow, self.binding["id"], parent_principal_id=self.parent,
                                                       expected_revision=uow.repository("operational_delegation_bindings")
                                                       .get(self.binding["id"])["revision"]))

    def advance(self, action):
        """Pause or resume through the store: every control moves the execution generation and revokes the worker bindings."""
        def move(uow):
            current = self.store.get(uow, self.assignment_id)
            return self.store.control(uow, self.assignment_id, parent_principal_id=self.parent,
                                      expected_revision=current["revision"], action=action)
        return self.run(move)

    def reclaim(self):
        """A fresh execution generation with its own live lease and a freshly issued binding (what a restarted worker would hold)."""
        self.advance("pause")
        self.advance("resume")
        claimed = self.run(lambda uow: self.store.claim(uow, self.assignment_id, actor_principal_id=self.parent))
        issued = self.run(lambda uow: self.store.issue_worker_binding(
            uow, self.assignment_id, parent_principal_id=self.parent, generation=claimed["execution_generation"],
            lease_id=claimed["lease_id"], expected_revision=claimed["revision"], workspace=self.workspace,
            service_url="http://127.0.0.1:9876", expires_at=iso(datetime.now(timezone.utc) + timedelta(minutes=4)),
            parent_registration_id=self.parent_registration_id))
        return claimed, issued["binding"]

    def reregister(self):
        """The parent's registrations (its own and its worker's) report its current profile revision, as its runtime does after a
        configuration change; the binding is otherwise untouched and stays exactly as live as before."""
        self.run(lambda uow: uow.connection.execute(
            "UPDATE machine_registrations SET profile_revision=(SELECT revision FROM agent_profiles WHERE agent_id=?) "
            "WHERE principal_id=?", (self.parent, self.parent)))


def delegating_parent(host) -> str:
    """The registered Ordubashy as the one delegating parent. Its registration holds only ``read_global_context``, so the owner
    grants the delegated-assignment capability once through the real ``agents_update``."""
    parent = host.agent_id("ordubashy")
    with host.database.read() as uow:
        scope = json.loads(uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (parent,)).fetchone()[0])["scope"]
    if "manage_delegated_assignments" not in scope["global_capabilities"]:
        host.data(host.human(None), "agents_update", {"agent_id": parent, "changes": {"scope": {
            **scope, "global_capabilities": sorted({*scope["global_capabilities"], "manage_delegated_assignments"})}}},
            expected_revision=host._revision(parent))
    return parent


def delegate(host, *, business="first", allowed=("context_edit",), record_ids=None) -> Live:
    """A running assignment of the registered global parent to ``business`` with a live lease and a worker binding."""
    parent = delegating_parent(host)
    store, now = OperationalDelegation(dispatch_reason=None), datetime.now(timezone.utc)
    workspace = str(host.tmp / f"worker-{new_id()[:8]}")
    with host.database.transaction() as uow:
        known = uow.connection.execute(
            "SELECT id FROM machine_registrations WHERE principal_id=? AND COALESCE(json_extract(compatibility_json,'$.kind'),'')"
            "!='delegated_worker'", (parent,)).fetchone()
        registration = known["id"] if known else new_id()
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (parent,)).fetchone()["revision"]
        if known:                                       # the parent's runtime reports the profile revision it now runs
            uow.connection.execute("UPDATE machine_registrations SET profile_revision=? WHERE id=?", (revision, registration))
        else:
            uow.repository("machine_registrations").insert({
                "id": registration, "principal_id": parent, "workspace": str(host.tmp / "parent-ws"),
                "service_url": "http://127.0.0.1:9876", "profile_revision": revision,
                "compatibility_json": {"durable_delegation_enabled": True}, "created_at": iso(now), "updated_at": iso(now)})
        business_id = host.ids[business]
        assignment = store.create(uow, parent_principal_id=parent, business_id=business_id,
                                  scope={"kind": "business", "business_id": business_id, "record_ids": record_ids or {}, "source_ids": {}},
                                  objective="Correct the recorded facts", plan={"steps": ["edit"]}, allowed_operations=list(allowed),
                                  links={}, resource_bounds={"max_turns": 20})
        running = store.claim(uow, assignment["id"], actor_principal_id=parent)
        issued = store.issue_worker_binding(
            uow, assignment["id"], parent_principal_id=parent, generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], workspace=workspace, service_url="http://127.0.0.1:9876",
            expires_at=iso(now + timedelta(minutes=4)), parent_registration_id=registration)
    return Live(host, parent, store, assignment["id"], issued["binding"], issued["registration_id"], registration, workspace, business)


def receipts(host, live):
    return host.query("SELECT * FROM operational_action_receipts WHERE assignment_id=?", live.assignment_id)


def refused(host, live, plan, match="", *, ctx=None):
    """The edit is refused by the retained checks, and nothing was queued, written or published."""
    before = host.fingerprint()
    result = host.call(ctx or live.context(), "context_edit", {"edit": plan})
    assert result.status == "error" and match in result.error["message"], (result.status, result.error)
    assert host.inputs() == [] and host.bindings() == [] and host.fingerprint() == before and receipts(host, live) == []
    return result.error["code"]


# -- the real allowed path --------------------------------------------------------------------------------------------------

def test_a_live_assignment_with_empty_record_ids_queues_the_exact_pair_and_publishes_one_event_with_its_delegation(host):
    graph(host)
    live = delegate(host)
    assignment = live.run(lambda uow: live.store.get(uow, live.assignment_id))
    assert assignment["scope_json"]["record_ids"] == {} and "context_edit" in assignment["allowed_operations_json"]
    plan, before = host.plan(put_node("aaaaa", "Alice Smith"), put_node("n1111", "New person"), put_edge("n2222", "n1111", "aaaaa")), host.fingerprint()

    result = live.edit(plan, key="worker-edit-1")
    assert result.status == "ok", result.error
    queued = plain(result.data)
    assert host.fingerprint() == before and host.context.pending() is None          # queued, not published
    [row], [binding] = host.inputs(), host.bindings()
    assert row["id"] == binding["input_id"] == queued["input"]["id"] and row["state"] == "queued" and row["submitted_by"] == live.parent
    assert binding["attribution_json"]["delegation"] == live.triplet and binding["attribution_json"]["principal_id"] == live.parent
    assert set(binding["attribution_json"]["delegation"]) == {"binding_id", "assignment_id", "generation"}
    # The admitted invocation keeps its committed-action receipt, and an exact replay is the same result with no second row.
    [receipt] = receipts(host, live)
    assert (receipt["operation_name"], receipt["action_key"], receipt["state"], receipt["execution_generation"],
            receipt["delegation_binding_id"], receipt["parent_principal_id"]) == (
        "context_edit", "worker-edit-1", "applied", live.generation, live.binding["id"], live.parent)
    replay = live.edit(plan, key="worker-edit-1")
    assert replay.status == "ok" and plain(replay.data) == queued
    assert len(host.inputs()) == len(host.bindings()) == len(receipts(host, live)) == 1

    published = host.step()
    assert published["status"] == "published" and published["context_event_id"] == queued["execution"]["reserved_event_id"]
    event = host.context.snapshot().events[published["context_event_id"]]
    assert event["submitted_by"] == live.parent and event["record_context"]["scope"] == host.scope("first")
    attribution = event["record_context"]["provenance"]["attribution"]
    assert attribution["delegation"] == live.triplet and attribution["operation"] == "context_edit"
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith" and host.events().count(published["context_event_id"]) == 1
    assert host.step() == {"status": "idle"}


def test_an_unrelated_ordinary_operation_keeps_the_assignment_record_boundary(host):
    graph(host)
    live = delegate(host, allowed=("context_edit", "tasks_create"))
    result = host.call(live.context(), "tasks_create", {"title": "A task"})
    assert result.status == "error" and result.error["code"] == "forbidden" and "record boundary" in result.error["message"]
    assert host.count("tasks") == 0 and receipts(host, live) == []
    # The same assignment still lets its worker edit: only the complete context_edit group is exempt, nothing else.
    assert live.edit(host.plan(put_node("aaaaa", "Alice Smith"))).status == "ok"
    assert [receipt["operation_name"] for receipt in receipts(host, live)] == ["context_edit"]


# -- every retained delegated and parent check still refuses ----------------------------------------------------------------

def test_every_retained_delegated_check_still_refuses_an_edit_before_anything_is_queued(host):
    graph(host)
    plan = host.plan(put_node("aaaaa", "Alice Smith"))

    live = delegate(host)
    # A substituted or advanced identity is not the authenticated worker.
    for changes in (dict(delegation_generation=live.generation + 1), dict(delegation_binding_id=new_id()),
                    dict(delegation_assignment_id=new_id())):
        assert refused(host, live, plan, "mismatched", ctx=replace(live.context(), **changes)) == "forbidden"
    other = delegate(host)                                      # a second, valid assignment of the same parent
    assert refused(host, live, plan, "mismatched", ctx=replace(live.context(), delegation_binding_id=other.binding["id"])) == "forbidden"

    # The assignment does not allow the operation.
    assert refused(host, live, plan, "outside the approved assignment", ctx=delegate(host, allowed=("tasks_get",)).context()) == "forbidden"
    # The assignment's exact business, for the admission and for every object the edit changes.
    assert refused(host, live, plan, "exact project scope", ctx=live.context("second")) == "forbidden"
    assert refused(host, live, host.plan(put_node("bbbbb", "Bob Jones")), "exact project scope") == "forbidden"     # second's object
    assert refused(host, live, host.plan(put_node("ccccc", "Global fact edited")), "exact project scope") == "forbidden"   # Global's
    assert refused(host, live, host.plan(put_edge("n3333", "aaaaa", "bbbbb")), "exact project scope") == "forbidden"      # foreign endpoint

    # The lease, the binding and the registration each end the worker's authority.
    expired = delegate(host)
    expired.expire_lease()
    assert refused(host, expired, plan, "expired, revoked, or stale", ctx=expired.context()) == "unauthenticated"
    revoked = delegate(host)
    revoked.revoke_binding()
    assert refused(host, revoked, plan, "mismatched", ctx=revoked.context()) == "forbidden"
    stale = delegate(host)
    host.set_overrides(stale.parent, {"tasks_get": "deny"})        # the parent's profile moves on; the registration did not follow
    assert refused(host, stale, plan, "expired, revoked, or stale", ctx=stale.context()) == "unauthenticated"
    superseded = delegate(host)
    superseded.reclaim()                                           # a newer generation: the original triplet is fenced
    assert refused(host, superseded, plan, "mismatched", ctx=superseded.context()) == "forbidden"

    # The parent's own current permission is still required: the assignment never grants more than the parent holds.
    denied = delegate(host)
    host.set_overrides(denied.parent, {"context_edit": "deny"})
    denied.reregister()
    assert refused(host, denied, plan, ctx=denied.context()) == "policy_denied"


# -- the prepared edit belongs to the delegation that prepared it ----------------------------------------------------------

def prepare(host, ctx, plan):
    with host.database.read() as uow:
        return host.edits.prepare(ctx, {"edit": plan}, InvocationOptions(idempotency_key=new_id()), uow, host.clock)


def apply(host, ctx, prepared):
    with host.database.transaction() as uow:
        return host.edits.apply(ctx, prepared, uow)


def test_apply_requires_the_exact_delegation_that_prepared_the_edit_and_writes_neither_row_otherwise(host):
    graph(host)
    live, plan = delegate(host), host.plan(put_node("aaaaa", "Alice Smith"))
    delegated, direct = live.context(), host.as_agent(live.parent, "first")
    second = delegate(host)
    prepared = prepare(host, delegated, plan)
    attempts = {
        "another assignment": second.context(),
        "another generation": replace(delegated, delegation_generation=live.generation + 1),
        "another binding": replace(delegated, delegation_binding_id=second.binding["id"]),
        "the direct parent": direct,
    }
    for name, ctx in attempts.items():
        with pytest.raises(DomainError) as error:
            apply(host, ctx, prepared)
        assert error.value.code == "forbidden", name
        assert host.inputs() == [] and host.bindings() == [], name
    # A direct caller's prepared edit cannot be applied by a delegated worker either.
    prepared_direct = prepare(host, direct, plan)
    with pytest.raises(DomainError) as error:
        apply(host, delegated, prepared_direct)
    assert error.value.code == "forbidden" and host.inputs() == [] and host.bindings() == []
    # The exact caller applies it, with one row each.
    result = apply(host, delegated, prepared)
    assert result["input"]["state"] == "queued" and len(host.inputs()) == len(host.bindings()) == 1
    assert host.bindings()[0]["attribution_json"]["delegation"] == live.triplet


def test_apply_accepts_exactly_the_binding_and_input_pair_and_nothing_riding_along(host):
    graph(host)
    agent_id, agent = ordubashy(host)
    prepared = prepare(host, agent, host.plan(put_node("aaaaa", "Alice Smith")))
    companion, queued = prepared.changes
    extra = replace(queued, resource=replace(queued.resource, resource_id=new_id()))
    tampered = {
        "extra effect": replace(prepared, changes=prepared.changes + (extra,)),
        "missing effect": replace(prepared, changes=(companion,)),
        "reordered": replace(prepared, changes=(queued, companion)),
        "duplicated": replace(prepared, changes=(companion, companion)),
        "an update": replace(prepared, changes=(companion, replace(queued, action="update"))),
        "a delete": replace(prepared, changes=(replace(companion, action="delete"), queued)),
        "a file effect": replace(prepared, changes=(companion, replace(queued, kind="file"))),
        "a job effect": replace(prepared, changes=(companion, replace(queued, kind="job"))),
        "a differently named effect": replace(prepared, changes=(companion, replace(queued, operation_name="tasks_create"))),
        "another table": replace(prepared, changes=(companion, replace(queued, resource=replace(queued.resource, resource_type="tasks")))),
        "different identities": replace(prepared, changes=(companion, extra)),
        "another project": replace(prepared, changes=(companion, replace(queued, resource=replace(queued.resource, business_id=host.ids["second"])))),
        "a namespaced resource": replace(prepared, changes=(companion, replace(queued, resource=replace(queued.resource,
                                                                                                    namespace="business:" + host.ids["first"])))),
        "an extra target": replace(prepared, expected_targets=prepared.expected_targets + (ExpectedTarget(extra.resource, exists=False),)),
        "a missing target": replace(prepared, expected_targets=prepared.expected_targets[:1]),
        "an existing target": replace(prepared, expected_targets=(ExpectedTarget(companion.resource, exists=True),
                                                                   prepared.expected_targets[1])),
    }
    for name, plan in tampered.items():
        with pytest.raises(DomainError) as error:
            apply(host, agent, plan)
        assert error.value.code == "invalid_plan", name
        assert host.inputs() == [] and host.bindings() == [], name
    values = {name: plain(change.values) for name, change in (("companion", companion), ("queued", queued))}
    values["queued"]["id"] = new_id()
    with pytest.raises(DomainError) as error:                                                 # values that name another identity
        apply(host, agent, replace(prepared, changes=(companion, replace(queued, values=values["queued"]))))
    assert error.value.code == "invalid_plan" and host.inputs() == [] and host.bindings() == []
    assert apply(host, agent, prepared)["input"]["state"] == "queued" and len(host.bindings()) == 1


# -- expiry and recovery -----------------------------------------------------------------------------------------------------

def test_expiry_between_admission_and_dispatch_blocks_the_head_and_a_retry_cannot_adopt_a_fresh_generation(host):
    graph(host)
    live = delegate(host)
    queued = plain(live.edit(host.plan(put_node("aaaaa", "Alice Smith"))).data)
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
    before, stored = host.fingerprint(), host.bindings()

    live.expire_lease()
    blocked = host.step()
    assert blocked["status"] == "input_blocked" and head_error(blocked) == "authority_revoked"
    assert "expired, revoked, or stale" in blocked["error"]["message"]
    assert host.fingerprint() == before and host.input(input_id)["state"] == "blocked" and host.context.pending() is None
    assert host.step()["status"] == "blocked"
    assert head_error(retry_step(host, input_id)) == "authority_revoked" and host.fingerprint() == before

    # The assignment gets a fresh generation, lease and binding. The retry still holds the ORIGINAL triplet and is refused:
    # neither the parent's own authority nor the newer binding is substituted for it.
    claimed, fresh = live.reclaim()
    assert claimed["execution_generation"] > live.generation and fresh["id"] != live.binding["id"]
    assert head_error(retry_step(host, input_id)) == "authority_revoked" and host.fingerprint() == before
    assert host.bindings() == stored and host.bindings()[0]["attribution_json"]["delegation"] == live.triplet
    assert event_id not in host.events() and host.input(input_id)["state"] == "blocked"
    # The caller submits a new request when the original assignment can no longer execute; discard clears the head.
    host.data(host.human("first"), "context_queue_discard", {"input_id": input_id, "expected_state": "blocked"})
    discarded = host.step()
    assert discarded["status"] == "discarded" and discarded["context"] == "no_event" and host.step() == {"status": "idle"}
    assert event_id not in host.events() and host.input(input_id)["state"] == "discarded"


def retry_step(host, input_id):
    retry(host, host.human("first"), input_id)
    return host.step()


def test_restored_permission_allows_only_the_same_plan_while_the_same_binding_is_valid(host):
    graph(host)
    live = delegate(host)
    queued = plain(live.edit(host.plan(put_node("aaaaa", "Alice Smith"))).data)
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
    stored, before = host.bindings(), host.fingerprint()

    host.set_overrides(live.parent, {"context_edit": "deny"})
    live.reregister()
    assert head_error(host.step()) == "authority_revoked" and host.fingerprint() == before     # the parent's denial blocks the worker
    assert head_error(retry_step(host, input_id)) == "authority_revoked"
    host.set_overrides(live.parent, {})
    live.reregister()
    published = retry_step(host, input_id)
    assert published["status"] == "published" and published["context_event_id"] == event_id
    assert host.bindings() == stored and host.events().count(event_id) == 1
    event = host.context.snapshot().events[event_id]
    assert event["record_context"]["provenance"]["attribution"]["delegation"] == live.triplet
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith"


def stop_after_publication(host, monkeypatch):
    def stop(phase):
        if phase == "after_publication":
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()


def test_an_exact_published_edit_is_reconciled_after_its_delegation_expired_with_or_without_the_port(host, monkeypatch):
    graph(host)
    for index, wired in enumerate((True, False)):
        live = delegate(host)
        queued = plain(live.edit(host.plan(put_node("aaaaa", f"Alice {index}"))).data)
        input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
        stop_after_publication(host, monkeypatch)
        assert event_id in host.events() and host.input(input_id)["state"] == "processing"       # published, not yet recorded
        live.expire_lease()
        live.revoke_binding()                                                                     # and the delegation is gone
        host.restart_driver(wired=wired)
        recovered = host.driver.recover()
        assert recovered["status"] == "published" and recovered["recovered"] is True and recovered["context_event_id"] == event_id
        assert host.input(input_id)["state"] == "published" and host.events().count(event_id) == 1
        assert host.context.snapshot().nodes["aaaaa"]["summary"] == f"Alice {index}"
        assert host.driver.recover() == {"status": "idle"} and host.step() == {"status": "idle"}
        host.restart_driver()
