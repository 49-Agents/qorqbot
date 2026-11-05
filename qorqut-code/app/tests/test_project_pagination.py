"""Bounded Project inventory pagination: no total cap, scoped ties, filtered windows and real Task hierarchy."""

from __future__ import annotations

from qorqut_operations.contracts import new_id

from test_project_records import (call, context, host, item_of, list_items, ok, seed_project, task)


def insert_tasks(host, project, count, *, prefix, status="open", updated="2026-10-20T00:00:00Z"):
    template = {"kind": "ordinary", "fields_json": "{}"}
    ids = [new_id() for _ in range(count)]
    with host.db.transaction() as uow:
        uow.connection.executemany(
            "INSERT INTO tasks(business_id,id,title,status,fields_json,archived,created_by,revision,created_at,updated_at,kind) "
            "VALUES(?,?,?,?,?,0,?,1,?,?,?)",
            [(project, identity, f"{prefix} {position}", status, template["fields_json"], host.instance.owner,
              updated, updated, template["kind"]) for position, identity in enumerate(ids)])
    return ids


def all_pages(host, ctx, project, *, limit=100, **arguments):
    found, cursor = [], None
    while True:
        result = list_items(host, ctx, project, limit=limit, **arguments,
                            **({"cursor": cursor} if cursor else {}))
        found.extend(result["items"])
        cursor = result["next_cursor"]
        if not cursor:
            return found


def test_records_beyond_the_old_scan_cap_are_reachable_with_no_total_cap(host):
    project = seed_project(host, name="Deep Court", slug="deep-court")
    inserted = insert_tasks(host, project, 2050, prefix="Deep task")
    owner = context(host, project)
    found, cursor, pages = [], None, 0
    while True:
        result = list_items(host, owner, project, section="work", limit=100,
                            **({"cursor": cursor} if cursor else {}))
        found.extend(result["items"])
        cursor, pages = result["next_cursor"], pages + 1
        if not cursor:
            break
        assert pages < 40, "pagination must terminate"
    ids = {item["id"] for item in found}
    assert len(found) == 2050 and len(ids) == 2050
    assert set(inserted) <= ids
    # The last Task in the total order is reached too; nothing was lost behind a window cap.
    assert inserted[-1] in ids


def test_equal_timestamp_records_in_two_scopes_are_never_skipped(host):
    project = seed_project(host, name="Tie Court", slug="tie-court")
    foreign = seed_project(host, name="Tie Vault", slug="tie-vault")
    shared_id, now = new_id(), "2026-10-20T00:00:00Z"
    template = {"kind": "ordinary", "fields_json": "{}"}
    with host.db.transaction() as uow:
        for business in (project, foreign):
            uow.connection.execute(
                "INSERT INTO tasks(business_id,id,title,status,fields_json,archived,created_by,revision,created_at,updated_at,kind) "
                "VALUES(?,?,?,?,?,0,?,1,?,?,?)",
                (business, shared_id, f"Same id in {business}", "open", template["fields_json"],
                 host.instance.owner, now, now, template["kind"]))
    owner = context(host, project)
    linked = ok(call(host, owner, "project_items_link", {
        "project_id": project, "item": {"type": "task", "id": shared_id, "business_id": foreign}}))
    assert linked["link"]["item"]["business_id"] == foreign
    seen, cursor = [], None
    while True:
        page = list_items(host, owner, project, section="work", limit=1,
                          **({"cursor": cursor} if cursor else {}))
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert {(item["id"], item["business_id"]) for item in seen} == {
        (shared_id, project), (shared_id, foreign)}


def test_newer_closed_records_do_not_hide_older_open_ones_and_are_filtered(host):
    project = seed_project(host, name="Window Court", slug="window-court")
    closed = insert_tasks(host, project, 120, prefix="Closed task", status="done",
                          updated="2026-10-25T00:00:00Z")
    opened = insert_tasks(host, project, 5, prefix="Open task", status="open",
                          updated="2026-10-20T00:00:00Z")
    page = list_items(host, context(host, project), project, section="work", limit=5)
    assert {item["id"] for item in page["items"]} == set(opened)
    assert page["next_cursor"] is None
    with_closed = all_pages(host, context(host, project), project, section="work", include_closed=True)
    assert set(closed) <= {item["id"] for item in with_closed} and set(opened) <= {item["id"] for item in with_closed}


def test_root_tasks_only_uses_represented_parents_after_filters(host):
    project = seed_project(host, name="Root Court", slug="root-court")
    foreign = seed_project(host, name="Root Vault", slug="root-vault")
    owner = context(host, project)
    parent = task(host, project, "Owned parent")
    child = ok(call(host, owner, "tasks_create", {"title": "Owned child", "parent_id": parent["id"]}))["task"]
    foreign_parent = task(host, foreign, "Linked parent")
    foreign_child = ok(call(host, context(host, foreign),
                            "tasks_create", {"title": "Linked child", "parent_id": foreign_parent["id"]}))["task"]
    lone = task(host, foreign, "Lone linked root")
    for identity in (foreign_parent["id"], foreign_child["id"], lone["id"]):
        ok(call(host, owner, "project_items_link", {
            "project_id": project, "item": {"type": "task", "id": identity, "business_id": foreign}}))
    roots = list_items(host, owner, project, section="work", root_tasks_only=True)["items"]
    assert {item["id"] for item in roots} == {parent["id"], foreign_parent["id"], lone["id"]}
    # A linked child whose parent is not linked is a root; a represented parent's child is not.
    assert foreign_child["id"] not in {item["id"] for item in roots}
    assert child["id"] not in {item["id"] for item in roots}
    # A hidden (closed) represented parent no longer hides its open child.
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?", (project, parent["id"]))
    hidden_parent = list_items(host, owner, project, section="work", root_tasks_only=True)["items"]
    assert child["id"] in {item["id"] for item in hidden_parent}
    assert parent["id"] not in {item["id"] for item in hidden_parent}
    # With closed history requested the parent is represented again and the child stays below it.
    with_closed = list_items(host, owner, project, section="work", root_tasks_only=True, include_closed=True)["items"]
    assert parent["id"] in {item["id"] for item in with_closed}
    assert child["id"] not in {item["id"] for item in with_closed}


def test_related_records_page_independently_without_overlap(host):
    project = seed_project(host, name="Related Court", slug="related-court")
    owner = context(host, project)
    for position in range(5):
        task(host, project, f"Related task {position}")
    with host.db.read() as uow:
        first = host.runtime.projects.related(owner, project, {"offset": 0, "limit": 2}, uow)
        second = host.runtime.projects.related(owner, project, {"offset": 2, "limit": 2}, uow)
        rest = host.runtime.projects.related(owner, project, {"offset": 4, "limit": 2}, uow)
    first_ids = {item["id"] for item in first["items"]}
    second_ids = {item["id"] for item in second["items"]}
    rest_ids = {item["id"] for item in rest["items"]}
    assert len(first_ids) == 2 and len(second_ids) == 2 and first["next_offset"] == 2
    assert not (first_ids & second_ids) and not (first_ids & rest_ids) and not (second_ids & rest_ids)
    assert rest["next_offset"] is None


def test_story_and_customer_containers_keep_presentation_and_parent_semantics(host):
    project = seed_project(host, name="Container Court", slug="container-court")
    owner = context(host, project)
    story = ok(call(host, owner, "tasks_create", {"title": "Container story", "task_type": "story"}))["task"]
    customer = ok(call(host, owner, "tasks_create", {"title": "Container customer", "task_type": "customer"}))["task"]
    ordinary = task(host, project, "Ordinary closed task")
    child = ok(call(host, owner, "tasks_create", {"title": "Story child", "parent_id": story["id"]}))["task"]
    with host.db.transaction() as uow:
        for identity in (story["id"], customer["id"], ordinary["id"]):
            uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?", (project, identity))
    page = list_items(host, owner, project, section="work", limit=100)["items"]
    found = {item["id"]: item for item in page}
    assert story["id"] in found and customer["id"] in found
    assert ordinary["id"] not in found and child["id"] in found
    assert found[story["id"]]["task_type"] == "story" and found[customer["id"]]["task_type"] == "customer"
    roots = list_items(host, owner, project, section="work", root_tasks_only=True, limit=100)["items"]
    assert story["id"] in {item["id"] for item in roots}
    assert child["id"] not in {item["id"] for item in roots}


def test_a_global_task_cursor_chain_reaches_every_tied_and_distinct_identity(host):
    project = seed_project(host, name="Global Chain Court", slug="global-chain-court")
    same = insert_tasks(host, None, 3, prefix="Global tied", updated="2031-01-01T00:00:00Z")
    distinct = insert_tasks(host, None, 2, prefix="Global distinct", updated="2031-01-02T00:00:00Z")
    args = {"project_id": project, "source_business_id": None, "type": "task", "limit": 1, "query": "Global"}
    found, cursor, requests = [], None, 0
    while True:
        page = ok(call(host, context(host, project), "project_items_candidates",
                       {**args, **({"cursor": cursor} if cursor else {})}))
        found.extend(item["id"] for item in page["items"])
        cursor, requests = page["next_cursor"], requests + 1
        if not cursor:
            break
        assert requests < 10
    assert set(found) == set(same) | set(distinct)
    assert len(found) == 5


def test_linked_source_time_wins_over_link_time_for_source_order(host):
    project = seed_project(host, name="Source Order Court", slug="source-order-court")
    foreign = seed_project(host, name="Source Order Vault", slug="source-order-vault")
    newer = task(host, project, "Newer owned source")
    older = task(host, foreign, "Older source linked today")
    ok(call(host, context(host, project), "project_items_link", {
        "project_id": project, "item": {"type": "task", "id": older["id"], "business_id": foreign}}))
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET updated_at='2026-02-01T00:00:00Z' WHERE business_id=? AND id=?",
                               (project, newer["id"]))
        uow.connection.execute("UPDATE tasks SET updated_at='2026-01-01T00:00:00Z' WHERE business_id=? AND id=?",
                               (foreign, older["id"]))
    page = list_items(host, context(host, project), project, section="work", limit=2)
    assert [item["id"] for item in page["items"]] == [newer["id"], older["id"]]
