"""CRM tables: kinds, memberships of existing Task records, scoped references, projection, stages and atomic creation.

Everything runs on the synthetic CRM host (real engine, Task, Table and CRM services; test doubles only for the ports a
trusted host supplies)."""

from __future__ import annotations

import pytest

from crm_support import CRM_COLUMNS, STAGES, CRMHost, email_task, ref_of
from operational_support import BUSINESS, OTHER
from qorqut_operations.contracts import new_id
from tables_support import COLUMNS


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def global_owner(host):
    return host.owner(business=None)


def tick(host):
    host.later(seconds=1)


# -- kinds ---------------------------------------------------------------------------------------------------------------------

def test_general_tables_are_unchanged_and_crm_tables_are_a_distinct_kind(host):
    general = host.make("Plain")["table"]
    assert (general["table_kind"], general["crm_config"]) == ("general", None)
    saved = host.write(general, [{"cells": {"name": "row"}}], revision=1)
    assert saved["table"]["row_count"] == 1 and saved["rows"][0]["cells"] == {"name": "row"}
    crm = host.crm_table()
    assert (crm["table_kind"], crm["crm_config"]) == ("crm", STAGES) and crm["row_count"] == 0
    # A general table has no memberships; a CRM table holds no data rows; the kind never changes afterwards.
    assert host.error("crm_entries_list", {"table_id": general["id"]}) == "validation_error"
    assert host.error("crm_entries_add", {"table_id": general["id"], "create": {"title": "x"}}, expected_revision=2) == "validation_error"
    assert host.error("tables_rows_write", {"table_id": crm["id"], "rows": [{"cells": {"value": 1}}]}, expected_revision=1) == "validation_error"
    assert host.read("tables_rows_list", {"table_id": crm["id"]})["items"] == []
    assert host.error("tables_update", {"table_id": general["id"], "changes": {"crm_config": STAGES}}, expected_revision=2) == "validation_error"
    assert host.error("tables_update", {"table_id": general["id"], "changes": {"table_kind": "crm"}}, expected_revision=2) == "validation_error"
    with host.database.transaction() as uow:
        for table_id in (general["id"], crm["id"]):
            with pytest.raises(Exception, match="kind"):
                uow.connection.execute("UPDATE data_tables SET table_kind=? WHERE id=?", ("crm" if table_id == general["id"] else "general", table_id))
    assert [item["id"] for item in host.read("tables_list", {"table_kind": "crm"})["items"]] == [crm["id"]]


def test_crm_creation_validates_stages_and_bound_columns(host):
    cases = {
        "duplicate stage": {"stages": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]},
        "blank name": {"stages": [{"id": "a", "name": " "}]},
        "not an object": {"stages": "lead"},
    }
    for label, config in cases.items():
        assert host.error("tables_create", {"title": label, "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": config}) == "validation_error", label
    for label, columns in {
        "unknown field": [{"id": "x", "name": "X", "type": "text", "record_field": "assignee"}],
        "wrong type": [{"id": "x", "name": "X", "type": "number", "record_field": "title"}],
    }.items():
        assert host.error("tables_create", {"title": label, "table_kind": "crm", "columns": columns, "crm_config": STAGES}) == "validation_error", label
    # A general table has no bindings and no stages.
    assert host.error("tables_create", {"title": "g", "columns": CRM_COLUMNS}) == "validation_error"
    assert host.error("tables_create", {"title": "g", "columns": COLUMNS, "crm_config": STAGES}) == "validation_error"
    bare = host.data("tables_create", {"title": "Defaults", "table_kind": "crm"})["table"]
    assert bare["crm_config"] == {"stages": []} or bare["crm_config"]["stages"] is not None
    assert [column["record_field"] for column in bare["columns"] if column.get("record_field")]


# -- memberships of existing records -----------------------------------------------------------------------------------------

def test_all_four_task_types_are_supported_targets_and_the_task_is_untouched(host):
    table = host.crm_table()
    plain = host.create_task("Plain task")
    story = host.create_task("A story", task_type="story")
    customer = host.create_task("Acme", task_type="customer", customer_profile={"email": "ops@acme.test"})
    email = email_task(host)
    references = [ref_of(plain), ref_of(story), ref_of(customer), email]
    before = {reference["id"]: host.task(reference["id"]) for reference in references}
    edges = host.count("relationships")
    for reference in references:
        tick(host)
        added = host.data("crm_entries_add", {"table_id": table["id"], "record": reference}, expected_revision=host.get(table)["revision"])
        table = added["table"]
        assert added["created_record"] is False and added["entry"]["record_ref"] == reference
    items = host.entries(table)["items"]
    assert [item["record"]["task_type"] for item in items] == ["task", "story", "customer", "email"]
    assert items[2]["cells"]["email"] == "ops@acme.test" and items[3]["record"]["email_conversation_id"]
    assert all(host.task(identity) == row for identity, row in before.items())
    assert host.count("relationships") == edges      # a membership is not a relationship edge
    assert table["row_count"] == 4 and [item["position"] for item in items] == [0, 1, 2, 3]


def test_adding_is_idempotent_for_members_and_refuses_conflicting_values(host):
    table = host.crm_table()
    task = host.create_task("Acme", task_type="customer")
    first = host.add(table, task, cells={"value": 3}, stage_id="lead")
    again = host.add(first["table"], task, cells={"value": 3}, stage_id="lead")
    assert again["entry"]["id"] == first["entry"]["id"] and again["table"]["revision"] == first["table"]["revision"]
    assert host.count("table_versions") == 2 and host.count("thread_messages", "item_type='table'") == 1
    key = new_id()
    saved = host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=first["table"]["revision"],
                      idempotency_key=key)
    assert host.data("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=first["table"]["revision"],
                     idempotency_key=key) == saved
    result = host.invoke("crm_entries_add", {"table_id": table["id"], "record": ref_of(task), "cells": {"value": 9}},
                         expected_revision=first["table"]["revision"])
    assert result.error["code"] == "conflict" and result.error["details"]["columns"] == ("value",) or result.error["code"] == "conflict"
    assert host.error("crm_entries_add", {"table_id": table["id"]}, expected_revision=first["table"]["revision"]) == "validation_error"
    both = {"table_id": table["id"], "record": ref_of(task), "create": {"title": "x"}}
    assert host.error("crm_entries_add", both, expected_revision=first["table"]["revision"]) == "validation_error"
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}) == "revision_conflict"   # the revision is required
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task)}, expected_revision=1) == "revision_conflict"


def test_removing_and_restoring_a_membership_loses_nothing(host):
    table = host.crm_table()
    task = host.create_task("Acme")
    added = host.add(table, task, cells={"value": 4, "notes": "keep"}, stage_id="won")
    entry_id = added["entry"]["id"]
    other = host.create_task("Second")
    second = host.add(added["table"], other)
    tick(host)
    removed = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": entry_id}, expected_revision=second["table"]["revision"])
    assert removed["removed"] is True and removed["entry_id"] == entry_id and removed["table"]["row_count"] == 1
    assert [item["id"] for item in host.entries(removed["table"])["items"]] == [second["entry"]["id"]]
    hidden = host.entries(removed["table"], include_removed=True)["items"]
    assert [(item["id"], item["removed"]) for item in hidden] == [(entry_id, True), (second["entry"]["id"], False)]
    assert host.task(task["id"])["archived"] == 0 and host.read("tasks_get", {"task_id": task["id"]})["task"]["title"] == "Acme"
    # Removing again, or removing a removed entry, is a no-change success; editing it is refused.
    same = host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": entry_id}, expected_revision=removed["table"]["revision"])
    assert same["table"]["revision"] == removed["table"]["revision"]
    assert host.error("crm_entries_update", {"table_id": table["id"], "entry_id": entry_id, "changes": {"stage_id": "lost"}},
                      expected_revision=removed["table"]["revision"]) == "validation_error"
    tick(host)
    restored = host.add(removed["table"], task)
    assert restored["entry"]["id"] == entry_id and restored["entry"]["position"] == 0 and restored["entry"]["stage_id"] == "won"
    assert restored["entry"]["local_cells"] == {"value": 4, "notes": "keep"} and restored["entry"]["removed"] is False
    assert host.count("crm_entries") == 2
    bodies = [message["change"]["operation"] for message in host.thread(table)["messages"] if message["change"]]
    assert bodies == ["added", "added", "removed", "restored"]


def test_one_task_may_belong_to_several_crms_without_copying_it(host):
    first, second = host.crm_table("A"), host.crm_table("B")
    task = host.create_task("Shared", task_type="customer")
    one = host.add(first, task, cells={"value": 1})
    two = host.add(second, task, cells={"value": 2}, stage_id="lead")
    assert one["entry"]["id"] != two["entry"]["id"] and host.count("tasks") == 1
    assert [item["local_cells"] for item in (one["entry"], two["entry"])] == [{"value": 1}, {"value": 2}]
    # Edit the Task once: both Tables show it; neither Table revision moves.
    host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Renamed"}}, expected_revision=task["revision"])
    for table in (first, second):
        shown = host.entries(table)["items"][0]
        assert shown["cells"]["name"] == "Renamed" and shown["record"]["title"] == "Renamed"
        assert host.get(table)["revision"] == 2
    assert host.count("table_versions") == 4


def test_task_changes_never_change_the_table_and_projected_cells_are_read_only(host):
    table = host.crm_table()
    task = host.create_task("Acme", task_type="customer", customer_profile={"email": "a@acme.test"})
    added = host.add(table, task)
    revision = added["table"]["revision"]
    host.data("tasks_update", {"task_id": task["id"], "changes": {"customer_profile": {"email": "b@acme.test"}}}, expected_revision=1)
    shown = host.entries(table)["items"][0]
    assert shown["cells"]["email"] == "b@acme.test" and host.get(table)["revision"] == revision
    for cells in ({"name": "x"}, {"email": "x"}):
        assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task), "cells": cells}, expected_revision=revision) == "validation_error"
        assert host.error("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"cells": cells}},
                          expected_revision=revision) == "validation_error"
    assert host.error("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"cells": {"nope": 1}}},
                      expected_revision=revision) == "validation_error"
    assert host.error("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"cells": {"value": "text"}}},
                      expected_revision=revision) == "validation_error"
    # Local values change through the membership only; null clears; the Table takes one revision per batch.
    changed = host.data("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"],
                                               "changes": {"cells": {"value": 8, "notes": "n"}, "stage_id": "won"}}, expected_revision=revision)
    assert changed["entry"]["local_cells"] == {"value": 8, "notes": "n"} and changed["table"]["revision"] == revision + 1
    cleared = host.data("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"],
                                               "changes": {"cells": {"value": None}, "stage_id": None}}, expected_revision=revision + 1)
    assert cleared["entry"]["local_cells"] == {"value": None, "notes": "n"} and cleared["entry"]["stage_id"] is None
    noop = host.data("crm_entries_update", {"table_id": table["id"], "entry_id": added["entry"]["id"], "changes": {"cells": {"notes": "n"}}},
                     expected_revision=revision + 2)
    assert noop["table"]["revision"] == revision + 2


def test_a_projected_custom_field_follows_the_task_definition_and_a_mismatch_is_reported(host):
    definitions = {"v": 1, "fields": {"region": {"type": "string"}, "seats": {"type": "int"}}}
    host.data("tasks_config_update", {"config": definitions}, expected_revision=None)
    assert host.error("tables_create", {"title": "Bad", "table_kind": "crm", "columns": [
        {"id": "s", "name": "Seats", "type": "text", "record_field": "fields.seats"}], "crm_config": STAGES}) == "validation_error"
    assert host.error("tables_create", {"title": "Bad", "table_kind": "crm", "columns": [
        {"id": "s", "name": "Seats", "type": "number", "record_field": "fields.Not a field"}], "crm_config": STAGES}) == "validation_error"
    table = host.crm_table(columns=[{"id": "n", "name": "Name", "type": "text", "record_field": "title"},
                                    {"id": "r", "name": "Region", "type": "text", "record_field": "fields.region"},
                                    {"id": "s", "name": "Seats", "type": "number", "record_field": "fields.seats"}])
    task = host.create_task("Fielded", fields={"region": "EU", "seats": 12})
    host.add(table, task)
    assert host.entries(table)["items"][0]["cells"] == {"n": "Fielded", "r": "EU", "s": 12}
    # Redefining the field later cannot corrupt the Table: the stored value is reported, never coerced.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET fields_json=json_set(fields_json, '$.seats', 'many') WHERE id=?", (task["id"],))
    shown = host.entries(table)["items"][0]
    assert shown["cells"]["s"] is None and shown["cell_errors"] == {"s": "type_mismatch"}


def test_a_projected_custom_field_is_judged_by_the_definition_of_the_tasks_own_scope(host):
    columns = [{"id": "n", "name": "Name", "type": "text", "record_field": "title"},
               {"id": "v", "name": "Value", "type": "text", "record_field": "fields.value"},
               {"id": "r", "name": "Region", "type": "text", "record_field": "fields.region"}]       # no scope defines this one
    # the Table's project defines value as text; the linked Task's project defines it as a date
    host.data("tasks_config_update", {"config": {"v": 1, "fields": {"value": {"type": "string"}}}}, expected_revision=None)
    other = host.owner(business=OTHER)
    host.data("tasks_config_update", {"config": {"v": 1, "fields": {"value": {"type": "date"}}}}, other, expected_revision=None)
    table = host.crm_table(columns=columns)
    foreign = host.create_task("Date source", ctx=other, fields={"value": "2030-01-01"})
    native = host.create_task("Text source", fields={"value": "plain text"})
    host.add(table, foreign)
    host.add(host.get(table), native)
    shown = {item["record"]["title"]: item for item in host.entries(table)["items"]}
    # the date string fits a text column as a Python value but is a different kind of field: reported, not shown
    assert shown["Date source"]["cells"]["v"] is None and shown["Date source"]["cell_errors"] == {"v": "type_mismatch"}
    assert shown["Text source"]["cells"]["v"] == "plain text" and "cell_errors" not in shown["Text source"]
    # search sees only what is shown
    assert [item["record"]["title"] for item in host.entries(table, query="2030")["items"]] == []
    assert [item["record"]["title"] for item in host.entries(table, query="plain text")["items"]] == ["Text source"]
    # nothing was coerced or stored, and a Task edit does not move the Table
    revision = host.get(table)["revision"]
    host.data("tasks_update", {"task_id": native["id"], "changes": {"fields": {"value": "edited"}}}, expected_revision=native["revision"])
    assert host.get(table)["revision"] == revision
    assert host.query("SELECT cells_json FROM crm_entries WHERE record_task_id=?", foreign["id"])[0]["cells_json"] == {}
    # a definition changed later by its own scope is honored on the next read: the column agrees with it again
    host.data("tasks_config_update", {"config": {"v": 1, "fields": {"value": {"type": "string"}}}}, other, expected_revision=1)
    agreed = {item["record"]["title"]: item for item in host.entries(table)["items"]}["Date source"]
    assert agreed["cells"]["v"] == "2030-01-01" and "cell_errors" not in agreed
    # a field no scope defines shows nothing and is no error, even if some stored value exists for it
    bare = host.create_task("No definition", ctx=host.owner(business=OTHER))
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET fields_json=json_set(fields_json, '$.region', 'ghost') WHERE id=?", (bare["id"],))
    host.add(host.get(table), bare)
    for item in host.entries(table)["items"]:
        assert item["cells"]["r"] is None and "r" not in item.get("cell_errors", {})


def test_stage_rules(host):
    table = host.crm_table()
    task = host.create_task("Acme")
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(task), "stage_id": "bogus"}, expected_revision=1) == "validation_error"
    added = host.add(table, task, stage_id="lead")
    revision = added["table"]["revision"]
    kept = [{"id": "lead", "name": "Lead"}, {"id": "won", "name": "Won"}]
    # A stage that memberships (even removed ones) use cannot be dropped; it can be archived and keeps working for them.
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"crm_config": {"stages": kept[1:]}}}, expected_revision=revision) == "validation_error"
    archived = host.data("tables_update", {"table_id": table["id"], "changes": {"crm_config": {"stages": [
        {"id": "lead", "name": "Lead", "archived": True}, *kept[1:]]}}}, expected_revision=revision)["table"]
    assert host.entries(archived)["items"][0]["stage_id"] == "lead"
    other = host.create_task("Other")
    assert host.error("crm_entries_add", {"table_id": table["id"], "record": ref_of(other), "stage_id": "lead"}, expected_revision=archived["revision"]) == "validation_error"
    fresh = host.add(archived, other, stage_id="won")
    unused = host.data("tables_update", {"table_id": table["id"], "changes": {"crm_config": {"stages": [{"id": "lead", "name": "Lead", "archived": True},
                                                                                                       {"id": "won", "name": "Won"}]}}},
                       expected_revision=fresh["table"]["revision"])
    assert [stage["id"] for stage in unused["table"]["crm_config"]["stages"]] == ["lead", "won"]
    assert [item["stage_id"] for item in host.entries(table, stage_id="won")["items"]] == ["won"]
    assert host.error("crm_entries_list", {"table_id": table["id"], "stage_id": "bogus"}) == "validation_error"
    with host.database.transaction() as uow:       # the database refuses an unknown stage and a stage removal under the application
        with pytest.raises(Exception):
            uow.connection.execute("UPDATE crm_entries SET stage_id='bogus' WHERE table_id=?", (table["id"],))
        with pytest.raises(Exception):
            uow.connection.execute("UPDATE data_tables SET crm_config_json=? WHERE id=?", ('{"stages":[]}', table["id"]))


def test_filters_and_sorting_use_local_columns_and_never_task_values(host):
    table = host.crm_table()
    tasks = [host.create_task(name) for name in ("Zed", "Amy", "Bob")]
    for index, task in enumerate(tasks):
        tick(host)
        table = host.add(table, task, cells={"value": 30 - 10 * index})["table"]
    ordered = host.entries(table, order_by="value")["items"]
    assert [item["record"]["title"] for item in ordered] == ["Bob", "Amy", "Zed"]
    assert [item["record"]["title"] for item in host.entries(table, order_by="value", descending=True)["items"]] == ["Zed", "Amy", "Bob"]
    assert [item["record"]["title"] for item in host.entries(table, equals={"value": 20})["items"]] == ["Amy"]
    assert [item["record"]["title"] for item in host.entries(table, query="bo")["items"]] == ["Bob"]
    for arguments in ({"order_by": "name"}, {"equals": {"name": "Zed"}}, {"order_by": "missing"}):
        assert host.error("crm_entries_list", {"table_id": table["id"], **arguments}) == "validation_error"


def test_pages_are_bounded_stable_and_bound_to_the_table_revision(host):
    table = host.crm_table()
    with host.database.transaction() as uow:                     # 105 existing Tasks, so the membership count is the subject
        ids = []
        for number in range(105):
            identity = new_id()
            ids.append(identity)
            uow.repository("tasks", business_id=BUSINESS).insert({
                "id": identity, "title": f"Task {number:03}", "status": "open", "created_by": "00000000-0000-4000-8000-000000000001"})
    revision = 1
    for identity in ids:
        revision = host.data("crm_entries_add", {"table_id": table["id"], "record": {"type": "task", "id": identity, "business_id": BUSINESS}},
                             expected_revision=revision)["table"]["revision"]
    table = host.get(table)
    assert table["row_count"] == 105
    default = host.entries(table)
    assert len(default["items"]) == 50 and default["next_cursor"]
    assert host.error("crm_entries_list", {"table_id": table["id"], "limit": 101}) == "validation_error"
    seen, cursor = [], None
    while True:
        page = host.entries(table, limit=100, **({"cursor": cursor} if cursor else {}))
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 105
    first = host.entries(table, limit=10)
    assert host.error("crm_entries_list", {"table_id": table["id"], "limit": 10, "cursor": first["next_cursor"], "query": "x"}) == "validation_error"
    host.data("crm_entries_remove", {"table_id": table["id"], "entry_id": first["items"][0]["id"]}, expected_revision=table["revision"])
    assert host.error("crm_entries_list", {"table_id": table["id"], "limit": 10, "cursor": first["next_cursor"]}) == "validation_error"
    other = host.crm_table("Elsewhere")
    assert host.error("crm_entries_list", {"table_id": other["id"], "limit": 10, "cursor": first["next_cursor"]}) == "validation_error"


def test_the_table_row_read_serves_a_crms_entries_with_the_same_rules(host):
    table = host.crm_table()
    tasks = [host.create_task(name, task_type="customer") for name in ("Acme", "Bolt")]
    for index, task in enumerate(tasks):
        tick(host)
        table = host.add(table, task, cells={"value": index + 1})["table"]
    rows = host.read("tables_rows_list", {"table_id": table["id"]})
    assert rows == host.entries(table) and [item["cells"]["name"] for item in rows["items"]] == ["Acme", "Bolt"]
    assert [item["record"]["title"] for item in host.read("tables_rows_list", {"table_id": table["id"], "equals": {"value": 2}})["items"]] == ["Bolt"]
    assert host.error("tables_rows_list", {"table_id": table["id"], "equals": {"name": "Acme"}}) == "validation_error"
    assert len(host.read("tables_rows_list", {"table_id": table["id"], "limit": 200})["items"]) == 2      # clamped to a CRM page of at most 100
    assert [item["id"] for item in host.read("tables_list", {"table_kind": "general"})["items"]] == []


def test_schema_edits_of_a_crm_never_touch_tasks_and_keep_history(host):
    table = host.crm_table()
    task = host.create_task("Acme", task_type="customer", customer_profile={"email": "a@acme.test"})
    added = host.add(table, task, cells={"value": 3, "notes": "keep me"})
    before = host.task(task["id"])
    revision = added["table"]["revision"]
    # Removing a local column removes its stored values through an ordinary revisioned edit; one version, no per-entry development.
    without_value = [column for column in CRM_COLUMNS if column["id"] != "value"]
    edited = host.data("tables_update", {"table_id": table["id"], "changes": {"columns": without_value}}, expected_revision=revision)["table"]
    assert edited["revision"] == revision + 1 and host.entries(edited)["items"][0]["local_cells"] == {"notes": "keep me"}
    assert host.versions()[-1]["snapshot_json"]["entries"][0]["cells_json"] == {"notes": "keep me"}
    assert host.count("thread_messages", "item_type='table'") == 1
    # Removing a bound column only stops showing that field; the Task is exactly what it was.
    without_email = [column for column in without_value if column["id"] != "email"]
    shown = host.data("tables_update", {"table_id": table["id"], "changes": {"columns": without_email}}, expected_revision=edited["revision"])["table"]
    assert "email" not in host.entries(shown)["items"][0]["cells"] and host.task(task["id"]) == before
    # An existing column keeps its binding; a local column cannot be re-pointed at a Task field or the other way round.
    for columns in ([{**column, "record_field": "overview"} if column["id"] == "notes" else column for column in without_email],
                    [{k: v for k, v in column.items() if k != "record_field"} if column["id"] == "name" else column for column in without_email],
                    [{**column, "record_field": "status"} if column["id"] == "name" else column for column in without_email]):
        assert host.error("tables_update", {"table_id": table["id"], "changes": {"columns": columns}}, expected_revision=shown["revision"]) == "validation_error"
    added_column = [*without_email, {"id": "overview", "name": "Overview", "type": "text", "record_field": "overview"}]
    grown = host.data("tables_update", {"table_id": table["id"], "changes": {"columns": added_column}}, expected_revision=shown["revision"])["table"]
    assert "overview" in host.entries(grown)["items"][0]["cells"]
    # A local column's type cannot change under values that no longer fit.
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"columns": [
        {**column, "type": "number"} if column["id"] == "notes" else column for column in added_column]}}, expected_revision=grown["revision"]) == "validation_error"
    # An archived CRM is read-only: its schema, stages and memberships are frozen until it is restored.
    archived = host.data("tables_archive", {"table_id": table["id"]}, expected_revision=grown["revision"])["table"]
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"columns": without_email}}, expected_revision=archived["revision"]) == "validation_error"
    assert host.error("tables_update", {"table_id": table["id"], "changes": {"crm_config": STAGES, "title": "x"}}, expected_revision=archived["revision"]) == "validation_error"
    assert host.entries(archived)["items"][0]["record"]["title"] == "Acme"
    restored = host.data("tables_update", {"table_id": table["id"], "changes": {"archived": False}}, expected_revision=archived["revision"])["table"]
    assert restored["archived"] is False and restored["table_kind"] == "crm"
