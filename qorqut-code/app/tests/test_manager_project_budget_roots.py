"""Production-budget edge in the completed Project inventory revision; synthetic instance only."""
from test_project_records import host, call, ok, task, context, seed_project, list_items
from test_project_pagination import insert_tasks
from bazarlyk.domains.projects.inventory import Inventory, AUTHORIZATION_BUDGET


def test_root_filter_is_not_skipped_at_authorization_budget_boundary(host, monkeypatch):
    project = seed_project(host, name="Budget roots", slug="budget-roots")
    owner = context(host, project)
    parent = task(host, project, "Represented open parent")
    child = ok(call(host, owner, "tasks_create", {"title": "Child is not a root", "parent_id": parent["id"]}))["task"]
    insert_tasks(host, project, AUTHORIZATION_BUDGET - 1, prefix="Newer closed work", status="done", updated="2030-03-01T00:00:00Z")
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET updated_at=? WHERE business_id=? AND id=?", ("2030-02-01T00:00:00Z", project, child["id"]))
        uow.connection.execute("UPDATE tasks SET updated_at=? WHERE business_id=? AND id=?", ("2030-01-01T00:00:00Z", project, parent["id"]))
    original = Inventory.resolve_target
    calls = []
    def counted(self, *args, **kwargs):
        calls.append(args[2:5])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Inventory, "resolve_target", counted)
    page = list_items(host, owner, project, section="work", root_tasks_only=True, limit=1)
    assert len(calls) <= AUTHORIZATION_BUDGET
    assert child["id"] not in {item["id"] for item in page["items"]}, {"unexpected_non_root": page["items"], "reads": len(calls), "cursor": page["next_cursor"]}
    found = list(page["items"])
    cursor = page["next_cursor"]
    for _ in range(3):
        if not cursor:
            break
        next_page = list_items(host, owner, project, section="work", root_tasks_only=True, limit=1, cursor=cursor)
        found.extend(next_page["items"])
        cursor = next_page["next_cursor"]
    assert cursor is None
    assert [item["id"] for item in found] == [parent["id"]]
