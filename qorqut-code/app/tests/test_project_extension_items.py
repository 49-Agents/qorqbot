"""A genuine installed extension item kind is listed and read through its own contribution."""

from __future__ import annotations

from types import SimpleNamespace

from qorqut_operations.contracts import new_id
from qorqut_operations.items import ItemContribution

from bazarlyk.extensions import Extension, select
from host_foundation_support import build_instance
from bazarlyk.domains.projects.inventory import order_key
from test_project_records import call, item_of, list_items, ok, seed_project

FIXTURE_ITEM = new_id()


class FixtureLookup:
    """A synthetic extension's typed item lookup; it owns its own enumeration and read authorization."""

    def candidates(self, ctx, uow, kind=None):
        if not kind or kind == "fixture_item":
            yield {"type": "fixture_item", "id": FIXTURE_ITEM}

    def resource(self, ctx, item, uow):
        return "fixture_items", {"id": FIXTURE_ITEM, "business_id": ctx.business_id, "revision": 1}

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        return {"type": "fixture_item", "id": FIXTURE_ITEM, "business_id": ctx.business_id,
                "label": "Fixture item", "revision": 1, "resource_type": "fixture_items", "state": "open",
                "summary": "A synthetic extension item", "created_at": "2026-10-20T00:00:00Z",
                "updated_at": "2026-10-20T00:00:00Z"}

    def relationship_item(self, ctx, item, uow):
        return {"type": "fixture_item", "id": FIXTURE_ITEM, "business_id": ctx.business_id}

    def fingerprint(self, ctx, item, uow, builder=None):
        return "fixture-fingerprint"

    def project_inventory_window(self, ctx, uow, *, kind, project_id, source_business_id, include_links,
                                  after, limit):
        if kind != "fixture_item":
            return {"rows": [], "has_more": False}
        rows = [{"type": kind, "id": FIXTURE_ITEM, "business_id": source_business_id,
                 "updated_at": "2026-10-20T00:00:00Z"}]
        if include_links:
            linked = uow.connection.execute(
                "SELECT target_business_id FROM project_item_links WHERE business_id=? AND target_type=? AND target_id=?",
                (project_id, kind, FIXTURE_ITEM)).fetchall()
            for link in linked:
                if link["target_business_id"] != source_business_id:
                    rows.append({"type": kind, "id": FIXTURE_ITEM, "business_id": link["target_business_id"],
                                 "updated_at": "2026-10-20T00:00:00Z"})
        rows = [row for row in rows if after is None or
                order_key((row["updated_at"], kind, row["business_id"] or "", FIXTURE_ITEM)) > order_key(tuple(after))]
        return {"rows": rows[:limit], "has_more": len(rows) > limit}


class FixtureExtension(Extension):
    id = "fixture"

    def item_kinds(self):
        return (ItemContribution("fixture-items", ("fixture_item",), FixtureLookup()),)


def test_an_installed_extension_item_kind_is_inventory_and_exactly_readable(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",),
                                   extensions=select(instances=[FixtureExtension()]))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], owner_context=owner_context, ordubashy=None,
                           business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Extension Court", slug="extension-court")
        ctx = owner_context(project)
        listing = list_items(host, ctx, project, section="other", limit=100)["items"]
        found = item_of(listing, "fixture_item", FIXTURE_ITEM)
        assert found["label"] == "Fixture item" and found["business_id"] == project
        assert found["business_name"] == "Extension Court"
        detail = ok(call(host, ctx, "project_item_get", {
            "project_id": project, "item": {"type": "fixture_item", "id": FIXTURE_ITEM, "business_id": project}}))
        assert detail["record"]["item"]["label"] == "Fixture item"
        assert detail["item"]["id"] == FIXTURE_ITEM
    finally:
        instance.runtime.release_instance()
        instance.database.close()


MANY_ITEMS = {
    "11111111-1111-4111-8111-111111111111": "2026-01-01T00:00:00Z",
    "22222222-2222-4222-8222-222222222222": "2026-01-02T00:00:00Z",
    "33333333-3333-4333-8333-333333333333": "2026-01-03T00:00:00Z",
}


class ManyLookup(FixtureLookup):
    def candidates(self, ctx, uow, kind=None):
        if not kind or kind == "many_item":
            for identity in MANY_ITEMS:
                yield {"type": "many_item", "id": identity}

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy=False):
        identity = item["id"]
        return {"type": "many_item", "id": identity, "business_id": ctx.business_id,
                "label": f"Many {identity[:4]}", "revision": 1, "resource_type": "many_items",
                "summary": "", "created_at": MANY_ITEMS[identity], "updated_at": MANY_ITEMS[identity]}

    def resource(self, ctx, item, uow):
        return "many_items", {"id": item["id"], "business_id": ctx.business_id, "revision": 1}

    def project_inventory_window(self, ctx, uow, *, kind, project_id, source_business_id, include_links,
                                 after, limit):
        if kind != "many_item":
            return {"rows": [], "has_more": False}
        scope = source_business_id or ""
        rows = [{"type": kind, "id": identity, "business_id": source_business_id,
                 "updated_at": stamp} for identity, stamp in MANY_ITEMS.items()]
        if include_links:
            linked = uow.connection.execute(
                "SELECT target_id,target_business_id FROM project_item_links "
                "WHERE business_id=? AND target_type=?", (project_id, kind)).fetchall()
            existing = {(row["id"], row["business_id"]) for row in rows}
            for link in linked:
                if (link["target_id"], link["target_business_id"]) not in existing:
                    rows.append({"type": kind, "id": link["target_id"],
                                 "business_id": link["target_business_id"],
                                 "updated_at": MANY_ITEMS.get(link["target_id"], "1970-01-01T00:00:00Z")})
        rows.sort(key=lambda row: order_key((row["updated_at"], row["type"],
                                             row["business_id"] or "", row["id"])))
        if after is not None:
            rows = [row for row in rows if order_key((row["updated_at"], row["type"],
                    row["business_id"] or "", row["id"])) > order_key(tuple(after))]
        return {"rows": rows[:limit], "has_more": len(rows) > limit}


class ManyExtension(Extension):
    id = "many"

    def item_kinds(self):
        return (ItemContribution("many-items", ("many_item",), ManyLookup()),)


def test_extension_entries_and_sql_entries_share_one_bounded_order(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",),
                                   extensions=select(instances=[ManyExtension()]))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], owner_context=owner_context, ordubashy=None,
                           business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Many Court", slug="many-court")
        owner = owner_context(project)
        task_ids = []
        for title, stamp in (("Newest task", "2026-01-04T00:00:00Z"), ("Oldest task", "2025-12-31T00:00:00Z")):
            row = ok(call(host, owner, "tasks_create", {"title": title}))["task"]
            with instance.database.transaction() as uow:
                uow.connection.execute("UPDATE tasks SET updated_at=? WHERE business_id=? AND id=?",
                                       (stamp, project, row["id"]))
            task_ids.append(row["id"])
        found, cursor = [], None
        while True:
            page = list_items(host, owner, project, section="all", limit=1,
                              **({"cursor": cursor} if cursor else {}))
            found.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            assert len(found) <= 5
            if not cursor:
                break
        assert set(found) == set(MANY_ITEMS) | set(task_ids)
        assert len(found) == 5
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def test_a_linked_extension_uses_its_source_time_and_keeps_its_true_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(__import__(__name__), "MANY_ITEMS", {
        "44444444-4444-4444-8444-444444444444": "2026-01-01T00:00:00Z"})
    instance, ids = build_instance(tmp_path, businesses=("first", "foreign"),
                                   extensions=select(instances=[ManyExtension()]))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], foreign=ids["foreign"], owner_context=owner_context,
                           ordubashy=None, business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Extension Source Court", slug="extension-source-court")
        foreign = seed_project(host, name="Extension Source Vault", slug="extension-source-vault")
        owner = owner_context(project)
        task = ok(call(host, owner, "tasks_create", {"title": "Newer owned task"}))["task"]
        with instance.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET updated_at='2027-01-01T00:00:00Z' WHERE business_id=? AND id=?",
                                   (project, task["id"]))
        identity = "44444444-4444-4444-8444-444444444444"
        link = ok(call(host, owner, "project_items_link", {
            "project_id": project, "item": {"type": "many_item", "id": identity, "business_id": foreign}}))
        assert link["link"]["target_namespace"] == "business:" + foreign
        page = list_items(host, owner, project, section="all", limit=10)
        first = next(item for item in page["items"] if item["type"] == "many_item"
                     and item["business_id"] == foreign)
        assert first["updated_at"] == "2026-01-01T00:00:00Z"
        assert page["items"][0]["id"] == task["id"]
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def test_core_agent_contribution_uses_a_bounded_metadata_window(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], owner_context=owner_context, ordubashy=None,
                           business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Agent Metadata Court", slug="agent-metadata-court")
        owner = owner_context(project)
        result = ok(call(host, owner, "project_items_candidates", {
            "project_id": project, "source_business_id": None, "type": "agent", "limit": 100}))
        assert result["items"] == [] or all(item["type"] == "agent" for item in result["items"])
        assert result["next_cursor"] is None or isinstance(result["next_cursor"], str)
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def test_only_the_selected_extension_kind_is_enumerated(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("an unselected extension provider was enumerated")
    monkeypatch.setattr(FixtureLookup, "project_inventory_window", forbidden)
    instance, ids = build_instance(tmp_path, businesses=("first",),
                                   extensions=select(instances=[ManyExtension(), FixtureExtension()]))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], owner_context=owner_context, ordubashy=None,
                           business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Extension Isolation Court", slug="extension-isolation-court")
        owner = owner_context(project)
        page = ok(call(host, owner, "project_items_candidates", {
            "project_id": project, "source_business_id": project, "type": "many_item", "limit": 100}))
        assert page["items"] and all(item["type"] == "many_item" for item in page["items"])
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def test_mixed_extension_and_sql_ties_use_the_same_total_order(tmp_path, monkeypatch):
    item_id = "55555555-5555-4555-8555-555555555555"
    monkeypatch.setattr(__import__(__name__), "MANY_ITEMS", {item_id: "2027-06-01T00:00:00Z"})
    instance, ids = build_instance(tmp_path, businesses=("first",),
                                   extensions=select(instances=[ManyExtension()]))
    instance.runtime.freeze_registry()

    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")
    host = SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime,
                           first=ids["first"], owner_context=owner_context, ordubashy=None,
                           business_owner=instance.human(ids["first"]))
    try:
        project = seed_project(host, name="Extension Tie Court", slug="extension-tie-court")
        owner = owner_context(project)
        task = ok(call(host, owner, "tasks_create", {"title": "Tied task"}))["task"]
        with instance.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET updated_at='2027-06-01T00:00:00Z' WHERE business_id=? AND id=?",
                                   (project, task["id"]))
        page = list_items(host, owner, project, section="all", limit=10)
        tied = [item for item in page["items"] if item["updated_at"] == "2027-06-01T00:00:00Z"]
        assert [item["type"] for item in tied] == ["many_item", "task"]
    finally:
        instance.runtime.release_instance()
        instance.database.close()
