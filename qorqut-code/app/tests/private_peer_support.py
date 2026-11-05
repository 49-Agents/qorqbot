"""Synthetic retired per-worker private-thread data, written by direct SQL into a temporary host database.

The service no longer creates these rows (see ``bazarlyk.inbox.private_conversations``), so a check of how the Ordubashy-only
service treats them, or of the offline cleanup that removes them, plants the stored shapes the earlier releases left. Every
identity, source and text is synthetic and inert: no model, native session, provider, network or operational database is used.

``plant`` builds one coherent store: the real stock host instance (owner, a business, the registered Ordubashy, two global
workers, an ordinary Task), then

* retired conversations of worker A (a business Task) and worker B (a Global Task) with messages, owner reads, the
  ``private_message`` inbox events and deliveries, the forwarded ``private_conversation`` notices Ordubashy received, and the
  saved send / mark-read receipts of both sides;
* the data that must survive: the owner–Ordubashy channel (a null-peer row) with its messages, read position, event, delivery and
  receipts; a row stored with Ordubashy's own explicit UUID; the notices of other forwarding kinds; ordinary agent messages;
  canonical Task activity; unrelated receipts; and retained nudge records that name both a retired and a surviving event.

Texts carry a marker so a check can prove nothing readable leaves an inspection.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace

from qorqut_operations.contracts import InvocationOptions, canonical_hash, new_id, plain

from bazarlyk.inbox.owner_forwarding import NOTICE_SUMMARY, forwarded_id, write_notice
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context,
                                     register_sources, select_coordinator, set_agent_overrides)

MARKER = "SECRET-PRIVATE-TEXT"
PRIVATE_GRANTS = {"owner_conversation_get": "allow", "owner_conversation_send": "allow"}
NOW = "2026-10-01T00:00:00.000000Z"
LEGACY_HEADER = "Original: private Task conversation message "

# The tables whose complete contents a check compares before and after.
DATA_TABLES = ("owner_conversations", "owner_conversation_messages", "owner_conversation_reads", "inbox_events", "inbox_deliveries",
               "agent_messages", "idempotency_receipts", "thread_messages", "inbox_nudge_needs", "inbox_nudge_attempts",
               "inbox_nudge_evidence", "tasks", "principals", "agent_profiles", "businesses")


class Reads:
    """Whole-table reads for an exact before/after comparison, over whatever ``query`` the holder provides. Never printed."""

    def contents(self, tables=DATA_TABLES):
        return {table: self.query(f"SELECT * FROM {table} ORDER BY 1,2") for table in tables}

    def schema(self):
        return self.query("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")

    def sequences(self):
        return dict(self.query("SELECT name,seq FROM sqlite_sequence"))


@dataclass
class Legacy(Reads):
    """A planted store: the live instance plus the identifiers a check asserts on. ``ids`` maps a role name to its identifier."""

    instance: object
    business: str
    owner: str
    coordinator: str
    workers: dict
    ids: dict = field(default_factory=dict)

    @property
    def database(self):
        return self.instance.database

    @property
    def path(self):
        return self.instance.database.path

    def sql(self, statement, *parameters):
        with self.database.transaction() as uow:
            return uow.connection.execute(statement, parameters).lastrowid

    def query(self, statement, *parameters):
        with self.database.read() as uow:
            return [tuple(row) for row in uow.connection.execute(statement, parameters)]

    def call(self, ctx, name, arguments, key=None):
        return self.instance.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key or new_id()))

    # ----- planting -----------------------------------------------------------------------------------------------------

    def task(self, business, title):
        scope = {"scope": "business"} if business else {"scope": "global"}
        context = self.instance.human(business) if business else owner_global_context(self.instance)
        created = self.call(context, "tasks_create", {"title": title, **scope})
        assert created.status == "ok", created.error
        return created.data["task"]["id"]

    def conversation(self, name, *, peer, business, task, kind="task"):
        """A conversation row; ``peer`` None is the legacy owner–Ordubashy channel."""
        identity = new_id()
        base = f"source:{business or 'global'}:{kind}:{task}"
        key = f"peer:{peer}:{base}" if peer else base
        self.sql("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,revision,latest_sequence,"
                 "created_at,updated_at,peer_agent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 identity, key, business, kind, task, "owner_ordubashy", 1, 0, NOW, NOW, peer)
        self.ids[name] = identity
        return identity

    def message(self, name, conversation, *, role, author, text, deliver_to=None, source=None):
        """One immutable message; an owner message of a peer conversation also gets its ``private_message`` event and delivery."""
        identity = new_id()
        sequence = self.sql("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,summary,created_at)"
                            " VALUES(?,?,?,?,?,?,?)", identity, conversation, author, role, f"{MARKER} {text}", None, NOW)
        self.sql("UPDATE owner_conversations SET latest_sequence=? WHERE id=?", sequence, conversation)
        self.ids[name] = identity
        self.ids[name + ":sequence"] = sequence
        if deliver_to:
            event = self.sql("INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,"
                             "visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", source["business_id"], "owner_conversations",
                             conversation, 2, author, "private_message", "Private message",
                             json.dumps({"source": source}), NOW)
            self.sql("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", deliver_to, event)
            self.ids[name + ":event"] = event
        return identity

    def read(self, conversation, principal, sequence):
        self.sql("INSERT INTO owner_conversation_reads(conversation_id,principal_id,last_read_sequence,updated_at) VALUES(?,?,?,?)",
                 conversation, principal, sequence, NOW)

    def legacy_notice(self, name, message, worker, *, manager=None):
        """The forwarded notice the retired forwarding wrote for an owner message to a worker (its exact historical shape)."""
        manager = manager or self.coordinator
        identity = forwarded_id("private_conversation", self.ids[message], manager)
        event = self.sql("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at)"
                         " VALUES(?,?,?,?,?,?,?)", None, "agent_messages", identity, self.owner, "direct_message",
                         NOTICE_SUMMARY, NOW)
        header = (f"Forwarded notice: the owner sent a direct message to the worker {worker} (agent {worker}). This only keeps you "
                  f"informed.\n{LEGACY_HEADER}{self.ids[message]}, accepted {NOW}.\n\nOwner message:\n")
        self.sql("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,"
                 "current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", identity, event, None, self.owner,
                 manager, None, identity, header + f"{MARKER} forwarded", NOW, None, self.owner, manager)
        self.sql("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", manager, event)
        self.ids[name], self.ids[name + ":event"] = identity, event
        return identity

    def receipt(self, name, principal, operation, *, conversation, message, peer, namespace="global", key=None):
        """A saved result of a private-thread mutation, in the shape ``OperationResult.to_dict`` stores."""
        key = key or new_id()
        result = {"request_id": "synthetic", "status": "ok", "warnings": [],
                  "data": {"message": {"id": message}, "conversation": {"id": conversation, "agent_id": peer}}}
        self.sql("INSERT INTO idempotency_receipts(principal_id,target_namespace,operation_name,idempotency_key,input_hash,result_json,"
                 "created_at) VALUES(?,?,?,?,?,?,?)", principal, namespace, operation, key, canonical_hash({"synthetic": name}),
                 json.dumps(result), NOW)
        self.ids[name] = (principal, namespace, operation, key)

    def nudge(self, name, agent, events):
        """A retained, body-free nudge attempt that numbers events (the engine's own append-only record)."""
        identity = f"attempt-{name}-{new_id()}"[:80]
        self.sql("INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,"
                 "progress_json,state,reason,prepared_at,settled_at,outcome_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 identity, agent, 1, self.query("SELECT COALESCE(MAX(sequence),0)+1 FROM inbox_nudge_attempts WHERE agent_id=?", agent)[0][0],
                 "initial", json.dumps({"provider": "synthetic", "conversation_id": "synthetic-conversation"}), "a" * 64,
                 json.dumps(list(events)), "{}", "submitted", None, NOW, NOW, json.dumps({"state": "submitted"}))
        self.sql("INSERT INTO inbox_nudge_evidence(attempt_id,evidence_hash,state,outcome_json,recorded_at) VALUES(?,?,?,?,?)",
                 identity, "b" * 64, "submitted", "{}", NOW)
        self.ids[name] = identity


def plant(tmp_path):
    """Build the synthetic store described above and return it as ``Legacy`` (the instance stays open for service calls)."""
    instance, ids = build_instance(tmp_path, businesses=("first",))
    register_sources(instance, ids["first"], "first")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    workers = {"a": create_worker(instance, "worker-a"), "b": create_worker(instance, "worker-b")}
    for identity in (coordinator, *workers.values()):
        set_agent_overrides(instance, identity, PRIVATE_GRANTS, (ids["first"],))
    instance.runtime.freeze_registry()
    legacy = Legacy(instance, ids["first"], instance.owner, coordinator, workers)
    business, owner = legacy.business, legacy.owner
    a, b = workers["a"], workers["b"]
    legacy.ids.update(task_a=legacy.task(business, "Work for worker A"), task_b=legacy.task(None, "Global work for worker B"),
                      task_c=legacy.task(business, "Work for Ordubashy"), task_d=legacy.task(business, "Work for explicit row"))
    source = {"a": {"type": "task", "id": legacy.ids["task_a"], "business_id": business},
              "b": {"type": "task", "id": legacy.ids["task_b"], "business_id": None},
              "c": {"type": "task", "id": legacy.ids["task_c"], "business_id": business},
              "d": {"type": "task", "id": legacy.ids["task_d"], "business_id": business}}
    # ----- retired worker A (business Task) and worker B (Global Task) -----
    for letter, worker, task, scope in (("a", a, legacy.ids["task_a"], business), ("b", b, legacy.ids["task_b"], None)):
        conversation = legacy.conversation(f"conversation_{letter}", peer=worker, business=scope, task=task)
        legacy.message(f"owner_{letter}1", conversation, role="owner", author=owner, text="owner to worker", deliver_to=worker,
                       source=source[letter])
        legacy.message(f"worker_{letter}1", conversation, role="coordinator", author=worker, text="worker answers")
        legacy.message(f"owner_{letter}2", conversation, role="owner", author=owner, text="owner follows up", deliver_to=worker,
                       source=source[letter])
        legacy.read(conversation, owner, legacy.ids[f"worker_{letter}1:sequence"])
        legacy.legacy_notice(f"notice_{letter}1", f"owner_{letter}1", worker)
        legacy.legacy_notice(f"notice_{letter}2", f"owner_{letter}2", worker)
        namespace = f"business:{scope}" if scope else "global"
        legacy.receipt(f"send_owner_{letter}", owner, "owner_conversation_send", conversation=conversation,
                       message=legacy.ids[f"owner_{letter}1"], peer=worker, namespace=namespace)
        legacy.receipt(f"send_worker_{letter}", worker, "owner_conversation_send", conversation=conversation,
                       message=legacy.ids[f"worker_{letter}1"], peer=worker, namespace=namespace)
        legacy.receipt(f"mark_owner_{letter}", owner, "owner_conversation_mark_read", conversation=conversation,
                       message=None, peer=worker, namespace=namespace)
    # A saved mark-read result that names only the retired peer (no conversation identifier) is retired too.
    legacy.receipt("mark_peer_only", owner, "owner_conversation_mark_read", conversation=None, message=None, peer=b, namespace="global")
    # ----- the owner–Ordubashy channel (null peer) and an explicit-UUID row: both protected -----
    channel = legacy.conversation("conversation_c", peer=None, business=business, task=legacy.ids["task_c"])
    legacy.message("owner_c1", channel, role="owner", author=owner, text="owner to Ordubashy", deliver_to=coordinator, source=source["c"])
    legacy.message("ordubashy_c1", channel, role="coordinator", author=coordinator, text="Ordubashy answers")
    legacy.read(channel, owner, legacy.ids["ordubashy_c1:sequence"])
    legacy.receipt("send_owner_c", owner, "owner_conversation_send", conversation=channel, message=legacy.ids["owner_c1"], peer=None,
                   namespace=f"business:{business}")
    legacy.receipt("mark_owner_c", owner, "owner_conversation_mark_read", conversation=channel, message=None, peer=None,
                   namespace=f"business:{business}")
    explicit = legacy.conversation("conversation_d", peer=coordinator, business=business, task=legacy.ids["task_d"])
    legacy.message("owner_d1", explicit, role="owner", author=owner, text="owner to explicit Ordubashy", deliver_to=coordinator,
                   source=source["d"])
    legacy.read(explicit, owner, 0)
    legacy.receipt("send_owner_d", owner, "owner_conversation_send", conversation=explicit, message=legacy.ids["owner_d1"],
                   peer=coordinator, namespace=f"business:{business}")
    # ----- unrelated data -----
    # A notice of another forwarding kind and an ordinary worker-to-Ordubashy message: forwarded copies of other things stay.
    with legacy.database.transaction() as uow:
        chat = write_notice(uow.connection, kind="chat", source_id=new_id(), sender_id=owner, manager_id=coordinator, worker_id=a,
                            body=f"{MARKER} chat", accepted_at=NOW)
    legacy.ids["chat_notice"] = chat
    legacy.ids["chat_notice:event"] = legacy.query("SELECT event_id FROM agent_messages WHERE id=?", chat)[0][0]
    ordinary_event = legacy.sql("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
                                "VALUES(?,?,?,?,?,?,?)", None, "agent_messages", "ordinary", a, "direct_message", "Agent message", NOW)
    legacy.sql("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,summary,"
               "current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", "ordinary", ordinary_event, None, a,
               coordinator, None, "ordinary", f"{MARKER} private note between agents", NOW, None, a, coordinator)
    legacy.sql("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", coordinator, ordinary_event)
    legacy.ids["ordinary_event"] = ordinary_event
    # The newest event of the store belongs to a retired conversation: a later allocation must never reuse its number.
    legacy.message("owner_b3", legacy.ids["conversation_b"], role="owner", author=owner, text="owner's last message to worker B",
                   deliver_to=b, source=source["b"])
    legacy.legacy_notice("notice_b3", "owner_b3", b)
    # An unrelated receipt.
    legacy.receipt("unrelated", owner, "tasks_create", conversation=None, message=None, peer=None, namespace=f"business:{business}")
    # Retained nudge records: one attempt numbering only retired events, one numbering retired and surviving ones.
    legacy.nudge("only_retired", a, [legacy.ids["owner_a1:event"], legacy.ids["owner_a2:event"]])
    legacy.nudge("mixed", coordinator, [legacy.ids["notice_a1:event"], legacy.ids["ordinary_event"], legacy.ids["owner_c1:event"]])
    legacy.nudge("surviving_only", coordinator, [legacy.ids["owner_c1:event"]])
    return legacy


class Store(Reads):
    """A throwaway file copy of a planted store, reached by plain connections like a maintenance run would: no runtime."""

    def __init__(self, path, legacy_ids, *, owner, coordinator, workers, business):
        self.path, self.ids = path, dict(legacy_ids)
        self.owner, self.coordinator, self.workers, self.business = owner, coordinator, workers, business

    def connect(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function("inbox_actor", 0, lambda: None)
        return connection

    def query(self, statement, *parameters):
        connection = self.connect()
        try:
            return [tuple(row) for row in connection.execute(statement, parameters)]
        finally:
            connection.close()

    def execute(self, statement, *parameters):
        connection = self.connect()
        try:
            return connection.execute(statement, parameters).lastrowid
        finally:
            connection.close()


def copy_store(legacy, destination):
    """Copy the planted database consistently to ``destination`` and return it as a ``Store``."""
    source, target = sqlite3.connect(legacy.path), sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return Store(destination, legacy.ids, owner=legacy.owner, coordinator=legacy.coordinator, workers=legacy.workers,
                 business=legacy.business)


def retired_request_hash(legacy, ctx, operation, arguments):
    """The idempotency input hash the engine computes for ``operation`` and ``arguments`` from ``ctx``, built from the engine's own
    argument binding, context selection and namespace resolution.

    A control checks it against a receipt the engine really stored before it is used to plant the receipt of a retired request."""
    application = legacy.instance.runtime.application
    descriptor = application.registry.get(operation)
    bound = application.registry.bind_arguments(operation, arguments)
    options = InvocationOptions(idempotency_key=None)
    with legacy.database.read() as uow:
        context = application._options_context(application.current_context(ctx, uow), options)
        namespace = application.namespace(context, descriptor, bound)
    if namespace == "global":
        context = replace(context, business_id=None, review_business_id=None)
    return canonical_hash({"operation": descriptor.name, "version": descriptor.version, "arguments": bound,
                           "options": {**plain(options), "business_id": context.business_id,
                                       "review_business_id": context.review_business_id, "idempotency_key": None}}), namespace
