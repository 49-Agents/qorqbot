"""Move to project against a Paper-composed host: unsupported bindings fail the move and no dependency is left unclassified.

The composition is the real one a Paper installation runs (Paper's package, its store and catalog), not a core-only fixture:
Paper's handoff rows reference ``tasks`` by foreign key, so a move that carried them would silently orphan or reassign a
mailbox ownership claim. The inventory test walks the composed schema and fails when any stored column or foreign key that
names a Task is not classified, which is how a dependency added later is forced into a decision.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationOptions, new_id

from bazarlyk.domains.tasks.relocation import CORE_CHILDREN, TASK_COLUMNS
from bazarlyk.extensions import NO_EXTENSIONS, select
from host_foundation_support import build_instance
from test_extension_compositions import extension
from test_project_records import call, ok
from test_task_move import MOVE, TARGETS, preview, query, select as selection_of, state

NOW = "2026-10-28T00:00:00Z"
# Columns outside the classified registry that legitimately mention a task by name but are not references to a Task row.
NOT_REFERENCES = {"tasks.task_type"}
# Paper's own table that references tasks; it must stay a blocker, never a followed dependency.
EXTENSION_BLOCKING = {"outreach_handoffs.task_id"}


@pytest.fixture(scope="module", params=("core", "paper", "selected"))
def composed(request, tmp_path_factory):
    root = tmp_path_factory.mktemp("move-" + request.param)
    if request.param == 'core':
        selected = NO_EXTENSIONS
    elif request.param == 'paper':
        selected = select(instances=[extension('paper', root)])
    else:
        from qorqut_fastmail.extension import create as fastmail
        selected = select(instances=[extension('paper', root), extension('content', root), fastmail({
            'engine_url': 'http://127.0.0.1:1', 'admin_token_file': str(root / 'absent-token')})])
    instance, ids = build_instance(root, businesses=("first",), extensions=selected)
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business, review_business_id=business,
                                                       uow=uow, source="dashboard")

    try:
        yield SimpleNamespace(kind=request.param, instance=instance, db=instance.database, runtime=instance.runtime, first=ids["first"],
                              owner_context=owner_context)
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def project(composed, name, slug):
    identifier = new_id()
    root = composed.instance.paths.instance_root
    (root / "business" / slug / "hq").mkdir(parents=True, exist_ok=True)
    with composed.db.transaction() as uow:
        uow.repository("businesses").insert({
            "id": identifier, "slug": slug, "name": name, "stage": "operating", "workspace": str(root / "business" / slug),
            "hq_path": str(root / "business" / slug / "hq"), "config_json": "{}"})
        uow.repository("business_memberships", business_id=identifier).insert({"principal_id": composed.instance.owner, "is_owner": True})
    return identifier


def task(composed, ctx, title, parent=None):
    arguments = {"title": title, **({"parent_id": parent} if parent else {}), **({"scope": "global"} if ctx.business_id is None else {})}
    return ok(call(composed, ctx, "tasks_create", arguments))["task"]


def codes(shown):
    return {item["code"] for item in shown["blockers"]}


def test_every_stored_task_reference_and_foreign_key_is_classified(composed):
    """Fail on any column named for a Task, or any foreign key to ``tasks``, that the relocation has no decision for."""
    with composed.db.read() as uow:
        tables = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        named, children = set(), set()
        for table in tables:
            for column in uow.connection.execute(f'PRAGMA table_info("{table}")'):
                if re.search(r"task", column["name"]) and table != "tasks":
                    named.add(f"{table}.{column['name']}")
            for fk in uow.connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                if fk["table"] == "tasks":
                    children.add(table)
    classified = set(TASK_COLUMNS) | EXTENSION_BLOCKING | NOT_REFERENCES
    assert named <= classified, f"unclassified Task columns: {sorted(named - classified)}"
    assert children - CORE_CHILDREN <= ({"outreach_handoffs"} if composed.kind != "core" else set()), children
    assert CORE_CHILDREN <= children
    assert ("outreach_handoffs" in children) == (composed.kind != "core")


def test_destination_listing_works_in_this_composition_without_the_project_inventory(composed):
    first, other = composed.first, project(composed, "Inventory Other", "inventory-other")
    listed = ok(call(composed, composed.owner_context(None), TARGETS, {"source_business_id": first}))
    assert other in {item["id"] for item in listed["projects"]} and first not in {item["id"] for item in listed["projects"]}


def test_a_paper_handoff_blocks_the_whole_move_and_nothing_changes(composed):
    if composed.kind != "paper":
        pytest.skip("Paper handoffs exist only in the Paper composition")
    source, destination = project(composed, "Handoff Source", "handoff-source"), project(composed, "Handoff Destination", "handoff-destination")
    owner = composed.owner_context(source)
    root, child = task(composed, owner, "Root"), None
    child = task(composed, owner, "Child", root["id"])
    principal = new_id()
    with composed.db.transaction() as uow:
        uow.repository("principals").insert({"id": principal, "kind": "agent", "display_name": "Paper integration"})
        uow.connection.execute(
            "INSERT INTO integration_principals(id,slug,display_name,principal_id,enabled,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,1,1,?,?)", (principal, "paper-fixture", "Paper", principal, NOW, NOW))
        uow.connection.execute(
            "INSERT INTO outreach_handoffs(id,integration_id,external_handoff_id,business_id,task_id,payload_json,response_json,created_at) "
            "VALUES(?,?,?,?,?,'{}','{}',?)", (new_id(), principal, "external-1", source, child["id"], NOW))
    selection = selection_of(source, destination, [root["id"]])
    shown = preview(composed, composed.owner_context(None), selection)
    assert not shown["can_move"] and codes(shown) == {"unsupported_binding"}
    assert "outreach handoffs" in shown["blockers"][0]["message"]
    before = state(composed)
    result = composed.runtime.application.invoke(composed.owner_context(None), MOVE, {**selection, "preview_token": shown["token"]},
                                                 InvocationOptions(idempotency_key=new_id()))
    assert result.status == "error" and result.error["code"] == "move_blocked"
    assert state(composed) == before
    assert query(composed, "SELECT business_id FROM tasks WHERE id=?", child["id"]) == [{"business_id": source}]
    # The handoff is the only reason: a sibling tree of the same Project without one moves normally.
    free = task(composed, owner, "Unbound root")
    free_selection = selection_of(source, destination, [free["id"]])
    assert preview(composed, composed.owner_context(None), free_selection)["can_move"]
    # Removing the handoff (an extension action, never done by the move) makes the same selection movable.
    with composed.db.transaction() as uow:
        uow.connection.execute("DELETE FROM outreach_handoffs WHERE task_id=?", (child["id"],))
    assert preview(composed, composed.owner_context(None), selection)["can_move"]


def test_an_unclassified_foreign_key_child_with_rows_blocks_and_with_none_does_not(composed):
    source, destination = project(composed, "Unknown Source", "unknown-source"), project(composed, "Unknown Destination", "unknown-destination")
    owner = composed.owner_context(source)
    root = task(composed, owner, "Root")
    selection = selection_of(source, destination, [root["id"]])
    name = "future_task_binding_" + composed.kind
    with composed.db.transaction() as uow:
        uow.connection.execute(f"CREATE TABLE {name}(id TEXT PRIMARY KEY, business_id TEXT NOT NULL, task_id TEXT NOT NULL, "
                               "FOREIGN KEY(business_id, task_id) REFERENCES tasks(business_id, id))")
    try:
        assert preview(composed, composed.owner_context(None), selection)["can_move"]
        with composed.db.transaction() as uow:
            uow.connection.execute(f"INSERT INTO {name}(id,business_id,task_id) VALUES(?,?,?)", (new_id(), source, root["id"]))
        shown = preview(composed, composed.owner_context(None), selection)
        assert not shown["can_move"] and codes(shown) == {"unsupported_binding"} and name.replace("_", " ") in shown["blockers"][0]["message"]
    finally:
        with composed.db.transaction() as uow:
            uow.connection.execute(f"DROP TABLE {name}")


def test_an_email_bound_task_and_a_pending_approval_block_with_readable_reasons(composed):
    source, destination = project(composed, "Reason Source", "reason-source-" + composed.kind), project(composed, "Reason Destination", "reason-destination-" + composed.kind)
    owner = composed.owner_context(source)
    root = task(composed, owner, "Root")
    email = task(composed, owner, "Old thread")
    with composed.db.transaction() as uow:
        row = uow.repository("tasks", business_id=source).get(email["id"])
        uow.repository("tasks", business_id=source).update(
            email["id"], {"fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": "thread-1"}}},
            expected_revision=row["revision"])
    shown = preview(composed, composed.owner_context(None), selection_of(source, destination, [email["id"]]))
    assert not shown["can_move"] and codes(shown) == {"email_binding"} and shown["blockers"][0]["task_id"] == email["id"]
    # A pending approval whose exact prepared change names a Task would execute against the old scope: it blocks.
    selection = selection_of(source, destination, [root["id"]])
    assert preview(composed, composed.owner_context(None), selection)["can_move"]
    with composed.db.transaction() as uow:
        sequence = uow.connection.execute("SELECT COALESCE(MAX(updated_sequence),0)+1 FROM signoffs").fetchone()[0]
        uow.connection.execute(
            "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,operation_version,"
            "proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,policy_binding_json,affected_scope_json,"
            "state,updated_sequence,created_at,updated_at) VALUES(?,?,'business',?,'tasks_update',1,1,?,?,'{}',?,'{}','{}','pending',?,?,?)",
            (new_id(), source, composed.instance.owner, "0" * 64, ('{"changes":[{"resource":{"resource_id":"%s"}}]}' % root["id"]).encode(),
             '[{"resource":{"resource_type":"tasks","resource_id":"%s"}}]' % root["id"], sequence, NOW, NOW))
    shown = preview(composed, composed.owner_context(None), selection)
    assert not shown["can_move"] and codes(shown) == {"unsupported_binding"} and "approval" in shown["blockers"][0]["message"]
    with composed.db.transaction() as uow:
        uow.connection.execute("UPDATE signoffs SET state='cancelled' WHERE review_business_id=?", (source,))
    assert preview(composed, composed.owner_context(None), selection)["can_move"]
