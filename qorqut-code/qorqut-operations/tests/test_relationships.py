"""The neutral item resolver and the extracted relationship/review operations: real engine, real relationship tables,
small synthetic scoped domains. Nothing external, nothing from a host."""

from dataclasses import FrozenInstanceError
import sqlite3
from types import SimpleNamespace

import pytest

from qorqut_operations.application import OperationDescriptor, OperationRegistry
from qorqut_operations.application.plans import apply_records
from qorqut_operations.contracts import (ChangeSet, DomainError, InvocationContext, PolicyDecision, PreparedChange,
                                         Principal, ResourceRef, new_id, plain)
from qorqut_operations.items import UNAVAILABLE, ItemContribution, ItemResolver, authorize_item_read, item_revision
from qorqut_operations.relationships import RELATIONSHIP_ENTRIES, RelationshipOperations, RelationshipPorts, ReviewEdit
from qorqut_operations.storage import AssemblyError, TableSpec
from qorqut_operations.tasks.scope import task_namespace
from relationship_support import (REVIEW_EDITS, ROLES, STRANGER, WORKFLOW_ROLES, Candidates, RelationshipHost,
                                  RELATIONSHIP_OPERATIONS, file_hash)
from support import AGENT, BUSINESS, HUMAN, OTHER, item_plan


@pytest.fixture
def host(tmp_path):
    return RelationshipHost(tmp_path)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def pair(host, source_title="Source", **kwargs):
    """An item ``t1`` with one related source ``t2`` and the outgoing ``related_to`` edge between them."""
    item = host.topic("t1", "Item", "Item body")
    source = host.topic("t2", source_title, "Source body", **kwargs)
    return item, source, host.edge(item, source, "related_to", "evidence")


def refused_at_approval(host, signoff_id, cause_code=None):
    """Approval re-validates the exact prepared plan: a stale plan is marked conflicted and nothing is applied."""
    result = host.approve(signoff_id)
    assert result.status == "error" and result.error["code"] == "stale_signoff", result.data
    if cause_code:
        assert result.error["details"]["cause"]["code"] == cause_code
    assert host.row(signoff_id)["state"] == "conflicted" and host.service.run_one(signoff_id) is None
    return result.error["details"]["cause"]


def body_edit(identity="t1", body="Edited"):
    return {"operation": "topics_update", "arguments": {"topic_id": identity, "changes": {"body": body}}}


# -- 1. assembly: typed contributions, ports and a frozen, immutable resolver ----------------------------------------------

def test_item_contributions_are_typed_frozen_values_with_complete_lookups(host):
    lookup = host.notes
    good = dict(owner="extra", kinds=("extra_kind",), lookup=lookup)
    ItemContribution(**good)
    for bad in ({"owner": "Bad Owner"}, {"owner": 7}, {"kinds": ["extra_kind"]}, {"kinds": ()}, {"kinds": ("a", "a")},
                {"kinds": ("Bad-Kind",)}, {"kinds": (3,)}, {"lookup": object()}, {"lookup": None}):
        with pytest.raises(AssemblyError):
            ItemContribution(**{**good, **bad})

    class Partial:
        resource = resolve = relationship_item = staticmethod(lambda *args, **kwargs: None)   # no fingerprint
    with pytest.raises(AssemblyError, match="fingerprint"):
        ItemContribution("extra", ("extra_kind",), Partial())
    with pytest.raises(FrozenInstanceError):
        host.items.contributions[0].owner = "other"


def test_item_resolver_refuses_duplicates_untyped_values_and_incomplete_inputs(host):
    notes, tracker = host.items.contributions

    def build(contributions, **kwargs):
        return ItemResolver(host.app, contributions, **{"candidates": Candidates(), **kwargs})

    ItemResolver(host.app, [notes, tracker], candidates=Candidates())
    duplicate_kind = ItemContribution("other", ("topic",), host.tickets)
    duplicate_owner = ItemContribution("notes", ("other_kind",), host.tickets)
    for bad in ([], (), [notes, notes], [notes, duplicate_owner], [notes, duplicate_kind], "notes", {"notes": notes},
                [{"owner": "x", "kinds": ("a",), "lookup": host.notes}], [notes, None]):
        with pytest.raises(AssemblyError):
            build(bad)
    for candidates in (None, object(), type("NoMethod", (), {})()):
        with pytest.raises(AssemblyError, match="candidates"):
            build([notes], candidates=candidates)
    from qorqut_operations.application import Application
    for application in (None, object(), Application(host.database, host.registry)):   # the last has no policy
        with pytest.raises(AssemblyError, match="Application"):
            ItemResolver(application, [notes], candidates=Candidates())


def test_item_resolver_is_immutable_and_derives_its_schema_from_the_ordered_kinds(host):
    assert host.items.kinds == ("topic", "message", "ticket")
    schema = host.items.item_schema
    assert schema["properties"]["type"]["enum"] == ["topic", "message", "ticket"] and schema["required"] == ["type", "id"]
    schema["properties"]["type"]["enum"].append("forged")                    # a copy: the resolver is unchanged
    assert host.items.item_schema["properties"]["type"]["enum"] == ["topic", "message", "ticket"]
    for name in ("kinds", "application", "contributions", "item_schema"):
        with pytest.raises(AttributeError):
            setattr(host.items, name, ())
    with pytest.raises(AttributeError):
        host.items.extra_kind = host.notes
    assert not hasattr(host.items, "register") and not hasattr(host.items, "stories")
    assert host.items.application is host.app
    # The same schema is what the installed operations validate: an unregistered kind is not an item.
    result = host.invoke("context_items_get", {"item": {"type": "forged", "id": "x"}})
    assert code(result) == "validation_error"
    with host.database.read() as uow:
        with pytest.raises(DomainError) as error:
            host.items.resolve(host.owner(), {"type": "forged", "id": "x"}, uow)
    assert error.value.code == "not_found" and "forged" not in error.value.message


def test_relationship_ports_are_mandatory_typed_and_copied(host):
    good = dict(roles=ROLES, workflow_roles=WORKFLOW_ROLES, materials=host.ports.materials, archive=host.ports.archive,
                navigation=host.ports.navigation, review_edits=REVIEW_EDITS)
    RelationshipPorts(**good)
    for name in good:
        with pytest.raises(AssemblyError):
            RelationshipPorts(**{**good, name: None})
    for name in ("materials", "archive", "navigation"):
        with pytest.raises(AssemblyError, match=name):
            RelationshipPorts(**{**good, name: object()})
    with pytest.raises(TypeError):                                    # the setup-only edge guard is not a port any more
        RelationshipPorts(**good, protected_edges=object())
    for bad in ({"roles": {}}, {"roles": {"Bad Role": ("a", "b")}}, {"roles": {"related_to": ("only",)}},
                {"roles": {"related_to": ("a", "")}}, {"workflow_roles": {"unknown_role": "tool"}},
                {"workflow_roles": {"assigned_to": ""}}, {"review_edits": {}},
                {"review_edits": {"topics_update": ("topic", "topic_id")}},
                {"review_edits": {"Bad Name": ReviewEdit("topic", "topic_id")}}):
        with pytest.raises(AssemblyError):
            RelationshipPorts(**{**good, **bad})
    for bad in ({"item_kind": None}, {"item_kind": "Bad"}, {"argument": ""}, {"apply_through_domain": 1},
                {"resource_type": "Bad-Table"}):
        with pytest.raises(AssemblyError):
            ReviewEdit(**{"item_kind": "topic", "argument": "topic_id", **bad})
    roles = dict(ROLES)
    ports = RelationshipPorts(**{**good, "roles": roles})
    roles["forged"] = ("Forged", "Forged inverse")
    assert "forged" not in ports.roles and list(ports.roles) == list(ROLES)
    with pytest.raises(TypeError):
        ports.roles["forged"] = ("a", "b")
    with pytest.raises(FrozenInstanceError):
        ports.roles = {}
    with pytest.raises(AssemblyError, match="RelationshipPorts"):
        RelationshipOperations(host.items, object())
    with pytest.raises(AssemblyError, match="ItemResolver"):
        RelationshipOperations(object(), ports)
    unknown = RelationshipPorts(**{**good, "review_edits": {"x_update": ReviewEdit("unregistered_kind", "x_id")}})
    with pytest.raises(AssemblyError, match="unregistered_kind"):
        RelationshipOperations(host.items, unknown)
    with pytest.raises(AssemblyError, match="clock"):
        RelationshipOperations(host.items, ports, clock="now")


def test_the_catalog_contribution_declares_the_v1_authority_metadata(host):
    contribution = host.relationships.contribution()
    assert {entry.name for entry in contribution.entries} == set(RELATIONSHIP_OPERATIONS) == {
        descriptor.name for descriptor in contribution.descriptors} == {entry.name for entry in RELATIONSHIP_ENTRIES}
    assert host.registry.frozen
    for name in RELATIONSHIP_OPERATIONS:
        meta = host.registry.metadata(name)
        assert meta.exposure == {"mcp", "dashboard", "worker"} and not meta.owner_only
        assert meta.supports_signoff == (meta.effect_kind != "read")
        assert meta.effect_kind in {"read", "records"}
    assert host.registry.metadata("context_reviews_list").effect_kind == "read"
    assert host.registry.metadata("context_review").effect_kind == "records"
    schema = host.registry.schema_for("relationships_create")
    assert schema["properties"]["source"]["properties"]["type"]["enum"] == ["topic", "message", "ticket"]
    assert schema["required"] == ["source", "target", "role"] and schema["additionalProperties"] is False
    assert host.registry.schema_for("materials_update")["properties"]["changes"]["properties"]["material_type"]["enum"] == [
        "document", "note", "mixed"]
    assert host.registry.schema_for("context_review")["properties"]["edit"]["properties"]["operation"]["enum"] == list(REVIEW_EDITS)


# -- 2. the shared read-authorization helper -----------------------------------------------------------------------------

def test_item_read_authorization_selects_the_descriptor_policy_by_scope():
    seen, mode = [], ["allow"]

    class Policy:
        def resolve(self, ctx, descriptor, targets, uow):
            seen.append(descriptor)
            return PolicyDecision(mode[0], "test")

    registry = OperationRegistry()
    shape = {"type": "object"}
    read = dict(effect_kind="read", supports_signoff=False, read=lambda *args: {}, scope_kinds=frozenset({"business", "global"}))
    registry.register(OperationDescriptor("things_get", shape, global_capability="read_global_context", **read))
    registry.register(OperationDescriptor("account_get", shape, global_capability="manage_account",
                                          plugin_requirement="account-guidance", **read))
    registry.register(OperationDescriptor("local_get", shape, effect_kind="read", supports_signoff=False, read=lambda *args: {}))
    application = SimpleNamespace(registry=registry, policy=Policy())
    business = InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
    account = InvocationContext(Principal(HUMAN, "human"), business_id=None, source="dashboard")

    authorize_item_read(application, business, "things_get", None)
    authorize_item_read(application, account, "things_get", None)
    authorize_item_read(application, business, "account_get", None)
    authorize_item_read(application, account, "account_get", None)
    authorize_item_read(application, business, "local_get", None)
    # A business caller is judged without the global capability; a Global caller by the descriptor as declared.
    assert [descriptor.global_capability for descriptor in seen] == [None, "read_global_context", None, "manage_account", None]
    assert [descriptor.plugin_requirement for descriptor in seen[2:4]] == ["account-guidance", "account-guidance"]
    assert seen[4] is registry.get("local_get")
    for denied in ("deny", "signoff"):
        mode[0] = denied
        with pytest.raises(DomainError) as error:
            authorize_item_read(application, business, "things_get", None)
        assert error.value.code == "forbidden"
    with pytest.raises(DomainError) as error:
        authorize_item_read(application, business, "unknown_get", None)
    assert error.value.code == "operation_unavailable"
    assert {"not_found", "forbidden", "busy", "operation_unavailable"} <= UNAVAILABLE and "validation_error" not in UNAVAILABLE
    assert item_revision({"status_revision": 4}) == 4 and item_revision({"record_revision": 3}) == 3 and item_revision({}) == 1
    assert item_revision({"revision": 2, "status_revision": 9}) == 2


# -- 3. relationships: an authorized link, a denied endpoint, directional reads ----------------------------------------------

def test_a_link_is_created_only_between_endpoints_the_caller_may_read(host):
    topic, ticket = host.topic("t1", "Alpha", "alpha body"), host.ticket("k1", "Fix")
    agent = host.agent()
    host.agents.overrides["tickets_get"] = "deny"
    denied = host.invoke("relationships_create", {"source": topic, "target": ticket, "role": "related_to"}, agent)
    assert code(denied) == "forbidden" and host.edges() == []
    host.agents.overrides["tickets_get"] = "allow"
    created = host.invoke("relationships_create", {"source": topic, "target": ticket, "role": "related_to", "context": "because"}, agent)
    assert created.status == "ok", created.error
    edge = plain(created.data)["relationship"]
    assert edge["source"] == topic and edge["target"] == ticket and edge["role"] == "related_to"
    assert edge["predefined"] is True and edge["revision"] == 1 and edge["context"] == "because"
    assert edge["source_item"]["label"] == "Alpha" and edge["target_item"]["state"] == "open"
    assert {"from_type", "from_id", "to_type", "to_id"}.isdisjoint(edge)
    assert len(host.edges()) == 1 and host.edges()[0]["id"] == edge["id"]
    # An endpoint outside the business, or one that does not exist, is never linkable; a custom role is allowed.
    foreign = host.ticket("k9", "Foreign", business=OTHER)
    for target in (foreign, {"type": "ticket", "id": "missing"}):
        assert code(host.invoke("relationships_create", {"source": topic, "target": target, "role": "related_to"})) == "not_found"
    custom = host.ok("relationships_create", {"source": topic, "target": ticket, "role": "inspired_by"})
    assert plain(custom.data)["relationship"]["predefined"] is False and len(host.edges()) == 2


def test_directional_dtos_filters_and_cursors_are_unchanged(host):
    t1, t2, k1 = host.topic("t1", "Alpha"), host.topic("t2", "Beta"), host.ticket("k1", "Gamma")
    outgoing = host.edge(t1, k1, "related_to", "fix link")
    incoming = host.edge(k1, t1, "part_of", "child")
    chain = host.edge(t1, t2, "part_of", "")

    def listed(**filters):
        return [edge["id"] for edge in host.read("relationships_list", filters)["items"]]
    assert set(listed()) == {outgoing, incoming, chain}
    assert set(listed(item=t1)) == {outgoing, incoming, chain}
    assert set(listed(item=t1, direction="outgoing")) == {outgoing, chain}
    assert set(listed(item=t1, direction="incoming")) == {incoming}
    assert set(listed(item=k1, direction="incoming")) == {outgoing}
    assert set(listed(source=t1)) == {outgoing, chain} and set(listed(target=t1)) == {incoming}
    assert set(listed(role="part_of")) == {incoming, chain}
    assert set(listed(source_type="ticket")) == {incoming} and set(listed(target_type="topic")) == {incoming, chain}
    assert set(listed(query="FIX")) == {outgoing}                      # context, case-insensitive
    assert set(listed(query="gamma")) == {outgoing, incoming}          # endpoint labels
    assert set(listed(query="part_of")) == {incoming, chain}           # role text
    # Newest first, with a cursor bound to the operation, business and filters.
    everything = host.read("relationships_list", {"limit": 2})
    assert len(everything["items"]) == 2 and everything["next_cursor"]
    rest = host.read("relationships_list", {"limit": 2, "cursor": everything["next_cursor"]})
    assert [edge["id"] for edge in everything["items"] + rest["items"]] == [chain, incoming, outgoing] and rest["next_cursor"] is None
    assert code(host.invoke("relationships_list", {"limit": 2, "cursor": everything["next_cursor"], "role": "part_of"})) == "validation_error"
    # One edge by id, with direction-free DTO and the resolved endpoint items.
    one = host.read("relationships_get", {"relationship_id": outgoing})["relationship"]
    assert (one["source"], one["target"], one["revision"]) == (t1, k1, 1) and one["source_item"]["resource_type"] == "topics"
    # Archived endpoints are hidden unless asked for.
    host.ok("context_items_archive", {"item": t2, "archived": True}, expected_revision=1)
    assert set(listed()) == {outgoing, incoming} and set(listed(include_archived=True)) == {outgoing, incoming, chain}
    roles = host.read("relationship_roles_list")["roles"]
    assert [role["role"] for role in roles] == list(ROLES)
    assert roles[0] == {"role": "related_to", "label": "Related to", "inverse_label": "Related from", "workflow_tool": None}
    assert {role["role"]: role["workflow_tool"] for role in roles if role["workflow_tool"]} == WORKFLOW_ROLES


def test_private_and_unavailable_endpoints_are_skipped_not_leaked(host):
    shared = host.topic("t1", "Shared")
    journal = host.topic("p1", "Journal", visibility="private", owner=HUMAN)
    busy = host.topic("b1", "Busy", availability="applying")
    open_edge, private_edge, busy_edge = (host.edge(shared, journal), host.edge(shared, journal, "part_of"),
                                          host.edge(shared, busy))
    agent = host.agent()
    assert {edge["id"] for edge in host.read("relationships_list", ctx=agent)["items"]} == set()
    owner_edges = {edge["id"] for edge in host.read("relationships_list")["items"]}
    assert owner_edges == {open_edge, private_edge}                        # the busy endpoint drops out for everyone
    result = host.invoke("relationships_get", {"relationship_id": private_edge}, agent)
    assert code(result) == "not_found"                                    # a direct read reports, never leaks
    assert code(host.invoke("relationships_get", {"relationship_id": busy_edge})) == "busy"
    assert code(host.invoke("context_items_get", {"item": journal}, agent)) == "not_found"
    assert code(host.invoke("relationships_create", {"source": shared, "target": journal, "role": "related_to"}, agent)) == "not_found"
    listing = host.read("context_items_list", ctx=agent)["items"]
    assert [item["id"] for item in listing] == ["t1"]                     # enumeration alone grants nothing
    assert {item["id"] for item in host.read("context_items_list")["items"]} == {"t1", "p1"}
    assert host.read("context_navigation_get", ctx=agent)["counts"] == {"topic": 1, "ticket": 0}
    assert host.read("context_navigation_get")["counts"] == {"topic": 2, "ticket": 0}


def test_workflow_roles_belong_to_their_dedicated_tools(host):
    topic, ticket = host.topic("t1"), host.ticket("k1")
    for role, tool in WORKFLOW_ROLES.items():
        result = host.invoke("relationships_create", {"source": ticket, "target": topic, "role": role})
        assert code(result) == "workflow_required" and tool in result.error["message"]
    stored = host.edge(ticket, topic, "assigned_to")                      # written by the domain's own tool
    ordinary = host.edge(ticket, topic, "related_to")
    attempts = (("relationships_update", {"relationship_id": stored, "changes": {"role": "related_to"}}),
                ("relationships_update", {"relationship_id": ordinary, "changes": {"role": "blocked_by"}}),
                ("relationships_remove", {"relationship_id": stored}))
    for operation, arguments in attempts:
        assert code(host.invoke(operation, arguments, expected_revision=1)) == "workflow_required"
    assert {edge["id"]: edge["role"] for edge in host.edges()} == {stored: "assigned_to", ordinary: "related_to"}
    assert all(edge["revision"] == 1 for edge in host.edges())


def test_no_stored_edge_is_protected_by_role_name_and_the_ordinary_checks_remain(host):
    a, b = host.topic("t1"), host.topic("t2")
    named = host.edge(a, b, "protected_membership", "old")
    # A role name carries no protection of its own: the edge changes and is removed like any other.
    updated = host.invoke("relationships_update", {"relationship_id": named, "changes": {"context": "new"}}, expected_revision=1)
    assert updated.status == "ok" and next(edge for edge in host.edges() if edge["id"] == named)["context"] == "new"
    assert host.invoke("relationships_remove", {"relationship_id": named}, expected_revision=2).status == "ok"
    assert host.edges() == []
    # The ordinary checks stay: the revision, the workflow roles and the Signoff route of a policy-protected change.
    ordinary = host.edge(a, b, "related_to", "old")
    assert code(host.invoke("relationships_update", {"relationship_id": ordinary, "changes": {"context": "x"}}, expected_revision=7)) == "revision_conflict"
    host.agents.overrides["relationships_update"] = "signoff"
    staged = host.invoke("relationships_update", {"relationship_id": ordinary, "changes": {"context": "new"}}, host.agent(), expected_revision=1)
    assert staged.status == "signoff_required"
    assert next(edge for edge in host.edges() if edge["id"] == ordinary)["context"] == "old"


def test_a_message_endpoint_is_stored_as_its_canonical_conversation(host):
    topic, ticket = host.topic("t1", "Conversation"), host.ticket("k1")
    message = host.message("m1", "t1")
    created = host.ok("relationships_create", {"source": message, "target": ticket, "role": "related_to"})
    edge = plain(created.data)["relationship"]
    assert edge["source"] == topic and edge["target"] == ticket
    assert (host.edges()[0]["from_type"], host.edges()[0]["from_id"]) == ("topic", "t1")
    other = host.topic("t2")
    updated = host.ok("relationships_update", {"relationship_id": edge["id"], "changes": {"target": message}}, expected_revision=1)
    assert plain(updated.data)["relationship"]["target"] == topic and plain(updated.data)["relationship"]["revision"] == 2
    assert other and host.edges()[0]["revision"] == 2


def test_relationship_changes_need_the_current_revision_and_stay_in_their_business(host):
    a, b = host.topic("t1"), host.topic("t2")
    edge = host.edge(a, b, "related_to", "first")
    update = {"relationship_id": edge, "changes": {"context": "second"}}
    assert code(host.invoke("relationships_update", update, expected_revision=2)) == "revision_conflict"
    assert code(host.invoke("relationships_update", update)) == "revision_conflict"            # a revision is required
    assert code(host.invoke("relationships_update", {**update, "relationship_id": new_id()}, expected_revision=1)) == "not_found"
    assert host.edges()[0]["context"] == "first" and host.edges()[0]["revision"] == 1
    updated = host.ok("relationships_update", update, expected_revision=1)
    assert plain(updated.data)["relationship"]["revision"] == 2 and host.edges()[0]["context"] == "second"
    assert code(host.invoke("relationships_remove", {"relationship_id": edge}, expected_revision=1)) == "revision_conflict"
    removed = host.ok("relationships_remove", {"relationship_id": edge}, expected_revision=2)
    assert plain(removed.data) == {"removed_id": edge} and host.edges() == []
    assert code(host.invoke("relationships_remove", {"relationship_id": edge}, expected_revision=2)) == "not_found"
    # A relationship of another business is invisible even by its exact id.
    foreign_a, foreign_b = host.topic("f1", business=OTHER), host.topic("f2", business=OTHER)
    foreign = host.edge(foreign_a, foreign_b, "related_to", business=OTHER)
    assert code(host.invoke("relationships_get", {"relationship_id": foreign})) == "not_found"
    assert host.read("relationships_list")["items"] == []


SCOPED = ("relationship_roles_list", "relationships_list", "relationships_get", "relationships_create", "relationships_update",
          "relationships_remove", "context_items_list", "context_items_get")


def global_owner(**kwargs):
    """The installation owner on the Global root: no selected business, the Global capability the root route grants."""
    return InvocationContext(Principal(HUMAN, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard",
                             global_capabilities=frozenset({"read_global_context"}), **kwargs)


def test_exactly_eight_operations_take_the_business_or_global_namespace(host):
    for name in RELATIONSHIP_OPERATIONS:
        descriptor, properties = host.registry.get(name), host.registry.schema_for(name)["properties"]
        if name in SCOPED:
            assert descriptor.scope_kinds == {"business", "global"} and descriptor.global_capability == "read_global_context"
            assert descriptor.resolve_namespace is task_namespace and properties["scope"] == {"enum": ["business", "global"]}
        else:
            # Graph, navigation, archive, Material and review operations stay business-only, with no scope input.
            assert descriptor.scope_kinds == {"business"} and descriptor.global_capability is None
            assert descriptor.resolve_namespace is None and "scope" not in properties
    assert host.registry.schema_for("relationships_create")["required"] == ["source", "target", "role"]


def test_a_global_relationship_operation_never_sees_or_touches_a_business_row(host):
    a, b = host.topic("t1", "Alpha"), host.topic("t2", "Beta")
    edge = host.edge(a, b, "related_to", "business only")
    owner = global_owner()
    # The root infers Global from its context, and an explicit scope says the same; neither reads the business partition.
    for arguments in ({}, {"scope": "global"}):
        assert host.read("relationships_list", arguments, owner)["items"] == []
        assert host.read("context_items_list", arguments, owner)["items"] == []
        assert len(host.read("relationship_roles_list", arguments, owner)["roles"]) == len(ROLES)
    # A business row ID supplied to a Global operation is just not there, and Global writes cannot reach it either.
    assert code(host.invoke("relationships_get", {"relationship_id": edge}, owner)) == "not_found"
    assert code(host.invoke("relationships_update", {"relationship_id": edge, "changes": {"context": "x"}}, owner, expected_revision=1)) == "not_found"
    assert code(host.invoke("relationships_remove", {"relationship_id": edge, "scope": "global"}, owner, expected_revision=1)) == "not_found"
    assert code(host.invoke("context_items_get", {"item": a}, owner)) == "not_found"
    assert host.edges()[0]["context"] == "business only" and host.edges()[0]["revision"] == 1
    # Endpoints are resolved in the Global namespace only: a business item is not linkable from there, and nothing is written.
    created = host.invoke("relationships_create", {"source": a, "target": b, "role": "related_to", "scope": "global"}, owner)
    assert code(created) == "not_found" and host.edges(None) == [] and len(host.edges()) == 1
    # Without scope, a business caller keeps the business behavior exactly.
    assert host.read("relationships_get", {"relationship_id": edge})["relationship"]["source"] == a


def test_global_scope_needs_the_owner_or_an_authorized_global_agent_and_its_capability(host):
    a, b = host.topic("t1"), host.topic("t2")
    host.edge(a, b, "related_to")
    # A global agent without the global capability and a business-only owner context cannot enter Global, however the
    # scope is spelled.
    for ctx in (host.agent(), host.owner(), host.stranger()):
        for operation, arguments in (("relationships_list", {"scope": "global"}), ("context_items_list", {"scope": "global"}),
                                     ("relationship_roles_list", {"scope": "global"}),
                                     ("relationships_create", {"scope": "global", "source": a, "target": b, "role": "related_to"})):
            assert code(host.invoke(operation, arguments, ctx)) == "forbidden"
    # Claiming a capability the agent's profile does not grant is refused; so is a human who is not the owner.
    capable_agent = host.agent(global_capabilities=frozenset({"read_global_context"}))
    assert code(host.invoke("relationships_list", {"scope": "global"}, capable_agent)) == "forbidden"
    outsider = InvocationContext(Principal(STRANGER, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard",
                                 global_capabilities=frozenset({"read_global_context"}))
    assert code(host.invoke("relationships_list", {}, outsider)) in {"forbidden", "policy_denied"}
    # The owner without the Global capability is refused too; with it the read answers.
    assert code(host.invoke("relationships_list", {}, InvocationContext(
        Principal(HUMAN, "human"), allowed_business_ids=frozenset({BUSINESS}), source="dashboard"))) in {"forbidden", "scope_required"}
    assert host.read("relationships_list", {}, global_owner())["items"] == []
    # An unselected context cannot spell the business namespace either.
    assert code(host.invoke("relationships_list", {"scope": "business"}, global_owner())) in {"forbidden", "scope_required"}


def test_the_graph_holds_only_available_unarchived_nodes_and_the_edges_between_them(host):
    t1, t2, t3 = host.topic("t1", "One"), host.topic("t2", "Two"), host.topic("t3", "Three")
    k1 = host.ticket("k1", "Ticket")
    raw = host.topic("r1", "Raw upload", visibility="raw")
    archived = host.topic("a1", "Archived", archived=1)
    journal = host.topic("p1", "Journal", visibility="private", owner=HUMAN)
    busy = host.topic("b1", "Busy", availability="applying")
    kept = [host.edge(t1, t2), host.edge(t2, k1)]
    for target in (raw, archived, journal, busy):
        host.edge(t1, target)
    agent = host.agent()

    def graph(ctx=None, **args):
        data = host.read("context_graph_get", args, ctx)
        return {node["id"] for node in data["nodes"]}, {edge["id"] for edge in data["edges"]}
    assert graph(ctx=agent) == ({"t1", "t2", "t3", "k1"}, set(kept))
    assert graph() == ({"t1", "t2", "t3", "k1", "p1"}, {*kept, *{e["id"] for e in host.edges() if e["to_id"] == "p1"}})
    nodes, edges = graph(ctx=agent, include_archived=True)
    assert "a1" in nodes and "r1" not in nodes and "b1" not in nodes and len(edges) == 3
    assert graph(ctx=agent, item=t2) == ({"t1", "t2", "k1"}, set(kept))
    assert graph(ctx=agent, item=t3) == ({"t3"}, set())
    assert code(host.invoke("context_graph_get", {"item": {"type": "topic", "id": "missing"}})) == "not_found"


def test_item_listing_filters_use_the_material_port_and_cursors_are_bound(host):
    host.topic("t1", "Alpha", "first", material_type="note", tags_json=["red"])
    host.topic("t2", "Beta", "second", material_type="document", tags_json=["red", "blue"])
    host.topic("t3", "Gamma", "third", archived=1)
    host.topic("t4", "Raw", "fourth", visibility="raw")
    host.ticket("k1", "Alphabetical")

    def ids(**filters):
        return {item["id"] for item in host.read("context_items_list", filters)["items"]}
    assert ids() == {"t1", "t2", "k1"}
    assert ids(type="topic") == {"t1", "t2"} and ids(type="ticket") == {"k1"}
    assert ids(material_type="note") == {"t1"} and ids(tag="blue") == {"t2"} and ids(tag="red", material_type="document") == {"t2"}
    assert ids(query="ALPHA") == {"t1", "k1"} and ids(query="second") == {"t2"}              # label and summary
    assert ids(include_archived=True) == {"t1", "t2", "k1", "t3"} and ids(include_sources=True) == {"t1", "t2", "k1", "t4"}
    first = host.read("context_items_list", {"limit": 1, "type": "topic"})
    assert code(host.invoke("context_items_list", {"limit": 1, "type": "ticket", "cursor": first["next_cursor"]})) == "validation_error"


def test_material_archive_and_navigation_adapters_are_reached_through_their_ports(host):
    material = new_id()
    topic, ticket = host.topic(material, "Alpha"), host.ticket("k1")
    updated = host.ok("materials_update", {"material_id": material, "changes": {"material_type": "note", "tags": ["a"]}}, expected_revision=1)
    item = plain(updated.data)["item"]
    assert item["material_type"] == "note" and item["tags"] == ["a"] and item["revision"] == 2
    for arguments in ({"material_id": material, "changes": {"material_type": "forged"}}, {"material_id": material, "changes": {}},
                      {"material_id": material, "changes": {"tags": ["a", "a"]}}):
        assert code(host.invoke("materials_update", arguments, expected_revision=2)) == "validation_error"
    assert code(host.invoke("materials_update", {"material_id": material, "changes": {"tags": []}}, expected_revision=1)) == "revision_conflict"
    assert host.read("context_navigation_get")["counts"] == {"topic": 1, "ticket": 1}
    assert code(host.invoke("context_items_archive", {"item": topic, "archived": True}, expected_revision=1)) == "revision_conflict"
    archived = host.ok("context_items_archive", {"item": topic, "archived": True}, expected_revision=2)
    assert plain(archived.data)["item"]["archived"] is True and host.topic_row(material)["revision"] == 3
    assert host.read("context_navigation_get")["counts"] == {"topic": 0, "ticket": 1}
    assert code(host.invoke("context_items_archive", {"item": ticket, "archived": True}, expected_revision=1)) == "workflow_required"
    restored = host.ok("context_items_archive", {"item": topic, "archived": False}, expected_revision=3)
    assert plain(restored.data)["item"]["archived"] is False


# -- 4. reviews: explicit, revision-bound and append-only ----------------------------------------------------------------

def test_review_decisions_are_explicit_revision_bound_and_appended_once(host):
    item, source, edge = pair(host)
    status = host.pending(item)
    assert status["item_revision"] == 1 and len(status["items"]) == 1
    waiting = status["items"][0]
    assert waiting["pending"] and waiting["available"] and waiting["direction"] == "outgoing" and waiting["source"] == source
    assert waiting["review_revision"] == 0 and waiting["relationship_revision"] == 1 and waiting["latest_review"] is None
    assert waiting["source_token"] and waiting["source_item"]["label"] == "Source" and waiting["context"] == "evidence"
    # The other endpoint reviews the same relationship independently.
    mirrored = host.pending(source)["items"][0]
    assert mirrored["pending"] and mirrored["direction"] == "incoming" and mirrored["source"] == item
    assert mirrored["source_token"] != waiting["source_token"]

    reviewed = host.review(item, edge, result="incorporated", explanation="Folded into the item")
    assert reviewed.status == "ok", reviewed.error
    assert len(plain(reviewed.data)["review_ids"]) == 1 and plain(reviewed.data)["item"]["id"] == "t1"
    (review,) = host.reviews()
    assert (review["relationship_id"], review["item_type"], review["item_id"]) == (edge, "topic", "t1")
    assert (review["source_type"], review["source_id"], review["source_token"]) == ("topic", "t2", waiting["source_token"])
    assert (review["relationship_revision"], review["result"], review["actor_id"]) == (1, "incorporated", HUMAN)
    assert review["explanation"] == "Folded into the item" and review["prepared_at"] and review["created_at"]
    (state,) = host.states()
    assert (state["review_id"], state["revision"], state["item_id"]) == (review["id"], 1, "t1")
    assert host.pending(item)["items"] == []
    done = host.pending(item, include_reviewed=True)["items"][0]
    assert done["pending"] is False and done["review_revision"] == 1
    assert done["latest_review"]["actor"] == {"id": HUMAN, "name": "Owner", "kind": "human"}
    assert host.pending(source)["items"][0]["pending"]                   # the other endpoint is unaffected

    # Another decision from the old observation, or without reopening, is refused.
    stale = {"relationship_id": edge, "relationship_revision": 1, "review_revision": 0, "source_token": waiting["source_token"],
             "result": "no_change", "explanation": "Again"}
    result = host.invoke("context_review", {"item": item, "items": [stale]}, expected_revision=1)
    assert code(result) == "revision_conflict" and "Another participant" in result.error["message"]
    again = host.invoke("context_review", {"item": item, "items": [{**stale, "review_revision": 1}]}, expected_revision=1)
    assert code(again) == "conflict" and "reopen" in again.error["message"]
    assert len(host.reviews()) == 1

    reopen = {"item": item, "relationship_id": edge, "relationship_revision": 1, "review_revision": 1,
              "source_token": waiting["source_token"], "explanation": "Needs another look"}
    assert host.invoke("context_review_reopen", reopen, expected_revision=1).status == "ok"
    assert [row["result"] for row in host.reviews()] == ["incorporated", "pending"]
    assert host.states()[0]["revision"] == 2 and host.pending(item)["items"][0]["pending"]
    assert code(host.invoke("context_review_reopen", {**reopen, "review_revision": 2}, expected_revision=1)) == "conflict"
    history = host.read("context_reviews_history", {"item": item, "relationship_id": edge})
    assert [row["result"] for row in history["reviews"]] == ["pending", "incorporated"]   # newest first, none removed
    with host.database.transaction() as uow:
        for statement in ("UPDATE context_reviews SET explanation='x'", "DELETE FROM context_reviews"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                uow.connection.execute(statement)
        with pytest.raises(DomainError) as error:
            uow.repository("context_reviews", business_id=BUSINESS).update(review["id"], {"explanation": "x"})
        assert error.value.code == "immutable_record"


def test_review_conflicts_for_stale_item_edge_source_and_review_versions(host):
    item, source, edge = pair(host)
    third = host.topic("t3", "Third")
    other = host.edge(host.topic("t4"), third)                              # not related to the item
    observed = host.decision(item, edge)

    def attempt(decision=None, *, revision=1, items=None, **overrides):
        decisions = items if items is not None else [{**(decision or observed), **overrides}]
        return host.invoke("context_review", {"item": item, "items": decisions}, expected_revision=revision)
    assert code(attempt(revision=7)) == "revision_conflict"                      # the item's own revision
    assert code(attempt(relationship_revision=2)) == "revision_conflict"         # the edge's observed revision
    assert code(attempt(source_token="forged")) == "revision_conflict"           # the source content observed
    assert code(attempt(review_revision=1)) == "revision_conflict"               # the shared review state observed
    assert code(attempt(items=[observed, observed])) == "validation_error"       # one decision per relationship
    assert code(attempt(explanation="   ")) == "validation_error"
    assert code(attempt(explanation="")) == "validation_error"                   # schema floor
    assert code(attempt(relationship_id=other)) == "not_found"
    assert code(attempt(relationship_id=new_id())) == "not_found"
    assert host.reviews() == [] and host.states() == []
    # The source changed after it was observed: its fingerprint no longer matches.
    host.ok("topics_update", {"topic_id": "t2", "changes": {"body": "Newer evidence"}}, expected_revision=1)
    result = attempt()
    assert code(result) == "revision_conflict" and "Related content changed" in result.error["message"]
    # So did the edge itself.
    fresh = host.decision(item, edge)
    host.ok("relationships_update", {"relationship_id": edge, "changes": {"context": "better"}}, expected_revision=1)
    assert code(attempt(fresh)) == "revision_conflict" and host.reviews() == []
    assert host.review(item, edge).status == "ok" and len(host.reviews()) == 1


def test_reading_never_acknowledges_related_context(host):
    item, source, edge = pair(host)
    ticket = host.ticket("k1")
    before = (host.edges(), host.topic_row("t1"), host.topic_row("t2"))
    for ctx in (host.owner(), host.agent()):
        host.read("relationship_roles_list", ctx=ctx)
        host.read("relationships_list", ctx=ctx)
        host.read("relationships_get", {"relationship_id": edge}, ctx)
        host.read("context_items_list", ctx=ctx)
        host.read("context_items_get", {"item": item}, ctx)
        host.read("context_navigation_get", ctx=ctx)
        host.read("context_graph_get", {"item": item}, ctx)
        host.read("context_reviews_list", {"item": item, "include_reviewed": True}, ctx)
        host.read("context_reviews_history", {"item": item, "relationship_id": edge}, ctx)
        host.read("context_reviews_list", {"item": source}, ctx)
    # Neither filing a new relationship nor reading its source is a review.
    created = host.ok("relationships_create", {"source": item, "target": ticket, "role": "related_to"})
    assert plain(created.data)["relationship"]["revision"] == 1
    assert host.reviews() == [] and host.states() == []
    assert (host.edges()[0], host.topic_row("t1"), host.topic_row("t2")) == (before[0][0], before[1], before[2])
    assert len(host.pending(item)["items"]) == 2


def test_registered_kinds_grant_no_access_to_private_sources(host):
    journal = host.topic("p1", "Journal", "secret", visibility="private", owner=HUMAN)
    shared = host.topic("t1", "Shared")
    agent = host.agent()
    assert host.read("context_items_get", {"item": journal})["item"]["label"] == "Journal"
    assert code(host.invoke("context_items_get", {"item": journal}, agent)) == "not_found"
    assert code(host.invoke("context_items_get", {"item": journal}, host.stranger())) == "not_found"
    edge = host.edge(shared, journal)
    # Reviewing from the shared side must not reveal the private source's token or label to a non-owner.
    waiting = host.pending(shared, agent)["items"][0]
    assert waiting["available"] is False and waiting["source_token"] is None and "source_item" not in waiting
    assert host.pending(shared)["items"][0]["available"] is True
    mirrored = {"item": shared, "items": [{"relationship_id": edge, "relationship_revision": 1, "review_revision": 0,
                                           "source_token": "guess", "result": "no_change", "explanation": "Peeking"}]}
    assert code(host.invoke("context_review", mirrored, agent, expected_revision=1)) == "not_found"
    assert host.reviews() == []


# -- 5. a review composed with a same-item edit ----------------------------------------------------------------------------

def test_a_review_and_a_same_item_edit_commit_in_one_transaction(host):
    item, source, edge = pair(host)
    result = host.review(item, edge, edit=body_edit("t1", "Updated by the review"))
    assert result.status == "ok", result.error
    assert host.topic_row("t1")["body"] == "Updated by the review" and host.topic_row("t1")["revision"] == 2
    assert [row["content_hash"] for row in host.query("SELECT * FROM topic_files WHERE id='t1'")] == [file_hash("Updated by the review")]
    assert len(host.reviews()) == 1 and len(host.states()) == 1
    assert plain(result.data)["item"]["revision"] == 2                       # the answer is the item after both effects


def test_an_atomic_review_edit_must_change_the_item_being_reviewed(host):
    item, source, edge = pair(host)
    ticket = host.ticket("k1")
    ticket_edge = host.edge(ticket, source)
    cases = (
        (item, edge, body_edit("t2", "Someone else's topic")),                                          # another resource
        (item, edge, {"operation": "tickets_update", "arguments": {"ticket_id": "t1", "changes": {"title": "x"}}}),   # wrong kind
        (ticket, ticket_edge, {"operation": "docs_update", "arguments": {"document_id": "k1", "changes": {"body": "x"}}}),  # wrong table
    )
    for reviewed, relationship, edit in cases:
        assert code(host.review(reviewed, relationship, edit=edit)) == "validation_error"
    forged = {"operation": "relationships_remove", "arguments": {"relationship_id": edge}}
    assert code(host.review(item, edge, edit=forged)) == "validation_error"           # not an allowed edit
    assert host.reviews() == [] and host.topic_row("t1")["body"] == "Item body" and host.topic_row("t2")["revision"] == 1
    # A generic (resource-type) edit is accepted for any item kind stored in that table.
    generic = {"operation": "docs_update", "arguments": {"document_id": "t1", "changes": {"title": "Retitled"}}}
    assert host.review(item, edge, edit=generic).status == "ok" and host.topic_row("t1")["title"] == "Retitled"


def test_a_table_style_edit_applies_through_its_own_domain_handler(host):
    source = host.topic("t1", "Source")
    ticket = host.ticket("k1", "Ticket")
    edge = host.edge(ticket, source)
    edit = {"operation": "tickets_update", "arguments": {"ticket_id": "k1", "changes": {"title": "Renamed"}}}
    host.domain.fail_domain_apply = True
    failed = host.review(ticket, edge, edit=edit)
    assert code(failed) == "operation_failed" and host.domain.domain_applies == ["tickets_update"]
    assert host.reviews() == [] and host.states() == []                                  # the whole unit rolled back
    assert host.query("SELECT title FROM tickets")[0]["title"] == "Ticket"
    host.domain.fail_domain_apply, host.domain.domain_applies = False, []
    assert host.review(ticket, edge, edit=edit).status == "ok"
    assert host.domain.domain_applies == ["tickets_update"]                              # through its own apply handler
    assert host.query("SELECT title FROM tickets")[0]["title"] == "Renamed" and len(host.reviews()) == 1
    # A plain record edit is applied as prepared records and never reaches a domain handler.
    item, topic_source = host.topic("t5", "Item"), host.topic("t6", "Source")
    topic_edge = host.edge(item, topic_source)
    assert host.review(item, topic_edge, edit=body_edit("t5", "Plain")).status == "ok"
    assert host.domain.domain_applies == ["tickets_update"]


def test_a_denied_nested_edit_leaves_the_review_and_the_item_unchanged(host):
    item, source, edge = pair(host)
    agent = host.agent()
    host.agents.overrides["topics_update"] = "deny"
    denied = host.review(item, edge, ctx=agent, edit=body_edit())
    assert code(denied) == "policy_denied"
    assert host.reviews() == [] and host.states() == [] and host.topic_row("t1")["body"] == "Item body"
    assert host.topic_row("t1")["revision"] == 1
    host.agents.overrides["topics_update"] = "allow"
    assert host.review(item, edge, ctx=agent, edit=body_edit()).status == "ok"          # the control: allowed, both written
    assert host.topic_row("t1")["body"] == "Edited" and len(host.reviews()) == 1


def test_a_nested_signoff_floor_stages_the_whole_review(host):
    item, source, edge = pair(host)
    agent = host.agent()
    host.agents.overrides["topics_update"] = "signoff"
    staged = host.review(item, edge, ctx=agent, edit=body_edit())
    assert staged.status == "signoff_required"                                 # the edit's own floor holds the unit
    assert host.reviews() == [] and host.states() == [] and host.topic_row("t1")["body"] == "Item body"
    assert host.review(item, edge, ctx=agent).status == "ok"                   # no edit, no floor: allowed outright
    assert len(host.reviews()) == 1


def test_a_staged_review_keeps_nested_attribution_and_a_late_failure_leaves_both_unchanged(host):
    item, source, edge = pair(host)
    agent = host.agent()
    host.agents.overrides["context_review"] = "signoff"
    staged = host.review(item, edge, ctx=agent, edit=body_edit("t1", "Staged edit"))
    assert staged.status == "signoff_required"
    row = host.row(staged.signoff["id"])
    plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
    changes = {(change.resource.resource_type, change.action): change.operation_name for change in plan.changes}
    # The composed edit keeps its own operation attribution; the review records keep the review's.
    assert changes[("topics", "update")] == "topics_update" and changes[("topic_files", "update")] == "topics_update"
    assert changes[("context_reviews", "append_context_review")] is None
    assert changes[("context_review_states", "insert")] is None
    review_change = next(change for change in plan.changes if change.resource.resource_type == "context_reviews")
    assert "created_at" not in review_change.values                          # allocated only when applied
    targets = {(target.resource.resource_type, target.resource.resource_id): target for target in plan.expected_targets}
    assert targets[("topic_files", "t2")].content_hash == file_hash("Source body")      # the source fingerprint's file target
    assert targets[("topics", "t2")].revision == 1 and targets[("relationships", edge)].revision == 1
    assert host.reviews() == [] and host.topic_row("t1")["body"] == "Item body"
    # The source changes after staging, without a revision bump; approval cannot apply the review or the edit.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE topics SET body='Changed behind the plan' WHERE id='t2'")
    cause = refused_at_approval(host, staged.signoff["id"], "revision_conflict")
    assert "Related source changed" in cause["message"]
    assert host.reviews() == [] and host.states() == []
    assert host.topic_row("t1")["body"] == "Item body" and host.topic_row("t1")["revision"] == 1


def test_a_staged_review_applies_both_effects_once_when_nothing_changed(host):
    item, source, edge = pair(host)
    host.agents.overrides["context_review"] = "signoff"
    staged = host.review(item, edge, ctx=host.agent(), edit=body_edit("t1", "Approved edit"))
    assert host.approve(staged.signoff["id"]).status == "accepted"
    assert host.service.run_one(staged.signoff["id"])["state"] == "applied"
    assert host.topic_row("t1")["body"] == "Approved edit" and len(host.reviews()) == 1 and len(host.states()) == 1
    review = host.reviews()[0]
    assert review["actor_id"] == AGENT and review["created_at"] > review["prepared_at"]    # committed when applied
    assert host.service.run_one(staged.signoff["id"]) is None and len(host.reviews()) == 1


def test_a_file_content_change_after_preparation_blocks_the_review(host):
    item, source, edge = pair(host)
    host.agents.overrides["context_review"] = "signoff"
    staged = host.review(item, edge, ctx=host.agent())
    with host.database.transaction() as uow:                                 # only the source's file moved on
        uow.connection.execute("UPDATE topic_files SET content_hash=? WHERE id='t2'", (file_hash("moved"),))
    cause = refused_at_approval(host, staged.signoff["id"], "revision_conflict")
    assert "content changed" in cause["message"] and host.reviews() == [] and host.states() == []


def test_busy_and_unavailable_sources_keep_their_result_contract(host):
    item, source, edge = pair(host)
    assert host.review(item, edge).status == "ok"                        # reviewed while the source was ready
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE topics SET availability='applying' WHERE id='t2'")
    waiting = host.pending(item, include_reviewed=True)["items"][0]
    assert waiting["available"] is False and waiting["pending"] is True and waiting["source_token"] is None
    assert waiting["latest_review"] is None and "source_item" not in waiting
    # History still resolves a busy source for metadata, and shows the earlier decision untouched.
    history = host.read("context_reviews_history", {"item": item, "relationship_id": edge})
    assert [row["result"] for row in history["reviews"]] == ["incorporated"]
    # Reviewing the busy source itself, or the busy item's list, reports busy rather than an empty result.
    mirrored = {"relationship_id": edge, "relationship_revision": 1, "review_revision": 0, "source_token": "x",
                "result": "no_change", "explanation": "Now"}
    assert code(host.invoke("context_review", {"item": {"type": "topic", "id": "t2"}, "items": [{**mirrored, "relationship_id": edge}]},
                            expected_revision=1)) == "busy"
    assert code(host.invoke("context_reviews_list", {"item": {"type": "topic", "id": "t2"}})) == "busy"
    host.read("context_reviews_history", {"item": {"type": "topic", "id": "t2"}, "relationship_id": edge})


# -- 6. the engine-owned review effect and the relationship tables ---------------------------------------------------------

def test_the_review_append_effect_is_engine_owned(host):
    item, source, edge = pair(host)
    assert host.review(item, edge).status == "ok"
    hooks = host.database.demo.hooks
    assert ("context_reviews", "append_context_review") not in hooks.offered         # never offered to a host hook
    assert ("context_review_states", "insert") in hooks.offered                          # ordinary effects still are
    assert all(table != "context_reviews" for table, _, _ in hooks.applied)           # no new history event for decisions
    assert any(table == "context_review_states" for table, _, _ in hooks.applied)
    # The same action on any other table is not a recognized effect.
    ctx = host.owner()
    plan = item_plan(ctx, {"id": "x"}, extra=(PreparedChange("record", ResourceRef("items", "y", BUSINESS),
                                                            "append_context_review", {"id": "y", "title": "Y"}),))
    assert isinstance(plan, ChangeSet)
    with pytest.raises(DomainError) as error:
        with host.database.transaction(business_id=BUSINESS) as uow:
            apply_records(ctx, plan, uow)
    assert error.value.code == "invalid_plan" and host.count("items") == 0


def test_relationship_tables_are_engine_owned_with_v1_keys_constraints_and_no_host_hooks(host):
    tables = host.database.assembly.tables
    assert tables["relationships"] == TableSpec(("business_id", "id"), "business_id")
    assert tables["context_reviews"] == TableSpec(("business_id", "id"), "business_id", False, None)
    assert tables["context_review_states"] == TableSpec(
        ("business_id", "relationship_id", "item_type", "item_id"), "business_id")
    names = lambda kind: {row["name"] for row in host.query("SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'", kind)}
    assert {"relationships_from", "relationships_global_key", "relationships_memory_observe", "relationships_role",
            "relationships_to", "context_reviews_history"} <= names("index")
    assert {"context_reviews_no_update", "context_reviews_no_delete"} <= names("trigger")
    assert not {name for name in names("trigger") if name.startswith("inbox_capture_")}     # host-owned, not duplicated here
    a, b = host.topic("t1"), host.topic("t2")
    with host.database.transaction() as uow:
        insert = ("INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at) "
                  "VALUES (NULL,?,?,?,?,?,?,'',?,?)")
        with pytest.raises(sqlite3.IntegrityError):                       # a Global edge needs one of the allowed shapes
            uow.connection.execute(insert, (new_id(), "topic", "t1", "topic", "t2", "related_to", "now", "now"))
        with pytest.raises(sqlite3.IntegrityError):                       # hierarchy is the Task's parent, never an edge
            uow.connection.execute(insert, (new_id(), "task", "k", "task", "s", "part_of", "now", "now"))
        with pytest.raises(sqlite3.IntegrityError):                       # the retired group item type has no Global shape
            uow.connection.execute(insert, (new_id(), "material", "m", "story", "s", "concerns", "now", "now"))
        shared = new_id()
        uow.connection.execute(insert, (shared, "material", "m", "task", "s", "concerns", "now", "now"))   # related to a group Task
        uow.connection.execute(insert, (new_id(), "task", "k", "task", "s", "concerns", "now", "now"))     # a Task related to a group Task
        with pytest.raises(sqlite3.IntegrityError):                       # one Global id once
            uow.connection.execute(insert, (shared, "material", "m2", "task", "s2", "concerns", "now", "now"))
    # Removing a relationship removes its review state and keeps the decisions.
    edge = host.edge(a, b)
    assert host.review(a, edge).status == "ok"
    host.ok("relationships_remove", {"relationship_id": edge}, expected_revision=1)
    assert host.states() == [] and len(host.reviews()) == 1
