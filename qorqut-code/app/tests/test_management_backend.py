import pytest

from test_signoff_inputs import FORM, binding, create, scenario
from qorqut_operations.contracts import InvocationOptions, new_id
from bazarlyk.management import ManagementService


@pytest.fixture(autouse=True)
def paired_role_catalog(monkeypatch):
    """Make Scenario consume the task's companion role worktree under mapped envs."""
    import test_signoffs
    from instance_support import CODE, synthetic_paths
    monkeypatch.setattr(test_signoffs, "PATHS", synthetic_paths(CODE.parent))


def install_coordinator(s):
    coordinator = s.add_agent('ordubashy')
    with s.db.transaction() as uow:
        profile = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (coordinator.id,)).fetchone()
        import json
        value = json.loads(profile['profile_json'])
        value['runtime'] = 'codex'
        uow.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?', (json.dumps(value), coordinator.id))
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': s.owner.id,
                                               'management_coordinator_id': coordinator.id})
    return coordinator


def test_delegated_answer_preserves_coordinator_attribution_and_binding(scenario):
    s = scenario
    coordinator = install_coordinator(s)
    row = create(s, request_key='delegated-answer')
    with s.db.transaction() as uow:
        uow.connection.execute('''INSERT INTO management_routing
            (id,source_type,source_id,business_id,coordinator_id,attention_destination,
             management_allowed,created_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (new_id(), 'signoffs', row['id'], s.business, coordinator.id, 'management',
             1, s.agent.id, '2026-09-15T00:00:00Z', '2026-09-15T00:00:00Z'))
    answer = {key: value for key, value in binding(row).items() if key != 'idempotency_key'}
    result = s.app.invoke(s.agent_context(coordinator), 'signoffs_input_answer_delegated',
        {**answer, 'signoff_id': row['id'], 'notes': 'Use the first supplier', 'selected_choices': ['a']},
        {'idempotency_key': 'delegated-' + row['id']})
    assert result.status == 'ok', result.to_dict()
    decision = result.data['signoff']['decision']
    assert decision['by'] == coordinator.id
    assert result.data['signoff']['state'] == 'answered'
    assert s.count('apply_receipts') == 0


def test_coordinator_configuration_is_owner_only_and_exposes_availability(scenario):
    s = scenario
    coordinator = install_coordinator(s)
    ManagementService(s.app).register()
    result = s.app.invoke(s.owner_context, 'management_coordinator_set', {'coordinator_id': coordinator.id},
                          {'idempotency_key': 'configure-' + coordinator.id})
    assert result.status == 'ok', result.to_dict()
    with s.db.read() as uow:
        dto = s.app.management.coordinator_dto(uow)
    assert dto['configured'] and not dto['available'] and dto['runtime'] == 'codex' and dto['role_id'] == 'ordubashy'
    denied = s.app.invoke(s.agent_context(), 'management_coordinator_set', {'coordinator_id': coordinator.id},
                          {'idempotency_key': 'agent-configure'})
    assert denied.status == 'error'


def test_question_input_auto_routes_and_coordinator_can_escalate(scenario):
    s = scenario
    coordinator = install_coordinator(s)
    ManagementService(s.app).register()
    row = create(s, request_key='automatic-management')
    with s.db.read() as uow:
        route = uow.connection.execute('SELECT * FROM management_routing WHERE source_type=? AND source_id=?', ('signoffs', row['id'])).fetchone()
    assert route and route['coordinator_id'] == coordinator.id and route['management_allowed'] == 1
    listed = s.app.invoke(s.agent_context(coordinator), 'management_list', {'scope': 'business'}, {'idempotency_key': new_id()})
    assert listed.status == 'ok' and listed.data['items'][0]['source']['id'] == row['id']
    escalated = s.app.invoke(s.agent_context(coordinator), 'management_route',
        {'source_type': 'signoffs', 'source_id': row['id'], 'attention_destination': 'owner',
         'escalation_reason': 'Needs owner decision', 'expected_routing_revision': route['revision']}, {'idempotency_key': new_id()})
    assert escalated.status == 'ok', escalated.to_dict()
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT attention_destination FROM management_routing WHERE source_id=?', (row['id'],)).fetchone()[0] == 'owner'


def test_business_peer_cannot_redirect_another_agents_management_source(scenario):
    s = scenario
    install_coordinator(s)
    ManagementService(s.app).register()
    row = create(s, request_key='private-management-source')
    peer = s.add_agent('global-marketing')
    result = s.app.invoke(s.agent_context(peer), 'management_route',
        {'source_type': 'signoffs', 'source_id': row['id'], 'attention_destination': 'owner'},
        {'idempotency_key': new_id()})
    assert result.status == 'error'


def test_owner_only_question_cannot_enter_or_be_answered_by_management(scenario):
    s = scenario
    coordinator = install_coordinator(s)
    ManagementService(s.app).register()
    row = create(s, request_key='owner-only', form={**FORM, 'response_policy': 'owner_only'})
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT 1 FROM management_routing WHERE source_id=?', (row['id'],)).fetchone() is None
    answer = {key: value for key, value in binding(row).items() if key != 'idempotency_key'}
    result = s.app.invoke(s.agent_context(coordinator), 'signoffs_input_answer_delegated',
        {**answer, 'signoff_id': row['id'], 'selected_choices': ['a'], 'notes': 'No'},
        {'idempotency_key': new_id()})
    assert result.status == 'error'


def test_management_resolution_uses_route_revision_cas(scenario):
    s = scenario
    coordinator = install_coordinator(s)
    ManagementService(s.app).register()
    message_id = new_id()
    with s.db.transaction() as uow:
        now = '2026-09-15T00:00:00Z'
        event = uow.connection.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)', (s.business, 'agent_messages', message_id, s.agent.id, 'direct_message', 'Need a decision', now)).lastrowid
        uow.connection.execute('INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,thread_id,body,created_at,current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?)', (message_id, event, s.business, s.agent.id, coordinator.id, message_id, 'Need a decision', now, s.agent.id, coordinator.id))
        route_id = new_id()
        uow.connection.execute('INSERT INTO management_routing(id,source_type,source_id,business_id,coordinator_id,attention_destination,management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (route_id, 'agent_messages', message_id, s.business, coordinator.id, 'management', 1, s.agent.id, now, now))
    with s.db.read() as uow:
        route = uow.connection.execute('SELECT * FROM management_routing WHERE id=?', (route_id,)).fetchone()
    args = {'scope': 'business', 'routing_id': route_id, 'disposition': 'resolved'}
    result = s.app.invoke(s.agent_context(coordinator), 'management_resolve', args, {'idempotency_key': new_id(), 'expected_revision': route['revision']})
    assert result.status == 'ok', result.to_dict()
    stale = s.app.invoke(s.agent_context(coordinator), 'management_resolve', args, {'idempotency_key': new_id(), 'expected_revision': route['revision']})
    assert stale.status == 'error'
