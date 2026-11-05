"""Historical references across Task relocations: Global resolution, the pinned reference namespace and record-journey discovery.

The immutable discussion stores references relative to the scope the note was written in. These tests prove the resolver finds a
Global Task that moved, that the first relocation pins the old namespace beside (never inside) the note, that nothing but a
guarded relocation can write that pin, that references to other records keep resolving in their original scope after the holder
moves, and that a CRM record's development discovery follows the record through its moves without borrowing an unrelated Task's.
Synthetic data only."""

from __future__ import annotations

import sqlite3

import pytest

from crm_support import CRMHost
from operational_support import BUSINESS, HUMAN, OTHER, STRANGER
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import InvocationContext, Principal
from qorqut_operations.tasks.relocation import journey_spans, moved_location
from test_task_relocation import THIRD, comment, move


class ReadsHost(CRMHost):
    """The CRM host with the one read operation a ``topic`` reference is authorized through."""

    def extend(self, registry):
        super().extend(registry)
        registry.register(OperationDescriptor(
            "topics_get", {"type": "object", "properties": {"topic_id": {"type": "string"}}, "required": ["topic_id"],
                           "additionalProperties": False}, effect_kind="read", supports_signoff=False,
            read=lambda ctx, args, options, uow: {"row": uow.repository("topics", business_id=ctx.business_id).get(args["topic_id"], required=True)}))


@pytest.fixture
def host(tmp_path):
    host = ReadsHost(tmp_path)
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": THIRD, "slug": "third", "name": "third", "workspace": str(tmp_path / "third"),
                                             "hq_path": str(tmp_path / "third" / "hq")})
        uow.repository("business_memberships", business_id=THIRD).insert({"principal_id": HUMAN, "is_owner": True})
    return host


def task_notes(host, task_id, ctx):
    arguments = {"task_id": task_id, **({"scope": "global"} if ctx.business_id is None else {})}
    return host.read("tasks_notes_list", arguments, ctx)["notes"]


def note_with(host, task_id, text, refs, ctx):
    row = host.task(task_id, ctx.business_id)
    arguments = {"task_id": task_id, "text": text, "note_refs": refs, **({"scope": "global"} if ctx.business_id is None else {})}
    return host.data("tasks_comment", arguments, ctx, expected_revision=row["revision"])


def namespaces(host, task_id):
    return [(row["kind"], row["reference_namespace"]) for row in host.query(
        "SELECT kind, reference_namespace FROM thread_messages WHERE item_type='task' AND item_id=? ORDER BY created_at, id", task_id)]


# -- Global is a place a Task can leave, like any project ---------------------------------------------------------------

def test_a_global_task_that_moved_resolves_from_global_through_every_later_move(host):
    ctx = host.owner(None)
    task = host.create_task("Global wanderer", ctx)
    identity = task["id"]
    move(host, None, BUSINESS, [identity], ctx=ctx)
    with host.database.read() as uow:
        assert moved_location(uow, None, identity) == BUSINESS
    read = host.invoke("tasks_get", {"task_id": identity, "scope": "global"}, ctx)
    assert read.status == "error" and read.error["code"] == "record_moved"
    assert read.error["details"]["task"] == {"type": "task", "id": identity, "business_id": BUSINESS}
    # old-scope mutations are refused the same way and write nothing into either scope
    before = host.count("thread_messages", "item_id=?", identity), host.task(identity, BUSINESS)["revision"]
    for operation, arguments, options in (("tasks_update", {"task_id": identity, "scope": "global", "changes": {"title": "late"}},
                                           {"expected_revision": 1}),
                                          ("tasks_comment", {"task_id": identity, "scope": "global", "text": "late"}, {}),
                                          ("tasks_follow", {"task_id": identity, "scope": "global", "following": True}, {})):
        result = host.invoke(operation, arguments, ctx, **options)
        assert result.status == "error" and result.error["code"] == "record_moved", (operation, result.error)
    assert (host.count("thread_messages", "item_id=?", identity), host.task(identity, BUSINESS)["revision"]) == before
    # Global -> A -> B: both old scopes name the current one
    move(host, BUSINESS, OTHER, [identity])
    with host.database.read() as uow:
        assert moved_location(uow, None, identity) == OTHER and moved_location(uow, BUSINESS, identity) == OTHER
        assert moved_location(uow, OTHER, identity) is None
    read = host.invoke("tasks_get", {"task_id": identity, "scope": "global"}, ctx)
    assert read.error["code"] == "record_moved" and read.error["details"]["task"]["business_id"] == OTHER
    # a caller who may not read the destination learns nothing: no moved answer and no location detail
    outsider = InvocationContext(Principal(STRANGER, "human"), business_id=None, allowed_business_ids=frozenset(),
                                 global_capabilities=frozenset({"read_global_context"}), source="dashboard")
    refused = host.invoke("tasks_get", {"task_id": identity, "scope": "global"}, outsider)
    assert refused.status == "error" and refused.error["code"] != "record_moved" and not refused.error.get("details")
    # a current Global Task with the same bare id always wins over the history
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=None).insert({"id": identity, "title": "Newcomer", "status": "open", "created_by": HUMAN,
                                                         "created_at": "2026-11-01T00:00:00.000000Z"})
    with host.database.read() as uow:
        assert moved_location(uow, None, identity) is None
    assert host.read("tasks_get", {"task_id": identity, "scope": "global"}, ctx)["task"]["title"] == "Newcomer"


# -- the first relocation pins where the references were written ---------------------------------------------------------

def test_the_first_relocation_pins_the_old_namespace_beside_the_note_and_it_never_changes_again(host):
    ctx = host.owner()
    task = host.create_task("Holder", ctx)
    identity = task["id"]
    comment(host, identity, "first", ctx)
    assert {pin for _, pin in namespaces(host, identity)} == {None}
    stored = host.query("SELECT id, body, refs_json, created_at FROM thread_messages WHERE item_id=? ORDER BY id", identity)
    move(host, BUSINESS, OTHER, [identity])
    assert {pin for _, pin in namespaces(host, identity)} == {"business:" + BUSINESS}
    assert host.query("SELECT id, body, refs_json, created_at FROM thread_messages WHERE item_id=? ORDER BY id", identity) == stored
    host.later(seconds=1)
    comment(host, identity, "second, written after the move", host.owner(OTHER))
    assert [pin for _, pin in namespaces(host, identity)][-1] is None                                   # written in OTHER: unpinned
    move(host, OTHER, THIRD, [identity], ctx=host.owner(OTHER))
    # an existing pin is kept; the note written in OTHER is pinned to OTHER by the second move
    assert [pin for _, pin in namespaces(host, identity)] == ["business:" + BUSINESS, "business:" + OTHER]
    # a Global Task pins the Global namespace
    global_task = host.create_task("Global holder", host.owner(None))
    comment(host, global_task["id"], "note", host.coordinator())
    move(host, None, BUSINESS, [global_task["id"]], ctx=host.owner(None))
    assert {pin for _, pin in namespaces(host, global_task["id"])} == {"global"}


def test_only_a_guarded_relocation_may_fill_the_pin_and_only_with_the_exact_old_namespace(host):
    ctx = host.owner()
    task = host.create_task("Holder", ctx)
    comment(host, task["id"], "note", ctx)
    for sql in ("UPDATE thread_messages SET reference_namespace='business:x' WHERE item_id=?",
                "UPDATE thread_messages SET reference_namespace='global' WHERE item_id=?"):
        with host.database.transaction() as uow, pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute(sql, (task["id"],))
    with host.database.transaction() as uow:
        with uow.relocating(BUSINESS, OTHER, [task["id"]]):
            for pin in ("business:" + OTHER, "global", "business:not-this-one", None):
                with pytest.raises(sqlite3.DatabaseError):
                    uow.connection.execute("UPDATE thread_messages SET business_id=?, reference_namespace=? WHERE item_id=?",
                                           (OTHER, pin, task["id"]))
            with pytest.raises(sqlite3.DatabaseError):                               # moving without recording the old namespace
                uow.connection.execute("UPDATE thread_messages SET business_id=? WHERE item_id=?", (OTHER, task["id"]))
            assert uow.connection.execute("UPDATE thread_messages SET business_id=?, reference_namespace=? WHERE item_id=?",
                                          (OTHER, "business:" + BUSINESS, task["id"])).rowcount >= 1
    # once pinned it cannot be changed or cleared by anyone, guard or not
    for sql in ("UPDATE thread_messages SET reference_namespace=NULL WHERE item_id=?",
                "UPDATE thread_messages SET reference_namespace='global' WHERE item_id=?"):
        with host.database.transaction() as uow, pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute(sql, (task["id"],))
    with host.database.transaction() as uow:
        with uow.relocating(OTHER, THIRD, [task["id"]]):
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE thread_messages SET business_id=?, reference_namespace=? WHERE item_id=?",
                                       (THIRD, "business:" + OTHER, task["id"]))
    # the column rejects text that is not a namespace
    with host.database.transaction() as uow, pytest.raises(sqlite3.DatabaseError):
        uow.connection.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,created_at,reference_namespace) "
                               "VALUES (?, 'bad-pin', 'task', ?, 'system', 'x', ?, 'now', 'nowhere')", (BUSINESS, task["id"], HUMAN))


# -- references to other records keep their original scope ---------------------------------------------------------------

def test_a_reference_to_another_record_keeps_resolving_where_it_was_written_after_the_holder_moves(host):
    ctx = host.owner()
    holder = host.create_task("Holder that moves", ctx)
    topic = host.topic("11111111-0000-4000-8000-00000000aaaa", "Source topic", BUSINESS)
    note_with(host, holder["id"], "About the topic", [topic], ctx)
    before = task_notes(host, holder["id"], ctx)[-1]["refs"]
    # a readable scope-qualified reference names its place even in the reader's own scope; an unavailable one names none
    assert before[0]["label"] == "Source topic" and before[0]["business_id"] == BUSINESS and before[0]["business_slug"] == "first"
    # an unrelated topic with the same bare id exists in the destination
    host.topic("11111111-0000-4000-8000-00000000aaaa", "Unrelated destination topic", OTHER)
    move(host, BUSINESS, OTHER, [holder["id"]])
    shown = task_notes(host, holder["id"], host.owner(OTHER))[-1]["refs"][0]
    assert shown["label"] == "Source topic" and shown["business_id"] == BUSINESS and shown["business_slug"] == "first"
    # a caller who cannot read the original scope sees no label and no location
    other_only = InvocationContext(Principal(STRANGER, "human"), business_id=OTHER, allowed_business_ids=frozenset({OTHER}), source="dashboard")
    with host.database.transaction() as uow:
        uow.repository("business_memberships", business_id=OTHER).insert({"principal_id": STRANGER})
    unreadable = task_notes(host, holder["id"], other_only)[-1]["refs"][0]
    assert unreadable == {"unavailable": True}


# -- CRM discovery follows the record, not the bare id -----------------------------------------------------------------

def test_crm_developments_written_before_a_move_stay_with_the_record_and_a_later_same_id_task_does_not_borrow_them(host):
    ctx = host.owner()
    table = host.crm_table("Pipeline", ctx)
    customer = host.create_task("Acme", ctx, task_type="customer", customer_kind="company")
    host.add(table, customer, ctx=ctx)
    identity = customer["id"]

    def memberships(scope):
        with host.database.read() as uow:
            page = host.crm.local_activity(host.owner(scope), identity, {} if scope else {"scope": "global"}, uow)
        return [item for item in page["items"] if item["origin"] == "crm_membership"]

    assert len(memberships(BUSINESS)) == 1
    host.later(seconds=5)
    move(host, BUSINESS, OTHER, [identity])
    shown = memberships(OTHER)
    assert len(shown) == 1 and shown[0]["change"]["operation"] == "added"                    # the development names the old scope
    with host.database.read() as uow:
        spans = journey_spans(uow, OTHER, identity)
    assert [(scope, until) for scope, _, until in spans][0] == (OTHER, None) and spans[1][0] == BUSINESS and spans[1][2]
    # another, different Task takes the same bare id in the scope the record left and is added to the Table
    host.later(seconds=5)
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=BUSINESS).insert({"id": identity, "title": "Acme again", "status": "open", "created_by": HUMAN,
                                                             "created_at": host.stamp(),
                                                             "fields_json": {"$task": {"task_type": "customer", "customer_kind": "company"}}})
    newcomer = {**host.task(identity, BUSINESS), "business_id": BUSINESS}
    host.add(host.get(table), newcomer, ctx=ctx)
    assert len(memberships(OTHER)) == 1 and len(memberships(BUSINESS)) == 1                  # each record sees only its own development
    assert memberships(OTHER)[0]["id"] != memberships(BUSINESS)[0]["id"]
