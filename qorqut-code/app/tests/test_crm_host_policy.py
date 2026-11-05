"""CRM operation policy: each new name is bounded by its existing prerequisite operations.

Runs the Host policy mapping, combination rule and discovery against the real composed CRM Domain catalog. Handler-side
writes and revalidation are also exercised by test_crm_integration_flows."""

from __future__ import annotations

import pytest

from qorqut_operations import crm
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id

from bazarlyk.composition.catalog import builtin_registry
from bazarlyk.composition.policy import CRM_PREREQUISITES, GLOBAL_DISCOVERY_OPERATIONS, resolve_operation_policy
from host_foundation_support import (agent_context, build_instance, create_registered_global, create_worker, register_sources,
                                     select_coordinator, set_agent_overrides)

NEW_NAMES = (*(entry.name for entry in crm.CATALOG_ENTRIES), "crm_activity_list")


@pytest.fixture(scope="module")
def registry():
    return builtin_registry().freeze()


def decide(registry, name, defaults=None, overrides=None, *, role="ordubashy", scope="business"):
    return resolve_operation_policy(name, defaults or {}, overrides or {}, registry, role_id=role, scope=scope)


def test_every_new_operation_has_exactly_one_prerequisite_and_the_mapping_names_no_other_operation(registry):
    assert set(CRM_PREREQUISITES) == set(NEW_NAMES)
    assert len(NEW_NAMES) == len(set(NEW_NAMES)) == 9
    assert {name: CRM_PREREQUISITES[name] for name in NEW_NAMES} == {
        "crm_entries_list": "tables_rows_list", "crm_entries_add": "tables_rows_write", "crm_entries_update": "tables_rows_write",
        "crm_entries_remove": "tables_rows_write", "tables_thread_get": "tables_get", "tables_follow": "tables_get",
        "tables_comment": "tables_rows_write", "crm_activity_list": "tasks_get", "crm_activity_record": "tasks_comment"}
    known = {entry.name for entry in registry.entries()}
    assert set(CRM_PREREQUISITES.values()) <= known
    # A Table comment is Table write authority; it never borrows Task comment, schema or archive authority.
    assert CRM_PREREQUISITES["tables_comment"] == "tables_rows_write" and CRM_PREREQUISITES["crm_activity_record"] == "tasks_comment"


@pytest.mark.parametrize("name", NEW_NAMES)
def test_a_name_with_no_grant_of_its_own_follows_exactly_its_prerequisite(registry, name):
    base = CRM_PREREQUISITES[name]
    for mode in ("allow", "deny"):
        decision = decide(registry, name, {base: mode})
        assert decision.mode == mode, (name, mode, decision)
    assert decide(registry, name).mode == "deny"                                  # neither granted: nothing opens by default


@pytest.mark.parametrize("name", NEW_NAMES)
def test_an_allow_on_the_new_name_never_lifts_a_denied_prerequisite(registry, name):
    base = CRM_PREREQUISITES[name]
    assert decide(registry, name, {name: "allow", base: "deny"}).mode == "deny"
    assert decide(registry, name, {name: "allow"}, {base: "deny"}).mode == "deny"
    assert decide(registry, name, {name: "allow"}).mode == "deny"                  # prerequisite absent: also denied, not inferred


@pytest.mark.parametrize("name", NEW_NAMES)
def test_an_explicit_deny_on_the_new_name_wins_over_every_allow(registry, name):
    base = CRM_PREREQUISITES[name]
    assert decide(registry, name, {name: "allow", base: "allow"}, {name: "deny"}).mode == "deny"
    assert decide(registry, name, {base: "allow"}, {name: "deny", base: "allow"}).mode == "deny"


@pytest.mark.parametrize("name", [name for name in NEW_NAMES if name not in ("tables_follow",)
                                  and CRM_PREREQUISITES[name] in ("tables_rows_write", "tasks_comment")])
def test_a_signoff_requirement_of_the_prerequisite_carries_to_the_new_write(registry, name):
    base = CRM_PREREQUISITES[name]
    decision = decide(registry, name, {name: "allow", base: "signoff"})
    assert decision.mode == "signoff"
    assert decide(registry, name, {name: "signoff", base: "allow"}).mode == "signoff"   # and its own is never weakened either way


def test_a_signoff_the_operation_cannot_stage_is_a_refusal_not_an_allow():
    """No current mapping reaches this (every non-Signoff name builds on a read that cannot require one, and configuration refuses a
    Signoff mode on such a read), so the guard is shown with a stand-in resolver: an inherited Signoff on an operation without a
    Signoff form is a denial with its reason, never an allow."""
    from types import SimpleNamespace

    from qorqut_operations.policy.rules import PolicyDecision

    from bazarlyk.composition.policy import _prerequisites

    registry = SimpleNamespace(metadata=lambda name: SimpleNamespace(supports_signoff=False))
    decision = _prerequisites("crm_activity_list", {"crm_activity_list": "allow"}, {}, registry, role_id="ordubashy", scope="business",
                              resolve=lambda base, scope: PolicyDecision("signoff", "role_default"))
    assert (decision.mode, decision.provenance) == ("deny", "prerequisite_signoff_unsupported")


def test_configuration_refuses_a_signoff_mode_on_an_operation_without_a_signoff_form(registry):
    from qorqut_operations.contracts import DomainError

    with pytest.raises(DomainError):
        decide(registry, "crm_activity_list", {"crm_activity_list": "allow", "tasks_get": "signoff"})


def test_a_prerequisite_is_judged_in_the_callers_own_scope(registry):
    """Global reaches the Table rule, not the business one: an ordinary worker has no Global Table default."""
    grants = {"tables_rows_write": "allow", "crm_entries_add": "allow", "tables_rows_list": "allow", "crm_entries_list": "allow"}
    for name in ("crm_entries_add", "crm_entries_list"):
        assert decide(registry, name, grants, role="global-worker", scope="business").mode == "allow"
        assert decide(registry, name, grants, role="global-worker", scope="global").mode == "deny"
        # An explicit override on the prerequisite (the owner's grant) is what opens the Global Table for that agent.
        opened = {"tables_rows_write": "allow", "tables_rows_list": "allow"}
        assert decide(registry, name, grants, opened, role="global-worker", scope="global").mode == "allow"
        # Ordubashy holds the standing Global Table default, so its own role grant is enough there.
        assert decide(registry, name, grants, role="ordubashy", scope="global").mode == "allow"
        assert decide(registry, name, {name: "deny"}, role="ordubashy", scope="global").mode == "deny"
    # Task-based activity has no Global Table rule; its scope re-entry changes nothing for a non-table prerequisite.
    assert decide(registry, "crm_activity_list", {"crm_activity_list": "allow", "tasks_get": "allow"}, role="global-worker",
                  scope="global").mode == "allow"


def test_discovery_evaluates_both_scopes_for_the_table_based_names_only():
    table_based = {name for name, base in CRM_PREREQUISITES.items() if base.startswith("tables_")}
    assert table_based <= GLOBAL_DISCOVERY_OPERATIONS
    assert not ({"crm_activity_list", "crm_activity_record"} & GLOBAL_DISCOVERY_OPERATIONS)
    assert len(table_based) == 7


# ----- through the real invocation path --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    instance, ids = build_instance(tmp_path_factory.mktemp("crm-policy"), businesses=("first",))
    first = ids["first"]
    register_sources(instance, first, "first")
    ordubashy = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    worker = create_worker(instance, "worker-a")
    instance.runtime.freeze_registry()
    created = instance.invoke(instance.human(first), "tasks_create", {"title": "Synthetic record", "scope": "business"})
    assert created.status == "ok", created.error
    try:
        yield {"instance": instance, "business": first, "ordubashy": ordubashy, "worker": worker, "task": created.data["task"]["id"]}
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def invoke(host, agent, name, arguments):
    ctx = host["instance"].runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=host["business"])
    return host["instance"].runtime.application.invoke(ctx, name, {"scope": "business", **arguments}, InvocationOptions(idempotency_key=new_id()))


def tools(host, agent):
    configured = host["instance"].runtime.resolver.resolve_agent_config(Principal(agent, "agent")).data["tools"]
    return {row["canonical_operation"]: row for row in configured}


def test_the_activity_read_follows_the_task_read_decision_of_the_calling_agent(host):
    worker, task = host["worker"], host["task"]
    set_agent_overrides(host["instance"], worker, {"tasks_get": "deny", "crm_activity_list": "allow"}, [host["business"]])
    denied = invoke(host, worker, "crm_activity_list", {"task_id": task})
    assert (denied.status, denied.error["code"]) == ("error", "policy_denied")
    set_agent_overrides(host["instance"], worker, {"tasks_get": "allow", "crm_activity_list": "deny"}, [host["business"]])
    assert invoke(host, worker, "crm_activity_list", {"task_id": task}).error["code"] == "policy_denied"
    set_agent_overrides(host["instance"], worker, {"tasks_get": "allow", "crm_activity_list": "allow"}, [host["business"]])
    allowed = invoke(host, worker, "crm_activity_list", {"task_id": task})
    assert allowed.status == "ok" and allowed.data["record_ref"]["id"] == task
    set_agent_overrides(host["instance"], worker, {}, [host["business"]])


def test_discovery_matches_invocation_for_the_new_names(host):
    worker = host["worker"]
    set_agent_overrides(host["instance"], worker, {"tasks_get": "allow", "crm_activity_list": "allow", "tasks_comment": "deny",
                                                  "crm_activity_record": "allow"}, [host["business"]])
    rows = tools(host, worker)
    assert set(NEW_NAMES) <= set(rows)
    assert rows["crm_activity_list"]["available"] and rows["crm_activity_list"]["effective_mode"] == "allow"
    # The grant on the name does not make it discoverable while its Task comment prerequisite is denied.
    assert rows["crm_activity_record"]["effective_mode"] == "deny" and not rows["crm_activity_record"]["available"]
    # A complete recording request reaches the policy gate: the denial on its Task-comment prerequisite wins over the grant.
    assert invoke(host, worker, "crm_activity_record", {
        "task_id": host["task"], "kind": "email", "summary": "Synthetic observation", "body": "Synthetic recorded body",
        "occurred_at": "2026-10-05T10:00:00.000000Z", "source": {"kind": "email", "title": "Synthetic mailbox"},
        "event_key": "policy-1"}).error["code"] == "policy_denied"
    # Table-based names are never available to an agent that holds nothing on Tables.
    for name in ("crm_entries_list", "crm_entries_add", "tables_comment", "tables_thread_get", "tables_follow"):
        assert not rows[name]["available"], name
    set_agent_overrides(host["instance"], worker, {}, [host["business"]])


def test_the_registered_global_ordubashy_reaches_the_global_table_names_by_its_standing_table_default(host):
    rows = tools(host, host["ordubashy"])
    for name in ("crm_entries_list", "tables_thread_get"):
        assert rows[name].get("scope_modes", {}).get("global") in ("allow", None)
    # A plain worker has no standing Global Table default: discovery shows both scopes and denies where it must.
    worker_rows = tools(host, host["worker"])
    assert worker_rows["crm_entries_add"].get("scope_modes", {}).get("global", "deny") == "deny"
