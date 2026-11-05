import pytest

from qorqut_operations.contracts import DomainError, new_id
from test_service_transports import ServiceFixture
from test_signoff_inputs import FORM, binding
from bazarlyk.domains.inventory_cursors import InventoryCursors
from mcp_efficiency_support import isolated_service


@pytest.fixture
def service(tmp_path):
    return ServiceFixture(tmp_path, full=True)


def call(s, actor, name, args=None, *, business=None, revision=None):
    ctx = (s.fixture.ctx if actor.kind == 'human' else
           s.runtime.resolver.invocation_context(actor, business_id=business))
    result = s.runtime.application.invoke(ctx, name, args or {},
        {'idempotency_key': new_id(), **({'expected_revision': revision} if revision else {})})
    assert result.status == 'ok', result.to_dict()
    return result.to_dict()['data']


def pages(s, actor, **kwargs):
    result, after = [], None
    for _ in range(100):
        page = call(s, actor, 'agent_context_get', {'inventory': True, 'inventory_limit': 5,
                    **({'inventory_after': after} if after else {})}, **kwargs)
        result.append(page)
        if page['complete']:
            assert page['next_cursor'] is None
            return result
        assert page['next_cursor'] and page['next_cursor'] != after
        after = page['next_cursor']
    pytest.fail('Inventory pagination did not finish')


def test_complete_own_tasks_signoffs_and_pending_events_without_consumption(service):
    s = service
    business = s.fixture.business_id
    expected = set()
    for i in range(12):
        task = call(s, s.agent, 'tasks_create', {'title': f'Own {i}'})['task']
        expected.add(task['id'])
        if i == 0:
            call(s, s.agent, 'tasks_update', {'task_id': task['id'],
                'changes': {'snooze': {'indefinite': True}}}, revision=task['revision'])
        call(s, s.manager, 'tasks_create', {'title': f'Other agent {i}'}, business=business)
    closed = call(s, s.agent, 'tasks_create', {'title': 'Completed'})['task']
    call(s, s.agent, 'tasks_complete', {'task_id': closed['id']}, revision=closed['revision'])
    signoffs = set()
    for i in range(7):
        row = call(s, s.agent, 'signoffs_input_create', {'request_key': f'ask-{i}', 'form': FORM})['signoff']
        signoffs.add(row['id'])
        call(s, s.manager, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': f'Please review update {i}'},
             business=business)
    answered = s.runtime.signoffs.respond(s.fixture.ctx, row['id'], notes='Use the first supplier',
                                         selected_choices=['a'], **binding(row))
    assert answered.status == 'ok', answered.to_dict()
    with s.runtime.database.read() as uow:
        before = [tuple(r) for r in uow.connection.execute('SELECT * FROM inbox_deliveries ORDER BY agent_id,event_id')]
        leases = [tuple(r) for r in uow.connection.execute('SELECT * FROM inbox_watchers')]
    found = pages(s, s.agent)
    tasks = [t for p in found for t in p['tasks']]
    assert len(tasks) == 12 and {t['id'] for t in tasks} == expected
    assert sum(t['snoozed'] for t in tasks) == 1
    assert {t['id'] for p in found for t in p['signoffs']} == signoffs
    outcome = next(t for p in found for t in p['signoffs'] if t['id'] == row['id'])
    assert outcome['state'] == 'answered' and outcome['status_revision'] > row['status_revision']
    assert any(e['source_type'] == 'signoffs' and e['source_id'] == row['id'] for p in found for e in p['inbox'])
    assert found[0]['tasks'] and found[0]['signoffs'] and found[0]['inbox']
    messages = [e for p in found for e in p['inbox'] if e['source_type'] == 'agent_messages']
    assert len(messages) == 7 and len({e['id'] for e in messages}) == 7
    assert all('notes' not in t for t in tasks)
    assert all(not p['unavailable'] for p in found)
    larger = call(s, s.agent, 'agent_context_get', {'inventory': True})
    assert larger['complete'] and len(larger['tasks']) == 12
    with s.runtime.database.read() as uow:
        assert before == [tuple(r) for r in uow.connection.execute('SELECT * FROM inbox_deliveries ORDER BY agent_id,event_id')]
        assert leases == [tuple(r) for r in uow.connection.execute('SELECT * FROM inbox_watchers')]


def test_global_inventory_preserves_scope_and_cursor_binding(service):
    s = service
    ordu = s.fixture.create('ordu', 'ordubashy')
    global_task = call(s, ordu, 'tasks_create', {'title': 'Global own', 'scope': 'global'})['task']
    business_task = call(s, s.fixture.owner, 'tasks_create', {'title': 'Business own', 'assignee_ids': [ordu.id]})['task']
    other = call(s, s.agent, 'tasks_create', {'title': 'Not assigned to Ordu'})['task']
    for i in range(6):
        call(s, ordu, 'signoffs_input_create', {'request_key': f'global-{i}', 'form': FORM, 'scope': 'global'})
    business_signoff = call(s, ordu, 'signoffs_input_create', {'request_key': 'business', 'form': FORM},
                           business=s.fixture.business_id)['signoff']
    found = pages(s, ordu)
    tasks = [t for p in found for t in p['tasks']]
    assert {(t['id'], t['business_id']) for t in tasks} == {
        (global_task['id'], None), (business_task['id'], s.fixture.business_id)}
    assert other['id'] not in {t['id'] for t in tasks}
    decisions = [t for p in found for t in p['signoffs']]
    assert len(decisions) == 7 and len({t['id'] for t in decisions}) == 7
    assert any(t['id'] == business_signoff['id'] and t['business_id'] == s.fixture.business_id for t in decisions)
    cursor = found[0]['next_cursor']
    assert cursor
    ctx = s.runtime.resolver.invocation_context(s.manager)
    result = s.runtime.application.invoke(ctx, 'agent_context_get', {'inventory': True, 'inventory_after': cursor})
    assert result.error['code'] == 'validation_error'
    scoped = pages(s, ordu, business=s.fixture.business_id)
    assert all(t['business_id'] == s.fixture.business_id for p in scoped for k in ('tasks', 'signoffs') for t in p[k])
    profile = s.runtime.resolver.profile(ordu)[0].to_dict()
    # Ordubashy is currently required to cover every business. Switch the fixture
    # to a scope-selectable global role when testing revocation.
    s.fixture.update(ordu, {'role': s.fixture.refs['founder'],
                           'scope': {**profile['scope'], 'allowed_business_ids': []}})
    ctx = s.runtime.resolver.invocation_context(ordu)
    result = s.runtime.application.invoke(ctx, 'agent_context_get', {'inventory': True, 'inventory_after': cursor})
    assert result.error['code'] == 'validation_error'
    remaining = pages(s, ordu)
    assert all(t['business_id'] is None for p in remaining for k in ('tasks', 'signoffs', 'inbox') for t in p[k])


def test_inventory_short_handles_replay_and_bind_page_size(tmp_path):
    with isolated_service(tmp_path) as s:
        for i in range(3):
            call(s, s.manager, 'tasks_create', {'title': f'Cursor {i}'})
        ctx = s.runtime.resolver.invocation_context(s.manager)
        args = {'inventory': True, 'inventory_limit': 1}
        first = s.runtime.application.invoke(ctx, 'agent_context_get', args).to_dict()['data']
        handle = first['next_cursor']
        assert len(handle) == 16 and handle.startswith('i')

        next_args = {**args, 'inventory_after': handle}
        second = s.runtime.application.invoke(ctx, 'agent_context_get', next_args).to_dict()['data']
        replay = s.runtime.application.invoke(ctx, 'agent_context_get', next_args).to_dict()['data']
        assert second['tasks'] == replay['tasks']
        assert second['next_cursor'] == replay['next_cursor']

        changed_limit = s.runtime.application.invoke(ctx, 'agent_context_get',
            {**next_args, 'inventory_limit': 2})
        assert changed_limit.error['code'] == 'validation_error'


def test_inventory_cursor_cache_is_bounded_and_expires():
    now = [0.0]
    cursors = InventoryCursors(clock=lambda: now[0], max_handles=1, lifetime_seconds=5)
    positions = {'tasks': [1, None], 'signoffs': [0, 'upstream'], 'inbox': 3}
    first = cursors.issue('scope-a', positions)
    assert cursors.issue('scope-a', positions) == first
    assert cursors.resolve(first, 'scope-a') == positions
    with pytest.raises(DomainError) as mismatch:
        cursors.resolve(first, 'scope-b')
    assert getattr(mismatch.value, 'code', None) == 'validation_error'

    second = cursors.issue('scope-a', {'tasks': [2, None], 'signoffs': [0, None], 'inbox': 0})
    assert second != first
    with pytest.raises(DomainError) as evicted:
        cursors.resolve(first, 'scope-a')
    assert getattr(evicted.value, 'code', None) == 'inventory_cursor_expired'

    now[0] = 6.0
    with pytest.raises(DomainError) as expired:
        cursors.resolve(second, 'scope-a')
    assert getattr(expired.value, 'code', None) == 'inventory_cursor_expired'


def test_denied_queues_are_explicit_and_bootstrap_has_complete_index(service):
    s = service
    task = call(s, s.agent, 'tasks_create', {'title': 'Startup own assignment'})['task']
    boot = s.runtime.resolver.bootstrap(s.agent, registration_id=s.registrations[s.agent.id],
        session_id=new_id(), event='startup')
    assert task['id'] in boot['work_briefing']['rendered']
    assert 'startup_inventory' in boot['work_briefing']['rendered']
    assert 'complete' in boot['work_briefing']['rendered']
    assert boot['work_briefing']['context_hash']
    from starlette.testclient import TestClient
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        wire = s.tool(client, headers, 'agent_context_get', {'inventory': True})
        assert not wire['isError']
        assert wire['decoded']['data']['tasks'][0]['id'] == task['id']
    from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
    config = s.runtime.resolver.resolve_agent_config(s.agent).to_dict()
    config['mandatory_context']['rendered'] = 'x' * (MAX_CONTEXT_CHARACTERS - 500)
    brief = s.runtime.resolver.working_memory.bootstrap(s.agent, config)
    assert 'inventory=true' in brief['rendered'] and brief['omitted_for_budget']
    assert len(brief['rendered']) <= 300
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_list': 'deny', 'signoffs_list': 'deny'}}})
    found = pages(s, s.agent)
    assert not [t for p in found for t in p['tasks'] + p['signoffs']]
    assert {'tasks', 'signoffs'} <= {q['queue'] for p in found for q in p['unavailable']}
    ctx = s.runtime.resolver.invocation_context(s.agent)
    for args in ({'inventory_after': 'bad'}, {'inventory': True, 'inventory_after': 'bad'}):
        assert s.runtime.application.invoke(ctx, 'agent_context_get', args).error['code'] == 'validation_error'
