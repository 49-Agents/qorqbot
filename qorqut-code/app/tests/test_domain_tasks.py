from datetime import datetime, timezone
import pytest

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import InvocationOptions, canonical_hash
from bazarlyk.domains.tasks import TaskOperations
from bazarlyk.domains.tasks.models import DESIGN_SOFTWARE_TAG
from foundation_support import BUSINESS, OTHER, HUMAN, FixedPolicy, context, database_at
from bazarlyk.transport.mcp_presentation import present


def make_tasks(tmp_path):
    database = database_at(tmp_path)
    registry = builtin_registry()
    service = TaskOperations(clock=lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    app = Application(database, registry, policy=FixedPolicy())
    service.register(registry, app)
    return database, app


def invoke(app, operation, arguments, *, revision=None, key=None):
    return app.invoke(context(), operation, arguments, InvocationOptions(expected_revision=revision, idempotency_key=key or canonical_hash({"operation": operation, "args": arguments, "revision": revision})))


def test_task_create_assign_comment_and_completion_share_the_core(tmp_path):
    database, app = make_tasks(tmp_path)
    result = invoke(app, "tasks_create", {"id": "launch", "title": "Launch", "tags": ["marketing"]})
    assert result.status == "ok", result.to_dict()
    assert result.data["task"]["assignee_ids"] == ()
    assigned = invoke(app, "tasks_assign", {"task_id": "launch", "assignee_ids": [HUMAN]}, revision=1)
    assert assigned.data["task"]["assignee_ids"] == (HUMAN,)
    assert assigned.data["task"]["revision"] == 2
    stale = invoke(app, "tasks_comment", {"task_id": "launch", "text": "Stale comment"}, revision=1)
    assert stale.error["code"] == "revision_conflict"
    comment = invoke(app, "tasks_comment", {"task_id": "launch", "text": "Ready for review"}, revision=2)
    assert comment.status == "ok", comment.to_dict()
    assert comment.data["task"]["notes"][0]["created_by"] == HUMAN
    completed = invoke(app, "tasks_complete", {"task_id": "launch"}, revision=3)
    assert completed.data["task"]["closed"] is True
    assert app.invoke(context(), "tasks_list", {"include_closed": False}).data["tasks"] == ()
    with database.read() as uow:
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def test_task_cycle_self_and_cross_business_blockers_are_rejected(tmp_path):
    database, app = make_tasks(tmp_path)
    assert invoke(app, "tasks_create", {"id": "a", "title": "A"}).status == "ok"
    assert invoke(app, "tasks_create", {"id": "b", "title": "B", "blocked_by": ["a"]}).status == "ok"
    result = invoke(app, "tasks_update", {"task_id": "a", "changes": {"blocked_by": ["b"]}}, revision=1)
    assert result.error["code"] == "validation_error"
    assert invoke(app, "tasks_create", {"id": "self", "title": "Self", "blocked_by": ["self"]}).error["code"] == "validation_error"
    assert invoke(app, "tasks_create", {"id": "missing", "title": "Missing", "blocked_by": ["elsewhere"]}).error["code"] == "validation_error"
    completed = invoke(app, "tasks_complete", {"task_id": "a"}, revision=1)
    assert completed.status == "ok"
    assert app.invoke(context(), "tasks_get", {"task_id": "b"}).data["task"]["unresolved_blockers"] == ()


def test_configuration_revalidates_existing_tasks_and_custom_types(tmp_path):
    _, app = make_tasks(tmp_path)
    config = app.invoke(context(), "tasks_config_get").to_dict()["data"]["config"]
    config["fields"] = {"priority": {"type": "int"}, "due_at": {"type": "date"}}
    assert invoke(app, "tasks_config_update", {"config": config}).status == "ok"
    created = invoke(app, "tasks_create", {"id": "one", "title": "One", "fields": {"priority": 3}})
    assert created.status == "ok"
    config["fields"] = {}
    assert invoke(app, "tasks_config_update", {"config": config}, revision=1).error["code"] == "validation_error"
    assert invoke(app, "tasks_create", {"id": "two", "title": "Two", "fields": {"priority": True}}).error["code"] == "validation_error"


def test_compact_task_read_preserves_valid_custom_field_values(tmp_path):
    _, app = make_tasks(tmp_path)
    config = app.invoke(context(), "tasks_config_get").to_dict()["data"]["config"]
    config["fields"] = {"blocked": {"type": "bool"}, "stale": {"type": "bool"},
                         "zero": {"type": "int"}, "empty": {"type": "string"}}
    assert invoke(app, "tasks_config_update", {"config": config}).status == "ok"
    created = invoke(app, "tasks_create", {"id": "authored", "title": "Authored values",
        "fields": {"blocked": False, "stale": False, "zero": 0, "empty": ""}})
    assert created.status == "ok", created.to_dict()
    read = app.invoke(context(), "tasks_get", {"task_id": "authored"}).to_dict()
    projected = present("tasks_get", read)
    assert projected["data"]["task"]["fields"] == {"blocked": False, "stale": False,
        "zero": 0, "empty": ""}


@pytest.mark.parametrize("mode", ["closed", "open"])
def test_design_software_is_builtin_tag_without_rewriting_existing_config(tmp_path, mode):
    database, app = make_tasks(tmp_path)
    config = app.invoke(context(), "tasks_config_get").to_dict()["data"]["config"]
    assert DESIGN_SOFTWARE_TAG in config["tags"]["list"]

    # An older closed configuration does not need a data/config rewrite before
    # the built-in classification becomes usable.
    old = {**config, "tags": {**config["tags"], "mode": mode,
        "list": [tag for tag in config["tags"]["list"] if tag != DESIGN_SOFTWARE_TAG] + ["custom_tag"]}}
    assert invoke(app, "tasks_config_update", {"config": old}).status == "ok"
    with database.read() as uow:
        before_read_revision = uow.repository("business_configs", business_id=BUSINESS).get("tasks")["revision"]
    projected = app.invoke(context(), "tasks_config_get").to_dict()["data"]["config"]
    assert DESIGN_SOFTWARE_TAG in projected["tags"]["list"] and "custom_tag" in projected["tags"]["list"]
    with database.read() as uow:
        saved = uow.repository("business_configs", business_id=BUSINESS).get("tasks")
        assert DESIGN_SOFTWARE_TAG not in saved["config_json"]["tags"]["list"]
        assert saved["revision"] == before_read_revision

    created = invoke(app, "tasks_create", {"id": "software", "title": "One line code fix",
        "tags": [DESIGN_SOFTWARE_TAG, "custom_tag"]})
    assert created.status == "ok", created.to_dict()
    assert list(created.data["task"]["tags"]) == [DESIGN_SOFTWARE_TAG, "custom_tag"]
    untagged = invoke(app, "tasks_create", {"id": "untagged", "title": "No classification"})
    assert untagged.status == "ok" and list(untagged.data["task"]["tags"]) == []


def test_design_software_does_not_loosen_state_or_field_identifiers(tmp_path):
    _, app = make_tasks(tmp_path)
    config = app.invoke(context(), "tasks_config_get").to_dict()["data"]["config"]
    bad_state = {**config, "states": [{**config["states"][0], "name": "in/progress"}, *config["states"][1:]]}
    assert invoke(app, "tasks_config_update", {"config": bad_state}).error["code"] == "validation_error"
    bad_field = {**config, "fields": {"build/output": {"type": "string"}}}
    assert invoke(app, "tasks_config_update", {"config": bad_field}).error["code"] == "validation_error"
    bad_tag = {**config, "tags": {**config["tags"], "list": ["Bad Tag"]}}
    assert invoke(app, "tasks_config_update", {"config": bad_tag}).error["code"] == "validation_error"


def test_generic_update_cannot_bypass_assignment_or_completion_policy(tmp_path):
    _, app = make_tasks(tmp_path)
    assert invoke(app, "tasks_create", {"id": "one", "title": "One"}).status == "ok"
    app.policy.overrides["tasks_assign"] = "deny"
    assert invoke(app, "tasks_update", {"task_id": "one", "changes": {"assignee_ids": [HUMAN]}}, revision=1).error["code"] == "policy_denied"
    app.policy.overrides["tasks_complete"] = "deny"
    assert invoke(app, "tasks_update", {"task_id": "one", "changes": {"status": "done"}}, revision=1).error["code"] == "policy_denied"


def test_task_list_cursor_filters_and_scope(tmp_path):
    _, app = make_tasks(tmp_path)
    for identity in ("a", "b", "c"):
        assert invoke(app, "tasks_create", {"id": identity, "title": identity}).status == "ok"
    first = app.invoke(context(), "tasks_list", {"limit": 2}).data
    second = app.invoke(context(), "tasks_list", {"limit": 2, "cursor": first["next_cursor"]}).data
    assert len(first["tasks"]) == 2 and len(second["tasks"]) == 1
    assert app.invoke(context(), "tasks_list", {"limit": 2, "search": "x", "cursor": first["next_cursor"]}).error["code"] == "validation_error"


def test_task_creator_is_stable_across_assignment_changes(tmp_path):
    database, app = make_tasks(tmp_path)
    assert invoke(app, "tasks_create", {"id": "attribution", "title": "Assigned task"}).status == "ok"
    assigned = invoke(app, "tasks_assign", {"task_id": "attribution", "assignee_ids": [HUMAN]}, revision=1)
    assert assigned.status == "ok", assigned.to_dict()
    task = app.invoke(context(), "tasks_get", {"task_id": "attribution"}).data["task"]
    assert task["created_by"] == HUMAN
    # A later ordinary edit or assignment does not change the creator.
    invoke(app, "tasks_update", {"task_id": "attribution", "changes": {"title": "Edited"}}, revision=2)
    task = app.invoke(context(), "tasks_get", {"task_id": "attribution"}).data["task"]
    assert task["created_by"] == HUMAN
    invoke(app, "tasks_assign", {"task_id": "attribution", "assignee_ids": []}, revision=3)
    assert app.invoke(context(), "tasks_get", {"task_id": "attribution"}).data["task"]["created_by"] == HUMAN
    # Legacy/imported edges do not alter the task creator.
    with database.transaction() as uow:
        uow.repository("relationships", business_id=BUSINESS).insert({"id": "legacy-assignment",
            "from_type": "task", "from_id": "attribution", "to_type": "person", "to_id": HUMAN, "role": "assigned_to"})
    task = app.invoke(context(), "tasks_get", {"task_id": "attribution"}).data["task"]
    assert task["assignee_ids"] == (HUMAN,)
    assert task["created_by"] == HUMAN
