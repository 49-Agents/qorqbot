"""Task records on the engine: fixed states, fields, scopes, parents, dependencies, the shared snooze and protected writes."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from qorqut_operations import tasks
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import AssemblyError
from operational_support import (AGENT, AGENT2, BUSINESS, COORDINATOR, HOME_AGENT, HUMAN, OTHER, OTHER_AGENT, OperationalHost, STRANGER,
                                 utc)
from qorqut_operations.tasks import STATE_DEFINITIONS, normalize_snooze


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def test_create_defaults_and_fixed_states(host):
    task = host.create_task("Plan the launch", assignee_ids=[AGENT])
    assert task["status"] == "open" and task["revision"] == 1 and task["kind"] == "ordinary"
    assert task["assignee_ids"] == [AGENT] and task["snoozed"] is False and task["snooze_generation"] == 0
    for status in ("working", "blocked", "waiting", "done", "cancelled", "open"):
        updated = host.data("tasks_update", {"task_id": task["id"], "changes": {"status": status}}, expected_revision=task["revision"])["task"]
        assert updated["status"] == status
        task = updated
    config = host.read("tasks_config_get")["config"]
    assert [state["name"] for state in config["states"]] == ["open", "working", "blocked", "waiting", "done", "cancelled"]
    assert config["states"] == list(STATE_DEFINITIONS) and config["tags"] == {"source": "catalog", "operation": "tags_list"}


def test_closed_states_gate_nothing_and_complete_accepts_done_or_cancelled(host):
    parent = host.create_task("Parent")
    child = host.create_task("Child", parent_id=parent["id"])
    blocker = host.create_task("Blocker")
    blocked = host.create_task("Blocked", blocked_by=[blocker["id"]])
    # An unfinished child or blocker never stops the parent or the blocked task from finishing.
    done = host.data("tasks_complete", {"task_id": parent["id"]}, expected_revision=parent["revision"])["task"]
    assert done["status"] == "done" and host.read("tasks_get", {"task_id": child["id"]})["task"]["status"] == "open"
    finished = host.data("tasks_complete", {"task_id": blocked["id"], "status": "cancelled"}, expected_revision=blocked["revision"])["task"]
    assert finished["status"] == "cancelled" and finished["unresolved_blockers"] == [blocker["id"]]
    reopened = host.data("tasks_update", {"task_id": parent["id"], "changes": {"status": "open"}}, expected_revision=done["revision"])["task"]
    assert reopened["status"] == "open"
    assert host.error("tasks_complete", {"task_id": parent["id"], "status": "working"}, expected_revision=reopened["revision"]) == "validation_error"
    assert host.count("task_completions") == 1


def test_revision_conflicts_and_idempotent_creation_receipts(host):
    task = host.create_task("Idempotent")
    stale = host.error("tasks_update", {"task_id": task["id"], "changes": {"title": "Edit"}}, expected_revision=task["revision"] + 5)
    assert stale == "revision_conflict"
    first = host.ok("tasks_create", {"title": "Receipt"}, idempotency_key="same-key")
    second = host.ok("tasks_create", {"title": "Receipt"}, idempotency_key="same-key")
    assert first.data["task"]["id"] == second.data["task"]["id"] and host.count("tasks", "title='Receipt'") == 1


def test_custom_fields_follow_the_scope_configuration(host):
    config = host.read("tasks_config_get")
    assert config["config"]["fields"] == {} and config["revision"] is None
    fields = {"effort": {"type": "int"}, "area": {"type": "enum", "values": ["web", "mail"]}, "shipped": {"type": "date"}}
    updated = host.data("tasks_config_update", {"config": {"v": 1, "fields": fields}}, expected_revision=None)
    assert updated["config"]["fields"] == fields and updated["revision"] == 1
    task = host.create_task("Typed", fields={"effort": 3, "area": "web", "shipped": "2026-10-01"})
    assert task["fields"] == {"effort": 3, "area": "web", "shipped": "2026-10-01"}
    for bad in ({"effort": "3"}, {"area": "print"}, {"shipped": "yesterday"}, {"unknown": 1}):
        assert host.error("tasks_create", {"title": "Bad", "fields": bad}) == "validation_error"
    # A field in use cannot be removed from the configuration.
    assert host.error("tasks_config_update", {"config": {"v": 1, "fields": {"area": fields["area"], "shipped": fields["shipped"]}}},
                      expected_revision=1) == "validation_error"
    # The state list is fixed and the tag list is the shared catalog: neither can be configured per scope.
    assert host.error("tasks_config_update", {"config": {"v": 1, "states": [{"name": "stuck"}], "fields": fields}}, expected_revision=1) == "validation_error"
    assert host.error("tasks_config_update", {"config": {"v": 1, "tags": {"list": ["a"]}, "fields": fields}}, expected_revision=1) == "validation_error"
    echoed = host.read("tasks_config_get")["config"]
    assert host.data("tasks_config_update", {"config": echoed}, expected_revision=1)["revision"] == 1      # an echo changes nothing
    assert host.data("tasks_config_update", {"config": {**echoed, "fields": {**fields, "stage": {"type": "string"}}}}, expected_revision=1)["revision"] == 2
    assert host.error("tasks_config_update", {"config": {"v": 1, "fields": {"title": {"type": "string"}}}}, expected_revision=2) == "validation_error"


def test_owner_request_kind_requires_the_owner_assignee(host):
    assert host.error("tasks_create", {"title": "Ask", "kind": "owner_request", "assignee_ids": [AGENT]}) == "validation_error"
    task = host.create_task("Ask", kind="owner_request", assignee_ids=[HUMAN])
    assert task["kind"] == "owner_request" and task["assignee_ids"] == [HUMAN]


def test_scopes_keep_one_owning_namespace_and_tags_grant_nothing(host):
    business = host.create_task("In business", tags=["Shared label"])
    owned_elsewhere = host.create_task("Other business", ctx=host.owner(OTHER), tags=["Shared label"])
    global_task = host.data("tasks_create", {"title": "Global", "scope": "global", "tags": ["Shared label"]}, host.owner(None))["task"]
    assert global_task["business_id"] is None and business["business_id"] == BUSINESS and owned_elsewhere["business_id"] == OTHER
    # The same label on all three is a label only: each task stays readable only in its own scope.
    listed = host.read("tasks_list", {"tags": ["shared label"]}, host.agent())["tasks"]
    assert [task["id"] for task in listed] == [business["id"]]
    assert host.error("tasks_get", {"task_id": owned_elsewhere["id"]}, host.agent()) == "not_found"
    assert host.error("tasks_get", {"task_id": global_task["id"], "scope": "global"}, host.agent()) in {"forbidden", "scope_forbidden", "policy_denied"}
    # A global agent without the global capability cannot create or change Global work.
    assert host.error("tasks_create", {"title": "Escalate", "scope": "global"}, host.agent()) in {"forbidden", "scope_forbidden", "policy_denied"}
    # A global agent with the global capability reads and writes Global tasks only through the global scope.
    seen = host.read("tasks_list", {"scope": "global"}, host.coordinator())["tasks"]
    assert [task["id"] for task in seen] == [global_task["id"]]
    assert host.count("item_tags") == 3 and host.count("tags") == 1


def test_tags_are_ordinary_labels_kept_once_per_key_and_preserve_unrelated_labels(host):
    # A label that was once a department name or alias is just a label: it is stored as written and never rewritten.
    task = host.create_task("Tagged", tags=["Research", "Custom Label", "custom label"])
    assert sorted(task["tags"]) == ["Custom Label", "Research"]
    updated = host.data("tasks_update", {"task_id": task["id"], "changes": {"tags": ["custom label"]}}, expected_revision=task["revision"])["task"]
    assert updated["tags"] == ["Custom Label"]
    assert host.count("tags") == 2          # nothing is removed from the shared catalog
    aliased = host.data("tasks_update", {"task_id": task["id"], "changes": {"tags": ["Onbashlyk", "code", "marketing"]}},
                        expected_revision=updated["revision"])["task"]
    assert aliased["tags"] == ["code", "marketing", "Onbashlyk"]
    assert [item["id"] for item in host.data("tasks_list", {"tags": ["ONBASHLYK"]})["tasks"]] == [task["id"]]
    assert host.data("tasks_list", {"tags": ["operations"]})["tasks"] == []


def test_assignees_follow_membership_and_host_rules(host):
    task = host.create_task("Assign", assignee_ids=[AGENT])
    assigned = host.data("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT2, STRANGER]}, expected_revision=task["revision"])["task"]
    assert sorted(assigned["assignee_ids"]) == sorted([AGENT2, STRANGER])
    # A global agent needs no membership row; a retained business-home registration is never a new assignee, even here.
    host.home_agent(BUSINESS)
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [HOME_AGENT]}, expected_revision=assigned["revision"]) == "invalid_assignee"
    assert host.count("business_memberships", "principal_id=?", OTHER_AGENT) == 0
    elsewhere = host.data("tasks_assign", {"task_id": task["id"], "assignee_ids": [OTHER_AGENT, STRANGER]},
                          expected_revision=assigned["revision"])["task"]
    assert sorted(elsewhere["assignee_ids"]) == sorted([OTHER_AGENT, STRANGER])
    assigned = host.data("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT2, STRANGER]}, expected_revision=elsewhere["revision"])["task"]
    host.assignees.protected.add(AGENT)
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT]}, expected_revision=assigned["revision"]) == "forbidden"
    host.assignees.directed.add(AGENT2)
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT2]}, expected_revision=assigned["revision"]) == "invalid_assignee"
    listed = {entry["id"] for entry in host.read("assignees_list")["assignees"]}
    assert AGENT2 not in listed and HOME_AGENT not in listed and STRANGER in listed and OTHER_AGENT in listed
    # Global tasks are assigned only to the owner or global agents.
    host.assignees.protected.clear()
    assert host.error("tasks_create", {"title": "Global", "scope": "global", "assignee_ids": [HOME_AGENT]}, host.owner(None)) == "invalid_assignee"
    assert host.error("tasks_create", {"title": "Global", "scope": "global", "assignee_ids": [STRANGER]}, host.owner(None)) == "invalid_assignee"
    assert host.data("tasks_create", {"title": "Global", "scope": "global", "assignee_ids": [COORDINATOR]}, host.owner(None))["task"]["assignee_ids"] == [COORDINATOR]


def test_parent_tasks_are_independent_validated_and_acyclic(host):
    parent = host.create_task("Parent")
    child = host.create_task("Child", parent_id=parent["id"])
    grandchild = host.create_task("Grandchild", parent_id=child["id"])
    detail = host.read("tasks_get", {"task_id": parent["id"]})["task"]
    assert [entry["id"] for entry in detail["children"]] == [child["id"]]
    assert host.read("tasks_get", {"task_id": child["id"]})["task"]["parent"]["id"] == parent["id"]
    assert host.error("tasks_update", {"task_id": parent["id"], "changes": {"parent_id": grandchild["id"]}}, expected_revision=parent["revision"]) == "validation_error"
    assert host.error("tasks_update", {"task_id": parent["id"], "changes": {"parent_id": parent["id"]}}, expected_revision=parent["revision"]) == "validation_error"
    assert host.error("tasks_create", {"title": "Orphan", "parent_id": "missing"}) in {"validation_error", "not_found"}
    # A parent in another scope is unreachable, and a closed parent does not close the child.
    other = host.create_task("Other scope", ctx=host.owner(OTHER))
    assert host.error("tasks_create", {"title": "Cross", "parent_id": other["id"]}) in {"validation_error", "not_found"}
    host.data("tasks_complete", {"task_id": parent["id"]}, expected_revision=parent["revision"])
    assert host.read("tasks_get", {"task_id": grandchild["id"]})["task"]["status"] == "open"
    top = host.read("tasks_list", {"parent_id": None})["tasks"]
    assert [entry["id"] for entry in top] == [parent["id"]]
    cleared = host.data("tasks_update", {"task_id": child["id"], "changes": {"parent_id": None}}, expected_revision=child["revision"])["task"]
    assert cleared["parent"] is None
    # Storage refuses a cycle or a cross-scope parent even for a direct write.
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET parent_id=? WHERE business_id IS ? AND id=?", (grandchild["id"], BUSINESS, child["id"]))
            uow.connection.execute("UPDATE tasks SET parent_id=? WHERE business_id IS ? AND id=?", (child["id"], BUSINESS, grandchild["id"]))


def test_dependencies_are_informational_validated_and_acyclic(host):
    first = host.create_task("First")
    second = host.create_task("Second", blocked_by=[first["id"]])
    third = host.create_task("Third", blocked_by=[second["id"]])
    assert host.error("tasks_update", {"task_id": first["id"], "changes": {"blocked_by": [third["id"]]}}, expected_revision=first["revision"]) == "validation_error"
    assert host.error("tasks_update", {"task_id": first["id"], "changes": {"blocked_by": [first["id"]]}}, expected_revision=first["revision"]) == "validation_error"
    assert host.error("tasks_create", {"title": "Ghost", "blocked_by": ["missing"]}) in {"validation_error", "not_found"}
    detail = host.read("tasks_get", {"task_id": second["id"]})["task"]
    assert detail["blocked_by"] == [first["id"]] and detail["unresolved_blockers"] == [first["id"]] and detail["blocks"] == [third["id"]]
    assert host.read("tasks_list", {"blocked": True})["tasks"] and {entry["id"] for entry in host.read("tasks_list", {"blocked": True})["tasks"]} == {second["id"], third["id"]}
    host.data("tasks_complete", {"task_id": first["id"]}, expected_revision=first["revision"])
    assert host.read("tasks_get", {"task_id": second["id"]})["task"]["unresolved_blockers"] == []
    other = host.create_task("Elsewhere", ctx=host.owner(OTHER))
    assert host.error("tasks_update", {"task_id": third["id"], "changes": {"blocked_by": [other["id"]]}}, expected_revision=third["revision"]) in {"validation_error", "not_found"}


def test_snooze_is_one_idempotent_timestamp_and_not_a_state(host):
    task = host.create_task("Defer")
    until = utc(timedelta(days=2))
    snoozed = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": until}}, expected_revision=task["revision"])["task"]
    assert snoozed["snoozed"] is True and snoozed["snoozed_until"] == until and snoozed["status"] == "open" and snoozed["snooze_generation"] == 1
    # Repeating the same value is a no-op: no revision, no new generation.
    again = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": until}}, expected_revision=snoozed["revision"])["task"]
    assert again["revision"] == snoozed["revision"] and again["snooze_generation"] == 1
    assert [entry["id"] for entry in host.read("tasks_list")["tasks"]] == []
    assert [entry["id"] for entry in host.read("tasks_list", {"snooze_filter": "snoozed"})["tasks"]] == [task["id"]]
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": utc(-timedelta(hours=1))}}, expected_revision=again["revision"]) == "validation_error"
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": "2027-01-01T00:00:00"}}, expected_revision=again["revision"]) == "validation_error"
    later = utc(timedelta(days=3))
    moved = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": later}}, expected_revision=again["revision"])["task"]
    assert moved["snooze_generation"] == 2
    cleared = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": None}}, expected_revision=moved["revision"])["task"]
    assert cleared["snoozed_until"] is None and cleared["snooze_generation"] == 2 and cleared["snoozed"] is False
    # After the time passes the stored value still validates unchanged (idempotent replays of the same write).
    assert normalize_snooze(until, utc(timedelta(days=9)), until) == until


def test_snooze_expiry_emits_one_event_per_generation(host):
    task = host.create_task("Wake me", assignee_ids=[AGENT])
    until = utc(timedelta(hours=1))
    snoozed = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": until}}, expected_revision=task["revision"])["task"]
    before = host.count("inbox_events", "event_type='snooze_expired'")
    with host.database.transaction() as uow:
        assert host.tasks.reconcile_snoozes(uow, now=utc(timedelta(minutes=30))) == []
    with host.database.transaction() as uow:
        woken = host.tasks.reconcile_snoozes(uow, now=utc(timedelta(hours=2)))
    assert [entry["task_id"] for entry in woken] == [task["id"]]
    with host.database.transaction() as uow:
        assert host.tasks.reconcile_snoozes(uow, now=utc(timedelta(hours=3))) == []        # one event per generation
    deliveries = [row for row in host.deliveries(AGENT) if row["event_type"] == "snooze_expired"]
    assert host.count("inbox_events", "event_type='snooze_expired'") == before + 1 and len(deliveries) == 1 and deliveries[0]["required"] == 1
    # A new snooze time is a new generation and wakes again; a closed task never wakes.
    host.later(hours=3)
    second = utc(timedelta(hours=8))
    moved = host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": second}}, expected_revision=snoozed["revision"])["task"]
    with host.database.transaction() as uow:
        assert len(host.tasks.reconcile_snoozes(uow, now=utc(timedelta(hours=9)))) == 1
    host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": utc(timedelta(hours=20)), "status": "cancelled"}},
              expected_revision=moved["revision"])
    with host.database.transaction() as uow:
        assert host.tasks.reconcile_snoozes(uow, now=utc(timedelta(hours=30))) == []


def wake(host, task, **delta):
    with host.database.transaction() as uow:
        return host.tasks.reconcile_snoozes(uow, now=utc(timedelta(**delta)))


def snooze_wakes(host, task):
    return [(row["agent_id"], row["required"]) for row in host.deliveries(source_type="tasks")
            if row["source_id"] == task["id"] and row["event_type"] == "snooze_expired"]


def snooze(host, task, ctx=None, hours=1):
    return host.data("tasks_update", {"task_id": task["id"], "changes": {"snoozed_until": utc(timedelta(hours=hours))}}, ctx=ctx,
                     expected_revision=task["revision"])["task"]


def test_snooze_wake_uses_all_responsible_principals_and_routes_every_human_through_the_port(host):
    # A human-only assignment: the agent creator is NOT notified; the typed snooze port answers for the human.
    host.routing.snooze_required = [COORDINATOR]
    human_only = snooze(host, host.create_task("Owner decides", ctx=host.agent(), assignee_ids=[HUMAN]), host.agent())
    assert [entry["task_id"] for entry in wake(host, human_only, hours=2)] == [human_only["id"]]
    assert host.routing.snooze_calls == [(BUSINESS, human_only["id"], [HUMAN])]
    assert snooze_wakes(host, human_only) == [(COORDINATOR, 1)]
    assert wake(host, human_only, hours=3) == []                                      # one atomic wake per generation
    # A member who is not the owner: the port is asked for that member alone and may route nothing; the wake is still consumed.
    host.routing.snooze_required, host.routing.snooze_calls = [], []
    member = snooze(host, host.create_task("Member decides", ctx=host.agent(), assignee_ids=[STRANGER]), host.agent())
    assert len(wake(host, member, hours=2)) == 1 and host.routing.snooze_calls == [(BUSINESS, member["id"], [STRANGER])]
    assert snooze_wakes(host, member) == [] and host.count("task_snooze_wakes", "task_id=?", member["id"]) == 1
    assert wake(host, member, hours=3) == []
    # Agents and people together: the agent has its inbox row, the humans go through the port, nobody else is told.
    host.routing.snooze_required, host.routing.snooze_calls = [COORDINATOR], []
    mixed = snooze(host, host.create_task("Both", ctx=host.agent(), assignee_ids=[AGENT2, HUMAN, STRANGER]), host.agent())
    wake(host, mixed, hours=2)
    assert sorted(agent for agent, _ in snooze_wakes(host, mixed)) == sorted([AGENT2, COORDINATOR])
    assert host.routing.snooze_calls == [(BUSINESS, mixed["id"], sorted([HUMAN, STRANGER]))]
    # An assigned agent keeps the wake for itself; a human creator is not told and the port is not consulted.
    host.routing.snooze_calls = []
    for_agent = snooze(host, host.create_task("Agent work", assignee_ids=[AGENT2]))
    wake(host, for_agent, hours=2)
    assert snooze_wakes(host, for_agent) == [(AGENT2, 1)] and host.routing.snooze_calls == []


def test_a_snooze_wake_with_no_assignee_falls_back_to_the_creator_by_kind(host):
    # An agent creator is told through the inbox and the port is not consulted.
    agent_made = snooze(host, host.create_task("Nobody yet", ctx=host.agent(AGENT2), assignee_ids=[]), host.agent(AGENT2))
    assert agent_made["assignee_ids"] == []
    wake(host, agent_made, hours=2)
    assert snooze_wakes(host, agent_made) == [(AGENT2, 1)] and host.routing.snooze_calls == []
    # A human creator is human responsibility: no inbox row of its own, the port answers for it exactly once.
    host.routing.snooze_required = [COORDINATOR]
    owner_made = snooze(host, host.create_task("Owner reminder"))
    assert owner_made["assignee_ids"] == [] and owner_made["created_by"] == HUMAN
    assert [entry["task_id"] for entry in wake(host, owner_made, hours=2)] == [owner_made["id"]]
    assert host.routing.snooze_calls == [(BUSINESS, owner_made["id"], [HUMAN])]
    assert snooze_wakes(host, owner_made) == [(COORDINATOR, 1)]
    assert wake(host, owner_made, hours=3) == [] and host.count("inbox_events", "event_type='snooze_expired' AND source_id=?", owner_made["id"]) == 1
    # A closed Task is never woken and a wake never starts anything.
    assert host.task(owner_made["id"])["status"] == "open"


def test_the_routing_port_must_declare_the_snooze_route(host):
    class OrdinaryRoutesOnly:
        def routes(self, uow, business_id, task, actor_id):
            return [], []

    with pytest.raises(AssemblyError, match="snooze_routes"):
        replace(host.task_ports, routing=OrdinaryRoutesOnly())


def test_labels_are_ordinary_tags_with_no_campaign_port_rule_or_read_enrichment(host):
    assert not hasattr(tasks, "Campaigns") and "campaigns" not in tasks.TaskPorts.__dataclass_fields__
    with pytest.raises(TypeError):
        tasks.TaskPorts(assignees=host.assignees, focus=host.focus, incidents=host.incidents,
                        references=host.references, campaigns=object(), routing=host.routing)
    plain_task = host.create_task("No labels")
    task = host.create_task("Launch", tags=["Spring Launch", "marketing-campaign"])
    changed = host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Launch!"}}, expected_revision=task["revision"])["task"]
    updated = host.data("tasks_update", {"task_id": task["id"], "changes": {"tags": ["Spring Launch", "marketing-campaign", "Paid"]}},
                        expected_revision=changed["revision"])["task"]
    # No label is reserved or refused, and a Task read carries only its ordinary tags: no campaign facts.
    read = host.data("tasks_get", {"task_id": task["id"]})["task"]
    assert read["tags"] == ["marketing-campaign", "Paid", "Spring Launch"] and read["revision"] == updated["revision"]
    assert "campaign" not in read and "campaign" not in host.data("tasks_get", {"task_id": plain_task["id"]})["task"]


def test_protected_writes_go_through_signoff_and_apply_the_same_plan(host):
    host.agents.overrides["tasks_create"] = "signoff"
    signoff_id = host.stage("tasks_create", {"title": "Needs approval", "assignee_ids": [AGENT]})
    assert host.count("tasks", "title='Needs approval'") == 0
    assert host.apply_approved(signoff_id)["state"] == "applied"
    created = host.query("SELECT * FROM tasks WHERE title='Needs approval'")
    assert len(created) == 1 and host.count("thread_follows", "item_id=?", created[0]["id"]) >= 1
    # Reassigning, or completing through an update, is checked as its own operation.
    host.agents.overrides.pop("tasks_create")
    host.agents.overrides["tasks_assign"] = "deny"
    host.agents.overrides["tasks_complete"] = "deny"
    task = host.create_task("Mine", ctx=host.agent(), assignee_ids=[AGENT])
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"assignee_ids": [AGENT2]}}, host.agent(), expected_revision=task["revision"]) in {"forbidden", "policy_denied"}
    assert host.error("tasks_update", {"task_id": task["id"], "changes": {"status": "done"}}, host.agent(), expected_revision=task["revision"]) in {"forbidden", "policy_denied"}
    assert host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "working"}}, host.agent(), expected_revision=task["revision"])["task"]["status"] == "working"


def test_focus_runs_inside_the_task_write(host):
    task = host.create_task("Focused", focus={"card": "x"})
    assert host.focus.calls[0][0] == "access" and host.focus.calls[-1][:3] == ("present", {"card": "x"}, "task")
    renamed = host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Renamed"}}, expected_revision=task["revision"])["task"]
    assert renamed["title"] == "Renamed"


def test_a_task_read_carries_no_setup_facts_and_the_setup_port_is_gone(host):
    task = host.create_task("Plain")
    assert "setup" not in host.data("tasks_get", {"task_id": task["id"]})["task"]
    with pytest.raises(TypeError):
        tasks.TaskPorts(assignees=host.assignees, setup=object(), focus=host.focus, incidents=host.incidents,
                        references=host.references, routing=host.routing)
    assert not hasattr(tasks, "SetupGuards")


def test_dev_ticket_note_reference_links_the_task(host):
    task = host.create_task("Bug")
    updated = host.data("tasks_comment", {"task_id": task["id"], "text": "See the ticket", "note_refs": [{"type": "dev_ticket", "id": "T-1"}]},
                        expected_revision=task["revision"])["task"]
    assert host.incidents.links == [("T-1", BUSINESS, task["id"], HUMAN)] and updated["note_count"] == 1


def test_overview_is_the_coordinator_only_cross_scope_index(host):
    mine = host.data("tasks_create", {"title": "Global mine", "scope": "global", "assignee_ids": [COORDINATOR]}, host.owner(None))["task"]
    owners = host.create_task("Owner work", assignee_ids=[HUMAN])
    overview = host.read("tasks_overview", {}, host.coordinator())
    titles = {entry["id"] for group in overview["scopes"] for bucket in ("active", "snoozed") for entry in group[bucket]}
    assert titles == {mine["id"], owners["id"]} and overview["assignees"]["self"] == COORDINATOR
    assert host.error("tasks_overview", {}, host.owner(None)) in {"forbidden", "policy_denied"}
    assert host.error("tasks_overview", {}, host.agent()) in {"forbidden", "scope_forbidden", "policy_denied"}


def test_workers_are_independent_and_completion_tracks_reentry(host):
    host.assignees.worker_exclusions.add(AGENT2)
    assert AGENT2 not in {w['id'] for w in host.read('workers_list')['workers']}
    assert host.error('tasks_create', {'title': 'Excluded', 'worker_ids': [AGENT2]}) == 'invalid_worker'
    task = host.create_task('Work', assignee_ids=[AGENT2], worker_ids=[HUMAN, AGENT], status='done')
    assert task['worker_ids'] == sorted([HUMAN, AGENT])
    assert task['user_needed'] and {w['id'] for w in task['workers']} == {HUMAN, AGENT}
    assert task['assignee_ids'] == [AGENT2]
    first = task['latest_completed_at']
    host.now += timedelta(minutes=1)
    edited = host.data('tasks_update', {'task_id': task['id'], 'changes': {'worker_ids': [AGENT]}}, expected_revision=task['revision'])['task']
    assert not edited['user_needed'] and edited['latest_completed_at'] == first and edited['assignee_ids'] == [AGENT2]
    reopened = host.data('tasks_update', {'task_id': task['id'], 'changes': {'status': 'blocked'}}, expected_revision=edited['revision'])['task']
    host.now += timedelta(minutes=1)
    done = host.data('tasks_update', {'task_id': task['id'], 'changes': {'status': 'done'}}, expected_revision=reopened['revision'])['task']
    assert done['latest_completed_at'] > first and done['worker_ids'] == [AGENT]
    assert host.error('tasks_update', {'task_id': task['id'], 'changes': {'worker_ids': [AGENT, AGENT]}}, expected_revision=done['revision']) == 'validation_error'
    with host.database.transaction() as uow:
        with pytest.raises(Exception):
            uow.connection.execute("UPDATE tasks SET worker_ids_json='{}' WHERE id=?", (task['id'],))


def test_global_completion_and_shared_item_worker_projection(host):
    from qorqut_operations.tasks.workers import project_workers
    ctx = host.owner(None)
    task = host.data('tasks_create', {'title': 'Global done', 'scope': 'global', 'status': 'done',
                                      'worker_ids': [HUMAN, COORDINATOR]}, ctx)['task']
    first = task['latest_completed_at']
    with host.database.read() as uow:
        row = uow.repository('tasks', business_id=None).get(task['id'])
        projection = project_workers(uow, row)
    assert projection['workers'] == task['workers'] and projection['user_needed']
    reopened = host.data('tasks_update', {'scope': 'global', 'task_id': task['id'], 'changes': {'status': 'waiting'}},
                         ctx, expected_revision=task['revision'])['task']
    host.now += timedelta(minutes=1)
    done = host.data('tasks_complete', {'scope': 'global', 'task_id': task['id']}, ctx, expected_revision=reopened['revision'])['task']
    assert done['latest_completed_at'] > first
    assert done['worker_ids'] == task['worker_ids']


def test_only_installation_owner_is_human_worker_but_project_owner_remains_assignee(host):
    with host.database.transaction() as uow:
        uow.connection.execute('UPDATE business_memberships SET is_owner=1 WHERE business_id=? AND principal_id=?',
                               (BUSINESS, STRANGER))
    assignees = {item['id']: item for item in host.read('assignees_list')['assignees']}
    workers = {item['id']: item for item in host.read('workers_list')['workers']}
    assert assignees[STRANGER]['is_owner'] is True
    assert STRANGER not in workers and workers[HUMAN]['is_owner'] is True
    assert host.error('tasks_create', {'title': 'Invalid worker', 'worker_ids': [STRANGER]}) == 'invalid_worker'
    task = host.create_task('Ordinary assignee', assignee_ids=[STRANGER], worker_ids=[HUMAN])
    assert task['assignee_ids'] == [STRANGER]
    assert host.error('tasks_update', {'task_id': task['id'], 'changes': {'worker_ids': [STRANGER]}},
                      expected_revision=task['revision']) == 'invalid_worker'
    assert host.read('tasks_get', {'task_id': task['id']})['task']['worker_ids'] == [HUMAN]
