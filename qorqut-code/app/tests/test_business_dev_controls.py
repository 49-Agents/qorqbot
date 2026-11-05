"""The owner and scoped coordinators control Dev sessions; other boundaries remain explicit."""

from dataclasses import replace

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.transport.terminals import TerminalService
from test_terminal_control import control_setup, invoke
from test_terminal_execution import Broker


@pytest.fixture
def _setup(tmp_path):
    s, ctx, owner, backend = control_setup(tmp_path, dev=True)
    yield s, ctx, owner, backend
    s.runtime.stop()


def actions(s):
    with s.fixture.db.read() as u:
        return [dict(row) for row in u.connection.execute("SELECT * FROM terminal_actions ORDER BY created_at")]


def forbidden(result, *agent_ids):
    assert result.status == "error" and result.error["code"] == "forbidden", result.to_dict()
    if agent_ids:
        assert list(result.error["details"]["agent_ids"]) == sorted(agent_ids)


@pytest.mark.parametrize("action", ["start", "restart", "kill"])
def test_ordubashy_controls_dev_alone_mixed_or_by_business(setup, action):
    s, ctx, owner, backend = setup
    for selection in ({"target_agent_id": s.dev.id},
                      {"target_agent_ids": [s.agent.id, s.dev.id]},
                      {"business_ids": [s.fixture.business_id]}):
        result = invoke(s, ctx, "agent_terminal_" + action, {**selection, "rationale": "Coordinate the business"})
        assert result.status == "ok", result.to_dict()
    while s.runtime.terminal_control.tick():
        pass
    assert len(backend.calls) == 5
    assert {target for target, _, _ in backend.calls} == {s.dev.id, s.agent.id}
    assert all(kind == action for _, kind, _ in backend.calls)


@pytest.mark.parametrize("action", ["start", "restart", "kill"])
def test_marketing_controls_dev_but_not_other_agents(setup, action):
    s, ctx, owner, backend = setup
    from qorqut_operations.contracts import plain
    profile, _ = s.runtime.resolver.profile(ctx.principal)
    marketing = s.fixture.create('marketing', 'global-marketing', plugins=plain(profile.data['plugins']))
    selected = s.runtime.resolver.invocation_context(marketing)
    result = invoke(s, selected, 'agent_terminal_' + action, {'target_agent_id': s.dev.id, 'rationale': 'Coordinate Dev'})
    assert result.status == 'ok', result.to_dict()
    assert invoke(s, selected, 'agent_terminal_' + action, {'target_agent_id': s.agent.id, 'rationale': 'Wrong role'}).status == 'error'
    assert s.runtime.terminal_control.tick()
    assert backend.calls == [(s.dev.id, action, None)]


def test_ordubashy_can_relay_exact_owner_commands_to_dev(setup):
    s, ctx, owner, backend = setup
    assert invoke(s, ctx, 'agent_terminal_status', {'target_agent_id': s.dev.id}).status == 'ok'
    issued = invoke(s, owner, 'terminal_command_issue', {'target_agent_id': s.dev.id, 'action': 'ping', 'message': 'Owner: check in'})
    command = issued.data['records'][0]['id']
    assert invoke(s, ctx, 'agent_terminal_ping', {'command_id': command}).status == 'ok'
    assert invoke(s, ctx, 'agent_terminal_ping', {'command_id': command}).status == 'error'
    assert s.runtime.terminal_control.tick()
    assert backend.calls == [(s.dev.id, 'ping', 'Owner: check in')]


@pytest.mark.parametrize('change', ['permission', 'scope'])
def test_marketing_lifecycle_rechecks_authority_before_dispatch(setup, change):
    s, ctx, owner, backend = setup
    from qorqut_operations.contracts import plain
    profile, _ = s.runtime.resolver.profile(ctx.principal)
    marketing = s.fixture.create('marketing', 'global-marketing', plugins=plain(profile.data['plugins']))
    selected = s.runtime.resolver.invocation_context(marketing)
    assert invoke(s, selected, 'agent_terminal_restart',
                  {'target_agent_id': s.dev.id, 'rationale': 'Coordinate Dev'}).status == 'ok'
    changes = ({'tool_policy': {'overrides': {'agent_terminal_restart': 'deny'}}}
               if change == 'permission' else {'scope': {'kind': 'ecosystem', 'allowed_business_ids': [], 'global_capabilities': ['read_global_context']}})
    s.fixture.update(marketing, changes)
    assert s.runtime.terminal_control.tick()
    assert backend.calls == [] and [row['state'] for row in actions(s)] == ['failed']


def test_owner_controls_dev_directly(setup):
    s, ctx, owner, backend = setup
    result = invoke(s, owner, "agent_terminal_restart", {"target_agent_id": s.dev.id, "rationale": "Owner restart"})
    assert result.status == "ok", result.to_dict()
    while s.runtime.terminal_control.tick():
        pass
    assert backend.calls == [(s.dev.id, "restart", None)]


def test_a_queued_action_retargeted_to_missing_agent_never_dispatches(setup):
    s, ctx, owner, backend = setup
    result = invoke(s, ctx, "agent_terminal_kill", {"target_agent_id": s.agent.id, "rationale": "Stop Onbashy"})
    assert result.status == "ok", result.to_dict()
    # A stale or tampered queued action is rechecked against current registered state at dispatch.
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (s.dev.id,))
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE terminal_actions SET target_id=?", (s.dev.id,))
    while s.runtime.terminal_control.tick():
        pass
    assert backend.calls == [] and [row["state"] for row in actions(s)] == ["failed"]


def test_a_queued_mixed_batch_with_missing_target_fails_before_any_effect(setup):
    s, ctx, owner, backend = setup
    result = invoke(s, ctx, "agent_terminal_restart", {"target_agent_ids": [s.agent.id, s.manager.id], "rationale": "Refresh"})
    assert result.status == "ok", result.to_dict()
    queued = [row for row in actions(s) if row["batch_id"] == result.data["batch_id"]]
    # Delayed dispatch: the saved batch's later action now reaches Dev (a stale or tampered record);
    # its first action is still authorized and would otherwise run first.
    first, later = queued[0]["id"], queued[-1]["id"]
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (s.dev.id,))
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE terminal_actions SET target_id=? WHERE id=?", (s.dev.id, later))
    while s.runtime.terminal_control.tick():
        pass
    rows = {row["id"]: row for row in actions(s)}
    assert backend.calls == [] and rows[first]["state"] == rows[later]["state"] == "failed"
    assert rows[first]["detail"] == "Another target in this batch failed authorization before execution"


def test_only_the_installation_owner_opens_dev_terminal_transports(setup, tmp_path):
    s, ctx, owner, backend = setup
    terminals = TerminalService(s.runtime, Broker("host"), service_url="http://localhost/mcp")
    partner = new_id()
    with s.fixture.db.transaction() as u:
        u.repository("principals").insert({"id": partner, "kind": "human", "display_name": "Co-owner"})
        u.repository("business_memberships", business_id=s.fixture.business_id).insert({"principal_id": partner, "is_owner": True})
    with s.fixture.db.read() as u:
        co_owner = s.runtime.policy.context_for(partner, business_id=s.fixture.business_id, review_business_id=None, uow=u)
        installation = s.runtime.policy.context_for(s.fixture.owner.id, business_id=s.fixture.business_id, review_business_id=None, uow=u)
    # Start, stream, stop, input and resize all resolve their agent here.
    assert terminals.agent(co_owner, s.agent.id)[0].agent_id == s.agent.id
    with pytest.raises(DomainError) as error:
        terminals.agent(co_owner, "dev")
    assert error.value.code == "forbidden"
    assert terminals.agent(installation, "dev")[0].agent_id == s.dev.id


def test_dev_administrative_mutations_are_owner_only(setup):
    s, ctx, owner, backend = setup
    app = s.runtime.application
    manager = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id, review_business_id=s.fixture.business_id)
    profile, _ = s.runtime.resolver.profile(s.dev)
    for override in ({}, {"agents_update": "allow", "permissions_set": "allow", "agents_deactivate": "allow"}):
        if override:
            s.fixture.update(s.manager, {"tool_policy": {"overrides": override}})
            manager = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id,
                                                            review_business_id=s.fixture.business_id)
        for name, arguments in (("agents_update", {"agent_id": s.dev.id, "changes": {"runtime": "codex"}}),
                                ("permissions_set", {"agent_id": s.dev.id, "overrides": {"tasks_create": "signoff"}}),
                                ("agents_deactivate", {"agent_id": s.dev.id})):
            result = app.invoke(manager, name, arguments, InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()))
            assert result.status == "error" and result.error["code"] == "forbidden", (name, result.to_dict())
    assert s.runtime.resolver.profile(s.dev)[0].revision == profile.revision
    result = app.invoke(s.fixture.ctx, "permissions_set", {"agent_id": s.dev.id, "overrides": {"tasks_create": "signoff"}},
                        InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()))
    assert result.status == "ok", result.to_dict()


def test_an_approved_proposal_touching_dev_is_rechecked_at_apply(setup):
    s, ctx, owner, backend = setup
    ops = s.runtime.agents
    profile, _ = s.runtime.resolver.profile(s.dev)
    with s.fixture.db.read() as u:
        plan = ops.prepare("permissions_set", s.fixture.ctx, {"agent_id": s.dev.id, "overrides": {"tasks_create": "signoff"}},
                           InvocationOptions(expected_revision=profile.revision, idempotency_key=new_id()), u)
    # The same exact plan applied under a nonowner principal is refused before any record changes.
    manager = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id, review_business_id=s.fixture.business_id)
    with s.fixture.db.transaction() as u, pytest.raises(DomainError) as error:
        ops.apply(manager, plan, u)
    assert error.value.code == "forbidden"
    assert s.runtime.resolver.profile(s.dev)[0].revision == profile.revision


def test_peers_still_reach_dev_through_messages_and_tasks(setup):
    s, ctx, owner, backend = setup
    app = s.runtime.application
    for sender in (s.agent, s.manager):
        sender_ctx = s.runtime.resolver.invocation_context(sender, business_id=s.fixture.business_id,
                                                           review_business_id=s.fixture.business_id)
        task = app.invoke(sender_ctx, "tasks_create", {"title": "Fix the signup form", "assignee_ids": [s.dev.id]},
                          InvocationOptions(business_id=s.fixture.business_id, idempotency_key=new_id()))
        assert task.status == "ok", task.to_dict()
    assert backend.calls == [] and actions(s) == []


def test_input_broadcast_delivery_to_dev_is_owner_only(setup, monkeypatch):
    s, ctx, owner, backend = setup
    from bazarlyk.inbox.broadcasts import BroadcastDispatcher
    dispatcher = BroadcastDispatcher(s.runtime, s.runtime.broadcasts)
    payload = {"sender_id": s.fixture.owner.id, "scope_business_id": s.fixture.business_id, "delivery": "input"}
    delivery = {"recipient_id": s.dev.id, "business_id": s.fixture.business_id}
    with s.fixture.db.read() as u:
        assert dispatcher._delivery_context(payload, delivery, u).principal_id == s.fixture.owner.id
    # Even if another sender were later given Input permission, Dev stays owner-only.
    monkeypatch.setattr(dispatcher.broadcasts, "_input_allowed", lambda *args: True)
    manager = {**payload, "sender_id": s.manager.id}
    with s.fixture.db.read() as u:
        assert dispatcher._delivery_context(manager, {**delivery, "recipient_id": s.agent.id}, u).principal_id == s.manager.id
        with pytest.raises(DomainError) as error:
            dispatcher._delivery_context(manager, delivery, u)
    assert error.value.code == "forbidden"
