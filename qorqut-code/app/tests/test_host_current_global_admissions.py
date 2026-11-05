"""Acceptance case: one current-global predicate for the Operations engine and the host, and every current-agent admission.

One synthetic host with two businesses and four current globals (the selected Ordubashy, a worker granted business setup, a second
ordinary worker and Learning Applier), none with a membership row. The shared ``qorqut_operations.global_agents`` validator decides who
is current for the engine and for ``global_scope``; the host admissions below delegate to it and keep their own role,
runtime, capability and selected-scope restrictions:

* the engine and host predicates agree for every cohort, and every valid global reaches a business added later at once;
* principal-inactive, retired-row, inactive-profile, retired or unknown role, business-scoped, business-home and malformed
  registrations are refused by Viewbox placement, the attention queue, Focus, project-creation eligibility, terminal control,
  Telegram owner input, Signoff replies and chat readiness, unread Chat replies and the management coordinator, without
  crashing;
* management business scope admits a global without a membership row, while humans keep their membership rule;
* owner history readers (the Viewbox board, retained native replies and profiles) keep their records.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network, native process or live data."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from qorqut_operations import global_agents as engine
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, utc_now

from bazarlyk import attention_queue
from bazarlyk.agents.global_scope import (active_business_participant, active_global, effective_business_ids,
                                          global_profile_data, reaches_business)
from bazarlyk.agents.roles import SUPPORTED_GLOBAL_ROLES
from bazarlyk.domains.business_creation.access import COORDINATION_CAPABILITY, creator_actor
from bazarlyk.domains.viewbox import AgentViewbox
from bazarlyk.management.identity import configured_ordubashy, ordubashy_profile, require_coordinator
from bazarlyk.native_chat import store as native
from bazarlyk.native_chat.projection import Lineage
from bazarlyk.reply_notifications import replies
from bazarlyk.telegram import store as telegram_store
from bazarlyk.telegram.bridge import TelegramBridge
from bazarlyk.telegram.owner_input import Final, OwnerInputs
from bazarlyk.telegram.signoffs import SignoffBridge
from host_foundation_support import (build_instance, create_registered_global, create_worker,
                                     historical_business_agent, owner_global_context, register_sources, select_coordinator)


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    register_sources(instance, ids["first"], "first")
    register_sources(instance, ids["second"], "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    helper = create_worker(instance, "helper-worker")
    worker = create_worker(instance, "coordination-worker", capabilities=(COORDINATION_CAPABILITY, "read_global_context"))
    applier = create_registered_global(instance, "learning-applier")
    with instance.database.transaction() as uow:
        # The configured Ordubashy may create projects: the capability creation eligibility checks beside the role.
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',"
                               "json('[\"coordinate_businesses\",\"read_global_context\"]')) WHERE agent_id=?", (coordinator,))
    instance.runtime.freeze_registry()
    host = SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, first=ids["first"],
                           second=ids["second"], coordinator=coordinator, helper=helper, worker=worker, applier=applier)
    host.four = {"ordubashy": coordinator, "global-worker": worker, "helper-worker": helper, "learning-applier": applier}
    host.roles = {"ordubashy": "ordubashy", "global-worker": "global-worker", "helper-worker": "global-worker",
                  "learning-applier": "learning-applier"}
    with host.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM business_memberships m JOIN principals p ON p.id=m.principal_id "
                                      "WHERE p.kind='agent'").fetchone()[0] == 0
    return host


def owner(host):
    return replace(owner_global_context(host.instance), allowed_business_ids=frozenset({host.first, host.second}))


def bare(identity):
    return InvocationContext(Principal(identity, "agent"), source="mcp")


def agent(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def cohorts(host):
    """Each way a registration is not a current supported global, as a stored-row change; restored afterwards."""
    def profile(path, value):
        return lambda uow, identity: uow.connection.execute(
            f"UPDATE agent_profiles SET profile_json=json_set(profile_json,'{path}',json(?)) WHERE agent_id=?",
            (json.dumps(value), identity))

    def principal_inactive(uow, identity):
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (identity,))

    def row_retired(uow, identity):
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (identity,))

    def not_an_object(uow, identity):
        uow.connection.execute("UPDATE agent_profiles SET profile_json='[]' WHERE agent_id=?", (identity,))

    def business_home(uow, identity):
        home = host.second if identity == host.helper else host.first        # two workers never share one home (one active role per home)
        uow.connection.execute("UPDATE agent_profiles SET home_business_id=?, namespace=? WHERE agent_id=?", (home, home, identity))

    return {"principal_inactive": principal_inactive, "row_retired": row_retired,
            "profile_inactive": profile("$.status", "inactive"), "retired_role": profile("$.role.id", "onbashy"),
            "unknown_role": profile("$.role.id", "strategist-x"), "role_text": profile("$.role", "ordubashy"),
            "business_scope": profile("$.scope", {"kind": "business", "business_id": host.first}),
            "scope_business": profile("$.scope.business_id", host.first),
            "capabilities_text": profile("$.scope.global_capabilities", "read_global_context"),
            "scope_text": profile("$.scope", "ecosystem"), "not_an_object": not_an_object, "business_home": business_home}


def apply(host, identity, change):
    with host.database.transaction() as uow:
        saved = dict(uow.connection.execute(
            "SELECT a.status, a.profile_json, a.home_business_id, a.namespace, p.active FROM agent_profiles a "
            "JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=?", (identity,)).fetchone())
        change(uow, identity)
    return saved


def restore(host, identity, saved):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=? WHERE id=?", (saved["active"], identity))
        uow.connection.execute("UPDATE agent_profiles SET status=?, profile_json=?, home_business_id=?, namespace=? WHERE agent_id=?",
                               (saved["status"], saved["profile_json"], saved["home_business_id"], saved["namespace"], identity))


def row(uow, identity):
    return uow.connection.execute("SELECT a.*, p.kind, p.active FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
                                  "WHERE a.agent_id=?", (identity,)).fetchone()


def parity(uow, identities):
    """The host and engine answers for each identity; they must be the same answer."""
    for identity in identities:
        found = engine.global_profile(uow.connection, identity)
        assert active_global(uow.connection, identity) == (found and found["profile"])
        assert global_profile_data(row(uow, identity)) == engine.current_global_row(row(uow, identity))
        assert effective_business_ids(uow.connection, identity) == engine.effective_business_ids(uow.connection, identity)
    assert engine.global_agent_ids(uow.connection) == sorted(i for i in identities if active_global(uow.connection, i) is not None)


def test_the_host_and_engine_share_one_predicate_and_every_valid_global_reaches_a_new_business_at_once(host):
    assert SUPPORTED_GLOBAL_ROLES is engine.SUPPORTED_GLOBAL_ROLES
    assert set(host.roles.values()) <= SUPPORTED_GLOBAL_ROLES and "founder" not in SUPPORTED_GLOBAL_ROLES    # retired Founder is no current role
    historical = historical_business_agent(host.instance, host.first)
    identities = [*host.four.values(), historical]
    with host.database.read() as uow:
        parity(uow, identities)
        assert {name: active_global(uow.connection, identity)["role"]["id"] for name, identity in host.four.items()} == host.roles
        assert engine.global_agent_ids(uow.connection) == sorted(host.four.values())
    third = new_id()
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": third, "slug": "third", "name": "Third",
                                             "workspace": str(host.instance.paths.business_root("third")),
                                             "hq_path": str(host.instance.paths.business_hq("third"))})
    stranger = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Stranger"})
    with host.database.read() as uow:
        for identity in host.four.values():
            assert effective_business_ids(uow.connection, identity) == {host.first, host.second, third}
            assert reaches_business(uow.connection, identity, third) and active_business_participant(uow.connection, third, identity)
        assert not active_business_participant(uow.connection, third, historical)
        assert not active_business_participant(uow.connection, third, stranger)     # humans still need a membership
    for name, change in cohorts(host).items():
        saved = apply(host, host.worker, change)
        with host.database.read() as uow:
            assert active_global(uow.connection, host.worker) is None, name
            parity(uow, identities)
        restore(host, host.worker, saved)


def test_every_current_agent_admission_refuses_each_non_current_cohort_without_crashing(host):
    reply = native.insert_reply
    owner_ctx = owner(host)

    def placed(identity):
        with host.database.transaction() as uow:
            item, now = new_id(), utc_now()
            sequence = 1 + uow.connection.execute("SELECT count(*) FROM agent_viewbox_items WHERE agent_id=?", (identity,)).fetchone()[0]
            uow.connection.execute("INSERT INTO agent_viewbox_items(id,agent_id,canonical_link,link,title,created_at,placed_at,"
                                   "placement_sequence) VALUES (?,?,?,?,?,?,?,?)",
                                   (item, identity, f"/files/view?path={item}", f"/files/view?path={item}", "Look", now, now, sequence))
        return item

    def answered(identity):
        with host.database.transaction() as uow:
            conversation = new_id()
            return reply(uow.connection, lineage=Lineage(identity, "claude", conversation, f"claude:{conversation}"),
                         kind="native_id", key=new_id(), session_id=None, receipt_id="synthetic-receipt", offset=0, length=5,
                         completed_at=datetime.now(timezone.utc).isoformat(), preview="Reply")

    def attention(identity):
        return {item["agent_id"] for item in attention_queue.queue(host.runtime, owner_ctx)["items"] if item["kind"] == "viewbox"}

    def unread(identity):
        return {item["agent_id"] for item in replies(host.runtime, owner_ctx)["items"] if item.get("chat")}

    binding = {"owner": host.instance.owner, "generation": 1, "fingerprint": "binding"}
    inputs = SimpleNamespace(bridge=SimpleNamespace(check_binding=lambda uow: binding))

    with host.database.read() as uow:
        # The target registration an owner input was accepted for, frozen while the worker was current.
        accepted = telegram_store.target_fingerprint(dict(uow.connection.execute(
            "SELECT * FROM agent_profiles WHERE agent_id=?", (host.worker,)).fetchone()))

    def owner_input(uow, identity):
        return OwnerInputs._authorize(inputs, uow, {
            "owner_principal_id": binding["owner"], "config_generation": 1, "binding_fingerprint": "binding",
            "agent_id": identity, "purpose": "signoff_reply", "target_fingerprint": accepted})

    focus_ctx = terminal_ctx = agent(host, host.coordinator)          # terminal control is Ordubashy's (and the owner's)
    for identity in (host.worker, host.coordinator, host.helper):
        placed(identity), answered(identity)

    def admitted():
        """Every admission each identity group passes while it is current."""
        with host.database.read() as uow:
            AgentViewbox._agent(bare(host.worker), uow)
            assert host.runtime.focus.context(focus_ctx, uow).principal_id == host.coordinator
            assert creator_actor(uow, host.coordinator) == "ordubashy" and creator_actor(uow, host.worker) is None
            host.runtime.terminal_control.actor(terminal_ctx, uow)
            assert owner_input(uow, host.worker)["agent_id"] == host.worker
            assert SignoffBridge._can_receive(uow, {"proposer_agent_id": host.worker})
            TelegramBridge.check_agent_chat(uow, SimpleNamespace(name="ordubashy", agent_id=host.coordinator))
            assert ordubashy_profile(uow, host.coordinator, require_codex=True)
            assert configured_ordubashy(uow, require_codex=True) == require_coordinator(uow) == host.coordinator
        assert {host.worker, host.coordinator, host.helper} <= attention(None)
        assert {host.worker, host.coordinator, host.helper} <= unread(None)

    def refused(call, *codes):
        with pytest.raises(DomainError) as error:
            call()
        assert not codes or error.value.code in codes, (name, error.value.code)

    admitted()
    for name, change in cohorts(host).items():
        saved = {identity: apply(host, identity, change) for identity in (host.worker, host.coordinator, host.helper)}
        with host.database.read() as uow:
            refused(lambda: AgentViewbox._agent(bare(host.worker), uow), "inactive_agent")
            refused(lambda: host.runtime.focus.context(focus_ctx, uow))
            # Creation eligibility reads the Global-scoped profile, so no refused registration is ever an actor.
            assert creator_actor(uow, host.coordinator) is None and creator_actor(uow, host.worker) is None, name
            refused(lambda: host.runtime.terminal_control.actor(terminal_ctx, uow), "forbidden")
            with pytest.raises(Final) as final:
                owner_input(uow, host.worker)
            assert (final.value.state, final.value.code) == ("failed", "agent_unavailable"), name
            assert not SignoffBridge._can_receive(uow, {"proposer_agent_id": host.worker}), name
            refused(lambda: TelegramBridge.check_agent_chat(uow, SimpleNamespace(name="ordubashy", agent_id=host.coordinator)),
                    "binding_invalid")
            assert not ordubashy_profile(uow, host.coordinator, require_codex=False), name
            assert configured_ordubashy(uow, require_codex=False) is None, name
            refused(lambda: require_coordinator(uow), "management_unavailable")
        assert not {host.worker, host.coordinator, host.helper} & attention(None), name
        assert not {host.worker, host.coordinator, host.helper} & unread(None), name
        for identity, values in saved.items():
            restore(host, identity, values)
    admitted()


def test_role_specific_restrictions_stay_with_their_admissions(host):
    with host.database.read() as uow:
        # Focus is Ordubashy's; creation eligibility is the capability on the configured Ordubashy; terminal control is Ordubashy's (and the owner's) alone.
        with pytest.raises(DomainError) as focus:
            host.runtime.focus.context(agent(host, host.worker), uow)
        assert focus.value.code == "forbidden"
        assert creator_actor(uow, host.worker) is None and creator_actor(uow, new_id()) is None   # a worker never creates, whatever it holds
        for other in (host.helper, host.applier):                     # neither is a creator, with or without the capability
            assert creator_actor(uow, other) is None
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',"
                               "json('[\"coordinate_businesses\",\"read_global_context\"]')) WHERE agent_id=?", (host.applier,))
    with host.database.read() as uow:
        assert creator_actor(uow, host.applier) is None
        host.runtime.terminal_control.actor(agent(host, host.coordinator), uow)
        for other in (host.worker, host.applier, host.helper):
            with pytest.raises(DomainError) as terminal:
                host.runtime.terminal_control.actor(agent(host, other), uow)
            assert terminal.value.code == "forbidden"
        # The one agent chat (Ordubashy's) binds the exact role; management needs a Codex Ordubashy.
        with pytest.raises(DomainError) as chat:
            TelegramBridge.check_agent_chat(uow, SimpleNamespace(name="ordubashy", agent_id=host.worker))
        assert chat.value.code == "binding_invalid"
        assert not ordubashy_profile(uow, host.worker, require_codex=False)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',"
                               "json('[\"read_global_context\"]')) WHERE agent_id=?", (host.helper,))
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.runtime','claude-code') "
                               "WHERE agent_id=?", (host.coordinator,))
    with host.database.read() as uow:
        assert active_global(uow.connection, host.helper) is not None
        assert ordubashy_profile(uow, host.coordinator, require_codex=False)
        assert not ordubashy_profile(uow, host.coordinator, require_codex=True)
        with pytest.raises(DomainError) as unavailable:
            require_coordinator(uow)
        assert unavailable.value.code == "management_unavailable"


def management_message(host, business):
    """A durable business message from a worker to the coordinator, routed to management (the stored v1 shape)."""
    with host.database.transaction() as uow:
        message, route, now = new_id(), new_id(), utc_now()
        event = uow.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,"
                                       "created_at) VALUES(?,?,?,?,?,?,?)", (business, "agent_messages", message, host.helper,
                                                                              "direct_message", "Need a decision", now)).lastrowid
        uow.connection.execute("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,thread_id,body,created_at,"
                               "current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (message, event, business, host.helper, host.coordinator, message, "Need a decision", now,
                                host.helper, host.coordinator))
        uow.connection.execute("INSERT INTO management_routing(id,source_type,source_id,business_id,coordinator_id,"
                               "attention_destination,management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (route, "agent_messages", message, business, host.coordinator, "management", 1, host.helper, now, now))
    return route


def test_a_business_management_operation_needs_no_agent_membership_and_humans_keep_their_membership_rule(host):
    route = management_message(host, host.first)
    management = host.runtime.application.management
    got = host.instance.invoke(agent(host, host.coordinator, host.first), "management_get", {"routing_id": route})
    assert got.status == "ok", got.error
    assert got.data["routing"]["id"] == route
    stranger = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Stranger"})
    human = InvocationContext(Principal(stranger, "human"), business_id=host.first, allowed_business_ids=frozenset({host.first}),
                              source="dashboard")
    with host.database.read() as uow:
        for ctx in (agent(host, host.coordinator, host.first), agent(host, host.helper, host.first)):
            management._scope(ctx, host.first, uow)
        # Reach is still intersected with the context's businesses and its exact selected scope.
        for ctx in (agent(host, host.coordinator, host.second), replace(agent(host, host.coordinator), allowed_business_ids=frozenset())):
            with pytest.raises(DomainError) as outside:
                management._scope(ctx, host.first, uow)
            assert outside.value.code == "forbidden"
        with pytest.raises(DomainError) as member:
            management._scope(human, host.first, uow)
        assert member.value.code == "forbidden"
    with host.database.transaction() as uow:
        uow.repository("business_memberships", business_id=host.first).insert({"principal_id": stranger})
    with host.database.read() as uow:
        management._scope(human, host.first, uow)
    ctx = agent(host, host.coordinator, host.first)
    saved = apply(host, host.coordinator, cohorts(host)["retired_role"])
    with host.database.read() as uow:
        with pytest.raises(DomainError) as retired:
            management._scope(ctx, host.first, uow)
        assert retired.value.code == "forbidden"
    restore(host, host.coordinator, saved)


def test_owner_history_readers_keep_the_records_of_non_current_registrations(host):
    with host.database.transaction() as uow:
        now = utc_now()
        uow.connection.execute("INSERT INTO agent_viewbox_items(id,agent_id,canonical_link,link,title,created_at,placed_at,"
                               "placement_sequence) VALUES (?,?,?,?,?,?,?,1)",
                               (new_id(), host.helper, "/files/view?path=a", "/files/view?path=a", "Kept", now, now))
        conversation = new_id()
        kept = native.insert_reply(uow.connection, lineage=Lineage(host.helper, "claude", conversation, f"claude:{conversation}"),
                                   kind="native_id", key=new_id(), session_id=None, receipt_id="synthetic-receipt", offset=0, length=4,
                                   completed_at=datetime.now(timezone.utc).isoformat(), preview="Kept")
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (host.helper,))
    board = host.runtime.domains.viewbox.owner_board(owner(host), host.helper)
    assert [item["title"] for item in board["items"]] == ["Kept"]
    with host.database.read() as uow:
        assert active_global(uow.connection, host.helper) is None
        assert native.active_profile(uow.connection, host.helper) is None
        assert native.active_profile(uow.connection, host.helper, any_status=True)["agent_id"] == host.helper
        assert native.reply_by_sequence(uow.connection, host.helper, kept["completion_sequence"])["id"] == kept["id"]
    assert host.helper not in {item["agent_id"] for item in replies(host.runtime, owner(host))["items"] if item.get("chat")}
