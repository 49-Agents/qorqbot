"""Task relocation between owning scopes: identity kept, everything that belongs to a Task travels, nothing else changes.

Synthetic data only. The unit of work, triggers, guard and helper are the real engine parts; the host adapters of the product
(private conversations, Focus) have their own tests in the host."""

from __future__ import annotations

import json
import sqlite3

import pytest

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.relocation import RelocationGuard
from qorqut_operations.tasks.relocation import AdapterReport, TaskRelocation, moved_location
from crm_support import CRMHost, ref_of, email_task
from operational_support import AGENT, BUSINESS, HUMAN, OTHER, STRANGER

THIRD = "00000000-0000-4000-8000-000000000009"


@pytest.fixture
def host(tmp_path):
    host = CRMHost(tmp_path)
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": THIRD, "slug": "third", "name": "third", "workspace": str(tmp_path / "third"),
                                             "hq_path": str(tmp_path / "third" / "hq")})
        uow.repository("business_memberships", business_id=THIRD).insert({"principal_id": HUMAN, "is_owner": True})
    return host


def inspect(host, source, destination, ids, ctx=None):
    with host.database.read() as uow:
        return host.tasks.relocation.inspect(ctx or host.owner(source), uow, source, destination, ids)


def move(host, source, destination, ids, *, ctx=None, token=None, at=None):
    ctx = ctx or host.owner(source)
    token = token or inspect(host, source, destination, ids, ctx).fingerprint(ctx.principal_id)
    with host.database.transaction() as uow:
        return host.tasks.relocation.apply(ctx, uow, source, destination, ids, token, move_id=new_id(), at=at or host.stamp())


def snapshot(host):
    """Every row of every ordinary table, by rowid: a full content comparison."""
    with host.database.read() as uow:
        names = [row[0] for row in uow.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        return {name: [tuple(row) for row in uow.connection.execute(f'SELECT rowid, * FROM "{name}" ORDER BY rowid')] for name in names}


def build_tree(host, business=BUSINESS):
    """root (with an outside parent) -> done child -> grandchild, and an archived sibling child; an unrelated task."""
    ctx = host.owner(business)
    outside = host.create_task("Outside parent", ctx)
    root = host.create_task("Funding story", ctx, parent_id=outside["id"], assignee_ids=[AGENT], tags=["finance"])
    done = host.create_task("Completed child", ctx, parent_id=root["id"])
    host.data("tasks_complete", {"task_id": done["id"]}, ctx, expected_revision=done["revision"])
    grandchild = host.create_task("Grandchild", ctx, parent_id=done["id"])
    sibling = host.create_task("Archived child", ctx, parent_id=root["id"])
    host.data("tasks_update", {"task_id": sibling["id"], "changes": {"archived": True}}, ctx, expected_revision=sibling["revision"])
    grandchild = host.data("tasks_update", {"task_id": grandchild["id"], "changes": {"blocked_by": [sibling["id"]]}}, ctx,
                           expected_revision=grandchild["revision"])["task"]
    other = host.create_task("Unrelated", ctx)
    for task, text in ((root, "Root note"), (done, "Done note"), (grandchild, "Deep note")):
        comment(host, task["id"], text, ctx)
    host.data("tasks_follow", {"task_id": root["id"], "following": False}, ctx)
    host.data("tasks_follow", {"task_id": done["id"], "following": True}, host.agent(AGENT, business))
    return {"outside": outside, "root": root, "done": done, "grandchild": grandchild, "sibling": sibling, "other": other}


def comment(host, task_id, text, ctx=None):
    scope = (ctx or host.owner()).business_id
    row = host.task(task_id, scope)
    arguments = {"task_id": task_id, "text": text, **({"scope": "global"} if scope is None else {})}
    return host.data("tasks_comment", arguments, ctx, expected_revision=row["revision"])


def ids_of(tree, *names):
    return [tree[name]["id"] for name in names]


# -- a successful move keeps identity and carries everything that belongs to the tree --------------------------------

def test_moving_a_tree_keeps_ids_parents_discussion_follows_tags_assignments_and_history(host):
    tree = build_tree(host)
    root, done, grandchild, sibling = (tree[name]["id"] for name in ("root", "done", "grandchild", "sibling"))
    members = (root, done, grandchild, sibling)
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO task_snooze_wakes(business_id,task_id,snooze_generation,snoozed_until,event_id,created_at) "
                               "VALUES (?,?,1,'2026-10-01T00:00:00.000000Z',NULL,'2026-10-01T00:00:00.000000Z')", (BUSINESS, root))
        uow.connection.execute("UPDATE tasks SET snooze_generation=1, snoozed_until='2026-10-01T00:00:00.000000Z' WHERE id=?", (root,))
    before = snapshot(host)
    rows_before = {name: host.task(name) for name in members}
    preview = inspect(host, BUSINESS, OTHER, [root])
    assert preview.can_move and [item["id"] for item in preview.roots] == [root]
    assert preview.roots[0]["previous_parent_id"] == tree["outside"]["id"]
    assert [item["id"] for item in preview.tasks][0] == root and {item["id"] for item in preview.tasks} == set(members)
    assert {item["id"]: item["archived"] for item in preview.tasks}[sibling] is True
    assert preview.counts["tasks"] == 4 and preview.counts["comments"] == host.count("thread_messages", "item_type='task' AND item_id IN (?,?,?,?)", *members)
    result = move(host, BUSINESS, OTHER, [root])
    assert [row["id"] for row in result["moved"]][0] == root and {row["business_id"] for row in result["moved"]} == {OTHER}
    assert result["counts"]["tasks"] == 4
    for identity in members:
        after = host.task(identity, OTHER)
        old = rows_before[identity]
        assert after["business_id"] == OTHER and after["revision"] == old["revision"] + 1
        for column in ("id", "title", "status", "due", "archived", "created_by", "created_at", "snoozed_until", "snooze_generation", "kind"):
            assert after[column] == old[column], column
        assert host.query("SELECT 1 FROM tasks WHERE business_id=? AND id=?", BUSINESS, identity) == []
    assert host.task(root, OTHER)["parent_id"] is None                       # the selected root is detached from its outside parent
    assert host.task(done, OTHER)["parent_id"] == root and host.task(grandchild, OTHER)["parent_id"] == done
    assert host.task(tree["outside"]["id"])["parent_id"] is None and host.task(tree["other"]["id"])["business_id"] == BUSINESS
    # the unselected parent is untouched and the discussion keeps ids, bodies, authors and event association
    after = snapshot(host)
    old_messages = {row[2]: row for row in before["thread_messages"] if row[3] == "task" and row[4] in members}
    new_messages = {row[2]: row for row in after["thread_messages"] if row[3] == "task" and row[4] in members}
    assert old_messages and set(old_messages) == set(new_messages)
    for identity, old in old_messages.items():
        new = new_messages[identity]
        # everything but the scope is byte-for-byte what was written; the old namespace is pinned beside the references
        assert new[1] == OTHER and old[1] == BUSINESS and new[2:-1] == old[2:-1]
        assert old[-1] is None and new[-1] == "business:" + BUSINESS
    follows = host.query("SELECT item_id, principal_id, following, explicit, reason FROM thread_follows WHERE business_id=? ORDER BY item_id, principal_id", OTHER)
    assert (root, HUMAN, 0, 1) in {(row["item_id"], row["principal_id"], row["following"], row["explicit"]) for row in follows}
    assert (done, AGENT, 1, 1) in {(row["item_id"], row["principal_id"], row["following"], row["explicit"]) for row in follows}
    assert host.count("thread_follows", "business_id=?", BUSINESS) == sum(1 for row in before["thread_follows"] if row[1] == BUSINESS and row[3] not in members)
    assert host.count("item_tags", "business_id=? AND item_id=?", OTHER, root) == 1
    edges = host.query("SELECT role, from_id, to_id FROM relationships WHERE business_id=? ORDER BY role, from_id", OTHER)
    assert {(row["role"], row["from_id"], row["to_id"]) for row in edges} == {("assigned_to", root, AGENT), ("blocked_by", grandchild, sibling)}
    assert host.count("task_completions", "business_id=? AND task_id=?", OTHER, done) == 1 and host.count("task_completions", "task_id=?", done) == 1
    wake = host.query("SELECT * FROM task_snooze_wakes WHERE task_id=?", root)
    assert len(wake) == 1 and wake[0]["business_id"] == OTHER and wake[0]["snooze_generation"] == 1
    # relocation is not new activity, and it is audited per task
    assert after["inbox_events"] == before["inbox_events"] and after["inbox_deliveries"] == before["inbox_deliveries"]
    assert host.count("inbox_changes") == len(before["inbox_changes"])
    audit = host.query("SELECT task_id, source_business_id, destination_business_id, previous_parent_id, revision_before, revision_after, actor_id "
                       "FROM task_moves ORDER BY seq")
    assert [row["task_id"] for row in audit][0] == root and {row["task_id"] for row in audit} == set(members)
    assert {row["actor_id"] for row in audit} == {HUMAN} and {row["destination_business_id"] for row in audit} == {OTHER}
    assert {row["previous_parent_id"] for row in audit if row["task_id"] == root} == {tree["outside"]["id"]}
    # the tasks table of the guard: nothing leaked to the next unit
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (BUSINESS, root))


def test_global_story_children_move_into_a_project_and_the_story_stays(host):
    ctx = host.owner(None)
    story = host.create_task("Funding story", ctx, **{"task_type": "story"})
    kids = [host.create_task(name, ctx, parent_id=story["id"]) for name in ("One", "Two")]
    comment(host, kids[0]["id"], "Global note", host.coordinator())
    result = move(host, None, BUSINESS, [kid["id"] for kid in kids], ctx=ctx)
    assert {row["business_id"] for row in result["moved"]} == {BUSINESS} and all(row["parent_id"] is None for row in result["moved"])
    assert host.task(story["id"], None)["business_id"] is None and host.count("tasks", "business_id IS NULL") == 1
    assert host.count("thread_messages", "business_id=? AND item_id=?", BUSINESS, kids[0]["id"]) >= 1
    assert host.count("thread_messages", "business_id IS NULL AND item_id=?", kids[0]["id"]) == 0


def test_overlapping_selection_moves_once_and_a_changed_tree_or_stale_token_needs_a_new_preview(host):
    tree = build_tree(host)
    root, done = tree["root"]["id"], tree["done"]["id"]
    preview = inspect(host, BUSINESS, OTHER, [root, done])
    assert [item["id"] for item in preview.roots] == [root] and preview.counts["tasks"] == 4
    token = preview.fingerprint(HUMAN)
    assert inspect(host, BUSINESS, OTHER, [done, root]).fingerprint(HUMAN) == token == inspect(host, BUSINESS, OTHER, [root]).fingerprint(HUMAN)
    # a descendant added after the preview changes the exact snapshot
    host.create_task("Late child", host.owner(BUSINESS), parent_id=root)
    before = snapshot(host)
    with pytest.raises(DomainError) as stale:
        move(host, BUSINESS, OTHER, [root, done], token=token)
    assert stale.value.code == "revision_conflict" and snapshot(host) == before
    # a different caller's token is a different token
    fresh = inspect(host, BUSINESS, OTHER, [root, done])
    assert fresh.fingerprint(HUMAN) != fresh.fingerprint(STRANGER)
    with pytest.raises(DomainError) as other_caller:
        move(host, BUSINESS, OTHER, [root, done], token=fresh.fingerprint(STRANGER))
    assert other_caller.value.code == "revision_conflict" and snapshot(host) == before
    assert move(host, BUSINESS, OTHER, [root, done])["counts"]["tasks"] == 5


def test_missing_tasks_destination_and_selection_bounds_are_ordinary_errors_not_previews(host):
    task = host.create_task("Only", host.owner())
    for ids, destination, code in ((["missing"], OTHER, "not_found"), ([], OTHER, "validation_error"), ([task["id"], task["id"]], OTHER, "validation_error"),
                                   ([task["id"]], BUSINESS, "validation_error"), ([task["id"]], new_id(), "not_found")):
        with pytest.raises(DomainError) as error:
            inspect(host, BUSINESS, destination, ids)
        assert error.value.code == code
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-01T00:00:00Z' WHERE id=?", (OTHER,))
    with pytest.raises(DomainError) as archived:
        inspect(host, BUSINESS, OTHER, [task["id"]])
    assert archived.value.code == "not_found"
    with pytest.raises(DomainError) as many:
        inspect(host, BUSINESS, THIRD, [str(index) for index in range(101)])
    assert many.value.code == "validation_error"


# -- blockers: concrete incompatible data, never silently dropped -----------------------------------------------------

def codes(preview):
    return sorted({item["code"] for item in preview.blockers})


def test_blockers_name_collisions_email_bindings_crossing_dependencies_links_and_invalid_fields(host):
    ctx = host.owner()
    task = host.create_task("Mover", ctx)
    # same id in the destination
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=OTHER).insert({"id": task["id"], "title": "Same bare id", "status": "open", "created_by": HUMAN})
    preview = inspect(host, BUSINESS, OTHER, [task["id"]])
    assert codes(preview) == ["task_id_exists"] and not preview.can_move and preview.blockers[0]["task_id"] == task["id"]
    before = snapshot(host)
    with pytest.raises(DomainError) as blocked:
        move(host, BUSINESS, OTHER, [task["id"]], token=preview.fingerprint(HUMAN))
    assert blocked.value.code == "move_blocked" and blocked.value.details["blockers"][0]["code"] == "task_id_exists" and snapshot(host) == before
    # a crossing dependency, an outside link and an Email binding
    blocker = host.create_task("Blocks it", ctx)
    other_task = host.create_task("Dependent", ctx, blocked_by=[blocker["id"]])
    assert codes(inspect(host, BUSINESS, THIRD, [other_task["id"]])) == ["crossing_relationship"]
    assert codes(inspect(host, BUSINESS, THIRD, [blocker["id"]])) == ["crossing_relationship"]
    linker = host.create_task("Linker", ctx)
    target = host.create_task("Linked", ctx)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET fields_json=? WHERE id=?", (json.dumps({"$task_links": [{"business_id": BUSINESS, "task_id": target["id"]}]}), linker["id"]))
    preview = inspect(host, BUSINESS, THIRD, [target["id"]])
    assert codes(preview) == ["external_task_link"]
    assert inspect(host, BUSINESS, THIRD, [target["id"], linker["id"]]).can_move
    email = email_task(host)
    assert codes(inspect(host, BUSINESS, THIRD, [email["id"]])) == ["email_binding"]
    # a typed field the destination does not define
    fields = {"effort": {"type": "int"}}
    host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, ctx, expected_revision=None)
    typed = host.create_task("Typed", ctx, fields={"effort": 3})
    assert codes(inspect(host, BUSINESS, OTHER, [typed["id"]])) == ["fields_invalid"]
    host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, host.owner(OTHER), expected_revision=None)
    assert inspect(host, BUSINESS, OTHER, [typed["id"]]).can_move


def test_a_destination_that_cannot_take_an_assignee_blocks_the_move(host):
    task = host.create_task("Assigned", host.owner(), assignee_ids=[STRANGER])                    # a member of the source project only
    preview = inspect(host, BUSINESS, OTHER, [task["id"]])
    assert codes(preview) == ["assignee_unavailable"] and preview.blockers[0]["task_id"] == task["id"]
    with host.database.transaction() as uow:
        uow.repository("business_memberships", business_id=OTHER).insert({"principal_id": STRANGER})
    assert inspect(host, BUSINESS, OTHER, [task["id"]]).can_move


def test_crm_memberships_and_observations_follow_their_task_and_the_table_stays(host):
    ctx = host.owner()
    table = host.crm_table("Pipeline", ctx)
    customer = host.create_task("Acme", ctx, task_type="customer", customer_kind="company")
    added = host.add(table, customer, ctx=ctx)
    entry = host.entries(table, ctx)
    assert entry
    before = host.query("SELECT * FROM crm_entries")
    assert len(before) == 1
    preview = inspect(host, BUSINESS, OTHER, [customer["id"]])
    assert preview.can_move and preview.counts["crm_entries"] == 1
    move(host, BUSINESS, OTHER, [customer["id"]])
    after = host.query("SELECT * FROM crm_entries")
    assert len(after) == 1
    old, new = before[0], after[0]
    assert new["business_id"] == BUSINESS and new["table_id"] == table["id"]                    # table ownership does not move
    assert (new["record_business_id"], new["record_namespace"]) == (OTHER, "business:" + OTHER)
    assert new["revision"] == old["revision"] + 1
    for column in ("id", "stage_id", "position", "cells_json", "removed", "created_by", "created_at", "record_task_id"):
        assert new[column] == old[column], column


# -- the guard is default-deny ----------------------------------------------------------------------------------------

def test_ordinary_updates_and_direct_sql_cannot_relocate_anything(host):
    ctx = host.owner()
    task = host.create_task("Stuck here", ctx)
    comment(host, task["id"], "kept", ctx)
    host.crm_table("Pipeline", ctx)
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"business_id": OTHER}}, ctx, expected_revision=task["revision"]) == "validation_error"
    statements = [("UPDATE tasks SET business_id=? WHERE id=?", (OTHER, task["id"])),
                  ("UPDATE thread_messages SET business_id=? WHERE item_id=?", (OTHER, task["id"]))]
    for sql, parameters in statements:
        with pytest.raises(DomainError):
            with host.database.transaction() as uow:
                uow.connection.execute(sql, parameters)
    assert host.task(task["id"])["business_id"] == BUSINESS
    # a raw connection that was never armed refuses too, and its function is the default refusal
    connection = sqlite3.connect(host.database.path)
    from qorqut_operations.storage.sqlfunctions import install_sql_functions
    install_sql_functions(connection)
    assert connection.execute("SELECT relocation_permits(?,?,?)", (BUSINESS, OTHER, task["id"])).fetchone()[0] == 0
    with pytest.raises(sqlite3.DatabaseError):
        connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (OTHER, task["id"]))
    connection.close()
    # without the function the schema cannot even be evaluated: a connection cannot bypass it by omission
    bare = sqlite3.connect(host.database.path)
    with pytest.raises(sqlite3.DatabaseError):
        bare.execute("UPDATE tasks SET business_id=? WHERE id=?", (OTHER, task["id"]))
    bare.close()


def test_an_armed_guard_permits_only_the_exact_ids_and_scope_pair_and_never_survives_its_block(host):
    ctx = host.owner()
    first, second = host.create_task("First", ctx), host.create_task("Second", ctx)
    with host.database.transaction() as uow:
        with uow.relocating(BUSINESS, OTHER, [first["id"]]):
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (OTHER, second["id"]))      # another id
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (THIRD, first["id"]))       # another destination
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE tasks SET business_id=?, created_by=? WHERE id=?", (OTHER, STRANGER, first["id"]))
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE tasks SET business_id=?, id=? WHERE id=?", (OTHER, "renamed", first["id"]))
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (OTHER, first["id"]))
        with pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (BUSINESS, first["id"]))        # disarmed
        assert uow.relocation is None
        with pytest.raises(DomainError):
            RelocationGuard(BUSINESS, BUSINESS, frozenset({"x"}))
    with host.database.read() as uow:
        with pytest.raises(DomainError):
            with uow.relocating(BUSINESS, OTHER, [first["id"]]):
                pass
    # the pooled connection the failed unit used carries no armed guard into the next unit
    with host.database.transaction() as uow:
        assert uow.relocation is None
        with pytest.raises(sqlite3.DatabaseError):
            uow.connection.execute("UPDATE tasks SET business_id=? WHERE id=?", (BUSINESS, first["id"]))


def test_guarded_relocation_cannot_rewrite_identity_authors_or_immutable_content(host):
    ctx = host.owner()
    task = host.create_task("Content", ctx)
    comment(host, task["id"], "original", ctx)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET title='x' WHERE id=?", (task["id"],))
    with host.database.transaction() as uow:
        with uow.relocating(BUSINESS, OTHER, [task["id"]]):
            for sql in ("UPDATE thread_messages SET body='tampered', business_id=? WHERE item_id=?",
                        "UPDATE thread_messages SET author_id=NULL, business_id=? WHERE item_id=?",
                        "UPDATE thread_messages SET created_at='x', business_id=? WHERE item_id=?",
                        "UPDATE thread_messages SET id='other', business_id=? WHERE item_id=?"):
                with pytest.raises(sqlite3.DatabaseError):
                    uow.connection.execute(sql, (OTHER, task["id"]))
            with pytest.raises(sqlite3.DatabaseError):
                uow.connection.execute("UPDATE tasks SET created_at='2000-01-01T00:00:00Z', business_id=? WHERE id=?", (OTHER, task["id"]))


# -- failure leaves nothing behind -----------------------------------------------------------------------------------

class Failing:
    name = "failing"

    def inspect(self, uow, relocation):
        return AdapterReport()

    def apply(self, uow, relocation, at):
        raise RuntimeError("injected failure after the core rows moved")


def test_a_failure_in_the_middle_of_the_transaction_rolls_everything_back(host):
    tree = build_tree(host)
    host.tasks.relocation.adapters = (Failing(),)
    before = snapshot(host)
    with pytest.raises(RuntimeError):
        move(host, BUSINESS, OTHER, [tree["root"]["id"]])
    assert snapshot(host) == before
    host.tasks.relocation.adapters = ()
    assert move(host, BUSINESS, OTHER, [tree["root"]["id"]])["counts"]["tasks"] == 4


# -- adapters take part in the preview, fingerprint and application -----------------------------------------------------

class Recording:
    name = "recording"

    def __init__(self):
        self.applied, self.blockers = [], []

    def inspect(self, uow, relocation):
        return AdapterReport({"private_messages": 2}, self.blockers, {"ids": list(relocation.ids)})

    def apply(self, uow, relocation, at):
        self.applied.append((relocation.source, relocation.destination, relocation.ids, relocation.roots))
        assert uow.relocation is not None
        return {"private_messages": 2}


def test_adapters_add_counts_blockers_and_run_inside_the_armed_guard(host):
    task = host.create_task("Adapter", host.owner())
    adapter = Recording()
    host.tasks.relocation.adapters = (adapter,)
    preview = inspect(host, BUSINESS, OTHER, [task["id"]])
    assert preview.counts["private_messages"] == 2 and preview.can_move and preview.material["adapter:recording"] == {"ids": [task["id"]]}
    adapter.blockers = [{"code": "unsupported", "message": "Not supported", "task_id": task["id"]}]
    blocked = inspect(host, BUSINESS, OTHER, [task["id"]])
    assert codes(blocked) == ["unsupported"] and blocked.fingerprint(HUMAN) != preview.fingerprint(HUMAN)
    adapter.blockers = []
    assert move(host, BUSINESS, OTHER, [task["id"]])["counts"]["private_messages"] == 2
    assert adapter.applied == [(BUSINESS, OTHER, (task["id"],), (task["id"],))]


# -- stale references: audit, successive moves and the moved response ------------------------------------------------------

def test_successive_moves_resolve_stale_references_and_current_rows_win(host):
    ctx = host.owner()
    task = host.create_task("Wanderer", ctx)
    identity = task["id"]
    move(host, BUSINESS, OTHER, [identity])
    move(host, OTHER, THIRD, [identity], ctx=host.owner(OTHER))
    with host.database.read() as uow:
        assert moved_location(uow, BUSINESS, identity) == THIRD and moved_location(uow, OTHER, identity) == THIRD
        assert moved_location(uow, THIRD, identity) is None                                # the current row wins
        assert moved_location(uow, None, identity) is None and moved_location(uow, BUSINESS, "unknown") is None
    move(host, THIRD, BUSINESS, [identity], ctx=host.owner(THIRD))                         # back to where it started
    with host.database.read() as uow:
        assert moved_location(uow, BUSINESS, identity) is None
        assert moved_location(uow, OTHER, identity) == BUSINESS and moved_location(uow, THIRD, identity) == BUSINESS
    assert host.count("task_moves", "task_id=?", identity) == 3


def test_a_reused_bare_id_makes_a_stale_reference_ambiguous_not_guessed(host):
    ctx = host.owner()
    first = host.create_task("First holder", ctx)
    identity = first["id"]
    move(host, BUSINESS, OTHER, [identity])
    # another, different Task takes the same bare id in the old scope and then leaves for a third scope
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=BUSINESS).insert({"id": identity, "title": "Second holder", "status": "open",
                                                             "created_by": HUMAN, "created_at": "2026-11-01T00:00:00.000000Z"})
    move(host, BUSINESS, THIRD, [identity])
    with host.database.read() as uow:
        assert moved_location(uow, BUSINESS, identity) is None                              # two journeys left that scope
        assert moved_location(uow, OTHER, identity) is None and moved_location(uow, THIRD, identity) is None   # current rows win


def test_old_scope_reads_and_writes_get_record_moved_only_for_callers_who_may_read_the_destination(host):
    ctx = host.owner()
    task = host.create_task("Moves away", ctx)
    comment(host, task["id"], "before", ctx)
    move(host, BUSINESS, OTHER, [task["id"]])
    for operation, arguments, options in (("tasks_get", {"task_id": task["id"]}, {}), ("tasks_thread_get", {"task_id": task["id"]}, {}),
                                          ("tasks_notes_list", {"task_id": task["id"]}, {}),
                                          ("tasks_update", {"task_id": task["id"], "changes": {"title": "late"}}, {"expected_revision": 1}),
                                          ("tasks_comment", {"task_id": task["id"], "text": "late"}, {}),
                                          ("tasks_follow", {"task_id": task["id"], "following": True}, {})):
        result = host.invoke(operation, arguments, ctx, **options)
        assert result.status == "error" and result.error["code"] == "record_moved", (operation, result.error)
        details = result.error["details"]
        assert details["task"] == {"type": "task", "id": task["id"], "business_id": OTHER}
        assert details["destination"] == {"id": OTHER, "name": "second", "slug": "second"}
    # nothing was written into either scope by the refused mutations
    assert host.count("thread_messages", "item_id=?", task["id"]) == 1 and host.task(task["id"], OTHER)["title"] == "Moves away"
    # a caller with no access to the destination learns nothing: the answer is the ordinary not_found
    outsider = host.stranger()
    assert host.invoke("tasks_get", {"task_id": task["id"]}, outsider).error["code"] == "not_found"
    # a reference that never existed is not_found too
    assert host.invoke("tasks_get", {"task_id": "never-existed"}, ctx).error["code"] == "not_found"
    # the destination itself reads normally
    assert host.read("tasks_get", {"task_id": task["id"]}, host.owner(OTHER))["task"]["title"] == "Moves away"


# -- schema inventory ---------------------------------------------------------------------------------------------------

def test_every_foreign_key_to_tasks_is_classified_by_the_relocation_helper(host):
    with host.database.read() as uow:
        children = set()
        for (table,) in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for key in uow.connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                if key["table"] == "tasks":
                    children.add(table)
    from qorqut_operations.tasks.relocation import FOREIGN_KEY_CHILDREN
    assert children == FOREIGN_KEY_CHILDREN


def test_move_preserves_workers_and_latest_completion_and_fingerprints_them(host):
    task = host.create_task('Worker move', worker_ids=[HUMAN, AGENT], status='done')
    preview = inspect(host, BUSINESS, OTHER, [task['id']])
    token = preview.fingerprint(host.owner(BUSINESS).principal_id)
    with host.database.transaction() as uow:
        uow.connection.execute('UPDATE tasks SET latest_completed_at=? WHERE business_id=? AND id=?',
                               ('2026-01-01T00:00:00.000000Z', BUSINESS, task['id']))
    with pytest.raises(DomainError) as error:
        move(host, BUSINESS, OTHER, [task['id']], token=token)
    assert error.value.code == 'revision_conflict'
    move(host, BUSINESS, OTHER, [task['id']])
    moved = host.read('tasks_get', {'task_id': task['id']}, host.owner(OTHER))['task']
    assert moved['worker_ids'] == sorted([HUMAN, AGENT])
    assert moved['latest_completed_at'] == '2026-01-01T00:00:00.000000Z'
