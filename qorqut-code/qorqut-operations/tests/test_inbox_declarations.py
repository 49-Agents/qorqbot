"""The operational inbox module: it loads alone, requires its host adapters and declares exactly the existing storage.

Synthetic only: an in-memory SQLite database and stand-in adapters. The message and broadcast flows themselves are exercised
against the stock host composition in the host's ``test_inbox_operations_split.py``.
"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from qorqut_operations.history_exports import TECHNICAL_EXCLUDED
from qorqut_operations.inbox import (BROADCAST_READ_ENTRIES, BROADCAST_SCHEMA, CAPTURE_SCHEMA, CHANGE_SCHEMA, CLASSIFICATIONS,
                                     HISTORY_CONTRIBUTION, HOST_METHODS, INBOX_HOST_METHODS, INBOX_OPERATIONS, MESSAGE_SCHEMA,
                                     MUTATION_ENTRIES, PRINCIPAL_CAPTURE_SCHEMA, READ_ENTRIES, RELATIONSHIP_CAPTURE_SCHEMA,
                                     ROUTING_METHODS, TABLE_NAMES, TABLE_SPECS, THREAD_SOURCES, BroadcastDispatch, Broadcasts,
                                     EventFinalizer, InboxService, Messages, core_default, related, visible_types)
from qorqut_operations.inbox.events import assigned, row_for, source_summary
from qorqut_operations.inbox.history import ITEM_SOURCES, MARKS

ROOT = Path(__file__).resolve().parents[1]


def test_the_module_loads_without_a_host_context_or_extension():
    code = "import sys, qorqut_operations.inbox; print(' '.join(sorted({name.partition('.')[0] for name in sys.modules})))"
    result = subprocess.run([sys.executable, "-s", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                            env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.split())
    assert "qorqut_operations" in loaded
    assert loaded.isdisjoint({"bazarlyk", "qorqut_context", "qorqut_roles", "qorqut_paper", "qorqut_content", "document_history"})


def test_messages_need_a_complete_host_adapter_and_have_no_default():
    inbox = SimpleNamespace(app=SimpleNamespace(), clock=None)
    with pytest.raises(TypeError):
        Messages(inbox)
    with pytest.raises(TypeError, match="owner_directed_target"):
        Messages(inbox, host=None)
    partial = SimpleNamespace(**{name: (lambda *a, **k: None) for name in HOST_METHODS if name != "queue_terminal"})
    with pytest.raises(TypeError, match="queue_terminal"):
        Messages(inbox, host=partial)
    complete = SimpleNamespace(**{name: (lambda *a, **k: None) for name in HOST_METHODS})
    assert Messages(inbox, host=complete).host is complete
    assert HOST_METHODS == ("owner_directed_target", "binding", "validate_recipient", "attribution", "history_scope",
                            "forward_owner_message", "retirement_backlog", "validate_terminal", "queue_terminal",
                            "terminal_projection")


def test_broadcasts_and_dispatch_require_every_host_function():
    inbox = SimpleNamespace(app=SimpleNamespace())
    with pytest.raises(TypeError):
        Broadcasts(inbox)
    with pytest.raises(TypeError):
        Broadcasts(inbox, active_global=None, global_profile_data=None, reaches_business=None)
    with pytest.raises(TypeError):
        BroadcastDispatch(None, None, None)


def test_storage_declarations_are_the_message_and_broadcast_blocks_and_create_cleanly_together():
    assert TABLE_NAMES == ("inbox_changes", "broadcasts", "broadcast_deliveries", "agent_messages")
    assert {name: (spec.keys, spec.scope_column, spec.mutable, spec.revision_column) for name, spec in TABLE_SPECS.items()} == {
        "agent_messages": (("id",), "business_id", False, None),
        "broadcasts": (("id",), "business_id", True, "revision"),
        "broadcast_deliveries": (("broadcast_id", "occurrence", "recipient_id"), "business_id", True, None),
        "inbox_changes": (("id",), None, True, None)}
    assert [(item.table, item.kind) for item in CLASSIFICATIONS] == [
        ("agent_messages", "private_excluded"), ("broadcasts", "private_excluded"), ("broadcast_deliveries", "private_excluded"),
        ("inbox_changes", TECHNICAL_EXCLUDED)]
    assert len({item.reason for item in CLASSIFICATIONS[:3]}) == 1 and CLASSIFICATIONS[3].reason.startswith("Transient mutation")
    connection = sqlite3.connect(":memory:")
    for statement in (*BROADCAST_SCHEMA, *MESSAGE_SCHEMA):
        connection.execute(statement)
    kinds = {(row[0], row[1]) for row in connection.execute("SELECT type,name FROM sqlite_master")}
    assert {name for kind, name in kinds if kind == "table"} == set(TABLE_NAMES) - {"inbox_changes"}
    assert {name for kind, name in kinds if kind == "trigger"} == {"agent_messages_immutable_update", "agent_messages_immutable_delete"}
    indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    assert indexes == {"broadcasts_due", "broadcast_delivery_pending", "agent_messages_sender", "agent_messages_recipient",
                       "agent_messages_thread", "agent_messages_delegation", "agent_messages_current_sender",
                       "agent_messages_current_recipient"}
    # The messages stay immutable apart from the two current participants.
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("INSERT INTO agent_messages(id,event_id,sender_id,recipient_id,thread_id,body,created_at,current_sender_id,"
                       "current_recipient_id) VALUES('m',1,'a','b','m','hello','t','a','b')")
    connection.execute("UPDATE agent_messages SET current_recipient_id='a' WHERE id='m'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute("UPDATE agent_messages SET body='changed' WHERE id='m'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute("DELETE FROM agent_messages WHERE id='m'")


def test_the_capture_declarations_are_the_remaining_noncontiguous_blocks_of_the_owner():
    assert [len(block) for block in (CHANGE_SCHEMA, CAPTURE_SCHEMA, PRINCIPAL_CAPTURE_SCHEMA, RELATIONSHIP_CAPTURE_SCHEMA)] == [1, 12, 3, 3]
    assert CHANGE_SCHEMA[0].startswith("CREATE TABLE inbox_changes (")
    names = [statement.split()[2] for block in (CAPTURE_SCHEMA, PRINCIPAL_CAPTURE_SCHEMA, RELATIONSHIP_CAPTURE_SCHEMA) for statement in block]
    assert names == [f"inbox_capture_{table}_{action}" for table in ("signoffs", "signoff_attention", "agent_profiles", "business_memberships")
                     for action in ("insert", "update", "delete")] + [f"inbox_capture_principals_{action}" for action in ("insert", "update", "delete")] + [
                         f"inbox_capture_relationships_{action}" for action in ("delete", "insert", "update")]
    connection = sqlite3.connect(":memory:")
    for table in ("signoffs", "signoff_attention", "agent_profiles", "business_memberships", "principals", "relationships"):
        connection.execute(f"CREATE TABLE {table}(id)")
    for statement in (*CHANGE_SCHEMA, *CAPTURE_SCHEMA, *PRINCIPAL_CAPTURE_SCHEMA, *RELATIONSHIP_CAPTURE_SCHEMA):
        connection.execute(statement)
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0] == 18


def test_history_and_catalog_declarations_are_data_with_the_deployed_names():
    assert HISTORY_CONTRIBUTION.owner == "host.inbox"
    assert [(export.table, export.key, tuple(export.columns)) for export in HISTORY_CONTRIBUTION.exports] == [
        ("inbox_events", ("id",), ("id", "business_id", "source_type", "source_id", "source_revision", "actor_id", "event_type", "summary", "created_at")),
        ("inbox_deliveries", ("agent_id", "event_id"), ("agent_id", "event_id", "acknowledged_at", "outcome", "required")),
        ("inbox_subscriptions", ("id",), ("id", "agent_id", "business_id", "source_type", "source_id", "include_related", "created_at"))]
    assert ITEM_SOURCES == ("task", "tasks", "action", "action_logs", "signoff", "signoffs", "table", "data_tables", "document", "documents",
                            "routine", "routines") and MARKS == ",".join("?" * 12)
    assert [export.selector.__name__ for export in HISTORY_CONTRIBUTION.exports] == ["item_inbox", "item_deliveries", "item_subscriptions"]
    assert [(entry.name, entry.effect_kind) for entry in MUTATION_ENTRIES] == [(name, "records") for name in (
        "inbox_acknowledge", "inbox_subscribe", "inbox_unsubscribe", "agents_message_send", "broadcasts_create", "broadcasts_update",
        "broadcasts_pause", "broadcasts_resume", "broadcasts_cancel", "broadcasts_send_now")]
    assert [(entry.name, entry.effect_kind) for entry in READ_ENTRIES + BROADCAST_READ_ENTRIES] == [(name, "read") for name in (
        "inbox_list", "inbox_subscriptions_list", "agents_messages_list", "agents_message_recipients", "broadcasts_list", "broadcasts_get",
        "broadcast_recipients")]
    assert {entry.name for entry in (*MUTATION_ENTRIES, *READ_ENTRIES, *BROADCAST_READ_ENTRIES)} == INBOX_OPERATIONS


def test_the_service_and_the_finalizer_require_their_host_adapters_and_have_no_default():
    host = SimpleNamespace(**{name: (lambda *a, **k: None) for name in INBOX_HOST_METHODS})
    sources = SimpleNamespace(subscribable=("widgets",), item_types={}, get=lambda key: None)
    arguments = dict(items=None, related=None, host=host, sources=sources, source_types=("tasks", "documents"),
                     item_types={"documents": "material"}, message_factory=lambda service: SimpleNamespace(service=service))
    with pytest.raises(TypeError):
        InboxService(SimpleNamespace(), **{key: value for key, value in arguments.items() if key != "host"})
    with pytest.raises(TypeError, match="active_event_claim"):
        InboxService(SimpleNamespace(), **{**arguments, "host": SimpleNamespace(**{n: host.__dict__[n] for n in INBOX_HOST_METHODS if n != "active_event_claim"})})
    with pytest.raises(TypeError, match="message factory"):
        InboxService(SimpleNamespace(), **{**arguments, "message_factory": None})
    service = InboxService(SimpleNamespace(), **arguments)
    assert service.subscribable == ("tasks", "documents", "widgets") and service.messages.service is service and service.host is host
    assert service.visible_types == {"documents": "material", **THREAD_SOURCES} == visible_types({"documents": "material"})
    assert INBOX_HOST_METHODS == ("managed_subject", "business_member", "private_visible", "integration_visible", "event_visible",
                                  "decorate_before_extensions", "decorate_after_extensions", "active_event_claim", "augment_subscriptions")
    routing = SimpleNamespace(**{name: (lambda *a, **k: None) for name in ROUTING_METHODS})
    with pytest.raises(TypeError):
        EventFinalizer(sources=sources, item_types={}, feed_only=frozenset())
    with pytest.raises(TypeError, match="recipient_allowed"):
        EventFinalizer(sources=sources, item_types={}, feed_only=frozenset(),
                       host=SimpleNamespace(**{n: routing.__dict__[n] for n in ROUTING_METHODS if n != "recipient_allowed"}))
    finalizer = EventFinalizer(sources=sources, item_types={"documents": "material", "signoffs": "signoff"}, feed_only=frozenset(), host=routing)
    assert finalizer.resource_types == {"signoff": "signoffs"} and ROUTING_METHODS == (
        "begin_batch", "coalesced_creation", "responsible", "early_targets", "additional_targets", "terminal_signoff", "augment_direct",
        "presentation", "augment_recipients", "recipient_allowed", "presentation_summary")


def test_the_pure_helpers_need_no_extension_sources():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE relationships(business_id TEXT, from_type TEXT, from_id TEXT, to_type TEXT, to_id TEXT, role TEXT);
        CREATE TABLE tasks(business_id TEXT, id TEXT, title TEXT);
        INSERT INTO tasks VALUES('b','t','Task title');
        INSERT INTO relationships VALUES('b','material','m','agent','a','assigned_to'),('b','material','m','agent','z','blocked_by');
    """)
    uow = SimpleNamespace(connection=connection)
    assert row_for(uow, "tasks", "b", "t") == row_for(uow, "tasks", "b", "t", None) == {"business_id": "b", "id": "t", "title": "Task title"}
    assert row_for(uow, "tasks", "b", "missing") is None
    handler = SimpleNamespace(row=lambda uow, business, identity: {"id": identity, "via": "handler"})
    assert row_for(uow, "widgets", "b", "w", SimpleNamespace(get=lambda key: handler)) == {"id": "w", "via": "handler"}
    assert row_for(uow, "tasks", "b", "t", SimpleNamespace(get=lambda key: SimpleNamespace(row=None))) is None
    assert assigned(uow, "b", "material", "m") == {"a"}
    assert source_summary({"title": "x" * 600}, "tasks") == "x" * 500 and source_summary(None, "data_tables") == "data table"


def test_the_standing_policy_allows_exactly_the_inbox_operations():
    assert len(INBOX_OPERATIONS) == 17
    assert {"agents_message_send", "broadcasts_create", "inbox_list"} <= INBOX_OPERATIONS
    decision = core_default("agents_message_send")
    assert (decision.mode, decision.provenance) == ("allow", "core_default")
    assert core_default("tasks_create") is None


def test_related_work_is_one_hop_and_follows_the_group_task():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE tasks(business_id TEXT, id TEXT, parent_id TEXT);
        CREATE TABLE relationships(business_id TEXT, from_type TEXT, from_id TEXT, to_type TEXT, to_id TEXT, role TEXT);
        CREATE TABLE thread_follows(business_id TEXT, item_type TEXT, item_id TEXT, principal_id TEXT, following INTEGER);
        CREATE TABLE inbox_subscriptions(business_id TEXT, agent_id TEXT, source_type TEXT, source_id TEXT, include_related INTEGER);
        INSERT INTO tasks VALUES('b','child','group'),('b','grandchild','child'),('b','assigned','other');
        INSERT INTO relationships VALUES('b','task','group','task','linked','relates_to'),
            ('b','task','assigned','task','group','blocked_by'),('b','task','linked','task','far','relates_to');
        INSERT INTO thread_follows VALUES('b','task','group','agent',1);
        INSERT INTO inbox_subscriptions VALUES('b','agent','tasks','group',1),('b','quiet','tasks','group',1);
    """)
    uow = SimpleNamespace(connection=connection)
    assert related.related(uow, "b", "group", "task", "child")
    assert related.related(uow, "b", "group", "task", "linked")
    assert not related.related(uow, "b", "group", "task", "grandchild")
    assert not related.related(uow, "b", "group", "task", "far")
    assert not related.related(uow, "b", "group", "task", "assigned")
    assert not related.related(uow, "b", "group", "task", "group")
    assert related.subscribers(uow, "b", "task", "child") == {"agent"}
    assert related.EXCLUDED_ROLES == ("assigned_to", "blocked_by")
