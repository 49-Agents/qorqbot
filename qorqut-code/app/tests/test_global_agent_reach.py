"""Acceptance case: the remaining global-agent reach sites, current work, System Dev incidents and agent resources, with no
agent membership anywhere.

One synthetic host with two businesses. The registered Ordubashy (the selected coordinator) and the Learning Applier are active
supported globals; neither has a membership row, and both reach every active business through the shared ``global_scope``
contract:

* current work: the roster is the active supported globals (the native eligibility predicate). A permitted business view
  lists them with only that business's items and no invented residence. A denied source never shows its title, and an exact
  session read and update stay the owner session's, with revision and incomplete-item protection;
* inactive, retired, unsupported-role, malformed and business-home identities are not current agents, and archived or
  unselected businesses are outside every scope;
* incidents: business notifications reach the configured coordinator without a membership; Ordubashy reads every Global
  impact while another global reads only its own; the system reporter's memberships and the owner's reads keep their meaning;
* agent resources: an active supported global resolves in any reachable active business with its principal and profile
  pinned, never a membership, under the ``agents_get`` policy.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network, native process or live data."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qorqut_operations.application.plans import PlanBuilder, ref
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, plain

from bazarlyk.incidents import IncidentOperations, IncidentReporter, ensure_system_reporter
from host_foundation_support import (build_instance, create_registered_global, create_worker, global_agent_without_capability,
                                     historical_business_agent, owner_global_context, register_sources, select_coordinator,
                                     set_agent_overrides, WORKER_ROLE)

SECRET = "Confidential launch plan"
ARCHIVED = "2026-10-14T00:00:00+00:00"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    colleague = create_registered_global(instance, "learning-applier")
    instance.runtime.freeze_registry()
    # The Applier's role default grants no current-work operation: the owner allows the ones the cases use.
    set_agent_overrides(instance, colleague, {"agent_work_update": "allow", "agents_activity_list": "allow", "tasks_get": "allow"},
                        [first, second])
    host = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, tmp=tmp_path,
                           first=first, second=second, coordinator=coordinator, colleague=colleague)
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
                                      "WHERE p.kind='agent'").fetchone()[0] == 0
    return host


def owner(host):
    """The installation owner in Global, holding every global capability and its member businesses (the dashboard's)."""
    return replace(owner_global_context(host.instance), allowed_business_ids=frozenset({host.first, host.second}))


def agent(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def session(host, identity):
    """A registered native session of ``identity`` (its machine registration and the activity row registration makes)."""
    registration, session_id = new_id(), new_id()
    with host.database.transaction() as uow:
        uow.repository("machine_registrations").insert({
            "id": registration, "principal_id": identity, "workspace": str(host.tmp / registration),
            "service_url": "http://127.0.0.1:9", "profile_revision": 1})
    assert host.runtime.activity.register_session(agent(host, identity), registration, session_id, "codex")
    return session_id


def own_session(host, ctx, session_id, **scope):
    return ok(host.instance.invoke(ctx, "agents_activity_list", {"session_id": session_id, **scope}))["agents"][0]["sessions"][0]


def declare(host, identity, session_id, items):
    ctx = agent(host, identity)
    revision = own_session(host, ctx, session_id, scope="global")["work_revision"]
    return ok(host.instance.invoke(ctx, "agent_work_update", {"scope": "global", "session_id": session_id, "items": items,
                                                              "expected_work_revision": revision}))


def roster(host, ctx, **arguments):
    data = ok(host.instance.invoke(ctx, "agents_activity_list", arguments))
    return {row["agent_id"]: row for row in data["agents"]}


def summaries(row):
    return [item["summary"] for session_view in row["sessions"] for item in session_view["items"]]


def task(host, business, title):
    return ok(host.instance.invoke(host.instance.human(business), "tasks_create", {"title": title}))["task"]["id"]


def work(host):
    """The colleague's one session declaring work in both businesses and Global; the first item names a Task."""
    launch = task(host, host.first, SECRET)
    colleague_session = session(host, host.colleague)
    declare(host, host.colleague, colleague_session, [
        {"business_id": host.first, "task_id": launch, "summary": "First launch"},
        {"business_id": host.second, "summary": "Second follow-up"},
        {"business_id": None, "summary": "Global review"}])
    return colleague_session


# -- current work ----------------------------------------------------------------------------------------------------------

def test_global_current_work_reaches_every_active_business_and_a_business_view_filters_items_only(host):
    work(host)
    session(host, host.coordinator)
    historical = historical_business_agent(host.instance, host.first)
    with host.database.transaction() as uow:
        # The historical identity cannot register a session itself; its old row is written as v1 stored it.
        registration = new_id()
        uow.repository("machine_registrations").insert({"id": registration, "principal_id": historical,
            "workspace": str(host.tmp / registration), "service_url": "http://127.0.0.1:9", "profile_revision": 1})
        uow.connection.execute("INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime,items_json) "
                               "VALUES(?,?,?,?,?)", (historical, new_id(), registration, "codex",
                                                     json.dumps([{"business_id": host.first, "summary": "Old resident"}])))

    everything = roster(host, owner(host), scope="global")
    assert set(everything) == {host.coordinator, host.colleague}
    row = everything[host.colleague]
    assert (row["business_id"], row["business_slug"], row["business_name"]) == (None, None, "Global")
    assert summaries(row) == ["First launch", "Second follow-up", "Global review"]
    assert row["sessions"][0]["items"][0]["title"] == SECRET

    # A selected business filters the visible work; the agent stays a Global agent in the roster.
    for business, expected in ((host.first, ["First launch"]), (host.second, ["Second follow-up"])):
        for ctx in (host.instance.human(business), agent(host, host.coordinator, business)):
            view = roster(host, ctx)
            assert set(view) == {host.coordinator, host.colleague}
            assert summaries(view[host.colleague]) == expected
            assert view[host.colleague]["business_id"] is None and view[host.colleague]["business_name"] == "Global"
    assert summaries(roster(host, agent(host, host.coordinator), scope="global")[host.colleague]) == summaries(row)

    # A denied source policy hides the item and never its title.
    set_agent_overrides(host.instance, host.coordinator, {"tasks_get": "deny"}, [host.first, host.second])
    denied = ok(host.instance.invoke(agent(host, host.coordinator), "agents_activity_list", {"scope": "global"}))
    assert summaries({r["agent_id"]: r for r in denied["agents"]}[host.colleague]) == ["Second follow-up", "Global review"]
    assert SECRET not in json.dumps(denied) and "First launch" not in json.dumps(denied)


def test_exact_session_work_keeps_ownership_revision_and_incomplete_item_protection(host):
    colleague_session = work(host)
    first_view = agent(host, host.colleague, host.first)
    exact = own_session(host, first_view, colleague_session)
    assert [item["summary"] for item in exact["items"]] == ["First launch"]
    assert exact["items_complete"] is False and exact["items_replacement_safe"] is False

    patched = ok(host.instance.invoke(first_view, "agent_work_update", {
        "session_id": colleague_session, "expected_work_revision": exact["work_revision"],
        "patch": [{"action": "replace", "item_key": exact["item_keys"][0],
                   "item": {"business_id": host.first, "summary": "First launch, revised"}}]}))
    assert patched["work_revision"] == exact["work_revision"] + 1
    whole = own_session(host, agent(host, host.colleague), colleague_session, scope="global")
    assert [item["summary"] for item in whole["items"]] == ["First launch, revised", "Second follow-up", "Global review"]

    stale = host.instance.invoke(first_view, "agent_work_update", {
        "session_id": colleague_session, "expected_work_revision": exact["work_revision"], "patch": []})
    assert code(stale) == "revision_conflict"
    # Another global never reads or writes this exact session.
    other = agent(host, host.coordinator)
    assert code(host.instance.invoke(other, "agents_activity_list", {"scope": "global", "session_id": colleague_session})) == "not_found"
    assert code(host.instance.invoke(other, "agents_activity_list", {"scope": "global", "session_id": colleague_session,
                                                                      "agent_id": host.colleague})) == "forbidden"
    assert code(host.instance.invoke(other, "agent_work_update", {
        "scope": "global", "session_id": colleague_session, "expected_work_revision": whole["work_revision"],
        "items": []})) == "not_found"


def briefed(host, business):
    data = ok(host.instance.invoke(host.instance.human(business), "business_briefing", {}))
    return [row["agent_id"] for row in data["agents"]["items"]]


def test_a_business_briefing_lists_globals_with_work_there_once_and_ordubashy_starts_with_its_peers(host):
    work(host)
    coordinator_session = session(host, host.coordinator)
    declare(host, host.coordinator, coordinator_session, [{"business_id": host.second, "summary": "Second only"}])
    idle = create_worker(host.instance, "idle")
    session(host, idle)
    # Each global with relevant work appears once; an idle or other-business-only global does not.
    assert briefed(host, host.first) == [host.colleague]
    assert sorted(briefed(host, host.second)) == sorted([host.colleague, host.coordinator])

    resolver = host.runtime.resolver

    def startup(identity):
        principal = Principal(identity, "agent")
        return host.runtime.activity.startup(principal, resolver.resolve_agent_config(principal).data)

    historical = historical_business_agent(host.instance, host.first)
    team = startup(host.coordinator)
    assert team.startswith("TEAM CURRENT WORK") and host.colleague in team and idle in team
    assert host.coordinator not in team and historical not in team and "First launch" in team
    assert startup(host.colleague) == ""  # only Ordubashy starts with the team summary
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (idle,))
    assert idle not in startup(host.coordinator)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.colleague,))
    assert startup(host.coordinator) == ""


def mutations(host, identity):
    """Each way an identity stops being a current active supported global, applied and then restored."""
    def principal_inactive(uow):
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (identity,))

    def retired(uow):
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (identity,))

    def unsupported_role(uow):
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','business-agent') "
                               "WHERE agent_id=?", (identity,))

    def malformed(uow):
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope','ecosystem') "
                               "WHERE agent_id=?", (identity,))

    return principal_inactive, retired, unsupported_role, malformed


def restore(host, identity, saved):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (identity,))
        uow.connection.execute("UPDATE agent_profiles SET status=?, profile_json=? WHERE agent_id=?",
                               (saved["status"], saved["profile_json"], identity))


def test_unavailable_identities_and_unreachable_businesses_are_outside_current_work(host):
    colleague_session = work(host)
    activity = host.runtime.activity
    ctx = agent(host, host.colleague)
    with host.database.read() as uow:
        saved = dict(uow.connection.execute("SELECT status, profile_json FROM agent_profiles WHERE agent_id=?",
                                            (host.colleague,)).fetchone())
        assert activity.scopes(ctx, uow) == {None, host.first, host.second}
        # The ctx intersection, and the Global capability gate.
        narrowed = replace(ctx, allowed_business_ids=frozenset({host.first}))
        assert activity.scopes(narrowed, uow) == {None, host.first}
        with pytest.raises(DomainError) as outside:
            activity.allowed(narrowed, host.second, "agents_activity_list", uow)
        assert outside.value.code == "forbidden"
        assert activity.scopes(replace(ctx, global_capabilities=frozenset()), uow) == {host.first, host.second}
    for mutation in mutations(host, host.colleague):
        with host.database.transaction() as uow:
            mutation(uow)
        with host.database.read() as uow:
            assert activity.scopes(ctx, uow) == set(), mutation.__name__
        assert host.colleague not in roster(host, owner(host), scope="global"), mutation.__name__
        restore(host, host.colleague, saved)
    assert host.colleague in roster(host, owner(host), scope="global")

    # A business-home identity, even an active one with its old membership, has no scope at all.
    historical = historical_business_agent(host.instance, host.first)
    with host.database.read() as uow:
        resident = InvocationContext(Principal(historical, "agent"), business_id=host.first, home_business_id=host.first,
                                     allowed_business_ids=frozenset({host.first}), source="mcp")
        assert activity.scopes(resident, uow) == set()

    # An archived business leaves every scope: its stored item is hidden and a new one is refused.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.second))
    assert summaries(roster(host, agent(host, host.colleague), scope="global")[host.colleague]) == ["First launch", "Global review"]
    current = own_session(host, agent(host, host.colleague), colleague_session, scope="global")
    refused = host.instance.invoke(agent(host, host.colleague), "agent_work_update", {
        "scope": "global", "session_id": colleague_session, "expected_work_revision": current["work_revision"],
        "patch": [{"action": "add", "item": {"business_id": host.second, "summary": "Archived work"}}]})
    assert code(refused) == "forbidden"


# -- incidents -------------------------------------------------------------------------------------------------------------

def observe(host, reporter, operation, business=None, agent_id=None):
    incidents = host.runtime.application.incident_reporter
    observation = incidents.observation(component="tasks", operation=operation, error_code="storage_unavailable",
                                        classification="infrastructure_fault", call_site="tasks.get",
                                        business_id=business, agent_id=agent_id)
    with host.database.transaction() as uow:
        return incidents.observe(observation, uow=uow, reporter_id=reporter)["incident_id"]


def incident_ids(host, ctx, **arguments):
    return {row["id"] for row in ok(host.instance.invoke(ctx, "system_incidents_agent_list", {"status": "all", **arguments}))["items"]}


def test_incidents_reach_active_globals_without_memberships_and_keep_the_global_distinction(host):
    with host.database.transaction() as uow:
        reporter = ensure_system_reporter(uow, host.runtime.authority, business_ids=[host.first, host.second])
    with host.database.read() as uow:
        # The system reporter's own business memberships are legitimate and unchanged.
        assert {row[0] for row in uow.connection.execute(
            "SELECT business_id FROM business_memberships WHERE principal_id=? AND active=1", (reporter,))} == {host.first, host.second}
    first_fault = observe(host, reporter, "first_fault", host.first, host.colleague)
    colleague_global = observe(host, reporter, "colleague_global", None, host.colleague)
    coordinator_global = observe(host, reporter, "coordinator_global", None, host.coordinator)
    second_fault = observe(host, reporter, "second_fault", host.second, host.coordinator)

    with host.database.read() as uow:
        delivered = {(row["source_id"], row["agent_id"]) for row in uow.connection.execute(
            "SELECT e.source_id, d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
            "WHERE e.event_type='system_incident_opened'")}
    # The configured coordinator receives business and Global notices without a membership, never its own.
    assert delivered == {(first_fault, host.coordinator), (colleague_global, host.coordinator)}

    everything = {first_fault, colleague_global, coordinator_global, second_fault}
    assert incident_ids(host, agent(host, host.coordinator), scope="global") == everything
    # Another permitted global sees its own Global impacts only, and every reachable business.
    assert incident_ids(host, agent(host, host.colleague), scope="global") == {first_fault, colleague_global, second_fault}
    assert incident_ids(host, agent(host, host.colleague, host.first)) == {first_fault}
    assert code(host.instance.invoke(agent(host, host.colleague), "system_incidents_agent_get",
                                     {"scope": "global", "incident_id": coordinator_global})) == "forbidden"
    history = ok(host.instance.invoke(agent(host, host.coordinator), "system_incidents_agent_get",
                                      {"scope": "global", "incident_id": coordinator_global}))
    assert [impact["agent_id"] for impact in history["impacts"]] == [host.coordinator]

    with host.database.read() as uow:
        operations = IncidentOperations(host.runtime.application)
        narrowed = replace(agent(host, host.colleague), allowed_business_ids=frozenset({host.first}))
        assert {row["id"] for row in operations.agent_list(narrowed, {"status": "all"}, None, uow)["items"]} == {
            first_fault, colleague_global}
        historical = historical_business_agent(host.instance, host.first)
        resident = InvocationContext(Principal(historical, "agent"), business_id=host.first, home_business_id=host.first,
                                     allowed_business_ids=frozenset({host.first}), source="mcp")
        assert operations.agent_list(resident, {"status": "all"}, None, uow)["items"] == []
        with pytest.raises(DomainError) as human:
            IncidentOperations.impact_scope(host.instance.human(host.first), uow)
        assert human.value.code == "forbidden"
    owner_rows = ok(host.instance.invoke(owner(host), "system_incidents_list", {"status": "all"}))
    assert {row["id"] for row in owner_rows["items"]} == everything

    bare = global_agent_without_capability(host.instance, WORKER_ROLE)
    with host.database.read() as uow:
        current = IncidentReporter.recipient_current
        assert current(uow.connection, host.colleague, host.first) and current(uow.connection, host.colleague, None)
        assert current(uow.connection, bare, host.first) and not current(uow.connection, bare, None)
        assert not current(uow.connection, historical, host.first) and not current(uow.connection, reporter, host.first)
    colleague = agent(host, host.colleague)
    with host.database.read() as uow:
        saved = dict(uow.connection.execute("SELECT status, profile_json FROM agent_profiles WHERE agent_id=?",
                                            (host.colleague,)).fetchone())
    for mutation in mutations(host, host.colleague):
        with host.database.transaction() as uow:
            mutation(uow)
        with host.database.read() as uow:
            assert not current(uow.connection, host.colleague, host.first), mutation.__name__
            assert not current(uow.connection, host.colleague, None), mutation.__name__
            assert IncidentOperations.impact_scope(colleague, uow) == ("(0)", []), mutation.__name__
        restore(host, host.colleague, saved)

    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.second))
    assert incident_ids(host, agent(host, host.colleague), scope="global") == {first_fault, colleague_global}
    with host.database.read() as uow:
        assert not current(uow.connection, host.colleague, host.second)


# -- agent resources -------------------------------------------------------------------------------------------------------

def resolve(host, ctx, identity, builder=False):
    with host.database.read() as uow:
        plan = PlanBuilder(ctx, "tasks_update", {}, "Pin an agent resource") if builder else None
        view = host.runtime.domains.related.get(ctx, uow, "agent_profiles", identity, plan)
        if plan is not None:
            host.runtime.application._scope_plan(ctx, plan.build())
            revisions = {target.resource: target.revision for target in plan.targets.values()}
            profile = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()[0]
            return view, revisions, profile
        return view


def refused(host, ctx, identity):
    with pytest.raises(DomainError) as error:
        resolve(host, ctx, identity)
    return error.value.code


def test_an_active_global_is_an_agent_resource_in_every_reachable_business_without_a_membership(host):
    def pin(table, namespace=None):
        return ref(table, host.colleague, None, namespace, tables=host.database.assembly.tables)

    for ctx in (host.instance.human(host.first), host.instance.human(host.second), agent(host, host.coordinator, host.first)):
        view, revisions, profile = resolve(host, ctx, host.colleague, builder=True)
        assert view["label"] == "Learning Applier" and view["state"] == "active"
        assert set(revisions) == {pin("principals"), pin("agent_profiles", "ecosystem")}
        assert revisions[pin("agent_profiles", "ecosystem")] == profile
    view, revisions, profile = resolve(host, owner(host), host.colleague, builder=True)
    assert set(revisions) == {pin("principals"), pin("agent_profiles")} and revisions[pin("agent_profiles")] == profile

    # The agents_get policy still applies.
    set_agent_overrides(host.instance, host.coordinator, {"agents_get": "deny"}, [host.first, host.second])
    assert refused(host, agent(host, host.coordinator, host.first), host.colleague) == "forbidden"

    historical = historical_business_agent(host.instance, host.first)
    assert refused(host, host.instance.human(host.first), historical) == "not_found"
    for mutation in mutations(host, host.colleague):
        with host.database.read() as uow:
            saved = dict(uow.connection.execute("SELECT status, profile_json FROM agent_profiles WHERE agent_id=?",
                                                (host.colleague,)).fetchone())
        with host.database.transaction() as uow:
            mutation(uow)
        for ctx in (host.instance.human(host.first), owner(host)):
            assert refused(host, ctx, host.colleague) == "invalid_assignee", mutation.__name__
        restore(host, host.colleague, saved)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.second))
    assert refused(host, host.instance.human(host.second), host.colleague) == "invalid_assignee"
    assert resolve(host, host.instance.human(host.first), host.colleague)["label"] == "Learning Applier"
