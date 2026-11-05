"""Acceptance case: broadcasts, workspace presentation and owner-task followers with global agents and no agent membership.

One synthetic host with two businesses and the four supported globals; none has a membership row, and each reaches every
active business through the shared ``global_scope`` contract.

* broadcasts keep their own business or Global scope and timing. Recipients are the reachable globals, listed once whatever
  ``all_businesses`` says, and each selected identity is one delivery per occurrence attributed to the broadcast's scope.
  Delivery rechecks the sender, the recipient's reach and the contact rules, and a denial creates no message and is never
  replayed. Owner Input targets the recipient's Global native session through the terminal port, once;
* workspace presentation: a worker presents Outreach in either business. Other roles, retired or business-home
  identities and archived businesses cannot, at prepare or at apply, and the owner still reads stored history;
* owner-task following: the one current Ordubashy, never an invalid identity; the human owner's membership gate is unchanged;
* Codex startup defaults are asserted in ``test_codex_environment.py``.

Synthetic only: a tmp instance and rows the test appends; a fake terminal port; no model, provider, network, native process
or live data."""

import json
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from bazarlyk.inbox.owner_tasks import ordubashies, tasks
from host_foundation_support import (build_instance, create_registered_global, create_worker, historical_business_agent,
                                     owner_global_context, register_sources, set_agent_overrides)

ROLES = ("ordubashy", "learning-applier")
ARCHIVED = "2026-10-15T00:00:00+00:00"


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    globals_ = {role: create_registered_global(instance, role) for role in ROLES}
    globals_["global-worker"] = create_worker(instance, "colleague")
    instance.runtime.freeze_registry()
    host = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=first,
                           second=second, coordinator=globals_["ordubashy"],
                           colleague=globals_["global-worker"], applier=globals_["learning-applier"])
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
                                      "WHERE p.kind='agent'").fetchone()[0] == 0
    return host


class Probe(Exception):
    """Raised to roll back a probing transaction."""


class FakeTerminals:
    """The dispatcher's terminal port: it records each write and answers per recipient; it cannot open or start anything."""

    def __init__(self, outcomes=None):
        self.calls, self.outcomes = [], dict(outcomes or {})

    def input_existing(self, business_id, agent_id, data, key=None):
        self.calls.append((business_id, agent_id, data, key))
        outcome = self.outcomes.get(agent_id, {"state": "delivered"})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def owner(host, business=None):
    """The installation owner: in Global with every capability and both member businesses, or in one business."""
    if business is not None:
        return host.instance.human(business)
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


def execute(host, sql, *params):
    with host.database.transaction() as uow:
        uow.connection.execute(sql, params)


def recipients(host, ctx, **arguments):
    return ok(call(host, ctx, "broadcast_recipients", arguments))["items"]


def create(host, ctx, people, *, delivery="inbox", scope=None, message="Weekly note"):
    arguments = {"definition": {"name": "Note", "delivery": delivery, "message": message, "recipients": list(people),
                                "timing": {"kind": "once", "at": "2030-01-01T09:00:00Z"}}, "send_now": True}
    if scope:
        arguments["scope"] = scope
    return call(host, ctx, "broadcasts_create", arguments)


def created(host, ctx, people, **options):
    return ok(create(host, ctx, people, **options))["records"][0]


def dispatch(host, terminals=None):
    dispatcher = BroadcastDispatcher(host.runtime, host.runtime.broadcasts, terminals)
    for _ in range(20):
        if dispatcher.tick() is None:
            return
    raise AssertionError("the dispatcher kept finding due work")


def deliveries(host, broadcast):
    with host.database.read() as uow:
        return {row["recipient_id"]: dict(row) for row in uow.connection.execute(
            "SELECT * FROM broadcast_deliveries WHERE broadcast_id=? ORDER BY occurrence", (broadcast,))}


def messages(host):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(
            "SELECT m.id,m.business_id,m.sender_id,m.recipient_id,m.body,d.agent_id AS inbox_agent FROM agent_messages m "
            "LEFT JOIN inbox_deliveries d ON d.event_id=m.event_id ORDER BY m.created_at,m.id")]


def stored_recipients(host, broadcast):
    with host.database.read() as uow:
        return json.loads(uow.connection.execute("SELECT recipients_json FROM broadcasts WHERE id=?", (broadcast,)).fetchone()[0])


def resident(identity, business):
    """A hand-made context for a business-home identity, which the resolver no longer builds."""
    return InvocationContext(Principal(identity, "agent"), business_id=business, home_business_id=business,
                             allowed_business_ids=frozenset({business}), source="mcp")


# -- broadcasts ------------------------------------------------------------------------------------------------------------

def test_recipients_are_the_reachable_globals_listed_once_in_the_selected_scope(host):
    historical = historical_business_agent(host.instance, host.first)
    everyone = {host.coordinator, host.colleague, host.applier}
    for ctx, business, slug in ((owner(host, host.first), host.first, "first"), (owner(host, host.second), host.second, "second"),
                                (owner(host), None, None)):
        scope = {} if business else {"scope": "global"}
        for fan_out in (False, True):
            rows = recipients(host, ctx, all_businesses=fan_out, **scope)
            # all_businesses adds no per-business copy: the same unique globals, in the selected message scope.
            assert Counter(row["agent_id"] for row in rows) == Counter(everyone)
            assert {(row["business_id"], row["business_slug"], row["group"]) for row in rows} == {(business, slug, "Global")}
    # An agent keeps the contact rules: Learning Applier is owner-directed; itself stays for self-reminders.
    coordinator = agent(host, host.coordinator, host.first)
    assert Counter(row["agent_id"] for row in recipients(host, coordinator, all_businesses=True)) == Counter(
        {host.coordinator, host.colleague})
    assert [row["agent_id"] for row in recipients(host, coordinator, role_id="global-worker")] == [host.colleague]
    assert code(create(host, coordinator, [host.applier])) == "not_found"
    # History and retired identities are never addressable.
    execute(host, "UPDATE agent_profiles SET status='retired' WHERE agent_id=?", host.applier)
    rows = recipients(host, owner(host, host.first), all_businesses=True)
    assert {row["agent_id"] for row in rows} == everyone - {host.applier} and historical not in {row["agent_id"] for row in rows}
    assert code(create(host, owner(host, host.first), [historical])) == "not_found"


def test_each_selected_global_gets_one_delivery_per_occurrence_in_the_broadcasts_own_scope(host):
    first = created(host, owner(host, host.first), [host.coordinator, host.colleague, host.applier])
    second = created(host, agent(host, host.coordinator, host.second), [host.colleague, host.coordinator])
    global_ = created(host, owner(host), [host.applier], scope="global")
    for row, business in ((first, host.first), (second, host.second), (global_, None)):
        assert {item["business_id"] for item in stored_recipients(host, row["id"])} == {business}
    dispatch(host)
    dispatch(host)        # nothing is due any more; no occurrence is delivered twice
    expected = {first["id"]: (host.first, host.instance.owner, {host.coordinator, host.colleague, host.applier}),
                second["id"]: (host.second, host.coordinator, {host.colleague, host.coordinator}),
                global_["id"]: (None, host.instance.owner, {host.applier})}
    sent = messages(host)
    for broadcast, (business, sender, people) in expected.items():
        rows = deliveries(host, broadcast)
        assert set(rows) == people and {(r["occurrence"], r["business_id"], r["state"]) for r in rows.values()} == {(1, business, "delivered")}
        mine = [m for m in sent if m["id"] in {r["message_id"] for r in rows.values()}]
        assert Counter((m["business_id"], m["sender_id"], m["recipient_id"], m["inbox_agent"]) for m in mine) == Counter(
            (business, sender, person, person) for person in people)
    assert len(sent) == 6
    # The recipient reads each message in its scope, and only there.
    def received(ctx):
        return Counter(item["sender_id"] for item in ok(call(host, ctx, "agents_messages_list", {}))["items"])
    assert received(agent(host, host.colleague, host.first)) == Counter({host.instance.owner: 1})
    assert received(agent(host, host.colleague, host.second)) == Counter({host.coordinator: 1})
    assert ok(call(host, agent(host, host.applier), "agents_messages_list", {"scope": "global"}))["items"][0]["business_id"] is None


def test_delivery_rechecks_sender_reach_and_contact_and_never_replays_a_denial(host):
    first_owner = owner(host, host.first)
    coordinator_second = agent(host, host.coordinator, host.second)
    cases = [
        ("retired recipient", lambda: created(host, first_owner, [host.applier]),
         "UPDATE agent_profiles SET status='retired' WHERE agent_id=?", "UPDATE agent_profiles SET status='active' WHERE agent_id=?",
         host.applier),
        ("inactive recipient", lambda: created(host, first_owner, [host.applier]),
         "UPDATE principals SET active=0 WHERE id=?", "UPDATE principals SET active=1 WHERE id=?", host.applier),
        ("business-home recipient", lambda: created(host, first_owner, [host.colleague]),
         f"UPDATE agent_profiles SET home_business_id='{host.first}',namespace='{host.first}' WHERE agent_id=?",
         "UPDATE agent_profiles SET home_business_id=NULL,namespace='ecosystem' WHERE agent_id=?", host.colleague),
        ("archived business", lambda: created(host, first_owner, [host.colleague]),
         f"UPDATE businesses SET archived_at='{ARCHIVED}' WHERE id=?", "UPDATE businesses SET archived_at=NULL WHERE id=?", host.first),
        ("sender no longer a current global", lambda: created(host, coordinator_second, [host.colleague]),
         "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.status','inactive') WHERE agent_id=?",
         "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.status','active') WHERE agent_id=?", host.coordinator),
        ("Global contact lost", lambda: created(host, agent(host, host.coordinator), [host.colleague], scope="global"),
         "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',json('[]')) WHERE agent_id=?",
         "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',json('[\"read_global_context\"]')) "
         "WHERE agent_id=?", host.colleague),
    ]
    for name, make, change, restore, subject in cases:
        row = make()
        saved, before = stored_recipients(host, row["id"]), len(messages(host))
        execute(host, change, subject)
        dispatch(host)
        (only,) = deliveries(host, row["id"]).values()
        assert only["state"] == "denied" and only["message_id"] is None, name
        assert len(messages(host)) == before and stored_recipients(host, row["id"]) == saved, name
        # Restoring the identity or business replays nothing: the denial is final for that occurrence.
        execute(host, restore, subject)
        dispatch(host)
        assert list(deliveries(host, row["id"]).values()) == [only] and len(messages(host)) == before, name
    # Lost permission: the sender's current policy no longer allows the broadcast.
    row = created(host, coordinator_second, [host.colleague])
    set_agent_overrides(host.instance, host.coordinator, {"broadcasts_create": "deny"}, [host.first, host.second])
    dispatch(host)
    assert [r["state"] for r in deliveries(host, row["id"]).values()] == ["denied"]
    assert code(create(host, coordinator_second, [host.colleague])) == "policy_denied"


def test_owner_input_reaches_the_global_native_session_once_and_an_uncertain_write_is_not_replayed(host):
    terminals = FakeTerminals({host.coordinator: RuntimeError("lost"),
                               host.applier: {"state": "unavailable", "detail": "No live terminal mirror is attached"}})
    business = created(host, owner(host, host.first), [host.colleague, host.coordinator], delivery="input")
    global_ = created(host, owner(host), [host.applier], delivery="input", scope="global")
    before = len(messages(host))
    dispatch(host, terminals)
    dispatch(host, terminals)
    # Each recipient's own Global session (None), once, keyed by the delivery; the business stays the delivery's scope.
    assert sorted(terminals.calls) == sorted(
        [(None, person, "Weekly note", f"broadcast:{business['id']}:1:{person}") for person in (host.colleague, host.coordinator)]
        + [(None, host.applier, "Weekly note", f"broadcast:{global_['id']}:1:{host.applier}")])
    rows = deliveries(host, business["id"])
    assert {person: (row["state"], row["business_id"]) for person, row in rows.items()} == {
        host.colleague: ("delivered", host.first), host.coordinator: ("uncertain", host.first)}
    assert [(r["state"], r["business_id"]) for r in deliveries(host, global_["id"]).values()] == [("unavailable", None)]
    assert len(messages(host)) == before
    # A recipient that is no longer a current global is denied before any terminal write.
    later = created(host, owner(host, host.first), [host.colleague], delivery="input")
    execute(host, "UPDATE agent_profiles SET status='retired' WHERE agent_id=?", host.colleague)
    dispatch(host, terminals)
    assert len(terminals.calls) == 3 and [r["state"] for r in deliveries(host, later["id"]).values()] == ["denied"]
    assert code(create(host, agent(host, host.coordinator, host.first), [host.coordinator], delivery="input")) == "forbidden"


# -- workspace presentation ------------------------------------------------------------------------------------------------

def present(host, ctx, business, *, revision=None, **fields):
    return call(host, ctx, "workspace_present", {"workspace": "campaign_review", "business_id": business, **fields},
                revision=revision)


def test_a_worker_presents_outreach_in_either_business_and_nothing_else_can(host):
    historical = historical_business_agent(host.instance, host.first)
    colleague = agent(host, host.colleague, host.first)
    assert ok(present(host, colleague, host.first, status="Drafting"))["workspace"]["revision"] == 1
    assert ok(present(host, agent(host, host.colleague, host.second), host.second))["workspace"]["business_id"] == host.second
    second_view = ok(call(host, agent(host, host.colleague, host.second), "workspace_get", {"workspace": "campaign_review"}))
    assert (second_view["business_id"], second_view["role_id"], second_view["workspace"]["agent_id"]) == (
        host.second, "global-worker", host.colleague)
    types = {item["key"]: item["supported_roles"] for item in ok(call(host, colleague, "workspace_types", {}))["types"]}
    assert types == {"campaign_review": ["global-worker", "ordubashy"], "focus": ["global-worker", "ordubashy"]}
    assert code(present(host, colleague, host.first, revision=7)) == "revision_conflict"
    # Ordubashy is eligible to present Outreach too (actual Outreach access still needs its connection qualification and credential).
    assert ok(present(host, agent(host, host.coordinator, host.first), host.first))["workspace"]["agent_id"] == host.coordinator
    assert code(present(host, agent(host, host.applier, host.first), host.first)) == "policy_denied"

    # Apply re-derives the identity and the business: a retirement or an archive after prepare refuses the write.
    service = host.runtime.application.registry.get("workspace_present").apply.__self__
    arguments = {"workspace": "campaign_review", "business_id": host.first, "status": "Late"}
    for change, subject, expected in (("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", host.colleague, "forbidden"),
                                      ("UPDATE businesses SET archived_at=? WHERE id=?", (ARCHIVED, host.first), "not_found")):
        with pytest.raises(Probe):
            with host.database.transaction() as uow:
                plan = service.prepare("workspace_present", colleague, arguments,
                                       InvocationOptions(idempotency_key=new_id(), expected_revision=1), uow)
                uow.connection.execute(change, subject if isinstance(subject, tuple) else (subject,))
                with pytest.raises(DomainError) as refused:
                    service.apply(colleague, plan, uow)
                assert refused.value.code == expected
                raise Probe
    with host.database.read() as uow:
        assert pytest.raises(DomainError, service.access, resident(historical, host.first), uow,
                             workspace_key="campaign_review", business_id=host.first).value.code == "forbidden"
    execute(host, "UPDATE businesses SET archived_at=? WHERE id=?", ARCHIVED, host.second)
    assert code(present(host, colleague, host.second)) == "not_found"

    # The owner still reads stored presentation history, including a business-home identity's, unchanged.
    execute(host, "INSERT INTO workspace_presentations(business_id,id,agent_id,workspace_key,presentation_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)", host.first, new_id(), historical, "campaign_review",
            json.dumps({"workflow_profile": "mcp_upload_v1", "stage": "offer"}), ARCHIVED, ARCHIVED)
    read = lambda identity: ok(call(host, owner(host, host.first), "workspace_get",
                                    {"workspace": "campaign_review", "agent_id": identity, "business_id": host.first}))["workspace"]
    assert (read(historical)["stage"], read(historical)["revision"]) == ("offer", 1)
    assert (read(host.colleague)["status"], read(host.colleague)["revision"]) == ("Drafting", 1)


# -- owner-task followers --------------------------------------------------------------------------------------------------

def followers(host):
    with host.database.read() as uow:
        return ordubashies(uow)


def test_owner_task_followers_are_only_the_one_current_ordubashy_and_the_owner_gate_is_unchanged(host):
    historical_business_agent(host.instance, host.first, role="ordubashy")
    assert followers(host) == {host.coordinator}
    for change, restore in (
            ("UPDATE principals SET active=0 WHERE id=?", "UPDATE principals SET active=1 WHERE id=?"),
            ("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", "UPDATE agent_profiles SET status='active' WHERE agent_id=?"),
            (f"UPDATE agent_profiles SET home_business_id='{host.second}',namespace='{host.second}' WHERE agent_id=?",
             "UPDATE agent_profiles SET home_business_id=NULL,namespace='ecosystem' WHERE agent_id=?"),
            ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.kind','business') WHERE agent_id=?",
             "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.kind','ecosystem') WHERE agent_id=?")):
        execute(host, change, host.coordinator)
        assert followers(host) == set(), change
        execute(host, restore, host.coordinator)
    with pytest.raises(Probe):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json('\"broken\"') WHERE agent_id=?", (host.coordinator,))
            assert ordubashies(uow) == set()
            raise Probe
    # A second current holder is the established configuration error, never an arbitrary choice.
    with pytest.raises(Probe):
        with host.database.transaction() as uow:
            twin = new_id()
            uow.repository("principals").insert({"id": twin, "kind": "agent", "display_name": "Twin"})
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id,principal_id,home_business_id,namespace,name,workspace,status,profile_json,created_at,"
                "updated_at) SELECT ?,?,NULL,namespace,'twin',workspace||'-twin','active',json_set(profile_json,'$.agent_id',?),created_at,"
                "updated_at FROM agent_profiles WHERE agent_id=?", (twin, twin, twin, host.coordinator))
            assert pytest.raises(DomainError, ordubashies, uow).value.code == "invalid_configuration"
            raise Probe
    assert followers(host) == {host.coordinator}

    # The owner-task rows still require the HUMAN owner's active membership of the Task's business.
    task = ok(call(host, owner(host, host.first), "tasks_create", {"title": "Sign", "assignee_ids": [host.instance.owner]}))["task"]
    def owned():
        with host.database.read() as uow:
            return [row["id"] for row in tasks(uow, business=host.first)]
    assert owned() == [task["id"]]
    execute(host, "UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?", host.instance.owner, host.first)
    assert owned() == []
