"""The inbox service and the event finalizer after their move into Operations, driven through the stock host composition.

Real: the host assembly, schema, catalog and history catalog, the Operations ``InboxService`` and ``EventFinalizer`` with the
host adapters bound (``HostInbox``, ``HostEventRouting``), the discussion writer and the host finalizer hook. Synthetic: a tmp
instance with its globals, a recording wrapper around each adapter and a fake extension source. No model, native session,
provider or network is used.

Each scenario states the boundary it pins: that the host service is the Operations one with the adapter bound and keeps only the
native ownership, the order of the adapter calls the flows make (so a call cannot drift to another point), the precedence of the
selected extensions' source handlers, the one before-commit finalizer, and that the declarations
the host still carries are exactly the ones Operations now declares. The byte-for-byte behavior against the previous tree is
checked by the transcript comparison, not here."""

import inspect
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain
from qorqut_operations.inbox import (BROADCAST_READ_ENTRIES, BROADCAST_SCHEMA, CAPTURE_SCHEMA, CHANGE_SCHEMA, CLASSIFICATIONS,
                                     HISTORY_CONTRIBUTION, INBOX_HOST_METHODS, MESSAGE_SCHEMA, MUTATION_ENTRIES, PRINCIPAL_CAPTURE_SCHEMA,
                                     READ_ENTRIES, RELATIONSHIP_CAPTURE_SCHEMA, ROUTING_METHODS, TABLE_SPECS, EventFinalizer,
                                     InboxService as OperationsInboxService)

from bazarlyk.composition import transactions
from bazarlyk.composition.capture import CAPTURED
from bazarlyk.composition.catalog import BUILTIN_CATALOG
from bazarlyk.composition.schema_ddl import OWNER_SCHEMA
from bazarlyk.extensions import NO_SOURCES, SourceHandler, SourceHandlers
from bazarlyk.inbox import events as host_events
from bazarlyk.inbox.events import ITEM_TYPES, HostEventRouting, finalize_events
from bazarlyk.inbox.messages import Messages
from bazarlyk.inbox.service import SOURCES, HostInbox, InboxService
from bazarlyk.operations_history.catalog import catalog as history_catalog
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources, select_coordinator, set_agent_overrides

GRANTS = {"tasks_get": "allow", "inbox_list": "allow"}


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    alpha, beta = create_worker(instance, "alpha"), create_worker(instance, "beta")
    for worker in (alpha, beta):
        set_agent_overrides(instance, worker, GRANTS, [first, second])
    instance.runtime.freeze_registry()
    return SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first, second=second,
                           coordinator=coordinator, alpha=alpha, beta=beta)


def agent(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def call(host, ctx, operation, arguments):
    return host.runtime.application.invoke(ctx, operation, arguments, InvocationOptions(idempotency_key=new_id()))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def task(host, title, **arguments):
    return ok(call(host, host.instance.human(host.first), "tasks_create", {"title": title, **arguments}))["task"]


class Recorder:
    """Wraps an adapter: every call is appended to ``calls`` by name (arguments kept) and then forwarded."""

    def __init__(self, target, calls, names):
        self.target, self.calls, self.names = target, calls, names

    def __getattr__(self, name):
        method = getattr(self.target, name)
        if name not in self.names:
            return method

        def recorded(*arguments, **keywords):
            self.calls.append((name, arguments))
            return method(*arguments, **keywords)
        return recorded


def names(calls):
    return [name for name, _ in calls]


# -- the host service is the Operations service with the adapter bound --------------------------------------------------------

def test_the_host_service_is_the_operations_service_with_the_adapter_bound_and_keeps_only_native_ownership(host):
    service = host.runtime.inbox
    assert isinstance(service, OperationsInboxService) and type(service) is InboxService
    assert type(service.host) is HostInbox and service.host.inbox is service and isinstance(service.messages, Messages)
    assert service.subscribable == (*SOURCES, *service.sources.subscribable) and SOURCES == (
        "tasks", "actions", "signoffs", "documents")
    assert service.visible_types == {**ITEM_TYPES, "tasks": "task", "actions": "action"} and service.item_types is ITEM_TYPES
    assert list(ITEM_TYPES) == ["signoffs", "documents", "data_tables", "agent_profiles", "context_entities", "principals"]
    assert service.items is service.domains.items and service.related is service.domains.related and service.app is host.runtime.application
    assert InboxService(SimpleNamespace(), None).delivery("a") == {"state": "unknown", "reason": "nudge_backend_not_running", "last_attempt_at": None}
    # The host class defines the owner inspection and the native session ownership and nothing generic.
    assert set(vars(InboxService)) - {"__module__", "__doc__", "__qualname__", "__firstlineno__", "__static_attributes__"} == {
        "__init__", "owner_agent_context", "owner_document", "owner_snapshot", "startup_owned",
        "current_owner", "session_owner", "session_context", "start_session", "pending_owner_conversations"}
    generic = {"register", "context", "subject", "visible", "event_visible", "pending", "event_item", "subscriptions", "read",
               "validate_mutation", "prepare", "apply", "thread_item", "follow", "unfollow", "related_work", "business_member"}
    assert generic <= set(vars(OperationsInboxService)) and not generic & set(vars(InboxService))
    assert not {"start_session", "session_owner", "current_owner", "owner_snapshot"} & set(vars(OperationsInboxService))
    assert all(name in vars(HostInbox) for name in INBOX_HOST_METHODS)
    for operation in ("inbox_list", "inbox_subscriptions_list", "inbox_acknowledge", "inbox_subscribe", "inbox_unsubscribe"):
        assert host.runtime.registry.get(operation) is not None


def test_the_host_event_module_keeps_the_focus_functions_and_defines_none_of_the_generic_ones():
    for moved in ("targets", "row_for", "source_summary", "assigned", "_json", "item_type_for", "RESOURCE_TYPES", "IGNORED_FIELDS"):
        assert not hasattr(host_events, moved) or moved in {"_json", "assigned", "row_for", "source_summary"} and getattr(
            host_events, moved).__module__ == "qorqut_operations.inbox.events"
    assert {"focus_edit_targets", "focus_reader", "finalize_events", "HostEventRouting"} <= set(vars(host_events))
    assert inspect.signature(finalize_events).parameters["sources"].default is NO_SOURCES
    assert all(name in vars(HostEventRouting) for name in ROUTING_METHODS)


# -- the order of the adapter calls ----------------------------------------------------------------------------------------

def test_visible_calls_the_adapter_at_its_fixed_slots_and_handlers_and_private_sources_come_first(host):
    service, ctx = host.runtime.inbox, agent(host, host.alpha, host.first)
    group = task(host, "Group")
    calls = []
    service.host = Recorder(service.host, calls, INBOX_HOST_METHODS)
    widgets = SimpleNamespace(source="widgets", subscription_visible=lambda svc, c, identity, business, uow: identity == "w1")
    service.sources = SimpleNamespace(get=lambda key: widgets if key == "widgets" else None, subscribable=(), item_types={})

    def check(*arguments, **keywords):
        calls.clear()
        with host.database.read() as uow:
            return service.visible(ctx, *arguments, uow, **keywords)

    assert check("owner_conversations", new_id(), None) is False and names(calls) == ["private_visible"]
    assert check("widgets", "w1", host.first) is True and check("widgets", "w2", host.first) is False and names(calls) == ["private_visible"]
    assert check("mystery", "x", host.first) is False and names(calls) == ["private_visible"]
    assert check("tasks", group["id"], host.first) is True
    assert names(calls) == ["private_visible", "business_member", "integration_visible"]
    check("principals", host.instance.owner, host.first)    # resolved by the shared item lookup, never by the integration
    assert names(calls) == ["private_visible", "business_member"]
    missing = new_id()
    assert check("documents", missing, host.first) is False
    assert names(calls) == ["private_visible", "business_member", "integration_visible"]
    assert calls[2][1][1:4] == ("documents", missing, host.first)
    assert check("documents", new_id(), None) is False and names(calls) == ["private_visible", "integration_visible"]    # Global: no membership check


def test_event_visibility_asks_the_handler_then_the_host_then_the_generic_branches(host):
    service, ctx = host.runtime.inbox, agent(host, host.alpha, host.first)
    calls = []
    service.host = Recorder(service.host, calls, INBOX_HOST_METHODS)
    handler = SimpleNamespace(event_visible=lambda svc, c, row, uow: row["source_id"] == "yes")
    service.sources = SimpleNamespace(get=lambda key: handler if key == "widgets" else None, subscribable=(), item_types={})

    def event(source, identity="x", kind="updated", business=None, visibility="{}"):
        return {"source_type": source, "source_id": identity, "event_type": kind, "business_id": business, "visibility_json": visibility}

    def check(row):
        calls.clear()
        with host.database.read() as uow:
            return service.event_visible(ctx, row, uow)

    assert check(event("widgets", "yes")) is True and check(event("widgets", "no")) is False and calls == []
    assert check(event("context_inputs")) is False and names(calls) == ["event_visible"]
    assert check(event("operational_assignments", new_id(), business=host.first)) is False and names(calls) == ["event_visible"]
    assert check(event("agent_messages", new_id())) is False and names(calls) == ["event_visible"]
    # A deleted Task whose current source is gone falls back to the current policy, with a fresh membership check.
    assert check(event("tasks", new_id(), "deleted", host.first)) in {True, False}
    assert names(calls) == ["event_visible", "private_visible", "business_member", "integration_visible", "business_member"]
    group = task(host, "Still here")
    assert check(event("tasks", group["id"], "deleted", host.first)) is True
    assert names(calls) == ["event_visible", "private_visible", "business_member", "integration_visible"]


def test_event_item_decorates_before_and_after_the_extension_pointer(host):
    service, ctx = host.runtime.inbox, agent(host, host.alpha, host.first)
    calls = []
    service.host = Recorder(service.host, calls, INBOX_HOST_METHODS)
    pointer = SimpleNamespace(pointer=("widget", lambda c, row, uow: calls.append(("pointer", ())) or {"id": row["source_id"]}))
    service.sources = SimpleNamespace(get=lambda key: pointer if key == "widgets" else None, subscribable=(), item_types={})
    row = {"id": 1, "business_id": None, "source_type": "widgets", "source_id": "w1", "actor_id": None, "visibility_json": "{}"}
    with host.database.read() as uow:
        item = service.event_item(ctx, row, uow)
    assert names(calls) == ["decorate_before_extensions", "pointer", "decorate_after_extensions"]
    assert item == {"id": 1, "business_id": None, "source_type": "widgets", "source_id": "w1", "actor_id": None,
                    "business_slug": None, "actor_name": "System", "widget": {"id": "w1"}}


def test_subject_claim_and_subscription_slots(host):
    service = host.runtime.inbox
    alpha, coordinator = agent(host, host.alpha, host.first), agent(host, host.coordinator, host.first)
    group = task(host, "Assigned", assignee_ids=[host.alpha])
    calls = []
    service.host = Recorder(service.host, calls, INBOX_HOST_METHODS)
    ok(call(host, agent(host, host.beta, host.first), "inbox_subscriptions_list", {}))
    assert names(calls) == ["augment_subscriptions"]    # nothing followed: only the host's automatic follows are merged in
    calls.clear()
    ok(call(host, alpha, "inbox_subscriptions_list", {}))
    assert names(calls)[-1] == "augment_subscriptions" and names(calls).count("augment_subscriptions") == 1
    assert set(names(calls)[:-1]) == {"private_visible", "business_member", "integration_visible"}    # one visible() per followed item
    calls.clear()
    ok(call(host, coordinator, "inbox_subscriptions_list", {"agent_id": host.beta}))
    assert names(calls) == ["managed_subject", "augment_subscriptions"] and calls[0][1][1] == host.beta
    service_argument, _, result, _ = calls[1][1]
    assert service_argument is service and result == []
    calls.clear()
    event = rows(host, "SELECT e.id FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? AND e.source_id=?", host.alpha, group["id"])[0]["id"]
    assert code(call(host, alpha, "inbox_acknowledge", {"event_ids": [999999]})) == "not_found" and "active_event_claim" not in names(calls)
    calls.clear()
    ok(call(host, alpha, "inbox_acknowledge", {"event_ids": [event]}))
    claims = [arguments[:2] for name, arguments in calls if name == "active_event_claim"]
    assert claims and set(claims) == {(host.alpha, event)}    # asked only for a visible event, about the acknowledging agent
    assert code(call(host, coordinator, "inbox_acknowledge", {"agent_id": host.alpha, "event_ids": [event]})) == "validation_error"


def test_the_finalizer_calls_the_routing_slots_in_the_old_order(host):
    calls = []
    routing = Recorder(HostEventRouting(), calls, ROUTING_METHODS)
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,include_related,created_at) VALUES(?,?,?,?,?,0,?)",
                               (new_id(), host.alpha, host.first, "principals", host.instance.owner, "t"))
        uow.actor_id = host.coordinator
        uow.connection.execute("UPDATE principals SET display_name='Owner Renamed' WHERE id=?", (host.instance.owner,))
        EventFinalizer(sources=NO_SOURCES, item_types=ITEM_TYPES, feed_only=CAPTURED, host=routing).finalize(uow)
        assert uow.connection.execute("SELECT count(*) FROM inbox_changes").fetchone()[0] == 0
    # The Principal's update fans out to its two projects in the (unordered) membership order, and only the first project has a
    # subscriber, so the one delivery group comes before or after the empty one; every other call is in a fixed place.
    delivered = ["augment_recipients", "recipient_allowed", "presentation_summary"]
    assert names(calls)[:8] == ["begin_batch", "coalesced_creation", "responsible", "early_targets", "augment_direct", "presentation",
                                "augment_direct", "presentation"]
    assert names(calls)[8:] in ([*delivered, "augment_recipients"], ["augment_recipients", *delivered])
    assert [(r["business_id"], r["source_type"], r["event_type"]) for r in rows(host, "SELECT * FROM inbox_events")] == [(host.first, "principals", "updated")]
    assert [r["agent_id"] for r in rows(host, "SELECT * FROM inbox_deliveries")] == [host.alpha]
    calls.clear()
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO inbox_changes(source_type,action,actor_id,after_json) VALUES('signoff_attention','insert',NULL,'{\"signoff_id\":\"none\"}')")
        EventFinalizer(sources=NO_SOURCES, item_types=ITEM_TYPES, feed_only=CAPTURED, host=routing).finalize(uow)
    assert names(calls) == ["begin_batch", "coalesced_creation", "responsible", "early_targets", "additional_targets"]
    with host.database.transaction() as uow:
        assert EventFinalizer(sources=NO_SOURCES, item_types=ITEM_TYPES, feed_only=CAPTURED, host=routing).finalize(uow) is None
    assert names(calls)[5:] == []    # no capture: nothing is read or called


# -- extension handlers and the single finalizer -------------------------------------------------------------

def test_selected_extension_handlers_keep_their_precedence_in_targets_visibility_and_pointers(host):
    seen = []
    widgets = SourceHandler("widgets", item_type="widget", subscribable=True,
                            row=lambda uow, business, identity: {"id": identity, "business_id": business, "title": "Widget " + identity},
                            targets=lambda uow, data, business, actor: seen.append((data, business, actor)) or [
                                ("widgets", {"id": data["id"], "business_id": business, "title": "Widget"}, actor, "created", {host.beta})],
                            subscription_visible=lambda service, ctx, identity, business, uow: seen.append(("visible", type(service).__name__)) or True,
                            event_visible=lambda service, ctx, row, uow: True, pointer=("widget", lambda ctx, row, uow: {"id": row["source_id"]}))
    sources = SourceHandlers([widgets])
    service = InboxService(host.runtime.application, host.runtime.domains, resolver=host.runtime.resolver, sources=sources)
    assert service.subscribable == (*SOURCES, "widgets")
    finalizer = EventFinalizer(sources=sources, item_types=ITEM_TYPES, feed_only=CAPTURED, host=HostEventRouting())
    capture = {"source_type": "widgets", "action": "insert", "actor_id": host.alpha, "before_json": None,
               "after_json": '{"id":"w1","business_id":null}'}
    with host.database.read() as uow:
        assert finalizer.targets(uow, capture) == [("widgets", {"id": "w1", "business_id": None, "title": "Widget"}, host.alpha, "created", {host.beta})]
        assert finalizer.targets(uow, {**capture, "source_type": "tasks"}) == []                # the engine's own writer announces Tasks
        assert finalizer.targets(uow, {**capture, "source_type": "documents", "after_json": '{"kind":"working_memory"}'}) == []
        edge = {"source_type": "relationships", "action": "insert", "actor_id": None, "before_json": None,
                "after_json": '{"business_id":null,"from_type":"widget","from_id":"w1","to_type":"agent","to_id":"%s","role":"assigned_to"}' % host.beta}
        result = finalizer.targets(uow, edge)
        assert [(table, event, recipients) for table, _, _, event, recipients in result] == [
            ("widgets", "relationship_changed", set()), ("agent_profiles", "relationship_changed", set()),
            ("widgets", "assignment_changed", {host.beta})]
        assert result[0][1] == {"id": "w1", "business_id": None, "title": "Widget w1"}      # the handler's row, not a table read
        assert service.visible(agent(host, host.alpha), "widgets", "w1", None, uow) is True and ("visible", "InboxService") in seen
        row = {"id": 5, "business_id": None, "source_type": "widgets", "source_id": "w1", "actor_id": None, "visibility_json": "{}"}
        assert service.event_visible(agent(host, host.alpha), row, uow) is True
        assert service.event_item(agent(host, host.alpha), row, uow)["widget"] == {"id": "w1"}
        assert finalizer.item_type_for("widgets") == "widget" and finalizer.item_type_for("signoffs") == "signoff"


def test_one_finalizer_runs_after_the_capture_and_is_the_hosts_wrapper(host):
    assert transactions.inbox_finalizers() == (finalize_events,)
    wrapped = transactions.inbox_finalizers(SourceHandlers([SourceHandler("widgets")]))
    assert len(wrapped) == 1 and isinstance(wrapped[0], partial) and wrapped[0].func is finalize_events
    assert "sources" in wrapped[0].keywords and len(host.database.assembly.finalizers) == 1


# -- the declarations the host still carries are the ones Operations declares ---------------------------------------------------

def test_the_host_composition_carries_exactly_the_declarations_operations_now_owns(host):
    statements = list(OWNER_SCHEMA["inbox"])
    placed = {0: CHANGE_SCHEMA, 2: CAPTURE_SCHEMA, 14: BROADCAST_SCHEMA[:2], 16: BROADCAST_SCHEMA[2:], 24: PRINCIPAL_CAPTURE_SCHEMA,
              27: MESSAGE_SCHEMA, 37: RELATIONSHIP_CAPTURE_SCHEMA}
    for start, block in placed.items():
        assert statements[start:start + len(block)] == list(block)
    assert all(statements.count(statement) == 1 for block in placed.values() for statement in block)
    assert {name: host.database.assembly.tables[name] for name in TABLE_SPECS} == TABLE_SPECS
    classifications = {item.table: item for contribution in history_catalog().contributions for item in contribution.classifications}
    assert all(classifications[item.table] == item for item in CLASSIFICATIONS)
    order = [BUILTIN_CATALOG.index(entry) for entry in (*MUTATION_ENTRIES, *READ_ENTRIES, *BROADCAST_READ_ENTRIES)]
    assert order[:10] == sorted(order[:10]) and order[10:14] == sorted(order[10:14]) and order[14:] == sorted(order[14:])
    assert all(BUILTIN_CATALOG.count(entry) == 1 for entry in (*MUTATION_ENTRIES, *READ_ENTRIES, *BROADCAST_READ_ENTRIES))
    contribution = next(item for item in history_catalog().contributions if item.owner == "host.inbox")

    class Request:
        def select(self, uow, where, parameters):
            return (where, tuple(parameters))

    for ours, theirs in zip(HISTORY_CONTRIBUTION.exports, contribution.exports):
        assert (ours.table, dict(ours.columns), ours.key, dict(ours.structured), ours.shard_hex) == (
            theirs.table, dict(theirs.columns), theirs.key, dict(theirs.structured), theirs.shard_hex)
        assert ours.selector(None, Request()) == theirs.selector(None, Request())
    assert len(HISTORY_CONTRIBUTION.exports) == len(contribution.exports) == 3
