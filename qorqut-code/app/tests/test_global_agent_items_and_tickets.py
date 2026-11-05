"""Acceptance case: global agents as context items and the System Dev agent tools, with no agent membership anywhere.

One synthetic host with two businesses. Ordubashy, a worker and Learning Applier are active supported globals; none
has a membership row, and each reaches every active business through the shared ``global_scope`` contract.

* agent items: a current global resolves in Global and in every active selected business and is listed once. Retired,
  malformed and business-home profiles read only as archived history, a business-home one only in its own business, and
  normal lists omit them. Old relationship endpoints stay readable and removable. A business plan pins a global's principal and
  its read-only ecosystem profile, and the engine detects a later revision. ``agents_get`` and workflow roles still apply;
* System Dev: Ordubashy lists, reads, follows up and resolves authorized business and Global reports without a membership.
  Another global keeps its own reports plus the explicit system-incident read, and cannot resolve anyone else's. A resolution
  notifies only a current reporter. The owner queue and the system reporter keep their behavior.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network, native process or live data."""

import json
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qorqut_operations.application.plans import PlanBuilder, ref
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.domains.dev_tickets import DevTickets
from bazarlyk.incidents import ensure_system_reporter
from host_foundation_support import (build_instance, create_registered_global, create_worker, historical_business_agent,
                                     owner_global_context, register_sources, set_agent_overrides)

ARCHIVED = "2026-10-14T00:00:00+00:00"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = create_registered_global(instance, "ordubashy")
    colleague = create_worker(instance, "colleague")
    applier = create_registered_global(instance, "learning-applier")
    instance.runtime.freeze_registry()
    host = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first,
                           second=second, coordinator=coordinator, colleague=colleague, applier=applier)
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
                                      "WHERE p.kind='agent'").fetchone()[0] == 0
    return host


def owner(host):
    """The installation owner in Global with its member businesses (the dashboard's context)."""
    return replace(owner_global_context(host.instance), allowed_business_ids=frozenset({host.first, host.second}))


def agent(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def call(host, ctx, operation, arguments, *, revision=None):
    return host.runtime.application.invoke(ctx, operation, arguments,
                                           InvocationOptions(idempotency_key=new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def item(identity):
    return {"type": "agent", "id": identity}


def listed(host, ctx, **arguments):
    return Counter(row["id"] for row in ok(call(host, ctx, "context_items_list", {"type": "agent", **arguments}))["items"])


def archived_flags(host, ctx, **arguments):
    rows = ok(call(host, ctx, "context_items_list", {"type": "agent", "include_archived": True, **arguments}))["items"]
    return {row["id"]: row["archived"] for row in rows}


def resident(host, identity, business):
    """A hand-made context for a business-home identity, which the resolver no longer builds."""
    return InvocationContext(Principal(identity, "agent"), business_id=business, home_business_id=business,
                             allowed_business_ids=frozenset({business}), source="mcp")


# -- agent items -----------------------------------------------------------------------------------------------------------

def test_a_current_global_is_an_agent_item_everywhere_and_history_stays_archived_in_its_own_scope(host):
    historical = historical_business_agent(host.instance, host.first)
    current = {host.coordinator, host.colleague, host.applier}
    views = {"first": host.instance.human(host.first), "second": host.instance.human(host.second), "global": owner(host)}
    for ctx in (*views.values(), agent(host, host.coordinator, host.first)):
        view = ok(call(host, ctx, "context_items_get", {"item": item(host.colleague)}))["item"]
        assert (view["label"], view["archived"], view["resource_type"]) == ("Colleague", False, "agent_profiles")
        assert not {"profile_json", "workspace", "assignment_brief"} & set(view) and "Synthetic fixture" not in json.dumps(view)
    # Normal lists show each current global once; a still-active business-home profile is history of its own business.
    for ctx in views.values():
        assert listed(host, ctx) == Counter(current)
    assert archived_flags(host, views["first"]) == {**dict.fromkeys(current, False), historical: True}
    assert archived_flags(host, views["second"]) == dict.fromkeys(current, False)
    assert archived_flags(host, views["global"]) == dict.fromkeys(current, False)
    for ctx in (views["second"], views["global"]):
        assert code(call(host, ctx, "context_items_get", {"item": item(historical)})) == "not_found"

    # A retired global is archived history wherever it was readable; normal lists omit it.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (host.applier,))
    for name, ctx in views.items():
        assert listed(host, ctx) == Counter({host.coordinator, host.colleague}), name
        assert archived_flags(host, ctx)[host.applier] is True, name
        assert ok(call(host, ctx, "context_items_get", {"item": item(host.applier)}))["item"]["archived"] is True

    # A malformed stored profile fails closed as archived history, without an exception.
    for broken in ("json_set(profile_json,'$.role','learning-applier')", "json('\"broken\"')"):
        with host.database.transaction() as uow:
            uow.connection.execute(f"UPDATE agent_profiles SET status='active', profile_json={broken} WHERE agent_id=?",
                                   (host.applier,))
        view = ok(call(host, views["first"], "context_items_get", {"item": item(host.applier)}))["item"]
        assert view["archived"] is True and "departments" not in view and view["label"]
        assert listed(host, views["first"]) == Counter({host.coordinator, host.colleague})


def test_policy_scope_and_workflow_roles_still_refuse_agent_items(host):
    historical = historical_business_agent(host.instance, host.first)
    reader = agent(host, host.coordinator, host.first)
    assert ok(call(host, reader, "context_items_get", {"item": item(host.colleague)}))["item"]["archived"] is False
    set_agent_overrides(host.instance, host.coordinator, {"agents_get": "deny"}, [host.first, host.second])
    assert code(call(host, agent(host, host.coordinator, host.first), "context_items_get",
                     {"item": item(host.colleague)})) == "forbidden"

    work = ok(call(host, host.instance.human(host.first), "tasks_create", {"title": "Launch"}))["task"]
    assert code(call(host, host.instance.human(host.first), "relationships_create", {
        "source": {"type": "task", "id": work["id"]}, "target": item(host.colleague), "role": "assigned_to"})) == "workflow_required"

    # An archived business is outside every agent item, current or historical.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.second))
    assert call(host, host.instance.human(host.second), "context_items_get", {"item": item(host.colleague)}).status == "error"
    with host.database.read() as uow:
        items = host.runtime.domains.items
        for ctx, identity in ((host.instance.human(host.second), host.colleague),
                              (host.instance.human(host.second), historical), (owner(host), historical)):
            with pytest.raises(DomainError) as missing:
                items.resource(ctx, item(identity), uow)
            assert missing.value.code == "not_found"


def test_old_relationship_endpoints_stay_readable_and_removable_and_a_business_plan_pins_the_global_profile(host):
    historical = historical_business_agent(host.instance, host.first)
    owner_first = host.instance.human(host.first)
    work = ok(call(host, owner_first, "tasks_create", {"title": "Launch"}))["task"]
    source = {"type": "task", "id": work["id"]}
    edges = {identity: ok(call(host, owner_first, "relationships_create", {
        "source": source, "target": item(identity), "role": "related_to"}))["relationship"]
        for identity in (host.colleague, host.applier, historical)}
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (host.applier,))

    def targets(**arguments):
        rows = ok(call(host, owner_first, "relationships_list", {"source": source, **arguments}))["items"]
        return {row["target_item"]["id"]: row["target_item"]["archived"] for row in rows}

    assert targets() == {host.colleague: False}
    assert targets(include_archived=True) == {host.colleague: False, host.applier: True, historical: True}
    for identity in (host.applier, historical):
        current = ok(call(host, owner_first, "relationships_get", {"relationship_id": edges[identity]["id"]}))["relationship"]
        assert current["target_item"]["archived"] is True
        assert ok(call(host, owner_first, "relationships_remove", {"relationship_id": current["id"]},
                       revision=current["revision"]))["removed_id"] == current["id"]
    assert targets(include_archived=True) == {host.colleague: False}

    items, application = host.runtime.domains.items, host.runtime.application
    with host.database.read() as uow:
        builder = PlanBuilder(owner_first, "relationships_create", {}, "Pin an agent item")
        items.resolve(owner_first, item(host.colleague), uow, builder)
        items.resolve(owner_first, item(historical), uow, builder)
        tables = host.database.assembly.tables
        pins = {target.resource: target.revision for target in builder.targets.values()}
        profile = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (host.colleague,)).fetchone()[0]
        assert pins == {ref("principals", host.colleague, None, tables=tables): pins[ref("principals", host.colleague, None, tables=tables)],
                        ref("agent_profiles", host.colleague, None, "ecosystem", tables=tables): profile,
                        ref("agent_profiles", historical, host.first, tables=tables): 1}
        plan = builder.build()
        application._scope_plan(owner_first, plan)
        application.check_targets(owner_first, plan, uow)
        assert items.relationship_item(owner_first, item(host.colleague), uow) == item(host.colleague)
        fingerprint = items.fingerprint(owner_first, item(host.colleague), uow)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET revision=revision+1, "
                               "profile_json=json_set(profile_json,'$.display_name','Renamed') WHERE agent_id=?", (host.colleague,))
    with host.database.read() as uow:
        with pytest.raises(DomainError) as stale:
            application.check_targets(owner_first, plan, uow)
        assert stale.value.code == "revision_conflict"
        assert items.fingerprint(owner_first, item(host.colleague), uow) != fingerprint


# -- System Dev ------------------------------------------------------------------------------------------------------------

def report(host, identity, business, title):
    return ok(call(host, agent(host, identity, business), "dev_tickets_create", {"title": title}))["ticket_id"]


def system_ticket(host, reporter, operation, business, agent_id=None):
    incidents = host.runtime.application.incident_reporter
    observation = incidents.observation(component="tasks", operation=operation, error_code="storage_unavailable",
                                        classification="infrastructure_fault", call_site="tasks.get",
                                        business_id=business, agent_id=agent_id)
    with host.database.transaction() as uow:
        return incidents.observe(observation, uow=uow, reporter_id=reporter)["ticket_id"]


def tickets(host, ctx, **arguments):
    return {row["id"] for row in ok(call(host, ctx, "dev_tickets_agent_list", {"status": "all", **arguments}))["items"]}


def notices(host, recipient):
    with host.database.read() as uow:
        return [row["body"] for row in uow.connection.execute(
            "SELECT m.body FROM agent_messages m JOIN inbox_events e ON e.id=m.event_id "
            "WHERE e.event_type='dev_ticket_resolved' AND m.recipient_id=?", (recipient,))]


def resolve_ticket(host, ctx, ticket, note="Fixed"):
    return call(host, ctx, "dev_tickets_resolve", {"ticket_id": ticket, "resolution": note, "verification": "Checked"})


def test_ordubashy_manages_reachable_reports_and_other_globals_keep_their_own_without_memberships(host):
    with host.database.transaction() as uow:
        reporter = ensure_system_reporter(uow, host.runtime.authority, business_ids=[host.first, host.second])
    colleague_first = report(host, host.colleague, host.first, "Colleague in first")
    colleague_global = report(host, host.colleague, None, "Colleague in Global")
    applier_second = report(host, host.applier, host.second, "Applier in second")
    incident_first = system_ticket(host, reporter, "first_fault", host.first, host.coordinator)
    incident_second = system_ticket(host, reporter, "second_fault", host.second)
    everything = {colleague_first, colleague_global, applier_second, incident_first, incident_second}

    coordinator_first, coordinator_global = agent(host, host.coordinator, host.first), agent(host, host.coordinator)
    assert tickets(host, coordinator_first) == {colleague_first, incident_first}
    assert tickets(host, coordinator_global, scope="global") == everything
    assert ok(call(host, coordinator_first, "dev_tickets_get", {"ticket_id": colleague_first}))["ticket"]["id"] == colleague_first
    ok(call(host, coordinator_first, "dev_tickets_follow_up", {"ticket_id": colleague_first, "details": "Reproduced"}))
    # The invocation's businesses still bound Ordubashy.
    narrowed = replace(coordinator_global, allowed_business_ids=frozenset({host.second}))
    with host.database.read() as uow:
        rows = DevTickets().agent_list(narrowed, {"status": "all", "scope": "global"}, None, uow)["items"]
        assert {row["id"] for row in rows} == {colleague_global, applier_second, incident_second}

    # Another global: its own reports and the explicit system-incident read exception, never somebody else's report.
    colleague_view = agent(host, host.colleague, host.first)
    assert tickets(host, colleague_view) == {colleague_first, incident_first}
    assert tickets(host, agent(host, host.colleague, host.second)) == {incident_second}
    assert tickets(host, agent(host, host.colleague), scope="global") == {colleague_global}
    assert ok(call(host, colleague_view, "dev_tickets_get", {"ticket_id": incident_first}))["ticket"]["reporter_id"] == reporter
    assert code(call(host, colleague_view, "dev_tickets_follow_up", {"ticket_id": incident_first, "details": "Me too"})) == "forbidden"
    assert code(resolve_ticket(host, colleague_view, incident_first)) == "forbidden"
    colleague_second = agent(host, host.colleague, host.second)
    assert code(call(host, colleague_second, "dev_tickets_get", {"ticket_id": applier_second})) == "forbidden"
    assert code(resolve_ticket(host, colleague_second, applier_second)) == "forbidden"

    # Resolution notifies the current reporter only, never the resolving caller.
    ok(resolve_ticket(host, coordinator_first, colleague_first, "Fixed in first"))
    assert [body.splitlines()[1] for body in notices(host, host.colleague)] == ["Resolution: Fixed in first"]
    ok(resolve_ticket(host, agent(host, host.colleague), colleague_global, "Own fix"))
    assert len(notices(host, host.colleague)) == 1

    # The owner queue and the system reporter's memberships keep their behavior.
    queue = ok(call(host, owner(host), "dev_tickets_list", {"status": "all"}))["items"]
    assert {row["id"] for row in queue} == everything
    closed = next(row for row in queue if row["id"] == applier_second)
    ok(call(host, owner(host), "dev_tickets_status_set", {"ticket_id": applier_second, "status": "closed"},
            revision=closed["revision"]))
    with host.database.read() as uow:
        assert {row[0] for row in uow.connection.execute(
            "SELECT business_id FROM business_memberships WHERE principal_id=? AND active=1", (reporter,))} == {host.first, host.second}


class Probe(Exception):
    """Carries a probe's result out of a transaction that it rolls back."""


def test_resolution_notices_reach_only_current_reporters_and_unavailable_agents_have_no_tools(host):
    historical = historical_business_agent(host.instance, host.first)
    in_first, in_second = report(host, host.applier, host.first, "In first"), report(host, host.applier, host.second, "In second")
    in_global = report(host, host.colleague, None, "In Global")
    own = report(host, host.coordinator, host.first, "Own report")
    resident_ticket, now = new_id(), "2026-10-14T00:00:00+00:00"
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?)", (resident_ticket, "Old resident", "", "bug", "open", historical,
                                                              host.first, now, now))
    service, resolver = DevTickets(), agent(host, host.coordinator)

    def notified(ticket_id, change=None):
        """Messages a resolution would send to the ticket's reporter after ``change``, rolled back afterwards."""
        try:
            with host.database.transaction() as uow:
                if change:
                    uow.connection.execute(*change)
                ticket = dict(uow.repository("dev_tickets").get(ticket_id))
                service.notify_reporter(uow, ticket, resolver, {"resolution": "Fixed", "verification": "Checked"})
                raise Probe(uow.connection.execute("SELECT count(*) FROM agent_messages WHERE recipient_id=?",
                                                   (ticket["reporter_id"],)).fetchone()[0])
        except Probe as probe:
            return probe.args[0]

    assert notified(in_first) == 1 and notified(in_global) == 1  # the eligible reporters
    assert notified(in_first, ("UPDATE principals SET active=0 WHERE id=?", (host.applier,))) == 0
    assert notified(in_first, ("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (host.applier,))) == 0
    assert notified(in_second, ("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.second))) == 0
    assert notified(in_global, ("UPDATE agent_profiles SET profile_json=json_set(profile_json,"
                                "'$.scope.global_capabilities',json('[]')) WHERE agent_id=?", (host.colleague,))) == 0
    assert notified(resident_ticket) == 0 and notified(own) == 0

    # Unavailable identities and unselected businesses have no System Dev agent tools.
    with host.database.read() as uow:
        ticket = dict(uow.repository("dev_tickets").get(in_first))
        for ctx in (resident(host, historical, host.first),
                    replace(agent(host, host.coordinator, host.first), allowed_business_ids=frozenset({host.second}))):
            with pytest.raises(DomainError) as refused:
                service.agent_access(ctx, ticket, uow, read_only=True)
            assert refused.value.code == "forbidden"
        with pytest.raises(DomainError) as unavailable:
            service.agent_list(resident(host, historical, host.first), {"status": "all"}, None, uow)
        assert unavailable.value.code == "forbidden"
