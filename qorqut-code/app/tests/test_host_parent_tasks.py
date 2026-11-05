"""Acceptance case: a work group is an ordinary parent Task, composed on the host and the Operations feature wheel.

One synthetic host (two businesses, two registered global agents, the retained Signoff service, Focus and the program registry)
proves what replacing the retired Story domain must not change:

* a group is a plain Task: its children are ordinary Tasks (``parent_id``), and notes and tables relate to one or
  several groups through ordinary ``concerns`` relationships, never through a hidden Story record, alias or table;
* a Task label is only a label: the host has no campaign rule, so no label implies responsibility, attribution or review;
* completing or reopening a parent never propagates to its children, and the other way round;
* Focus presents a group Task with the access it already had, and a retained Signoff applies the exact Task change;
* the related-work subscription of a Task is one hop, never recursive, never required, and stops when the Task is unfollowed;
* the active operation catalog has no ``stories_*`` handler and no campaign operation.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network, native session or operational data."""

import json
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from host_foundation_support import create_worker, build_instance, create_registered_global, register_sources, set_agent_overrides

WORKER_GRANTS = {name: "allow" for name in ("tasks_create", "notes_create", "notes_get", "signoffs_list")}

COLUMNS = [{"id": "metric", "name": "Metric", "type": "text"}]


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Businesses have no resident agents: Ordubashy works in the first business; a second worker, the independent peer,
    # selects the second.
    agent, other = create_registered_global(instance, "ordubashy"), create_worker(instance, "colleague")
    # A worker starts with the minimal everyday tools: the Task and note work of these cases is granted explicitly.
    set_agent_overrides(instance, other, WORKER_GRANTS, [first, second])
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [first, second])
    return SimpleNamespace(
        instance=instance, runtime=runtime, first=first, second=second, agent=agent, other=other,
        owner=instance.human(first), owner_second=instance.human(second),
        owner_global=InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset({first, second}),
                                       global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
        agent_context=runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=first),
        other_context=runtime.resolver.invocation_context(Principal(other, "agent"), business_id=second))


def call(host, ctx, operation, arguments=None, *, revision=None, key=None):
    return host.runtime.application.invoke(ctx, operation, arguments or {}, InvocationOptions(
        idempotency_key=key or new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def task(host, title, ctx=None, **arguments):
    return ok(call(host, ctx or host.owner, "tasks_create", {"title": title, **arguments}))["task"]


def get(host, identity, ctx=None):
    return ok(call(host, ctx or host.owner, "tasks_get", {"task_id": identity}))["task"]


def status(host, current, value, ctx=None):
    return ok(call(host, ctx or host.owner, "tasks_update", {"task_id": current["id"], "changes": {"status": value}},
                   revision=current["revision"]))["task"]


def link(host, source, target, role="concerns"):
    return ok(call(host, host.owner, "relationships_create", {"source": source, "target": target, "role": role}))["relationship"]


def concerning(host, group):
    """What relates to the group Task through an ordinary ``concerns`` relationship, as (type, id) pairs."""
    return {(row["from_type"], row["from_id"]) for row in rows(
        host, "SELECT from_type, from_id FROM relationships WHERE to_type='task' AND to_id=? AND role='concerns'", group["id"])}


def business_note(host, ctx, title, task_ids, business=None):
    """A business-owned note related to Tasks (a global agent's private notes are Global records and relate no business Task)."""
    return call(host, ctx, "notes_create", {"title": title, "body": "x\n",
                                            "target_business_id": business or host.first, "task_ids": task_ids})


def test_a_group_is_an_ordinary_parent_task_and_resources_relate_to_several_groups(host):
    launch, press = task(host, "Launch"), task(host, "Press")
    child = task(host, "Write copy", parent_id=launch["id"], assignee_ids=[host.agent])
    grandchild = task(host, "Proofread", parent_id=child["id"])
    note = ok(business_note(host, host.owner, "Brief", [launch["id"], press["id"]]))["document"]
    table = ok(call(host, host.owner, "tables_create", {"title": "Results", "columns": COLUMNS, "task_ids": [launch["id"]]}))["table"]
    link(host, {"type": "table", "id": table["id"]}, {"type": "task", "id": press["id"]})

    # One note and one table belong to two groups; each group lists everything related to it, in ordinary relationships.
    assert concerning(host, launch) == {("material", note["id"]), ("table", table["id"])}
    assert concerning(host, press) == {("material", note["id"]), ("table", table["id"])}
    # Hierarchy is the Task's own parent link and nothing infers it from a shared resource.
    detail = get(host, launch["id"])
    assert [row["id"] for row in detail["children"]] == [child["id"]] and detail["parent_id"] is None
    assert get(host, press["id"])["children"] == [] and get(host, press["id"])["parent_id"] is None
    assert get(host, child["id"])["parent"]["id"] == launch["id"] and get(host, grandchild["id"])["parent"]["id"] == child["id"]
    # The retired record leaves no table, alias or Task kind behind.
    assert rows(host, "SELECT name FROM sqlite_master WHERE name GLOB 'stor*' OR name GLOB '*_stor*' OR name GLOB '*stories*'") == []
    assert {row["kind"] for row in rows(host, "SELECT DISTINCT kind FROM tasks")} <= {"ordinary", "owner_request"}

    # A group's resources are reachable through the ordinary relationship read by anyone who may read the Task.
    seen = ok(call(host, host.owner, "relationships_list", {"target": {"type": "task", "id": launch["id"]}, "role": "concerns"}))
    assert note["id"] in json.dumps(seen) and table["id"] in json.dumps(seen)


def test_asset_and_data_uploads_relate_to_groups_and_store_a_normalized_file_name(host):
    import base64
    launch = task(host, "Launch")
    content = base64.b64encode(b"synthetic bytes").decode()
    asset = ok(call(host, host.owner, "assets_upload", {"filename": "C:\\Brand Kit\\Hero Shot (1).PNG", "content_b64": content,
                                                      "group": "polished", "task_ids": [launch["id"]]}))["asset"]
    upload = ok(call(host, host.owner, "data_uploads_upload", {"filename": "Q3 Results.CSV", "content_b64": content,
                                                             "task_ids": [launch["id"]]}))["upload"]
    assert asset["path"] == "assets/polished/hero-shot-1.png" and upload["path"] == "data-uploads/q3-results.csv"
    # Both are ordinary materials related to the group Task; an unknown Task is refused before anything is written.
    assert concerning(host, launch) == {("material", asset["id"]), ("material", upload["id"])}
    refused = call(host, host.owner, "assets_upload", {"filename": "Other.png", "content_b64": content, "group": "drafted", "task_ids": [new_id()]})
    assert code(refused) == "not_found"
    assert code(call(host, host.owner, "assets_upload", {"filename": ".hidden", "content_b64": content, "group": "drafted"})) == "validation_error"


def test_task_links_are_scoped_to_the_business_and_never_widen_access(host):
    launch = task(host, "Launch")
    # The Task must exist in the selected scope; another business and the global scope cannot be linked to.
    assert code(business_note(host, host.other_context, "Foreign", [launch["id"]], host.second)) == "not_found"
    assert code(business_note(host, host.owner, "Unknown", [new_id()])) == "not_found"
    # A global agent's private note is a Global record, which relates no business Task.
    assert code(call(host, host.agent_context, "notes_create", {"title": "Mine", "body": "x\n", "task_ids": [launch["id"]]})) == "forbidden"
    assert code(call(host, host.owner_global, "tables_create", {
        "scope": "global", "title": "Global", "columns": COLUMNS, "task_ids": [launch["id"]]})) in {"validation_error", "forbidden"}
    # A link grants nothing: a global agent selecting another business still reads neither the group nor what relates to it.
    ok(business_note(host, host.owner, "Brief", [launch["id"]]))
    assert code(call(host, host.other_context, "tasks_get", {"task_id": launch["id"]})) in {"not_found", "forbidden"}
    assert rows(host, "SELECT COUNT(*) AS n FROM relationships WHERE business_id=?", host.second)[0]["n"] == 0


def test_completing_or_reopening_a_parent_never_changes_its_children(host):
    parent = task(host, "Launch")
    first, second = task(host, "One", parent_id=parent["id"]), task(host, "Two", parent_id=parent["id"], assignee_ids=[host.agent])
    closed = status(host, parent, "done")
    assert closed["status"] == "done"
    assert [get(host, child["id"])["status"] for child in (first, second)] == ["open", "open"]
    reopened = status(host, closed, "open")
    assert reopened["status"] == "open"
    # A child finishing leaves the parent exactly as it was, and a child can finish while the parent is still open.
    finished = status(host, get(host, first["id"]), "done")
    assert finished["status"] == "done" and get(host, parent["id"])["status"] == "open"
    assert get(host, second["id"])["status"] == "open"
    # The parent is completed under the ordinary Task permissions: another business's selection cannot touch it.
    assert code(call(host, host.other_context, "tasks_update", {"task_id": parent["id"], "changes": {"status": "done"}},
                     revision=reopened["revision"])) in {"not_found", "forbidden"}
    assert get(host, parent["id"])["status"] == "open"


def test_focus_presents_a_group_task_with_the_access_it_already_had(host):
    launch = task(host, "Launch")
    source = {"type": "task", "id": launch["id"], "business_id": host.first}
    ok(call(host, host.owner_global, "focus_present", {
        "scope": "global", "source": source, "reason": "Needs a decision", "requested_response": "Choose the date"}))
    shown = ok(call(host, host.owner_global, "focus_get", {"scope": "global"}))
    text = json.dumps(shown)
    assert launch["id"] in text and "Launch" in text and "unavailable" not in text
    # A Task of another business is still not presentable by an agent that cannot read it, and the business agent has no Focus authority.
    assert code(call(host, host.agent_context, "focus_present", {"scope": "global", "source": source, "reason": "x"})) in {
        "forbidden", "not_found", "invalid_scope", "operation_unavailable"}


def deliveries(host, agent, identity):
    return rows(host, "SELECT e.event_type, d.required FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                      "WHERE e.source_type='tasks' AND e.source_id=? AND d.agent_id=? ORDER BY e.id", identity, agent)


def test_a_related_work_subscription_is_one_hop_optional_and_follows_the_task_follow(host):
    group = task(host, "Launch")
    subscribed = ok(call(host, host.agent_context, "inbox_subscribe", {
        "source_type": "tasks", "source_id": group["id"], "include_related": True}))
    assert subscribed["following"] is True
    row = rows(host, "SELECT include_related FROM inbox_subscriptions WHERE source_type='tasks' AND source_id=?", group["id"])
    assert row == [{"include_related": 1}]
    listed_subscriptions = ok(call(host, host.agent_context, "inbox_subscriptions_list", {}, ))["items"]
    assert [item for item in listed_subscriptions if item.get("source_id") == group["id"]]

    child = task(host, "Child", parent_id=group["id"])
    grandchild = task(host, "Grandchild", parent_id=child["id"])
    unrelated = task(host, "Unrelated")
    linked = task(host, "Linked")
    link(host, {"type": "task", "id": linked["id"]}, {"type": "task", "id": group["id"]})
    for item in (child, grandchild, unrelated, linked):
        status(host, get(host, item["id"]), "working")
    assert [event for event in deliveries(host, host.agent, child["id"])] and all(
        entry["required"] == 0 for entry in deliveries(host, host.agent, child["id"]))
    assert deliveries(host, host.agent, linked["id"]) and deliveries(host, host.agent, grandchild["id"]) == []
    assert deliveries(host, host.agent, unrelated["id"]) == []

    # The Task's own follow stays authoritative: unfollowing it stops its related work too, until it is followed again.
    assert ok(call(host, host.agent_context, "tasks_follow", {"task_id": group["id"], "following": False}))["following"] is False
    seen = len(deliveries(host, host.agent, child["id"]))
    status(host, get(host, child["id"]), "waiting")
    assert len(deliveries(host, host.agent, child["id"])) == seen
    assert ok(call(host, host.agent_context, "tasks_follow", {"task_id": group["id"], "following": True}))["following"] is True
    status(host, get(host, child["id"]), "working")
    assert len(deliveries(host, host.agent, child["id"])) == seen + 1


def test_the_active_operation_catalog_has_no_story_handlers(host):
    installed = {descriptor.name for descriptor in host.runtime.application.registry.installed()}
    assert not [name for name in installed if {"story", "stories"} & set(name.split("_"))]
    assert {"tasks_create", "tasks_update", "tasks_complete", "relationships_create", "inbox_subscribe"} <= installed
    assert not [name for name in installed if name.startswith("campaign_") or name.startswith("products_")]
    for name in ("stories_get", "stories_create", "stories_close", "stories_reopen"):
        assert code(call(host, host.owner, name, {})) in {"operation_unavailable", "not_found", "validation_error"}


def test_a_retained_signoff_applies_the_exact_task_change_once(host):
    current = task(host, "Launch")
    set_agent_overrides(host.instance, host.agent, {"tasks_update": "signoff"}, [host.first])
    host.agent_context = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"), business_id=host.first)
    proposed = call(host, host.agent_context, "tasks_update", {"task_id": current["id"], "changes": {"title": "Renamed"}},
                    revision=current["revision"])
    assert proposed.status == "signoff_required", proposed.error
    assert get(host, current["id"])["title"] == "Launch"
    pending = ok(call(host, host.owner, "signoffs_list", {}))["items"][0]
    signoff = ok(call(host, host.owner, "signoffs_get", {"signoff_id": pending["id"]}))["signoff"]

    def decide(**changes):
        return host.runtime.signoffs.review(
            host.owner, signoff["id"], decision="approve", proposal_revision=signoff["proposal_revision"],
            payload_hash=changes.get("payload_hash", signoff["payload_hash"]), expected_status_revision=signoff["status_revision"],
            idempotency_key=new_id())

    stale = decide(payload_hash="0" * 64)
    assert stale.status == "error" and stale.error["code"] == "stale_signoff"
    assert decide().status == "accepted"
    # Approval prepares the exact change; nothing is applied until the executor runs, and then only once.
    assert get(host, current["id"])["title"] == "Launch"
    assert host.runtime.signoffs.run_pending() == 1
    assert get(host, current["id"])["title"] == "Renamed"
    assert host.runtime.signoffs.run_pending() == 0
    assert rows(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"])[0]["state"] == "applied"
