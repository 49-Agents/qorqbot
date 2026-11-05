"""Installed synthetic evidence for the retained operations composed onto the Operations engine.

One real synthetic host (the retained Signoff service, background service, program registry, background authority,
inbox/Chat domains and the owner attention projection) proves what the engine extraction must not change:

* a protected retained-domain mutation proposed by a registered global agent is applied only after the owner's exact decision,
  once, and its committed outcome is an available inbox row and a read-only row selection for the history exporter;
* the native Chat reply, read-position and attention-acknowledgment metadata keep their separate meanings.

Everything is synthetic: a tmp instance, an injected clock and rows the test appends.
No model, provider, native session, thread, network or operational data is used."""

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk import attention_queue
from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.native_chat import store as native
from bazarlyk.native_chat.projection import Lineage
from bazarlyk.reply_notifications import replies
from host_foundation_support import (build_instance, create_registered_global, create_worker, register_sources, set_agent_overrides,
                                     synthetic_ordubashy)

class Clock:
    """The injected background clock: deferral is decided by this time, never by sleeping."""

    def __init__(self):
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    business = ids["first"]
    register_sources(instance, business, "first")
    # The role package is not the subject (its source is owned elsewhere); the proposer reads its own inbox and Signoff.
    synthetic_ordubashy(instance, allow=("inbox_list", "signoffs_get"))
    # Businesses have no resident agents: Ordubashy proposes in the first business, under a Signoff floor the owner sets
    # explicitly for this generic protected transaction.
    agent = create_registered_global(instance, "ordubashy")
    set_agent_overrides(instance, agent, {"tasks_create": "signoff"}, [business])
    runtime, clock = instance.runtime, Clock()
    background = attach_background(runtime, runtime.programs, clock=clock)
    runtime.freeze_registry()
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [business])
    return SimpleNamespace(instance=instance, runtime=runtime, business=business, other=ids["second"], agent=agent,
                           clock=clock, background=background, owner=instance.human(),
                           agent_context=runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=business))


def read(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def jobs(host):
    return read(host, "SELECT id,kind,state,attempts,next_retry_at,principal_id,result_json FROM jobs ORDER BY created_at,rowid")


def propose(host, name="Widget"):
    """The agent's protected ``tasks_create``, returning the owner's current binding of the proposal."""
    result = host.runtime.application.invoke(host.agent_context, "tasks_create", {"title": name},
                                             InvocationOptions(idempotency_key=new_id()))
    assert result.status == "signoff_required", result.error
    assert read(host, "SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0
    pending = host.instance.invoke(host.owner, "signoffs_list", {})
    signoff_id = plain(pending.data)["items"][0]["id"]
    return plain(host.instance.invoke(host.owner, "signoffs_get", {"signoff_id": signoff_id}).data)["signoff"]


def decide(host, signoff, *, context=None, decision="approve", key=None, payload_hash=None):
    return host.runtime.signoffs.review(
        context or host.owner, signoff["id"], decision=decision, proposal_revision=signoff["proposal_revision"],
        payload_hash=payload_hash or signoff["payload_hash"], expected_status_revision=signoff["status_revision"],
        idempotency_key=key or new_id())


def test_a_protected_proposal_is_applied_once_after_the_owners_exact_decision(host):
    signoff = propose(host)
    assert signoff["state"] == "pending"

    # A binding the owner did not see, and the proposing agent itself, can decide nothing.
    stale = decide(host, signoff, payload_hash="0" * 64)
    assert stale.status == "error" and stale.error["code"] == "stale_signoff"
    forbidden = decide(host, signoff, context=host.agent_context)
    assert forbidden.status == "error" and forbidden.error["code"] == "forbidden"
    assert read(host, "SELECT state FROM signoffs")[0]["state"] == "pending"
    assert read(host, "SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0

    key = new_id()
    approved = decide(host, signoff, key=key)
    assert approved.status == "accepted", approved.error
    # Approval prepares the exact change but does not run it inline: nothing is applied until the executor runs.
    assert read(host, "SELECT state FROM signoffs")[0]["state"] == "approved"
    assert read(host, "SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 0

    assert host.runtime.signoffs.run_pending() == 1
    tasks = read(host, "SELECT title,business_id FROM tasks")
    assert tasks == [{"title": "Widget", "business_id": host.business}]
    applied = read(host, "SELECT state,applied_at FROM signoffs")[0]
    assert applied["state"] == "applied" and applied["applied_at"]

    # Replaying the decision or running the executor again applies nothing a second time.
    replay = decide(host, signoff, key=key)
    assert replay.status == "ok" and replay.data["replayed"] is True
    assert host.runtime.signoffs.run_pending() == 0
    assert read(host, "SELECT COUNT(*) AS n FROM tasks")[0]["n"] == 1
    assert [row["state"] for row in read(host, "SELECT state FROM apply_receipts WHERE signoff_id=?", signoff["id"])] == ["applied"]

    # The committed outcome is an available inbox row for the proposer, through the retained inbox read.
    inbox = host.runtime.application.invoke(host.agent_context, "inbox_list", {}, InvocationOptions())
    assert inbox.status == "ok", inbox.error
    outcomes = [item for item in plain(inbox.data)["items"] if item["event_type"] == "action_outcome"]
    assert [(item["source_type"], item["source_id"]) for item in outcomes] == [("signoffs", signoff["id"])]


# The explicit, reviewable selection a history exporter reads: named tables and columns, no secrets, no blanket dump.
OUTCOME_SELECTION = {
    "signoffs": ("id", "state", "proposal_revision", "payload_hash", "status_revision", "applied_at"),
    "inbox_events": ("id", "business_id", "source_type", "source_id", "source_revision", "event_type", "summary"),
    "tasks": ("id", "business_id", "title", "revision"),
}


def select_outcome(connection, signoff_id):
    where = {"signoffs": "id=?", "inbox_events": "source_id=?", "tasks": "1=1"}
    return {table: [dict(row) for row in connection.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {where[table]} ORDER BY id",
        () if table == "tasks" else (signoff_id,))] for table, columns in OUTCOME_SELECTION.items()}


def test_the_committed_outcome_is_readable_by_a_read_only_row_selection(host):
    signoff = propose(host)
    assert decide(host, signoff).status == "accepted"
    assert host.runtime.signoffs.run_pending() == 1

    # An independent read-only connection (what an exporter would hold) sees exactly the committed rows.
    connection = sqlite3.connect(f"file:{host.instance.paths.database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        selected = select_outcome(connection, signoff["id"])
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM tasks")
    finally:
        connection.close()
    assert [row["state"] for row in selected["signoffs"]] == ["applied"]
    assert [row["event_type"] for row in selected["inbox_events"]] == ["response_submitted", "action_outcome"]
    assert [row["title"] for row in selected["tasks"]] == ["Widget"]
    assert all(set(row) == set(OUTCOME_SELECTION[table]) for table, rows in selected.items() for row in rows)
    # The service's own read handle is query-only too: selecting never writes.
    with host.instance.database.read() as uow:
        assert select_outcome(uow.connection, signoff["id"]) == selected
        with pytest.raises(sqlite3.OperationalError):
            uow.connection.execute("DELETE FROM tasks")


def test_chat_reply_read_and_attention_metadata_stay_independent(host):
    instance, runtime = host.instance, host.runtime
    owner = InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    # Two registered global agents (businesses have no resident agents): a worker and the fixture's Ordubashy.
    global_agent = create_worker(instance, "colleague")
    assert replies(runtime, owner)["items"] == []

    def append(agent, text):
        # What the backend's archive indexer stores for a completed reply, appended with no Chat view open.
        with instance.database.transaction() as uow:
            conversation = new_id()
            return native.insert_reply(uow.connection, lineage=Lineage(agent, "claude", conversation, f"claude:{conversation}"),
                                       kind="native_id", key=new_id(), session_id=None, receipt_id="synthetic-receipt", offset=0,
                                       length=len(text),
                                       completed_at=datetime.now(timezone.utc).isoformat(), preview=text)

    first_global, other_reply = append(global_agent, "Global reply"), append(host.agent, "Ordubashy reply")
    items = {item["agent_id"]: item for item in replies(runtime, owner)["items"]}
    # The replies arrive with Chat closed: stable identities derived from the indexed replies, each an attention card.
    assert items[global_agent]["id"] == "gchat:" + first_global["id"] and items[global_agent]["nav_only"] is False
    assert items[host.agent]["id"] == "gchat:" + other_reply["id"] and items[host.agent]["nav_only"] is False
    queue = attention_queue.queue(runtime, owner)["items"]
    assert {item["id"] for item in queue} == {items[global_agent]["id"], items[host.agent]["id"]}

    # Acknowledging one attention card is its own durable preference: that reply stays unread in Chat.
    assert [item["id"] for item in attention_queue.queue(runtime, owner, {"viewed": [items[global_agent]["id"]]})["items"]] == [
        items[host.agent]["id"]]
    assert items[global_agent]["id"] in {item["id"] for item in replies(runtime, owner)["items"]}

    # Reading through the rendered completion boundary clears only that agent's indication, and only up to it.
    with instance.database.transaction() as uow:
        assert native.mark_read(uow.connection, host.agent, instance.owner, other_reply["completion_sequence"]) == other_reply["completion_sequence"]
    assert [item["agent_id"] for item in replies(runtime, owner)["items"]] == [global_agent]
    assert attention_queue.queue(runtime, owner)["items"] == []

    # A later reply arriving concurrently with a stale read stays unread and gets its own identity.
    second_global = append(global_agent, "Second global reply")
    with instance.database.transaction() as uow:
        native.mark_read(uow.connection, global_agent, instance.owner, first_global["completion_sequence"])
    current = replies(runtime, owner)["items"]
    assert [item["id"] for item in current] == ["gchat:" + second_global["id"]]
    assert current[0]["sequence"] == second_global["completion_sequence"] > first_global["completion_sequence"]
    assert [item["id"] for item in attention_queue.queue(runtime, owner)["items"]] == ["gchat:" + second_global["id"]]

    # Both the read position and the attention acknowledgment are durable: a fresh read of the stores keeps them.
    assert attention_queue.viewed(runtime.paths.runtime, instance.owner) == {items[global_agent]["id"]}
    with instance.database.read() as uow:
        assert native.last_read(uow.connection, global_agent, instance.owner) == first_global["completion_sequence"]
        assert native.last_read(uow.connection, host.agent, instance.owner) == other_reply["completion_sequence"]
