"""Current supported globals reach every active business without membership rows, and nothing else widens.

A global agent here has no membership row anywhere and a stored ``allowed_business_ids`` naming only OTHER: its access
must come from the active business directory alone. A retained business-home registration, an unknown or retired role
and an inactive or malformed stored profile never act, take new work or are addressed, whatever their membership rows
say, while records naming them stay readable. Humans and system workers keep their membership rules."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from operational_support import (AGENT, AGENT2, BUSINESS, COORDINATOR, HOME_AGENT, HUMAN, OTHER, OTHER_AGENT, OperationalHost,
                                 STRANGER, WORKER)
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import InvocationContext, Principal
from qorqut_operations.discussion import ThreadItem, can_follow, eligible_agents, follow_state
from qorqut_operations.global_agents import (REPEATABLE_GLOBAL_ROLES, SUPPORTED_GLOBAL_ROLES, current_global_data, current_global_row,
                                             effective_business_ids, global_agent_ids, global_profile, is_global_agent)

GLOBAL = "00000000-0000-4000-8000-0000000000c4"     # an active global agent with no membership rows
SILENT = "00000000-0000-4000-8000-0000000000c5"     # an active global agent holding no global capability
NEW = "00000000-0000-4000-8000-0000000000b3"        # a business registered after both profiles


def global_data(capabilities=(), role="global-worker"):
    return {"status": "active", "role": {"id": role},
            "scope": {"kind": "ecosystem", "allowed_business_ids": [OTHER], "global_capabilities": list(capabilities)}}


def insert_global(host, identity, capabilities, role="global-worker"):
    profile = global_data(capabilities, role)
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": "Global " + identity[-2:]})
        uow.connection.execute(
            "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, created_at, "
            "updated_at) VALUES (?, ?, NULL, 'ecosystem', ?, ?, ?, 'now', 'now')",
            (identity, identity, "global-" + identity[-2:], "/nonexistent/" + identity, json.dumps(profile)))


@pytest.fixture
def host(tmp_path):
    host = OperationalHost(tmp_path)
    insert_global(host, GLOBAL, ["read_global_context"])
    insert_global(host, SILENT, [])
    return host


def add_business(host, identity=NEW, name="third"):
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": identity, "slug": name, "name": name, "workspace": "/nonexistent/" + name,
                                             "hq_path": "/nonexistent/" + name + "/hq"})
        uow.repository("business_memberships", business_id=identity).insert({"principal_id": HUMAN, "is_owner": True})


def archive(host, business):
    with host.database.transaction() as uow:
        uow.repository("businesses").update(business, {"archived_at": host.stamp(), "archived_by": HUMAN}, expected_revision=1)


def active(host):
    with host.database.read() as uow:
        return active_business_ids(uow.connection)


def as_global(host, business=None, *, identity=GLOBAL, allowed=None, **kwargs):
    """What the host resolver returns for a global agent: the active directory, and the profile's own capabilities."""
    return InvocationContext(Principal(identity, "agent"), business_id=business,
                             allowed_business_ids=frozenset(active(host) if allowed is None else allowed),
                             global_capabilities=frozenset({"read_global_context"}), profile_revision=1, source="mcp", **kwargs)


def owner(host, business):
    return InvocationContext(Principal(HUMAN, "human"), business_id=business, allowed_business_ids=active(host),
                             global_capabilities=frozenset({"read_global_context"}), source="dashboard")


def conflicts(host, signoff_id, code):
    """Owner approval re-checks the prepared plan: the proposal ends conflicted and nothing is applied."""
    result = host.approve(signoff_id)
    assert result.status == "error" and result.error["code"] == "stale_signoff"
    failed = host.row(signoff_id)
    assert failed["state"] == "conflicted" and failed["error_json"]["code"] == code


def delivered(host, agent, *, required):
    return [(row["business_id"], row["event_type"]) for row in host.deliveries(agent, source_type="tasks") if row["required"] == required]


def test_the_helper_derives_scope_from_the_active_directory_only(host):
    add_business(host)
    with host.database.read() as uow:
        assert global_agent_ids(uow.connection) == sorted([AGENT, AGENT2, OTHER_AGENT, COORDINATOR, GLOBAL, SILENT])
        assert effective_business_ids(uow.connection, GLOBAL) == {BUSINESS, OTHER, NEW}
        assert effective_business_ids(uow.connection, HUMAN) == frozenset()
        assert not is_global_agent(uow.connection, STRANGER) and not is_global_agent(uow.connection, WORKER)
    assert host.count("business_memberships", "principal_id=?", GLOBAL) == 0


def test_a_global_agent_works_in_existing_and_new_businesses_without_membership_rows(host):
    add_business(host)
    for business in (BUSINESS, NEW):
        task = host.create_task("Work " + business[-2:], ctx=owner(host, business), assignee_ids=[GLOBAL])
        assert task["assignee_ids"] == [GLOBAL]
        listed = host.read("tasks_list", {"assignee_id": GLOBAL}, as_global(host, business))["tasks"]
        assert [entry["id"] for entry in listed] == [task["id"]]
        updated = host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "working"}}, as_global(host, business),
                            expected_revision=task["revision"])["task"]
        assert updated["status"] == "working"
        assert GLOBAL in {entry["id"] for entry in host.read("assignees_list", {}, owner(host, business))["assignees"]}
        # The global agent assigns itself and others in that business through the ordinary operation.
        mine = host.data("tasks_create", {"title": "Self " + business[-2:], "assignee_ids": [GLOBAL, HUMAN]}, as_global(host, business))["task"]
        assert sorted(mine["assignee_ids"]) == sorted([GLOBAL, HUMAN])
    assert delivered(host, GLOBAL, required=1) == [(BUSINESS, "created"), (NEW, "created")]
    # Global work is addressed to it as well.
    global_task = host.data("tasks_create", {"title": "Global", "scope": "global", "assignee_ids": [GLOBAL]}, owner(host, None))["task"]
    assert delivered(host, GLOBAL, required=1)[-1] == (None, "created")
    assert GLOBAL in {entry["id"] for entry in host.read("assignees_list", {}, owner(host, None))["assignees"]}
    assert global_task["assignee_ids"] == [GLOBAL]
    assert host.count("business_memberships", "principal_id=?", GLOBAL) == 0


def test_discussion_follows_and_global_namespace_eligibility(host):
    add_business(host)
    task = host.create_task("Discuss", ctx=owner(host, NEW), assignee_ids=[GLOBAL])
    item = ThreadItem(NEW, "task", task["id"])
    with host.database.read() as uow:
        assert follow_state(uow, item, GLOBAL)["reason"] == "assignee" and can_follow(uow, item, GLOBAL)
        # Every active business, but Global needs a global capability.
        assert eligible_agents(uow, NEW, [GLOBAL, SILENT]) == {GLOBAL, SILENT}
        assert eligible_agents(uow, None, [GLOBAL, SILENT]) == {GLOBAL}
    host.data("tasks_comment", {"task_id": task["id"], "text": "Please look"}, owner(host, NEW), expected_revision=task["revision"])
    assert delivered(host, GLOBAL, required=0) == [(NEW, "note_added")]
    detail = host.read("tasks_get", {"task_id": task["id"]}, as_global(host, NEW))["task"]
    host.later(minutes=1)
    commented = host.data("tasks_comment", {"task_id": task["id"], "text": "On it"}, as_global(host, NEW),
                          expected_revision=detail["revision"])["task"]
    assert commented["notes"][-1]["author_id"] == GLOBAL
    assert host.data("tasks_follow", {"task_id": task["id"], "following": False}, as_global(host, NEW))["following"] is False


def test_the_overview_spans_every_active_business_without_membership_rows(host):
    add_business(host)
    with host.database.transaction() as uow:
        uow.connection.execute("DELETE FROM business_memberships WHERE principal_id=?", (COORDINATOR,))
    work = host.create_task("New business work", ctx=owner(host, NEW), assignee_ids=[COORDINATOR])
    coordinator = replace(host.coordinator(), allowed_business_ids=active(host))
    overview = host.read("tasks_overview", {}, coordinator)
    assert [entry["id"] for group in overview["scopes"] for entry in group["active"]] == [work["id"]]
    assert overview["scopes"][0]["business_id"] == NEW


def test_archived_businesses_stay_excluded(host):
    task = host.create_task("Before archive", ctx=host.owner(OTHER), assignee_ids=[GLOBAL])
    assert task["assignee_ids"] == [GLOBAL]
    archive(host, OTHER)
    with host.database.read() as uow:
        assert effective_business_ids(uow.connection, GLOBAL) == {BUSINESS}   # the stored list names only OTHER
        assert eligible_agents(uow, OTHER, [GLOBAL]) == set()
        assert not can_follow(uow, ThreadItem(OTHER, "task", task["id"]), GLOBAL)
    assert host.error("tasks_list", {}, as_global(host, OTHER, allowed={BUSINESS, OTHER})) == "forbidden"
    assert host.read("tasks_list", {}, as_global(host, BUSINESS))["tasks"] == []


def test_forged_scope_capability_or_home_cannot_expand_a_global_agent(host):
    unknown = "00000000-0000-4000-8000-0000000000b9"
    base = as_global(host, BUSINESS)
    assert host.read("tasks_list", {}, base)["tasks"] == []
    assert host.error("tasks_list", {}, replace(base, allowed_business_ids=base.allowed_business_ids | {unknown})) == "forbidden"
    assert host.error("tasks_list", {}, replace(base, global_capabilities=frozenset({"read_global_context", "manage_ideas"}))) == "forbidden"
    assert host.error("tasks_list", {}, replace(base, home_business_id=BUSINESS)) == "forbidden"
    assert host.error("tasks_list", {}, replace(base, profile_revision=2)) == "context_refresh_required"
    # A profile without a global capability cannot claim one, and neither can an agent that is not global.
    assert host.error("tasks_list", {}, as_global(host, BUSINESS, identity=SILENT)) == "forbidden"


@pytest.mark.parametrize("change", ["retire", "deactivate"])
def test_retirement_or_deactivation_removes_eligibility_and_invalidates_a_staged_assignment(host, change):
    task = host.create_task("Staged")
    host.agents.overrides["tasks_assign"] = "signoff"
    signoff_id = host.stage("tasks_assign", {"task_id": task["id"], "assignee_ids": [GLOBAL]}, host.coordinator(BUSINESS),
                            expected_revision=task["revision"])
    with host.database.transaction() as uow:
        if change == "retire":
            uow.repository("agent_profiles", business_id=None).update(GLOBAL, {"status": "retired"}, expected_revision=1)
        else:
            uow.repository("principals").update(GLOBAL, {"active": False}, expected_revision=1)
    conflicts(host, signoff_id, "revision_conflict")
    assert host.read("tasks_get", {"task_id": task["id"]})["task"]["assignee_ids"] == []
    with host.database.read() as uow:
        assert not is_global_agent(uow.connection, GLOBAL) and effective_business_ids(uow.connection, GLOBAL) == frozenset()
        assert eligible_agents(uow, BUSINESS, [GLOBAL]) == set() and eligible_agents(uow, None, [GLOBAL]) == set()
    host.agents.overrides.pop("tasks_assign")
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [GLOBAL]}, expected_revision=task["revision"]) == "invalid_assignee"
    assert GLOBAL not in {entry["id"] for entry in host.read("assignees_list")["assignees"]}
    assert host.error("tasks_list", {}, as_global(host, BUSINESS)) in {"inactive_agent", "invalid_principal"}


def test_a_retired_global_proposer_still_conflicts_its_prepared_signoff(host):
    host.agents.overrides["tasks_create"] = "signoff"
    signoff_id = host.stage("tasks_create", {"title": "Proposed"}, as_global(host, BUSINESS))
    with host.database.transaction() as uow:
        uow.repository("agent_profiles", business_id=None).update(GLOBAL, {"status": "retired"}, expected_revision=1)
    conflicts(host, signoff_id, "inactive_agent")
    assert host.count("tasks", "title='Proposed'") == 0


def test_humans_and_system_workers_keep_membership_rules(host):
    task = host.create_task("Human rules", ctx=host.owner(OTHER))
    # STRANGER is a member of BUSINESS only; a system worker is never an assignee.
    for identity in (STRANGER, WORKER):
        assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [identity]}, host.owner(OTHER),
                          expected_revision=task["revision"]) == "invalid_assignee"
    listed = {entry["id"] for entry in host.read("assignees_list", {}, host.owner(OTHER))["assignees"]}
    assert GLOBAL in listed and STRANGER not in listed and WORKER not in listed
    with host.database.read() as uow:
        assert not can_follow(uow, ThreadItem(OTHER, "task", task["id"]), STRANGER)
        assert can_follow(uow, ThreadItem(BUSINESS, "task", task["id"]), STRANGER)
        assert eligible_agents(uow, OTHER, [STRANGER, WORKER]) == set()


def test_owner_directed_restriction_still_applies_to_globals(host):
    host.assignees.directed.add(GLOBAL)
    task = host.create_task("Directed")
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [GLOBAL]}, expected_revision=task["revision"]) == "invalid_assignee"
    assert host.error("tasks_create", {"title": "Global", "scope": "global", "assignee_ids": [GLOBAL]}, host.owner(None)) == "invalid_assignee"
    assert GLOBAL not in {entry["id"] for entry in host.read("assignees_list")["assignees"]}


def test_a_business_home_registration_never_acts_takes_work_or_is_addressed(host):
    home = host.home_agent(BUSINESS)              # active profile and principal, with an active membership row
    for ctx in (home, replace(home, home_business_id=None), replace(home, global_capabilities=frozenset({"read_global_context"}))):
        assert host.error("tasks_list", {}, ctx) == "forbidden"
        assert host.error("tasks_create", {"title": "Mine"}, ctx) == "forbidden"
    assert host.error("actions_record", {"text": "Shipped"}, home) == "forbidden"
    task = host.create_task("Not for home agents")
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [HOME_AGENT]},
                      expected_revision=task["revision"]) == "invalid_assignee"
    assert HOME_AGENT not in {entry["id"] for entry in host.read("assignees_list")["assignees"]}
    with host.database.read() as uow:
        assert not is_global_agent(uow.connection, HOME_AGENT) and effective_business_ids(uow.connection, HOME_AGENT) == frozenset()
        assert eligible_agents(uow, BUSINESS, [HOME_AGENT]) == set() and eligible_agents(uow, None, [HOME_AGENT]) == set()
        assert not can_follow(uow, ThreadItem(BUSINESS, "task", task["id"]), HOME_AGENT)


def test_a_staged_proposal_from_a_retained_business_home_registration_cannot_be_applied(host):
    host.agents.overrides["tasks_create"] = "signoff"
    signoff_id = host.stage("tasks_create", {"title": "Proposed earlier"}, host.agent())
    # A host-side registration change turns the proposer into a business-home profile: the original proposer is
    # re-checked at approval, and the engine refuses it rather than executing on its behalf.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET home_business_id=?, namespace=?, profile_json=? WHERE agent_id=?",
                               (BUSINESS, BUSINESS, json.dumps({"role": {"id": "business-agent"}, "scope": {"kind": "business"}}), AGENT))
    conflicts(host, signoff_id, "forbidden")
    assert host.count("tasks", "title='Proposed earlier'") == 0


def store_profile(host, identity, profile_json):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (profile_json, identity))


def test_the_shared_authority_names_the_supported_global_roles_and_the_one_repeatable_role():
    assert SUPPORTED_GLOBAL_ROLES == {"ordubashy", "learning-applier", "global-worker"}
    assert not {"founder", "global-marketing", "business-agent", "dev"} & SUPPORTED_GLOBAL_ROLES
    assert REPEATABLE_GLOBAL_ROLES == {"global-worker"} and REPEATABLE_GLOBAL_ROLES < SUPPORTED_GLOBAL_ROLES
    row = {"home_business_id": None, "status": "active", "kind": "agent", "active": 1, "profile_json": json.dumps(global_data())}
    assert current_global_row(row) == global_data() == current_global_data(global_data())
    for change in ({"home_business_id": BUSINESS}, {"status": "retired"}, {"kind": "human"}, {"active": 0}, {"profile_json": "{"},
                   {"profile_json": b"\xff"}, {"profile_json": None}):
        assert current_global_row({**row, **change}) is None, change
    assert current_global_row(None) is None



def test_a_profile_too_deeply_nested_to_decode_or_copy_fails_closed():
    deep = "[" * 100_000 + "]" * 100_000
    nested = []
    for _ in range(100_000):
        nested = [nested]
    for profile in (deep, deep.encode(), {**global_data(), "extra": nested}):
        assert current_global_data(profile) is None
    row = {"home_business_id": None, "status": "active", "kind": "agent", "active": 1, "profile_json": deep}
    assert current_global_row(row) is None

def test_all_supported_roles_receive_and_follow_new_business_work_without_memberships(host):
    # COORDINATOR is Ordubashy, AGENT is a worker and AGENT2 is Learning Applier.
    roles = (COORDINATOR, AGENT, AGENT2)
    with host.database.read() as uow:
        assert {global_profile(uow.connection, agent)["profile"]["role"]["id"] for agent in roles} == SUPPORTED_GLOBAL_ROLES
    add_business(host)   # registered after every profile and membership: included at once
    with host.database.transaction() as uow:
        uow.connection.execute("DELETE FROM business_memberships WHERE principal_id IN (?,?,?)", roles)
    task = host.create_task("Everyone", ctx=owner(host, NEW), assignee_ids=list(roles))
    item = ThreadItem(NEW, "task", task["id"])
    with host.database.read() as uow:
        assert eligible_agents(uow, NEW, roles) == set(roles) and all(can_follow(uow, item, agent) for agent in roles)
    for agent in roles:
        assert delivered(host, agent, required=1) == [(NEW, "created")]
    host.data("tasks_comment", {"task_id": task["id"], "text": "Note"}, owner(host, NEW), expected_revision=task["revision"])
    for agent in roles:
        assert delivered(host, agent, required=0) == [(NEW, "note_added")]
    assert host.count("business_memberships", "principal_id IN (?,?,?)", *roles) == 0


WORKER_A = "00000000-0000-4000-8000-0000000000d1"
WORKER_B = "00000000-0000-4000-8000-0000000000d2"


def test_global_workers_are_current_globals_and_several_may_be_active_at_once(host):
    for identity in (WORKER_A, WORKER_B):
        insert_global(host, identity, ["read_global_context"], role="global-worker")
    add_business(host)   # registered after both profiles: reachable at once, with no membership rows
    task = host.create_task("Both workers", ctx=owner(host, NEW), assignee_ids=[WORKER_A, WORKER_B])
    with host.database.read() as uow:
        assert {WORKER_A, WORKER_B} <= set(global_agent_ids(uow.connection))
        for identity in (WORKER_A, WORKER_B):
            assert global_profile(uow.connection, identity)["profile"]["role"]["id"] == "global-worker"
            assert effective_business_ids(uow.connection, identity) == {BUSINESS, OTHER, NEW}
        assert eligible_agents(uow, NEW, [WORKER_A, WORKER_B]) == {WORKER_A, WORKER_B}
        assert all(can_follow(uow, ThreadItem(NEW, "task", task["id"]), identity) for identity in (WORKER_A, WORKER_B))
    for identity in (WORKER_A, WORKER_B):
        assert delivered(host, identity, required=1) == [(NEW, "created")]
    assert host.count("business_memberships", "principal_id IN (?,?)", WORKER_A, WORKER_B) == 0
    # Deactivating one leaves the other current.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (WORKER_A,))
    with host.database.read() as uow:
        assert not is_global_agent(uow.connection, WORKER_A) and is_global_agent(uow.connection, WORKER_B)


@pytest.mark.parametrize("profile", [
    {**global_data(), "role": {"id": "global-role"}},          # unknown role
    {**global_data(), "role": {"id": "business-agent"}},       # retired business role
    {**global_data(), "role": {"id": "onbashy"}},              # retired historical role
    {**global_data(), "role": {"id": "founder"}},              # retired Founder, even while its stored profile says active
    {**global_data(), "role": {"id": ["founder"]}},
    {**global_data(), "role": "founder"},
    {key: value for key, value in global_data().items() if key != "role"},
    {**global_data(), "status": "inactive"},                   # decoded status, with an active row
    {key: value for key, value in global_data().items() if key != "status"},
    {**global_data(), "status": True},
    {**global_data(), "scope": {"kind": "business", "business_id": BUSINESS}},
    {**global_data(), "scope": {**global_data()["scope"], "business_id": BUSINESS}},
    {**global_data(), "scope": {**global_data()["scope"], "allowed_business_ids": BUSINESS}},
    {**global_data(), "scope": {**global_data()["scope"], "global_capabilities": "read_global_context"}},
    {**global_data(), "scope": "ecosystem"},
    ["founder"], "founder", None,
], ids=lambda profile: json.dumps(profile)[:40])
def test_an_unsupported_inactive_or_malformed_no_home_profile_is_never_current(host, profile):
    task = host.create_task("Retained", ctx=owner(host, BUSINESS), assignee_ids=[GLOBAL])
    store_profile(host, GLOBAL, json.dumps(profile))
    assert current_global_data(profile) is None
    item = ThreadItem(BUSINESS, "task", task["id"])
    with host.database.read() as uow:
        assert global_profile(uow.connection, GLOBAL) is None and not is_global_agent(uow.connection, GLOBAL)
        assert GLOBAL not in global_agent_ids(uow.connection) and effective_business_ids(uow.connection, GLOBAL) == frozenset()
        assert eligible_agents(uow, BUSINESS, [GLOBAL]) == set() and eligible_agents(uow, None, [GLOBAL]) == set()
        assert not can_follow(uow, item, GLOBAL)
    before = host.deliveries(GLOBAL, source_type="tasks")
    commented = host.data("tasks_comment", {"task_id": task["id"], "text": "Still there?"}, owner(host, BUSINESS),
                          expected_revision=task["revision"])["task"]
    assert host.deliveries(GLOBAL, source_type="tasks") == before
    assert host.error("tasks_assign", {"task_id": task["id"], "assignee_ids": [AGENT, GLOBAL]}, owner(host, BUSINESS),
                      expected_revision=commented["revision"]) == "invalid_assignee"
    assert GLOBAL not in {entry["id"] for entry in host.read("assignees_list", {}, owner(host, BUSINESS))["assignees"]}
    # The retained record still names it for the owner.
    assert host.read("tasks_get", {"task_id": task["id"]}, owner(host, BUSINESS))["task"]["assignee_ids"] == [GLOBAL]
    assert host.error("tasks_list", {}, as_global(host, BUSINESS)) in {"forbidden", "inactive_agent", "invalid_principal"}


def test_an_explicit_unfollow_tombstone_survives_while_required_updates_still_arrive(host):
    task = host.create_task("Tombstone", ctx=owner(host, BUSINESS), assignee_ids=[GLOBAL])
    assert host.data("tasks_follow", {"task_id": task["id"], "following": False}, as_global(host, BUSINESS))["following"] is False
    current = host.read("tasks_get", {"task_id": task["id"]}, owner(host, BUSINESS))["task"]
    commented = host.data("tasks_comment", {"task_id": task["id"], "text": "FYI"}, owner(host, BUSINESS),
                          expected_revision=current["revision"])["task"]
    assert delivered(host, GLOBAL, required=0) == []
    host.data("tasks_update", {"task_id": task["id"], "changes": {"status": "working"}}, owner(host, BUSINESS),
              expected_revision=commented["revision"])
    assert [event for _, event in delivered(host, GLOBAL, required=1)][0] == "created" and len(delivered(host, GLOBAL, required=1)) == 2
    with host.database.read() as uow:
        assert follow_state(uow, ThreadItem(BUSINESS, "task", task["id"]), GLOBAL)["following"] is False
