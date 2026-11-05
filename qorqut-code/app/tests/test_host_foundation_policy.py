"""Acceptance case 3: the host's policy rules and record effects over the extracted Operations engine.

Principals are the owner and the registered global Ordubashy created through the real catalog (role published by the real
publisher, profile registered by ``agents_create``); businesses have no resident agents. Operations are the retained v1 Task operations, so the engine's
policy resolver runs the host's ``PolicyRules``/``AgentAuthority`` and its record path runs ``HostRecordEffects``."""

import pytest
from qorqut_operations.contracts import InvocationOptions, new_id, utc_now

from host_foundation_support import (agent_context, build_instance, create_registered_global, historical_business_agent,
                                     register_sources, snapshot)


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("policy"), businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    agent = create_registered_global(instance, "ordubashy")       # ordinary business work: the coordinator role
    instance.runtime.freeze_registry()
    return instance, first, second, agent


def invoke(instance, ctx, name, arguments, *, revision=None, key="new"):
    return instance.runtime.application.invoke(ctx, name, {"scope": "business", **arguments}, InvocationOptions(
        expected_revision=revision, idempotency_key=new_id() if key == "new" else key))


def create_task(instance, business, title, **arguments):
    created = invoke(instance, instance.human(business), "tasks_create", {"title": title, **arguments})
    assert created.status == "ok", created.error
    return created.data["task"]


def test_the_owner_and_a_registered_global_agent_read_and_mutate_inside_the_selected_business(host):
    instance, first, _, agent = host
    task = create_task(instance, first, "Shared work")
    acting = agent_context(agent, first)

    read = invoke(instance, acting, "tasks_get", {"task_id": task["id"]})
    assert read.status == "ok" and read.data["task"]["title"] == "Shared work"
    listed = invoke(instance, acting, "tasks_list", {})
    assert task["id"] in {item["id"] for item in listed.data["tasks"]}

    # Without the current revision the engine refuses; with it the registered agent's mutation applies.
    stale = invoke(instance, acting, "tasks_update", {"task_id": task["id"], "changes": {"title": "Agent edit"}})
    assert stale.error["code"] == "revision_conflict" and stale.error["current_revision"] == task["revision"]
    edited = invoke(instance, acting, "tasks_update", {"task_id": task["id"], "changes": {"title": "Agent edit"}},
                    revision=task["revision"])
    assert edited.status == "ok" and edited.data["task"]["title"] == "Agent edit"
    assert edited.data["task"]["revision"] == task["revision"] + 1
    created = invoke(instance, acting, "tasks_create", {"title": "Agent task"})
    assert created.status == "ok" and created.data["task"]["created_by"] == agent


def test_a_global_agents_business_selection_never_reaches_another_businesses_records(host):
    # Businesses have no resident agents: the registered global reaches every active business and its own Global target with
    # no membership, but the selected business is the namespace, so selecting the first never addresses the second's records.
    instance, first, second, agent = host
    foreign = create_task(instance, second, "Other business")
    acting = agent_context(agent, first)
    with instance.database.read() as uow:
        before = snapshot(uow.connection, ("tasks", "idempotency_receipts", "apply_receipts"))

    refused = [
        invoke(instance, acting, "tasks_get", {"task_id": foreign["id"]}),
        invoke(instance, acting, "tasks_update", {"task_id": foreign["id"], "changes": {"title": "Taken"}},
               revision=foreign["revision"]),
    ]
    assert [(item.status, item.error["code"]) for item in refused] == [("error", "not_found")] * 2
    with instance.database.read() as uow:
        assert snapshot(uow.connection, ("tasks", "idempotency_receipts", "apply_receipts")) == before
        assert uow.connection.execute("SELECT title FROM tasks WHERE id=?", (foreign["id"],)).fetchone()[0] == \
            "Other business"
    # The reach is derived: the same identity selecting the second business, or the Global target, reads there.
    assert invoke(instance, agent_context(agent, second), "tasks_get", {"task_id": foreign["id"]}).data["task"]["title"] == \
        "Other business"
    assert instance.runtime.application.invoke(acting, "tasks_list", {"scope": "global"}, InvocationOptions()).status == "ok"

    # A business that is not active is never reached, and a historical business-home identity reaches nothing.
    archived = new_id()
    with instance.database.transaction() as uow:
        uow.repository("businesses").insert({"id": archived, "slug": "archived", "name": "Archived",
                                             "workspace": str(instance.paths.business_root("archived")),
                                             "hq_path": str(instance.paths.business_hq("archived"))})
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), archived))
    historical = historical_business_agent(instance, second)
    outside = [
        invoke(instance, agent_context(agent, archived), "tasks_list", {}),
        invoke(instance, agent_context(historical, second), "tasks_get", {"task_id": foreign["id"]}),
        invoke(instance, agent_context(historical, second), "tasks_create", {"title": "Intrusion"}),
    ]
    assert [(item.status, item.error["code"]) for item in outside] == [
        ("error", "business_archived"), ("error", "inactive_agent"), ("error", "inactive_agent")]
    with instance.database.read() as uow:
        assert snapshot(uow.connection, ("tasks", "idempotency_receipts", "apply_receipts")) == before


def test_an_unregistered_agent_identity_has_no_authority(host):
    instance, first, _, _ = host
    task = create_task(instance, first, "Registered only")
    stranger = invoke(instance, agent_context(new_id(), first), "tasks_get", {"task_id": task["id"]})
    assert stranger.status == "error" and stranger.error["code"] == "invalid_principal"


def test_revision_guard_idempotent_replay_and_input_conflict(host):
    instance, first, _, _ = host
    owner = instance.human(first)
    task = create_task(instance, first, "Guarded")
    arguments = {"task_id": task["id"], "changes": {"title": "Guarded v2"}}

    stale = invoke(instance, owner, "tasks_update", arguments, revision=task["revision"] + 7)
    assert stale.error["code"] == "revision_conflict" and stale.error["current_revision"] == task["revision"]

    key = new_id()
    first_try = invoke(instance, owner, "tasks_update", arguments, revision=task["revision"], key=key)
    replay = invoke(instance, owner, "tasks_update", arguments, revision=task["revision"], key=key)
    assert first_try.status == replay.status == "ok" and first_try.data == replay.data
    assert replay.data["task"]["revision"] == task["revision"] + 1
    changed = invoke(instance, owner, "tasks_update", {**arguments, "changes": {"title": "Different"}},
                     revision=task["revision"], key=key)
    assert changed.error["code"] == "idempotency_conflict"
    current = invoke(instance, owner, "tasks_get", {"task_id": task["id"]}).data["task"]
    assert (current["title"], current["revision"]) == ("Guarded v2", task["revision"] + 1)


def test_retained_task_fields_round_trip_through_the_extracted_record_path(host):
    instance, first, _, _ = host
    owner = instance.human(first)
    config = invoke(instance, owner, "tasks_config_get", {}).data
    value = {**config["config"], "fields": {"effort": {"type": "int"}}}
    updated = invoke(instance, owner, "tasks_config_update", {"config": value})
    assert updated.status == "ok", updated.error

    created = create_task(instance, first, "Retained fields", due="2026-12-01", tags=["marketing"],
                          fields={"effort": 3}, assignee_ids=[instance.owner])
    assert (created["due"], created["tags"], created["fields"], created["assignee_ids"]) == (
        "2026-12-01", ("marketing",) if isinstance(created["tags"], tuple) else ["marketing"], {"effort": 3},
        (instance.owner,) if isinstance(created["assignee_ids"], tuple) else [instance.owner])
    archived = invoke(instance, owner, "tasks_update", {"task_id": created["id"], "changes": {"archived": True}},
                      revision=created["revision"])
    assert archived.status == "ok" and archived.data["task"]["archived"] is True
    visible = invoke(instance, owner, "tasks_list", {}).data["tasks"]
    assert created["id"] not in {item["id"] for item in visible}
    included = invoke(instance, owner, "tasks_list", {"include_archived": True}).data["tasks"]
    assert created["id"] in {item["id"] for item in included}

    request = create_task(instance, first, "Needs the owner", kind="owner_request", assignee_ids=[instance.owner])
    assert request["kind"] == "owner_request"
    unassigned = invoke(instance, owner, "tasks_create", {"title": "Nobody", "kind": "owner_request"})
    assert unassigned.error["code"] == "validation_error"
