"""Replayed Project links re-authorize; canonical and bare Actions converge on one scoped target."""

from __future__ import annotations

from qorqut_operations.contracts import InvocationOptions, new_id, plain

from test_project_records import (agent, call, context, context_global, host, ok, seed_project, task)


def link(host, ctx, project, source, identity, **extra):
    return call(host, ctx, "project_items_link",
                {"project_id": project, "item": {"type": "task", "id": identity, "business_id": source}, **extra})


def test_a_replayed_link_reauthorizes_the_project_and_the_source(host):
    project = seed_project(host, name="Replay Court", slug="replay-court")
    foreign = seed_project(host, name="Replay Vault", slug="replay-vault")
    row = task(host, foreign, "A privately titled record")
    key = new_id()
    arguments = {"project_id": project, "item": {"type": "task", "id": row["id"], "business_id": foreign}}
    options = InvocationOptions(idempotency_key=key)
    first = host.runtime.application.invoke(context(host, project), "project_items_link", arguments, options)
    assert first.status == "ok", first.error
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (foreign, host.instance.owner))
    refused = call(host, context(host, project), "project_item_get",
                   {"project_id": project, "item": {"type": "task", "id": row["id"], "business_id": foreign}})
    assert refused.status == "error"
    replay = host.runtime.application.invoke(context(host, project), "project_items_link", arguments, options)
    assert replay.status == "error", replay.data
    # The refusal keeps the stored association and the target row; no history is deleted on revocation.
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 1
        assert uow.connection.execute("SELECT count(*) FROM tasks WHERE business_id=? AND id=?",
                                      (foreign, row["id"])).fetchone()[0] == 1


def test_a_replayed_owned_noop_stops_when_the_project_is_archived(host):
    project = seed_project(host, name="Archive Court", slug="archive-court")
    row = task(host, project, "An owned record")
    arguments = {"project_id": project, "item": {"type": "task", "id": row["id"], "business_id": project}}
    options = InvocationOptions(idempotency_key=new_id())
    assert host.runtime.application.invoke(context(host, project), "project_items_link", arguments, options).status == "ok"
    with host.db.transaction() as uow:
        current = uow.repository("businesses").get(project)
        uow.repository("businesses").update(project, {"archived_at": "2026-01-01T00:00:00Z"},
                                            expected_revision=current["revision"])
    replay = host.runtime.application.invoke(context_global(host), "project_items_link", arguments, options)
    assert replay.status == "error"
    assert replay.error["code"] in {"business_archived", "not_found", "forbidden"}


def test_a_link_receipt_is_refused_after_the_association_was_removed(host):
    project = seed_project(host, name="Revoked Court", slug="revoked-court")
    foreign = seed_project(host, name="Revoked Vault", slug="revoked-vault")
    row = task(host, foreign, "A revoked association")
    arguments = {"project_id": project, "item": {"type": "task", "id": row["id"], "business_id": foreign}}
    options = InvocationOptions(idempotency_key=new_id())
    first = host.runtime.application.invoke(context(host, project), "project_items_link", arguments, options)
    assert first.status == "ok", first.error
    link_id = plain(first.data)["link"]["id"]
    assert ok(call(host, context(host, project), "project_items_unlink",
                   {"project_id": project, "link_id": link_id, "expected_revision": 1})) == {"removed": True}
    replay = host.runtime.application.invoke(context(host, project), "project_items_link", arguments, options)
    assert replay.status == "error"
    assert replay.error["code"] == "revision_conflict"


def test_bare_and_canonical_actions_keep_their_real_scope(host):
    project = seed_project(host, name="Action Court", slug="action-court")
    other = seed_project(host, name="Action Annex", slug="action-annex")
    third = seed_project(host, name="Action Outsider", slug="action-outsider")
    owner = context(host, project)
    # A Global-stored Action shared to two projects is one canonical target in each of them; it is owned in both.
    shared = ok(call(host, owner, "actions_record", {
        "text": "Shared the pilot quote with both projects", "category": "business-operational",
        "real_time": "2026-10-20", "business_ids": [project, other]}))["action"]
    with host.db.read() as uow:
        shared_row = uow.connection.execute("SELECT id,business_id,action_key FROM action_logs WHERE action_key=?",
                                            (shared["action_id"],)).fetchone()
    assert shared_row["business_id"] is None and shared_row["id"] != shared["action_id"]
    candidates = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": None, "type": "action", "limit": 100}))["items"]
    assert shared["action_id"] in {item["id"] for item in candidates}
    for scope in (project, other):
        linked = ok(call(host, context(host, scope), "project_items_link", {
            "project_id": scope, "item": {"type": "action", "id": shared["action_id"], "business_id": None}}))
        assert linked["item"]["id"] == shared["action_id"] and linked["item"]["business_id"] is None
        assert linked["already_present"] is True and linked["link"] is None
        with host.db.read() as uow:
            assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                          (scope,)).fetchone()[0] == 0
    # The bare storage row ID of a Global-stored Action is not its canonical identity; no alias authority is widened.
    bare = call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": shared_row["id"], "business_id": None}})
    assert bare.status == "error"
    # The owner can read the Global Action and explicitly link it into a Project outside its sharing; this creates an
    # association only and never adds action_businesses membership or another principal's access.
    outsider = ok(call(host, context(host, third), "project_items_link", {
        "project_id": third, "item": {"type": "action", "id": shared["action_id"], "business_id": None}}))
    assert outsider["link"]["target_namespace"] == "global" and outsider["item"]["id"] == shared["action_id"]
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM action_businesses WHERE action_key=?",
                                      (shared["action_id"],)).fetchone()[0] == 2

    # A wrong explicit scope is refused rather than retargeted.
    wrong = call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": shared["action_id"], "business_id": project}})
    assert wrong.status == "error"


def test_duplicate_foreign_links_converge_on_one_row_and_exact_idempotency_replays(host):
    project = seed_project(host, name="Convergence Court", slug="convergence-court")
    foreign = seed_project(host, name="Convergence Vault", slug="convergence-vault")
    row = task(host, foreign, "One convergence target")
    arguments = {"project_id": project, "item": {"type": "task", "id": row["id"], "business_id": foreign}}
    first = host.runtime.application.invoke(context(host, project), "project_items_link", arguments,
                                            InvocationOptions(idempotency_key=new_id()))
    assert first.status == "ok", first.error
    second = host.runtime.application.invoke(context(host, project), "project_items_link", arguments,
                                             InvocationOptions(idempotency_key=new_id()))
    assert second.status == "ok", second.error
    assert plain(second.data)["already_present"] is True
    assert plain(first.data)["link"]["id"] == plain(second.data)["link"]["id"]
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 1


def business_action(host, business):
    """A real business-stored Action row (its canonical key includes the owning business)."""
    identity, now = new_id(), "2026-10-20T00:00:00Z"
    key = f"business/{business}/{identity}"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO action_logs(business_id,id,action_key,category,action_text,recorded_by,origin,occurred_at,"
            "refs_json,related_objects_json,withdrawn,revision,created_at,updated_at) "
            "VALUES(?,?,?,'business-operational','A business-stored action',?,'manual',?,'{}','[]',0,1,?,?)",
            (business, identity, key, host.instance.owner, now, now, now))
        uow.connection.execute(
            "INSERT INTO action_businesses(action_key,business_id,created_at) VALUES(?,?,?)",
            (key, business, now))
    return {"action_id": key, "row_id": identity, "business_id": business}


def test_a_business_stored_bare_action_prepares_its_canonical_identity(host):
    project = seed_project(host, name="Bare Court", slug="bare-court")
    action = business_action(host, project)
    owner = context(host, project)
    with host.db.read() as uow:
        plan = host.runtime.projects.prepare_link(owner, {
            "project_id": project, "item": {"type": "action", "id": action["row_id"], "business_id": project}},
            InvocationOptions(idempotency_key=new_id()), uow)
    assert plan.arguments["item"] == {"type": "action", "id": action["action_id"], "business_id": project}
    expected = next(value for value in plan.expected_targets if value.resource.resource_type == "action_logs")
    assert (expected.resource.resource_id, expected.resource.business_id) == (action["row_id"], project)
    accepted = ok(call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": action["row_id"], "business_id": project}}))
    assert accepted["item"]["id"] == action["action_id"] and accepted["already_present"] is True
    canonical = ok(call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": action["action_id"], "business_id": project}}))
    assert canonical["item"]["id"] == action["action_id"] and canonical["already_present"] is True
    # A different Project can read the source Action and link it explicitly, still without changing membership.
    other = seed_project(host, name="Bare Annex", slug="bare-annex")
    foreign_link = ok(call(host, context(host, other), "project_items_link", {
        "project_id": other, "item": {"type": "action", "id": action["action_id"], "business_id": project}}))
    assert foreign_link["link"]["target_namespace"] == "business:" + project


def test_an_unreadable_action_source_is_refused_without_changing_the_target(host):
    project = seed_project(host, name="Action Source Court", slug="action-source-court")
    archived = seed_project(host, name="Action Source Vault", slug="action-source-vault")
    owner = context(host, project)
    shared = ok(call(host, owner, "actions_record", {
        "text": "An action whose only membership will be archived", "category": "business-operational",
        "real_time": "2026-10-20", "business_ids": [archived]}))["action"]
    with host.db.transaction() as uow:
        current = uow.repository("businesses").get(archived)
        uow.repository("businesses").update(archived, {"archived_at": "2026-01-01T00:00:00Z"},
                                            expected_revision=current["revision"])
    # The owner may still read the Global-stored Action and link it explicitly; no membership is added.
    positive = ok(call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": shared["action_id"], "business_id": None}}))
    assert positive["link"]["target_namespace"] == "global"
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM action_businesses WHERE action_key=?",
                                      (shared["action_id"],)).fetchone()[0] == 1
    # The configured Ordubashy cannot read that Action now, so its Project link is refused.
    denied = call(host, agent(host, project), "project_items_link", {
        "project_id": project, "item": {"type": "action", "id": shared["action_id"], "business_id": None}})
    assert denied.status == "error"
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 1
