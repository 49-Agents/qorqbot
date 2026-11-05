"""Explicit Context graph edits, end to end on a synthetic composed host (FEATURE_COMPLETION D2).

Real: the host runtime with its resolver, policy and application, the actual ``context_edit`` operation and ``GraphEdits``, the
Context integration's queue with the ``context_graph_edits`` binding, its ``ContextQueueDriver`` with its typed
``GraphEditAuthority`` port, and a disposable real Context (real Git, the accepted library's own ``Pipeline``). Faked: only the
model runner, which FAILS the test if the deterministic edit path ever calls it, the injected clock and the synthetic principals,
roles and files under a pytest ``tmp_path`` (see ``context_edit_support``). No model, provider, network or operational data.

Every controlled case asserts that no model started and no published event carries a model attempt or a usage receipt."""

import hashlib
import json
from dataclasses import replace

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.graph_edits")

from qorqut_context import Context, Pipeline
from qorqut_context.integration import graph_edits as context_graph_edits
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain, utc_now

from bazarlyk.shared_context.service import coordinator_of
from qorqut_context.integration.driver import ContextQueueDriver
from context_completion_support import Runner
from context_edit_support import Interrupted, NoModel, build, put_edge, put_node, remove_edge, remove_node
from host_foundation_support import SUPPORTED_GLOBAL_ROLES, agent_context, create_worker, historical_business_agent


@pytest.fixture
def host(tmp_path):
    built = build(tmp_path)
    built.models = False
    yield built
    if not built.models:
        built.no_model()


def graph(host):
    """Published disposable objects with every kind of first-creation scope."""
    host.seed([("aaaaa", "Alice"), ("a2222", "Annex")], [("e1111", "aaaaa", "a2222")], scope=host.scope("first"), event_id="s0001")
    host.seed([("bbbbb", "Bob")], scope=host.scope("second"), event_id="s0002")
    host.seed([("ccccc", "Global fact")], scope={"kind": "global"}, event_id="s0003")
    host.seed([("ddddd", "Unrecorded")], event_id="s0004")                                    # no record context at all
    host.seed([("eeeee", "Unresolvable")], scope={"kind": "business", "business_id": "gone"}, event_id="s0005")
    host.seed(edges=[("e3333", "aaaaa", "bbbbb")], scope=host.scope("first"), event_id="s0006")  # first links its node to second's


def digest_of(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def settled(host):
    """Step until the queue stops making progress; every result."""
    results = []
    for _ in range(8):
        results.append(host.step())
        if results[-1]["status"] not in ("published", "discarded"):
            break
    return results


def retry(host, ctx, input_id, state="blocked"):
    return host.data(ctx, "context_queue_retry", {"input_id": input_id, "expected_state": state})["result"]


def discard(host, ctx, input_id, state="blocked"):
    return host.data(ctx, "context_queue_discard", {"input_id": input_id, "expected_state": state})["result"]


def head_error(result):
    return result["error"]["code"]


def ordubashy(host, business="first"):
    """The registered Ordubashy selecting ``business``: the ordinary editing agent of these cases (businesses have no agents)."""
    agent_id = host.agent_id("ordubashy")
    return agent_id, host.as_agent(agent_id, business)


def archive(host, business):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), host.ids[business]))


# -- the owner and an allowed global agent --------------------------------------------------------------------------------

def test_the_owner_queues_one_immutable_input_and_a_driver_step_publishes_exactly_its_plan(host):
    graph(host)
    before = host.fingerprint()
    plan = host.plan(put_node("aaaaa", "Alice Smith"), put_node("n1111", "New person"), put_edge("n2222", "n1111", "aaaaa"))
    queued = host.queued(host.human("first"), plan)

    # Preparation and admission change nothing in Context: no file, event, draft or pending job.
    assert host.fingerprint() == before and host.context.pending() is None
    execution, item = queued["execution"], queued["input"]
    assert item["state"] == "queued" and item["execution"] == execution and item["scope"] == host.scope("first")
    assert execution["kind"] == "deterministic_graph_edit" and execution["expected_revision"] == plan["expected_revision"]
    [row] = [row for row in host.inputs() if row["id"] == item["id"]]
    [binding] = host.bindings()
    payload = row["payload_json"]
    assert row["input_kind"] == "general" and row["state"] == "queued" and row["submitted_by"] == host.owner
    assert payload["path"] == f"foundation/edits/{execution['reserved_event_id']}.txt" == f"foundation/edits/{binding['reserved_event_id']}.txt"
    assert payload["metadata"] == {"kind": "graph_edit", "title": "Explicit graph edit", "origin": "controlled_caller"}
    assert digest_of(payload["content"]) == execution["plan_digest"] == binding["plan_digest"]
    assert json.loads(payload["content"]) == binding["plan_json"] and binding["expected_revision"] == plan["expected_revision"]
    assert binding["attribution_json"]["principal_id"] == host.owner and binding["attribution_json"]["principal_kind"] == "human"
    assert binding["attribution_json"]["delegation"] is None                       # a direct caller records no delegation
    assert {(t["kind"], t["id"]): t["ownership"]["state"] for t in binding["targets_json"]} == {
        ("node", "aaaaa"): "recorded", ("node", "n1111"): "new", ("edge", "n2222"): "new"}

    # The queue input is immutable: neither its payload nor its binding can be rewritten.
    for statement, parameters in (("UPDATE context_inputs SET payload_json = json_set(payload_json, '$.content', 'x') WHERE id = ?", (row["id"],)),
                                  ("UPDATE context_graph_edits SET plan_digest = ? WHERE input_id = ?", (digest_of("x"), row["id"])),
                                  ("DELETE FROM context_graph_edits WHERE input_id = ?", (row["id"],))):
        with pytest.raises(DomainError), host.database.transaction() as uow:
            uow.connection.execute(statement, parameters)

    published = host.step()
    assert published["status"] == "published" and published["context_event_id"] == execution["reserved_event_id"]
    event = host.context.snapshot().events[execution["reserved_event_id"]]
    assert event["execution"] == {"kind": "deterministic_graph_edit", "plan_digest": execution["plan_digest"]}
    assert event["base_revision"] == plan["expected_revision"] and event["submitted_by"] == host.owner
    assert event["record_context"]["scope"] == host.scope("first")
    assert event["record_context"]["provenance"]["input_id"] == row["id"]
    assert event["record_context"]["provenance"]["attribution"] == binding["attribution_json"]
    assert event["handoff_note"] == {"disposition": "empty", "text": ""} and published["handoff_note"] == event["handoff_note"]
    assert {(c["kind"], c["id"], c["operation"]) for c in event["changes"]} == {
        ("node", "aaaaa", "edit"), ("node", "n1111", "add"), ("edge", "n2222", "add")}
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith"
    done = host.input(row["id"])
    assert done["state"] == "published" and done["context_event_id"] == execution["reserved_event_id"]
    assert host.read(host.human("first"), "context_queue_get", {"input_id": row["id"]})["input"]["execution"] == execution
    assert host.step() == {"status": "idle"} and host.events().count(execution["reserved_event_id"]) == 1


def test_an_allowed_global_agent_edits_in_its_selected_business_and_a_replay_returns_the_same_input(host):
    graph(host)
    agent_id, agent = ordubashy(host)
    plan = host.plan(put_node("aaaaa", "Alice at work"), put_node("n1111", "Teammate"), put_edge("n2222", "n1111", "a2222"),
                     remove_edge("e1111"))
    first = host.queued(agent, plan, key="replay-1")
    replay = host.queued(agent, plan, key="replay-1")
    assert replay == first and len(host.bindings()) == 1 and len(host.inputs()) == 1

    # Another request for an unchanged plan is a different edit with its own reserved ID, never the same event twice.
    assert host.step()["status"] == "published"
    assert host.step() == {"status": "idle"}
    [event_id] = [item for item in host.events() if item == first["execution"]["reserved_event_id"]]
    event = host.context.snapshot().events[event_id]
    assert event["submitted_by"] == agent_id and event["record_context"]["scope"] == host.scope("first")
    attribution = event["record_context"]["provenance"]["attribution"]
    assert attribution["principal_id"] == agent_id and attribution["principal_kind"] == "agent"
    assert attribution["role_id"] == "ordubashy" and attribution["operation"] == "context_edit" and attribution["delegation"] is None
    assert "e1111" not in host.context.snapshot().edges and "n2222" in host.context.snapshot().edges
    assert host.events().count(event_id) == 1


def test_preparation_never_begins_a_context_edit_or_stages_a_draft_and_replay_never_adds_one(host, monkeypatch):
    graph(host)

    def forbidden(*args, **kwargs):
        raise AssertionError("preparation and admission must not touch the Context writer")

    for target, name in ((Context, "begin_edit"), (Pipeline, "submit_edit"), (Context, "publish"), (Context, "stage_handoff_note")):
        monkeypatch.setattr(target, name, forbidden)
    before = host.fingerprint()
    plan = host.plan(put_node("aaaaa", "Alice Smith"))
    host.queued(host.human("first"), plan, key="same")
    host.queued(host.human("first"), plan, key="same")
    assert host.fingerprint() == before and host.context.pending() is None and len(host.inputs()) == 1


# -- who may queue what ---------------------------------------------------------------------------------------------------

def test_a_global_without_the_grant_a_historical_business_identity_and_a_non_owner_human_are_refused(host):
    graph(host)
    plan = host.plan(put_node("aaaaa", "Alice Smith"))
    # Every supported global role holds the edit by default; the owner's explicit deny removes it in a business and in Global.
    colleague = create_worker(host.instance, "colleague")
    host.set_overrides(colleague, {"context_edit": "deny"})
    assert host.refused(host.as_agent(colleague, "first"), plan) == "policy_denied"
    assert host.refused(host.as_agent(colleague, None), plan) == "policy_denied"
    # A historical business-home identity never edits, whatever role or grant it stored.
    historical = historical_business_agent(host.instance, host.ids["first"])
    assert host.refused(agent_context(historical, host.ids["first"]), plan) == "inactive_agent"
    stranger = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Member"})
        uow.repository("business_memberships", business_id=host.ids["first"]).insert({"principal_id": stranger, "is_owner": False})
    member = InvocationContext(Principal(stranger, "human"), business_id=host.ids["first"],
                               allowed_business_ids=frozenset({host.ids["first"]}), source="dashboard")
    assert host.refused(member, plan) == "policy_denied"
    # The operation takes no Signoff: a policy that would stage one cannot even be configured for it.
    agent_id, agent = ordubashy(host)
    assert host.error(host.human("first"), "permissions_set", {"agent_id": agent_id, "overrides": {"context_edit": "signoff"}},
                      expected_revision=host._revision(agent_id)) == "invalid_artifact"
    assert host.queued(agent, plan)["input"]["state"] == "queued" and len(host.inputs()) == 1


def test_the_body_is_exactly_the_closed_plan_and_the_published_base(host):
    graph(host)
    owner, plan = host.human("first"), host.plan(put_node("aaaaa", "Alice Smith"))
    for body in ({"edit": plan, "rationale": "outer rationale"}, {"edit": {**plan, "extra": 1}}, {"edit": {**plan, "format": "other"}},
                 {"edit": {**plan, "operations": []}}, {"edit": {**plan, "rationale": ""}}, {"rationale": "no plan"}, {}):
        assert host.error(owner, "context_edit", body) in ("validation_error", "invalid_input")
    stale = {**plan, "expected_revision": "0" * 40}
    assert host.refused(owner, stale) == "conflict"
    assert host.refused(owner, host.plan(remove_node("zzzzz"))) == "validation_error"
    assert host.refused(owner, host.plan(put_node("aaaaa", "Alice Smith"), put_edge("n2222", "aaaaa", "qqqqq"))) == "validation_error"
    assert host.refused(owner, host.plan(remove_node("a2222"))) == "validation_error"               # e1111 would be left dangling
    assert host.inputs() == [] and host.bindings() == []
    # The same removal that accounts for every incident edge is a valid plan, with no hidden cascade.
    assert host.queued(owner, host.plan(remove_node("a2222"), remove_edge("e1111")))["input"]["state"] == "queued"


def test_the_configured_source_byte_bound_applies_before_anything_is_queued(tmp_path):
    host = build(tmp_path, source_bytes=1200)
    graph(host)
    big = host.plan(*(put_node(f"x{i:04d}", "A summary of a person " * 5) for i in range(6)))
    assert host.refused(host.human("first"), big) == "validation_error"
    assert host.inputs() == [] and host.bindings() == []
    assert host.queued(host.human("first"), host.plan(put_node("aaaaa", "Alice Smith")))["input"]["state"] == "queued"
    host.no_model()


def test_a_reserved_event_id_collision_is_refused_and_a_second_edit_cannot_share_an_id(host):
    graph(host)
    owner = host.human("first")
    host.edits._new_id = lambda: "s0001"                                    # an ID the published history already used
    assert host.refused(owner, host.plan(put_node("aaaaa", "Alice Smith"))) == "conflict"
    host.edits._new_id = lambda: "r1111"
    host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    assert host.refused(owner, host.plan(put_node("a2222", "Annex two"))) == "conflict"               # taken by the queued edit
    assert [row["reserved_event_id"] for row in host.bindings()] == ["r1111"] and len(host.inputs()) == 1


# -- target ownership -----------------------------------------------------------------------------------------------------

def test_a_change_needs_authority_in_the_namespace_that_first_created_each_object(host):
    graph(host)
    _, agent = ordubashy(host)
    # An archived business's objects are out of every agent's reach: the edit needs that business's authority.
    archive(host, "second")
    for plan in (host.plan(put_node("bbbbb", "Changed")), host.plan(remove_node("bbbbb"), remove_edge("e3333"))):
        assert host.refused(agent, plan) == "business_archived"
    assert host.inputs() == []
    # Global-owned and unknown-owned objects need the Global namespace, which every registered global holds.
    for obj in ("aaaaa", "ccccc", "ddddd", "eeeee"):              # own, Global, no record, unresolvable record
        assert host.queued(agent, host.plan(put_node(obj, "Changed")))["input"]["state"] == "queued", obj
    owners = {target["id"]: target["ownership"] for binding in host.bindings() for target in binding["targets_json"]}
    assert owners["aaaaa"]["scope"] == host.scope("first") and owners["ccccc"]["scope"] == {"kind": "global"}
    assert owners["ddddd"]["state"] == owners["eeeee"]["state"] == "unknown"


def test_old_and_new_edge_endpoints_and_newly_created_endpoints_are_checked(host):
    graph(host)
    _, agent = ordubashy(host)
    archive(host, "second")                                   # second's objects now need an authority no agent holds
    # e3333 is first's own edge, but its old endpoint bbbbb is second's: moving it checks the old endpoint too.
    assert host.refused(agent, host.plan(put_edge("e3333", "aaaaa", "a2222"))) == "business_archived"
    assert host.refused(agent, host.plan(remove_edge("e3333"))) == "business_archived"
    # A new endpoint of an own edge, a newly created edge and a newly created node linked to second's node.
    assert host.refused(agent, host.plan(put_edge("e1111", "aaaaa", "bbbbb"))) == "business_archived"
    assert host.refused(agent, host.plan(put_edge("n2222", "aaaaa", "bbbbb"))) == "business_archived"
    assert host.refused(agent, host.plan(put_node("n1111", "New"), put_edge("n2222", "n1111", "bbbbb"))) == "business_archived"
    assert host.inputs() == []
    # A node or edge the plan itself creates is the admission business's own, wherever it points inside that business.
    created = host.queued(agent, host.plan(put_node("n1111", "New"), put_edge("n2222", "n1111", "a2222"), put_edge("n3333", "aaaaa", "n1111")))
    [binding] = host.bindings()
    assert {(t["kind"], t["id"], t["ownership"]["state"]) for t in binding["targets_json"]} >= {
        ("node", "n1111", "new"), ("edge", "n2222", "new"), ("edge", "n3333", "new"), ("node", "a2222", "recorded")}
    assert created["execution"]["reserved_event_id"] == binding["reserved_event_id"]


def test_support_added_from_another_business_never_transfers_ownership(host):
    graph(host)
    # Second's event later re-states first's aaaaa, adding second's support to it; first's own history is unchanged.
    host.seed([("aaaaa", "Alice (second's note)")], scope=host.scope("second"), event_id="s0007")
    _, second = ordubashy(host, "second")
    queued = host.queued(second, host.plan(put_node("aaaaa", "Changed from second")))
    # The frozen evidence names first as the owner, so the edit needs first's authority (not second's) at every check.
    [binding] = host.bindings()
    [target] = [item for item in binding["targets_json"] if item["id"] == "aaaaa"]
    assert target["ownership"] == {"state": "recorded", "event_id": "s0001", "scope": host.scope("first")}
    # The owner may change anything.
    assert host.queued(host.human("first"), host.plan(put_node("bbbbb", "Owner edits second's object")))["input"]["state"] == "queued"
    archive(host, "first")
    assert head_error(host.step()) == "authority_revoked" and host.input(queued["input"]["id"])["state"] == "blocked"


def test_a_global_agent_without_a_selected_business_edits_in_the_global_namespace(host):
    graph(host)
    agent_id, _ = ordubashy(host)
    unselected = host.as_agent(agent_id, None)
    queued = host.queued(unselected, host.plan(put_node("ccccc", "Global fact 2")))
    assert queued["input"]["scope"] == {"kind": "global"} and len(host.inputs()) == 1
    # A historical business-home identity has neither a home business nor the Global namespace any more.
    historical = historical_business_agent(host.instance, host.ids["first"])
    assert host.refused(agent_context(historical, None), host.plan(put_node("aaaaa", "Alice Smith"))) == "inactive_agent"
    assert len(host.inputs()) == 1


def test_every_supported_global_role_edits_any_active_business_and_global_and_an_explicit_deny_wins(host):
    graph(host)
    own, foreign, in_global = host.plan(put_node("aaaaa", "Own")), host.plan(put_node("bbbbb", "Foreign")), host.plan(put_node("ccccc", "Global"))
    # Each role's default grant applies in every active business and in Global: no saved business list narrows it.
    for role in SUPPORTED_GLOBAL_ROLES:
        agent = host.agent_id(role)
        assert host.queued(host.as_agent(agent, "first"), foreign)["input"]["state"] == "queued", role
        assert host.queued(host.as_agent(agent, None), in_global)["input"]["state"] == "queued", role
    # The owner's explicit deny wins in every namespace.
    denied = create_worker(host.instance, "denied-worker")
    host.set_overrides(denied, {"context_edit": "deny"})
    assert host.refused(host.as_agent(denied, "first"), own) == "policy_denied"
    assert host.refused(host.as_agent(denied, "second"), foreign) == "policy_denied"
    assert host.refused(host.as_agent(denied, None), in_global) == "policy_denied"
    assert len(host.inputs()) == 2 * len(SUPPORTED_GLOBAL_ROLES)


def test_a_global_worker_edits_only_with_the_ordinary_explicit_grant_and_a_revoked_grant_blocks_it(host):
    graph(host)
    worker = create_worker(host.instance, "context-worker")
    # Eligibility is not a grant: the role has no default Context write.
    assert host.refused(host.as_agent(worker, "first"), host.plan(put_node("aaaaa", "Worker fact"))) == "policy_denied"
    assert host.refused(host.as_agent(worker, None), host.plan(put_node("ccccc", "Global"))) == "policy_denied"
    host.set_overrides(worker, {"context_edit": "allow"})
    queued = host.queued(host.as_agent(worker, "first"), host.plan(put_node("aaaaa", "Worker fact")))
    assert queued["input"]["state"] == "queued" and host.step()["status"] == "published"
    [event_id] = [item for item in host.events() if item == queued["execution"]["reserved_event_id"]]
    attribution = host.context.snapshot().events[event_id]["record_context"]["provenance"]["attribution"]
    assert attribution["principal_id"] == worker and attribution["role_id"] == "global-worker"
    # Derived reach covers any active business; a grant revoked before execution blocks the queued edit.
    assert host.queued(host.as_agent(worker, "second"), host.plan(put_node("bbbbb", "Worker foreign fact")))["input"]["state"] == "queued"
    host.set_overrides(worker, {"context_edit": "deny"})
    blocked = host.step()
    assert blocked["status"] == "input_blocked" and head_error(blocked) == "authority_revoked"
    assert host.context.snapshot().nodes["bbbbb"]["summary"] != "Worker foreign fact"
    assert host.refused(host.as_agent(worker, "first"), host.plan(put_node("aaaaa", "Again"))) == "policy_denied"


def test_a_global_worker_instructs_the_context_only_with_the_ordinary_explicit_grant(host):
    worker = create_worker(host.instance, "instruct-worker")
    ctx = host.as_agent(worker, "first")
    arguments = {"text": "Alice leads the pilot.", "people": ["Alice"], "rationale": "The owner said so."}
    denied = host.call(ctx, "context_instruct", arguments)
    assert denied.status == "error" and denied.error["code"] == "policy_denied"
    host.set_overrides(worker, {"context_instruct": "allow"})
    assert host.data(ctx, "context_instruct", arguments)                                       # role gate admits, filing queued
    host.set_overrides(worker, {"context_instruct": "deny"})
    assert host.call(ctx, "context_instruct", arguments).error["code"] == "policy_denied"


# -- authority at dispatch ------------------------------------------------------------------------------------------------

def test_a_revoked_grant_blocks_the_head_and_a_retry_publishes_only_the_same_plan_once_restored(host):
    graph(host)
    agent_id, agent = ordubashy(host)
    plan = host.plan(put_node("aaaaa", "Alice Smith"))
    queued = host.queued(agent, plan)
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
    before, binding = host.fingerprint(), host.bindings()
    host.set_overrides(agent_id, {"context_edit": "deny"})

    blocked = host.step()
    assert blocked["status"] == "input_blocked" and head_error(blocked) == "authority_revoked"
    assert host.fingerprint() == before and host.input(input_id)["state"] == "blocked" and host.context.pending() is None
    assert host.step()["status"] == "blocked"                           # nothing is retried by itself
    # A retry only rechecks the restored grants; it cannot alter the plan, the base or the IDs.
    retry(host, host.human("first"), input_id)
    assert head_error(host.step()) == "authority_revoked" and host.fingerprint() == before
    assert host.bindings() == binding
    host.set_overrides(agent_id, {})
    retry(host, host.human("first"), input_id)
    published = host.step()
    assert published["status"] == "published" and published["context_event_id"] == event_id
    assert host.bindings() == binding and host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith"
    assert host.events().count(event_id) == 1


def test_a_deactivated_registration_and_a_changed_owner_block_the_head(host):
    graph(host)
    agent_id = host.agent_id("learning-applier")
    queued = host.queued(host.as_agent(agent_id, "first"), host.plan(put_node("aaaaa", "By the agent")))
    host.deactivate(agent_id)
    before = host.fingerprint()
    assert head_error(host.step()) == "authority_revoked" and host.fingerprint() == before
    discard(host, host.human("first"), queued["input"]["id"])
    assert host.step()["status"] == "discarded" and host.fingerprint() == before

    queued = host.queued(host.human("first"), host.plan(put_node("aaaaa", "By the owner")))
    successor = new_id()
    with host.database.transaction() as uow:                         # the installation owner changed since the edit was queued
        uow.repository("principals").insert({"id": successor, "kind": "human", "display_name": "Successor"})
        uow.connection.execute("UPDATE installation SET owner_principal_id=? WHERE id='service'", (successor,))
    assert head_error(host.step()) == "authority_revoked" and host.fingerprint() == before
    assert host.input(queued["input"]["id"])["state"] == "blocked"


def test_a_new_publication_ahead_makes_the_fixed_base_stale_and_later_inputs_cannot_bypass_it(host):
    graph(host)
    owner = host.human("first")
    stale = host.queued(owner, host.plan(put_node("aaaaa", "Planned against the old head")))
    host.seed([("zzzzz", "Published meanwhile")], scope=host.scope("first"), event_id="s0008")      # the head moves ahead
    later = host.queued(owner, host.plan(put_node("a2222", "Planned against the new head")))
    assert later["execution"]["expected_revision"] != stale["execution"]["expected_revision"]
    before = host.fingerprint()

    blocked = host.step()
    assert blocked["status"] == "input_blocked" and head_error(blocked) == "stale_base"
    assert host.step()["status"] == "blocked" and host.fingerprint() == before                       # the later input waits behind it
    assert host.input(later["input"]["id"])["state"] == "queued"
    retry(host, owner, stale["input"]["id"])
    assert head_error(host.step()) == "stale_base" and host.fingerprint() == before                  # never rebased
    discard(host, owner, stale["input"]["id"])
    results = settled(host)
    assert [result["status"] for result in results] == ["discarded", "published", "idle"]
    assert host.context.snapshot().nodes["a2222"]["summary"] == "Planned against the new head"
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice"


def test_two_edits_on_the_same_head_publish_one_and_block_the_other_as_stale(host):
    graph(host)
    owner = host.human("first")
    one = host.queued(owner, host.plan(put_node("aaaaa", "First plan")))
    two = host.queued(owner, host.plan(put_node("a2222", "Second plan")))
    assert one["execution"]["expected_revision"] == two["execution"]["expected_revision"]
    assert host.step()["status"] == "published"
    assert head_error(host.step()) == "stale_base"
    assert host.context.snapshot().nodes["a2222"]["summary"] == "Annex"


def test_missing_authority_wiring_refuses_the_edit_and_ordinary_inputs_are_unaffected(host):
    graph(host)
    queued = host.queued(host.human("first"), host.plan(put_node("aaaaa", "Alice Smith")))
    host.restart_driver(wired=False)
    before = host.fingerprint()
    assert head_error(host.step()) == "authority_unwired" and host.fingerprint() == before
    assert host.input(queued["input"]["id"])["state"] == "blocked"
    # An ordinary input is not subject to the port: once the unwired edit is out of the way, it publishes with its model run.
    discard(host, host.human("first"), queued["input"]["id"])
    assert host.step()["status"] == "discarded"
    host.models, runner = True, Runner()
    host.pipeline = Pipeline(host.context, runner=runner)
    host.driver = ContextQueueDriver(host.database, host.context, host.pipeline, driver_id=host.driver_id, outcomes=host.outcomes,
                                     programs=host.runtime.programs, coordinator=coordinator_of)
    host.data(host.human("first"), "context_queue_submit", {"operation": "add", "path": "foundation/notes/plain.txt", "content": "Plain",
                                                            "metadata": {"kind": "note", "title": "Plain", "origin": "user"}})
    assert host.step()["status"] == "published" and runner.propagations == 1


# -- crash, retry and discard ---------------------------------------------------------------------------------------------

def test_an_interrupted_partial_staging_is_completed_by_an_explicit_retry_into_one_exact_event(host, monkeypatch):
    graph(host)
    agent_id, agent = ordubashy(host)
    plan = host.plan(put_node("n1111", "New one"), put_node("n2222", "New two"), put_edge("n3333", "n1111", "n2222"))
    queued = host.queued(agent, plan)
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
    staged = []

    def stop(phase):
        staged.append(phase)
        if len(staged) == 2:
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()
    assert host.context.pending()["id"] == event_id and event_id not in host.events()      # a partial draft, nothing published
    assert host.input(input_id)["state"] == "processing"

    host.restart_driver()                                                                  # a new process; revoke before it recovers
    host.set_overrides(agent_id, {"context_edit": "deny"})
    recovered = host.driver.recover()
    assert recovered["status"] == "input_blocked" and recovered["error"]["code"] == "interrupted"
    retry(host, host.human("first"), input_id)
    assert head_error(host.step()) == "authority_revoked" and event_id not in host.events()   # a retry rechecks first
    host.set_overrides(agent_id, {})
    retry(host, host.human("first"), input_id)
    published = host.step()
    assert published["status"] == "published" and published["context_event_id"] == event_id
    assert host.events().count(event_id) == 1 and host.context.pending() is None
    event = host.context.snapshot().events[event_id]
    assert {(c["kind"], c["id"], c["operation"]) for c in event["changes"]} == {
        ("node", "n1111", "add"), ("node", "n2222", "add"), ("edge", "n3333", "add")}
    assert event["execution"]["plan_digest"] == queued["execution"]["plan_digest"] and host.bindings()[0]["reserved_event_id"] == event_id


def test_an_interruption_after_publication_before_recording_is_reconciled_even_if_authority_changed(host, monkeypatch):
    graph(host)
    agent_id, agent = ordubashy(host)
    queued = host.queued(agent, host.plan(put_node("aaaaa", "Alice Smith")))
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]

    def stop(phase):
        if phase == "after_publication":
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()
    assert event_id in host.events() and host.input(input_id)["state"] == "processing"       # published, not yet recorded
    host.set_overrides(agent_id, {"context_edit": "deny"})                                   # permission changed after publication
    host.restart_driver()
    recovered = host.driver.recover()
    assert recovered["status"] == "published" and recovered["recovered"] is True and recovered["context_event_id"] == event_id
    assert host.input(input_id)["state"] == "published" and host.events().count(event_id) == 1
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice Smith"                # the real effect is not erased
    assert host.driver.recover() == {"status": "idle"} and host.step() == {"status": "idle"}


def test_a_published_exact_event_is_reconciled_by_the_step_even_when_a_later_head_check_would_refuse(host, monkeypatch):
    graph(host)
    agent_id, agent = ordubashy(host)
    queued = host.queued(agent, host.plan(put_node("aaaaa", "Alice Smith")))

    def stop(phase):
        if phase == "after_publication":
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()
    host.set_overrides(agent_id, {"context_edit": "deny"})
    host.restart_driver()
    # The base is stale now (the edit itself moved the head), and the caller lost its grant: neither blocks reconciliation.
    result = host.step()
    assert result["status"] == "published" and result["context_event_id"] == queued["execution"]["reserved_event_id"]
    assert host.events().count(queued["execution"]["reserved_event_id"]) == 1


def test_a_retry_of_a_blocked_edit_whose_exact_event_was_published_meanwhile_is_reconciled_even_without_the_authority_port(host, monkeypatch):
    graph(host)
    agent_id, agent = ordubashy(host)
    queued = host.queued(agent, host.plan(put_node("n1111", "New one"), put_node("n2222", "New two")))
    input_id, event_id = queued["input"]["id"], queued["execution"]["reserved_event_id"]
    staged = []

    def stop(phase):
        staged.append(phase)
        if len(staged) == 1:
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()
    host.restart_driver(wired=False)                                       # a restarted process without its authority port
    assert host.driver.recover()["status"] == "input_blocked"
    # While the event is unpublished, a retry without the port is still refused: nothing runs.
    retry(host, host.human("first"), input_id)
    before = host.fingerprint()
    assert head_error(host.step()) == "authority_unwired" and host.fingerprint() == before and event_id not in host.events()
    # The exact event is completed some other way (an operator's Pipeline.retry), then the caller loses its grant. The head base is
    # stale now only because of that very event, the caller is no longer authorized and the port is unavailable: none may hide it.
    assert Pipeline(host.context, runner=NoModel()).retry(event_id)["event_id"] == event_id and host.events().count(event_id) == 1
    host.set_overrides(agent_id, {"context_edit": "deny"})
    retry(host, host.human("first"), input_id)
    reconciled = host.step()
    assert reconciled["status"] == "published" and reconciled["context_event_id"] == event_id
    assert host.input(input_id)["state"] == "published" and host.events().count(event_id) == 1
    assert {"n1111", "n2222"} <= set(host.context.snapshot().nodes) and host.runner.calls == []
    assert host.step() == {"status": "idle"} and host.events().count(event_id) == 1


def test_a_discard_before_dispatch_leaves_no_event_and_retains_the_immutable_binding(host):
    graph(host)
    owner = host.human("first")
    queued = host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    before = host.fingerprint()
    assert discard(host, owner, queued["input"]["id"], "queued")["state"] == "discarded"
    assert host.step() == {"status": "idle"} and host.fingerprint() == before
    assert len(host.bindings()) == 1 and host.input(queued["input"]["id"])["state"] == "discarded"
    shown = host.read(owner, "context_queue_get", {"input_id": queued["input"]["id"]})["input"]
    assert shown["execution"] == queued["execution"] and shown["state"] == "discarded"
    # The reserved ID is never reused by a later edit, discarded or not.
    again = host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    assert again["execution"]["reserved_event_id"] != queued["execution"]["reserved_event_id"]


def test_discarding_a_blocked_partial_edit_closes_its_draft_and_publishes_nothing(host, monkeypatch):
    graph(host)
    owner = host.human("first")
    queued = host.queued(owner, host.plan(put_node("n1111", "New one"), put_node("n2222", "New two")))
    staged = []

    def stop(phase):
        staged.append(phase)
        if len(staged) == 1:
            raise Interrupted

    with monkeypatch.context() as patch:
        patch.setattr(host.pipeline, "_checkpoint", stop)
        with pytest.raises(Interrupted):
            host.step()
    host.restart_driver()
    assert host.driver.recover()["status"] == "input_blocked"
    discard(host, owner, queued["input"]["id"])
    closed = host.step()
    assert closed["status"] == "discarded" and closed["context"] == "draft_discarded"
    assert host.context.pending() is None and queued["execution"]["reserved_event_id"] not in host.events()
    assert "n1111" not in host.context.snapshot().nodes


# -- the reserved path is never public ------------------------------------------------------------------------------------

def test_public_submission_cannot_claim_the_reserved_path_or_its_controlled_metadata(host):
    graph(host)
    owner = host.human("first")
    plan = host.plan(put_node("aaaaa", "Alice Smith"))
    content = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    controlled = {"kind": "graph_edit", "title": "Explicit graph edit", "origin": "controlled_caller"}
    for path in ("foundation/edits/abcde.txt", "foundation/edits/anything.txt", "foundation/edits/"):
        for metadata in (controlled, {"kind": "note", "title": "A note", "origin": "user"}):
            assert host.error(owner, "context_queue_submit", {"operation": "add", "path": path, "content": content,
                                                              "metadata": metadata}) == "validation_error"
    assert host.error(owner, "context_queue_submit", {"operation": "add", "path": "foundation/notes/x.txt", "content": content,
                                                      "metadata": controlled}) == "validation_error"
    assert host.inputs() == [] and host.bindings() == []


def test_a_reserved_path_input_without_or_with_a_mismatched_companion_is_never_dispatched(host):
    graph(host)
    owner = host.human("first")
    queued = host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    row = host.input(queued["input"]["id"])
    # The database refuses to store a reserved-path input without its companion, so the corruption is simulated by removing the
    # guard in this disposable database only (never in a real one).
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER context_graph_edits_controlled_inputs")
        uow.connection.execute("DROP TRIGGER context_graph_edits_keep")
        uow.connection.execute("DELETE FROM context_graph_edits WHERE input_id=?", (row["id"],))
    before = host.fingerprint()
    result = host.step()
    assert result["status"] == "input_blocked" and head_error(result) == "graph_edit_unbound" and host.fingerprint() == before
    assert host.error(owner, "context_queue_get", {"input_id": row["id"]}) == "graph_edit_unbound"       # never shown as ordinary


def test_a_companion_that_no_longer_matches_its_input_is_an_integrity_failure_not_an_edit(host):
    graph(host)
    owner = host.human("first")
    queued = host.queued(owner, host.plan(put_node("aaaaa", "Alice Smith")))
    # The database refuses to rewrite a binding; the corruption is simulated by removing that guard in this disposable database.
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER context_graph_edits_immutable")
        uow.connection.execute("UPDATE context_graph_edits SET plan_digest = ? WHERE input_id = ?", (digest_of("another plan"), queued["input"]["id"]))
    before = host.fingerprint()
    result = host.step()
    assert result["status"] == "input_blocked" and head_error(result) == "graph_edit_integrity" and host.fingerprint() == before
    assert host.error(owner, "context_queue_get", {"input_id": queued["input"]["id"]}) == "graph_edit_integrity"


def test_changed_prepared_arguments_a_moved_base_and_revoked_authority_leave_neither_row(host):
    graph(host)
    agent_id, agent = ordubashy(host)
    args = {"edit": host.plan(put_node("aaaaa", "Alice Smith"))}
    options = InvocationOptions(idempotency_key=new_id())

    def prepare():
        with host.database.read() as uow:
            return host.edits.prepare(agent, args, options, uow, host.clock)

    def apply(plan, ctx=agent):
        with host.database.transaction() as uow:
            return host.edits.apply(ctx, plan, uow)

    # The prepared companion no longer carries the plan its digest names.
    plan = prepare()
    changes = []
    for change in plan.changes:
        if change.resource.resource_type == "context_graph_edits":
            tampered = plain(change.values)
            tampered["plan_json"]["rationale"] = "A different rationale"
            change = replace(change, values=tampered)
        changes.append(change)
    with pytest.raises(DomainError) as changed:
        apply(replace(plan, changes=tuple(changes)))
    assert changed.value.code == "revision_conflict"
    # The published base moves between prepare and apply.
    plan = prepare()
    host.seed([("zzzzz", "Published meanwhile")], scope=host.scope("first"), event_id="s0008")
    with pytest.raises(DomainError) as moved:
        apply(plan)
    assert moved.value.code == "conflict"
    # The caller loses authority between prepare and apply.
    args = {"edit": host.plan(put_node("aaaaa", "Alice Smith"))}
    plan = prepare()
    host.set_overrides(agent_id, {"context_edit": "deny"})
    with pytest.raises(DomainError) as revoked:
        apply(plan, host.as_agent(agent_id, "first"))
    assert revoked.value.code == "policy_denied"
    assert host.inputs() == [] and host.bindings() == []


# -- the graph-edit storage is required ------------------------------------------------------------------------------------

def test_a_target_without_the_graph_edit_storage_refuses_before_any_context_writer_or_model_work(tmp_path, monkeypatch):
    host = build(tmp_path, graph_schema=False)                    # a deliberately incomplete custom assembly under the stock composition
    host.models = False
    graph(host)
    owner = host.human("first")
    writer_calls = []
    for target, name in ((Context, "begin"), (Context, "begin_edit"), (Pipeline, "submit"), (Pipeline, "submit_edit"), (Pipeline, "retry")):
        monkeypatch.setattr(target, name, lambda *args, _name=name, **kwargs: writer_calls.append(_name))
    before = host.fingerprint()

    # The composed queue refuses admission, naming the missing contribution: no input is ever queued against it.
    submit = host.call(owner, "context_queue_submit", {"operation": "add", "path": "foundation/notes/plain.txt", "content": "Plain",
                                                       "metadata": {"kind": "note", "title": "Plain", "origin": "user"}})
    assert submit.status == "error" and submit.error["code"] == "composition_error" and "context_graph_edits" in submit.error["message"]
    assert host.inputs() == []
    assert host.step() == {"status": "idle"}                                      # nothing is queued for the driver to start
    # Nothing started: no Context writer or model call, no attempt, no event ID, no block recorded.
    assert writer_calls == [] and host.runner.calls == [] and host.fingerprint() == before
    # An explicit edit cannot even be queued, and nothing is left behind.
    assert host.refused(owner, host.plan(put_node("aaaaa", "Alice Smith"))) == "composition_error"
    assert host.inputs() == []                                                    # the graph-edit binding table does not exist here
    host.no_model()


# -- ordinary inputs ------------------------------------------------------------------------------------------------------

def test_edit_shaped_source_text_is_an_ordinary_input_and_selects_no_deterministic_execution(host):
    graph(host)
    host.models = True
    runner = Runner()
    host.restart_driver(runner)
    plan = host.plan(put_node("aaaaa", "Not an edit"))
    content = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    item = host.data(host.human("first"), "context_queue_submit", {
        "operation": "add", "path": "foundation/notes/lookalike.txt", "content": content,
        "metadata": {"kind": "note", "title": "Looks like a plan", "origin": "user"}})["input"]
    assert item["execution"] is None and host.bindings() == []
    result = host.step()
    assert result["status"] == "published" and runner.propagations == 1                      # the model path ran: it is not an edit
    event = host.context.snapshot().events[result["context_event_id"]]
    assert "execution" not in event and event["source"]["path"] == "foundation/notes/lookalike.txt"
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice"
    # An ordinary upload, even one whose body is a plan, still files through the same queue and driver with a model run.
    upload = host.upload(host.human("first"), content, title="A plan as prose")
    assert host.step()["status"] == "published" and runner.propagations == 2
    filed = host.context.snapshot().events[host.input(host.inputs()[-1]["id"])["context_event_id"]]
    assert "execution" not in filed and filed["input_mode"] == "GENERAL" and filed["source"]["path"].startswith("foundation/documents/")
    assert host.context.snapshot().nodes["aaaaa"]["summary"] == "Alice" and host.bindings() == []


def test_the_exposure_and_signoff_contract_of_the_operation_is_unchanged(host):
    descriptor = host.runtime.registry.get("context_edit")
    assert descriptor.supports_signoff is False and descriptor.effect_kind == "compound"
    assert set(descriptor.input_schema["required"]) == {"edit"} and descriptor.input_schema["additionalProperties"] is False
    assert {"dashboard", "worker"} <= set(descriptor.exposure)
