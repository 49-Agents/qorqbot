"""Focused coverage for exact-session reads and revision-safe work patches."""
import pytest

from qorqut_operations.contracts import new_id, plain
from mcp_efficiency_support import isolated_service


@pytest.fixture
def service(tmp_path):
    with isolated_service(tmp_path) as value:
        yield value


def test_exact_session_read_and_item_patches_preserve_unrelated_work(service):
    s = service
    app = s.runtime.application
    session = new_id()
    s.runtime.activity.register_session(s.runtime.resolver.invocation_context(s.agent),
        s.registrations[s.agent.id], session, 'claude-code')
    task = app.invoke(s.runtime.resolver.invocation_context(s.agent), 'tasks_create',
        {'title': 'Prepare launch', 'assignee_ids': [s.agent.id]},
        {'idempotency_key': new_id()}).to_dict()['data']['task']

    def call(name, args):
        return app.invoke(s.runtime.resolver.invocation_context(s.agent), name, args,
            {'idempotency_key': new_id()})

    first = call('agent_work_update', {'session_id': session, 'expected_work_revision': 1,
        'patch': [
            {'action': 'add', 'item': {'business_id': s.fixture.business_id,
                'task_id': task['id'], 'summary': 'Prepare the launch notes'}},
            {'action': 'add', 'item': {'business_id': s.fixture.business_id,
                'summary': 'Review the handoff'}}]})
    assert first.status == 'ok', first.to_dict()
    assert first.data['work_revision'] == 2

    exact = call('agents_activity_list', {'session_id': session})
    assert exact.status == 'ok', exact.to_dict()
    agent = exact.data['agents'][0]
    work = agent['sessions'][0]
    assert work['session_id'] == session
    assert work['work_revision'] == 2
    assert len(work['items']) == len(work['item_keys']) == 2
    assert work['items_complete'] and work['items_replacement_safe']
    task_index = next(i for i, item in enumerate(work['items']) if item.get('task_id') == task['id'])
    other_index = 1 - task_index

    replaced = call('agent_work_update', {'session_id': session, 'expected_work_revision': 2,
        'patch': [{'action': 'replace', 'item_key': work['item_keys'][task_index],
            'item': {'business_id': s.fixture.business_id, 'task_id': task['id'],
                'summary': 'Verify launch notes'}}]})
    assert replaced.status == 'ok', replaced.to_dict()
    assert replaced.data['work_revision'] == 3

    current = call('agents_activity_list', {'session_id': session}).data['agents'][0]['sessions'][0]
    assert [item['summary'] for item in current['items']] == [
        'Verify launch notes' if item.get('task_id') == task['id'] else 'Review the handoff'
        for item in current['items']]
    other_key = current['item_keys'][other_index]
    removed = call('agent_work_update', {'session_id': session, 'expected_work_revision': 3,
        'patch': [{'action': 'remove', 'item_key': other_key}]})
    assert removed.status == 'ok', removed.to_dict()
    assert removed.data['work_revision'] == 4
    remaining = call('agents_activity_list', {'session_id': session}).data['agents'][0]['sessions'][0]
    assert len(remaining['items']) == len(remaining['item_keys']) == 1
    assert remaining['items'][0]['task_id'] == task['id']

    stale = call('agent_work_update', {'session_id': session, 'expected_work_revision': 3,
        'patch': []})
    assert stale.error['code'] == 'revision_conflict'


def test_exact_session_read_is_self_only_and_marks_filtered_items_unsafe(service):
    s = service
    app, activity = s.runtime.application, s.runtime.activity
    session, other = new_id(), new_id()
    activity.register_session(s.runtime.resolver.invocation_context(s.agent),
        s.registrations[s.agent.id], session, 'claude-code')
    activity.register_session(s.runtime.resolver.invocation_context(s.manager),
        s.registrations[s.manager.id], other, 'codex')

    def call(actor, name, args):
        return app.invoke(s.runtime.resolver.invocation_context(actor), name, args,
            {'idempotency_key': new_id()})

    forbidden = call(s.agent, 'agents_activity_list', {'session_id': other})
    assert forbidden.status == 'error'
    assert forbidden.error['code'] in {'forbidden', 'not_found'}

    task = call(s.agent, 'tasks_create', {'title': 'Restricted task', 'assignee_ids': [s.agent.id]})
    task_id = task.data['task']['id']
    created = call(s.agent, 'agent_work_update', {'session_id': session, 'expected_work_revision': 1,
        'items': [
            {'business_id': s.fixture.business_id, 'task_id': task_id, 'summary': 'Private linked item'},
            {'business_id': s.fixture.business_id, 'summary': 'Visible unrelated item'}]})
    assert created.status == 'ok', created.to_dict()
    exact = call(s.agent, 'agents_activity_list', {'session_id': session})
    work = exact.data['agents'][0]['sessions'][0]
    hidden_key = work['item_keys'][0]

    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_get': 'deny'}}})
    exact = call(s.agent, 'agents_activity_list', {'session_id': session})
    work = exact.data['agents'][0]['sessions'][0]
    assert len(work['items']) == len(work['item_keys']) == 1
    assert not work['items_complete'] and not work['items_replacement_safe']
    denied = call(s.agent, 'agent_work_update', {'session_id': session, 'expected_work_revision': 2,
        'patch': [{'action': 'remove', 'item_key': hidden_key}]})
    assert denied.status == 'error'
    assert denied.error['code'] in {'forbidden', 'policy_denied'}

    visible_key = work['item_keys'][0]
    preserved = call(s.agent, 'agent_work_update', {'session_id': session, 'expected_work_revision': 2,
        'patch': [{'action': 'replace', 'item_key': visible_key,
            'item': {'business_id': s.fixture.business_id, 'summary': 'Update visible unrelated item'}}]})
    assert preserved.status == 'ok', preserved.to_dict()
    reread = call(s.agent, 'agents_activity_list', {'session_id': session}).data['agents'][0]['sessions'][0]
    assert not reread['items_complete'] and not reread['items_replacement_safe']
    assert reread['items'][0]['summary'] == 'Update visible unrelated item'
    with s.fixture.db.read() as uow:
        saved = uow.repository('agent_activity').get({'agent_id': s.agent.id, 'session_id': session})
        assert any(item.get('task_id') == task_id for item in saved['items_json'])


def test_full_list_replacement_remains_compatible_with_exact_read(service):
    s = service
    app = s.runtime.application
    session = new_id()
    s.runtime.activity.register_session(s.runtime.resolver.invocation_context(s.agent),
        s.registrations[s.agent.id], session, 'claude-code')
    ctx = s.runtime.resolver.invocation_context(s.agent)
    first = app.invoke(ctx, 'agent_work_update', {'session_id': session,
        'expected_work_revision': 1, 'items': [{'business_id': s.fixture.business_id,
            'summary': 'Keep the old interface'}]}, {'idempotency_key': new_id()})
    assert first.status == 'ok', first.to_dict()
    exact = app.invoke(ctx, 'agents_activity_list', {'session_id': session})
    assert exact.status == 'ok', exact.to_dict()
    assert exact.data['agents'][0]['sessions'][0]['items_replacement_safe']
    invalid = app.invoke(ctx, 'agent_work_update', {'session_id': session,
        'expected_work_revision': 2, 'items': [], 'patch': []}, {'idempotency_key': new_id()})
    assert invalid.error['code'] == 'validation_error'


def test_canonical_exact_session_items_round_trip_through_legacy_replacement(service):
    s = service
    app = s.runtime.application
    ctx = s.runtime.resolver.invocation_context(s.agent)
    session = new_id()
    s.runtime.activity.register_session(ctx, s.registrations[s.agent.id], session, 'claude-code')
    task = app.invoke(ctx, 'tasks_create', {'title': 'Task handoff'},
        {'idempotency_key': new_id()})
    assert task.status == 'ok', task.to_dict()
    items = [
        {'business_id': s.fixture.business_id, 'summary': 'Review the owner handoff'},
        {'business_id': s.fixture.business_id, 'task_id': task.data['task']['id'],
         'summary': 'Coordinate the task handoff', 'blocked_reason': 'Waiting for a reply'},
    ]
    initial = app.invoke(ctx, 'agent_work_update', {'session_id': session,
        'expected_work_revision': 1, 'items': items}, {'idempotency_key': new_id()})
    assert initial.status == 'ok', initial.to_dict()

    exact = app.invoke(ctx, 'agents_activity_list', {'session_id': session})
    assert exact.status == 'ok', exact.to_dict()
    work = exact.data['agents'][0]['sessions'][0]
    canonical_items = plain(work['items'])
    assert canonical_items == items
    assert len(work['items']) == len(work['item_keys']) == 2
    assert work['items_complete'] and work['items_replacement_safe']

    replaced = app.invoke(ctx, 'agent_work_update', {'session_id': session,
        'expected_work_revision': work['work_revision'], 'items': canonical_items},
        {'idempotency_key': new_id()})
    assert replaced.status == 'ok', replaced.to_dict()
    assert replaced.data['work_revision'] == work['work_revision'] + 1
