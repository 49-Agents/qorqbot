"""Global is a private task namespace, not a cross-business collection."""
from dataclasses import replace
import json

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage import Database
from test_service_transports import ORIGIN, ServiceFixture
from test_signoff_inputs import FORM


@pytest.fixture
def _setup(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    s.ordu = s.manager
    s.app = s.runtime.application
    s.owner = replace(s.fixture.ctx, business_id=None, review_business_id=None)
    s.orductx = s.runtime.resolver.invocation_context(s.ordu)
    return s


def call(s, op, args, *, ctx=None, revision=None, key=None):
    opts = {'idempotency_key': key or new_id()}
    if revision is not None:
        opts['expected_revision'] = revision
    return s.app.invoke(ctx or s.orductx, op, args, opts)


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return result.to_dict()['data']


def test_global_crud_assignment_config_and_business_isolation(setup):
    s = setup
    task = ok(call(s, 'tasks_create', {'title': 'Personal follow-up', 'scope': 'global', 'id': 'shared-id'}))['task']
    assert task['business_id'] is None and task['assignee_ids'] == [s.ordu.id]
    ok(call(s, 'tasks_create', {'title': 'Business work', 'id': 'shared-id'}, ctx=s.fixture.ctx))
    assert [t['title'] for t in ok(call(s, 'tasks_list', {}, ctx=s.fixture.ctx))['tasks']] == ['Business work']
    assert [t['title'] for t in ok(call(s, 'tasks_list', {}))['tasks']] == ['Personal follow-up']
    from bazarlyk.storage.exports import build_exports
    exported = build_exports(s.fixture.db.path, s.fixture.root)
    projections = [json.loads(body) for path, body in exported.files.items() if path.endswith('/tasks.json')]
    assert all([t['title'] for t in projection['records']] ==
               (['Personal follow-up'] if projection['scope'] == 'global' else ['Business work']) for projection in projections)
    assert len(projections) == 2
    task = ok(call(s, 'tasks_assign', {'task_id': task['id'], 'assignee_ids': [s.fixture.owner.id]}, revision=1))['task']
    task = ok(call(s, 'tasks_comment', {'task_id': task['id'], 'text': 'Script: my scratchpad reference'}, revision=task['revision']))['task']
    assert len(task['notes']) == 1
    assert call(s, 'tasks_update', {'task_id': task['id'], 'changes': {'title': 'Stale'}}, revision=1).error['code'] == 'revision_conflict'
    task = ok(call(s, 'tasks_complete', {'task_id': task['id']}, revision=task['revision']))['task']
    assert task['closed']
    task = ok(call(s, 'tasks_update', {'task_id': task['id'], 'changes': {'status': 'open', 'archived': True}}, revision=task['revision']))['task']
    assert task['archived'] and not task['closed']
    assert ok(call(s, 'tasks_list', {}))['tasks'] == []
    cfg = ok(call(s, 'tasks_config_get', {}))['config']
    cfg['fields'] = {'priority': {'type': 'int'}}
    ok(call(s, 'tasks_config_update', {'config': cfg}))
    assert 'priority' not in ok(call(s, 'tasks_config_get', {}, ctx=s.fixture.ctx))['config']['fields']
    candidates = ok(call(s, 'assignees_list', {}))['assignees']
    assert {a['id'] for a in candidates} == {s.ordu.id, s.fixture.owner.id}
    ctx = s.runtime.resolver.invocation_context(s.agent)
    assert call(s, 'tasks_list', {'scope': 'global'}, ctx=ctx).status == 'error'
    assert call(s, 'tasks_create', {'scope': 'global', 'title': 'Forbidden'}, ctx=ctx).status == 'error'
    assert call(s, 'tasks_assign', {'task_id': task['id'], 'assignee_ids': [s.agent.id]}, revision=task['revision']).error['code'] == 'invalid_assignee'
    with TestClient(s.web) as client:
        assert client.get('/api/me/tasks').json()['tasks'] == []
        response = client.post('/api/root/operations/tasks_list', json={'include_archived': True}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert [t['title'] for t in response.json()['data']['tasks']] == ['Personal follow-up']
        assert client.post('/api/root/operations/tasks_list', json={'business_id': s.fixture.business_id}, headers=ORIGIN).status_code == 403


def test_business_and_global_tags_round_trip_for_owner_and_self(setup):
    s = setup
    assignees = [s.fixture.owner.id, s.ordu.id]
    for ctx in (s.fixture.ctx, s.owner):
        config = ok(call(s, 'tasks_config_get', {}, ctx=ctx))['config']
        config['tags']['list'].extend(['custom-work', 'verified'])
        ok(call(s, 'tasks_config_update', {'config': config}, ctx=ctx))
    business_task = ok(call(s, 'tasks_create', {
        'id': 'business-software', 'title': 'Business code fix',
        'assignee_ids': assignees, 'tags': ['design/software', 'custom-work'],
    }, ctx=s.fixture.ctx))['task']
    global_task = ok(call(s, 'tasks_create', {
        'scope': 'global', 'id': 'global-software', 'title': 'Global script fix',
        'assignee_ids': assignees, 'tags': ['design/software', 'custom-work'],
    }, ctx=s.owner))['task']
    assert business_task['tags'] == ['design/software', 'custom-work']
    assert global_task['tags'] == ['design/software', 'custom-work']
    assert business_task['assignee_ids'] == sorted(assignees)
    assert global_task['assignee_ids'] == sorted(assignees)

    edited = ok(call(s, 'tasks_update', {'task_id': 'business-software',
        'changes': {'tags': ['design/software', 'custom-work', 'verified']}},
        ctx=s.fixture.ctx, revision=business_task['revision']))['task']
    assert edited['tags'] == ['design/software', 'custom-work', 'verified']
    untagged = ok(call(s, 'tasks_create', {
        'id': 'global-untagged', 'scope': 'global', 'title': 'Unclassified follow-up',
        'assignee_ids': assignees,
    }, ctx=s.owner))['task']
    assert untagged['tags'] == []


def test_global_dependencies_scope_switch_and_owner_directed_targets(setup):
    s = setup
    selected = s.orductx.select_business(s.fixture.business_id)
    task = ok(call(s, 'tasks_create', {'scope': 'global', 'id': 'a', 'title': 'Global A'}, ctx=selected))['task']
    assert task['business_id'] is None
    ok(call(s, 'tasks_create', {'id': 'b', 'title': 'B', 'blocked_by': ['a']}))
    assert call(s, 'tasks_update', {'task_id': 'a', 'changes': {'blocked_by': ['b']}}, revision=1).status == 'error'
    assert call(s, 'tasks_create', {'title': 'Business blocked by Global', 'blocked_by': ['a']}, ctx=s.fixture.ctx).status == 'error'
    assert call(s, 'tasks_create', {'title': 'No Story', 'story_ids': [new_id()]}).status == 'error'
    applier = s.fixture.create('applier', 'learning-applier', scope={'kind': 'ecosystem', 'allowed_business_ids': [], 'global_capabilities': ['read_global_context']})
    assert call(s, 'tasks_assign', {'task_id': 'a', 'assignee_ids': [applier.id]}, revision=1).error['code'] == 'invalid_assignee'
    ok(call(s, 'tasks_assign', {'task_id': 'a', 'assignee_ids': [applier.id]}, ctx=s.owner, revision=1))
    # A role override does not turn a business agent into a Global agent.
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_list': 'allow'}}})
    assert call(s, 'tasks_list', {'scope': 'global'}, ctx=s.runtime.resolver.invocation_context(s.agent)).status == 'error'


def test_global_inbox_and_context_attention(setup):
    s = setup
    task = ok(call(s, 'tasks_create', {'title': 'Owner reminder', 'assignee_ids': [s.fixture.owner.id]}, ctx=s.owner))['task']
    with s.fixture.db.read() as uow:
        assert s.runtime.inbox.visible(s.orductx, 'tasks', task['id'], None, uow)
        assert not s.runtime.inbox.visible(s.runtime.resolver.invocation_context(s.agent), 'tasks', task['id'], None, uow)
        from bazarlyk.inbox.owner_tasks import tasks
        assert [r['id'] for r in tasks(uow)] == [task['id']]
        assert not list(tasks(uow, business=s.fixture.business_id))
        assert any(r['source_id'] == task['id'] for r in s.runtime.inbox.pending(s.orductx, uow)['items'])
    context = ok(call(s, 'agent_context_get', {}))
    assert any(t['id'] == task['id'] for t in context['attention'])
    observed = ok(call(s, 'working_memory_get', {'observe': [{'type': 'task', 'id': task['id']}]}))['observations']
    assert observed[0]['id'] == task['id']
    s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'tasks_get': 'deny'}}})
    s.orductx = s.runtime.resolver.invocation_context(s.ordu)
    with s.fixture.db.read() as uow:
        assert not s.runtime.inbox.pending(s.orductx, uow)['items']
    assert call(s, 'working_memory_get', {'observe': [{'type': 'task', 'id': task['id']}]}).status == 'error'


def test_owner_inbox_snapshot_keeps_global_and_business_task_subscriptions(setup):
    s = setup
    for ctx, title in ((s.owner, 'Global owner task'), (s.fixture.ctx, 'Business owner task')):
        ok(call(s, 'tasks_create', {'id': 'owner-follow-up', 'title': title,
            'assignee_ids': [s.fixture.owner.id]}, ctx=ctx))
    with TestClient(s.web) as client:
        route = '/api/me/agents/' + s.ordu.id + '/inbox'
        response = client.get(route)
        assert response.status_code == 200, response.text
        snapshot = response.json()['data']
        subscriptions = {row['business_id']: row for row in snapshot['subscriptions']}
        assert set(subscriptions) == {None, s.fixture.business_id}
        assert subscriptions[None]['business_slug'] is None
        assert subscriptions[s.fixture.business_id]['business_slug'] == 'widget'
        assert all(row['automatic'] and row['managed_by'] == 'owner_task_assignment'
                   for row in subscriptions.values())
        assert {row['business_id'] for row in snapshot['items']} == {None, s.fixture.business_id}
        manual = ok(call(s, 'inbox_subscribe', {'source_type': 'tasks', 'source_id': 'owner-follow-up'}))
        snapshot = client.get(route).json()['data']
        assert len(snapshot['subscriptions']) == 2
        global_row = next(row for row in snapshot['subscriptions'] if row['business_id'] is None)
        assert global_row['id'] == manual['subscription_id'] and global_row['automatic']
        s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'tasks_get': 'deny'}}})
        snapshot = client.get(route).json()['data']
        assert snapshot['subscriptions'] == [] and snapshot['items'] == []


def test_ordubashy_can_reprioritize_but_not_answer_or_cancel_other_signoffs(setup):
    s = setup
    proposer = s.runtime.resolver.invocation_context(s.agent)
    row = ok(call(s, 'signoffs_input_create', {'request_key': 'supplier', 'form': FORM}, ctx=proposer))['signoff']
    ordu = s.orductx.select_business(s.fixture.business_id)
    for urgency in (True, False):
        changed = ok(call(s, 'signoffs_attention_set', {'signoff_id': row['id'], 'attention': {
            'notify': urgency, 'urgent': urgency, 'blocking': urgency, 'reason': 'Owner prioritization'}}, ctx=ordu))['signoff']
        assert bool(changed['attention']['urgent']) is urgency
        assert changed['state'] == 'pending' and changed['execution_state'] == 'not_applicable'
    assert call(s, 'signoffs_input_cancel', {'signoff_id': row['id']}, ctx=ordu).status == 'error'
    marketing = s.fixture.create('marketing', 'global-marketing')
    unrelated = s.runtime.resolver.invocation_context(marketing, business_id=s.fixture.business_id)
    assert call(s, 'signoffs_attention_set', {'signoff_id': row['id'], 'attention': {}}, ctx=unrelated).status == 'error'
    assert call(s, 'signoffs_attention_set', {'scope': 'global', 'signoff_id': row['id'], 'attention': {}}).status == 'error'


def test_global_attention_preserves_snooze_and_current_role_authority(setup):
    s = setup
    other = s.fixture.create('other-ordu', 'ordubashy')
    row = ok(call(s, 'signoffs_input_create', {'scope': 'global', 'request_key': 'global-question', 'form': FORM},
        ctx=s.runtime.resolver.invocation_context(other)))['signoff']
    with s.fixture.db.transaction() as uow:
        uow.connection.execute('UPDATE signoff_attention SET snoozed_until=? WHERE signoff_id=?', ('2026-09-20T12:00:00Z', row['id']))
    args = {'scope': 'global', 'signoff_id': row['id'], 'attention': {'urgent': True, 'reason': 'Time sensitive'}}
    ok(call(s, 'signoffs_attention_set', args))
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT snoozed_until FROM signoff_attention WHERE signoff_id=?', (row['id'],)).fetchone()[0] == '2026-09-20T12:00:00Z'
    s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'signoffs_attention_set': 'deny'}}})
    assert call(s, 'signoffs_attention_set', args).status == 'error'


def test_global_task_signoff_retains_exact_scope_and_assignment_checks(setup):
    s = setup
    s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'tasks_create': 'signoff'}}})
    s.orductx = s.runtime.resolver.invocation_context(s.ordu)
    result = call(s, 'tasks_create', {'scope': 'global', 'title': 'Global approved task'})
    assert result.status == 'signoff_required', result.to_dict()
    service = s.app.signoffs
    row = ok(service.get(s.owner, result.signoff['id']))['signoff']
    assert row['review_scope']['kind'] == 'global'
    approved = service.review(s.owner, row['id'], decision='approve', proposal_revision=row['proposal_revision'],
        payload_hash=row['payload_hash'], expected_status_revision=row['status_revision'], idempotency_key=new_id())
    assert approved.status == 'accepted', approved.to_dict()
    assert service.run_pending() == 1
    tasks = ok(call(s, 'tasks_list', {}))['tasks']
    assert len(tasks) == 1 and tasks[0]['business_id'] is None


def test_populated_migration_preserves_business_rows_and_integrity(tmp_path, monkeypatch):
    from bazarlyk.application import Application
    from bazarlyk.application.catalog import builtin_registry
    from bazarlyk.domains import register_business_domains
    from qorqut_operations.contracts import Principal
    from foundation_support import BUSINESS, HUMAN, FixedPolicy, context, database_at
    from test_domain_documents import invoke

    TEAMMATE = '00000000-0000-4000-8000-000000000021'

    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, 'migrations', staticmethod(lambda: migrations[:45]))
        # Construct historical records without asking today's service to start
        # against missing modern operational tables.
        database = database_at(tmp_path)
        with database.transaction() as uow:
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': HUMAN})
            uow.repository('principals').insert({'id': TEAMMATE, 'kind': 'human', 'display_name': 'Teammate'})
            uow.repository('business_memberships', business_id=BUSINESS).insert({'principal_id': TEAMMATE})
            uow.repository('tasks', business_id=BUSINESS).insert({'id': 'keep', 'title': 'Keep me', 'status': 'open', 'created_by': HUMAN})
            uow.repository('task_notes', business_id=BUSINESS).insert({'id': 'note', 'task_id': 'keep', 'body': 'Original note', 'created_by': HUMAN})
            uow.repository('business_configs', business_id=BUSINESS).insert({'domain': 'fixture', 'config_json': {'preserved': True}})
        tables = ('tasks', 'task_notes', 'relationships', 'business_configs', 'inbox_events')
        with database.read() as uow:
            before = {table: [tuple(r) for r in uow.connection.execute('SELECT * FROM '+table)] for table in tables}
            columns = {table: ','.join('"'+r[1]+'"' for r in uow.connection.execute('PRAGMA table_info('+table+')')) for table in tables}
    database.initialize()
    database.open_existing()
    with database.read() as uow:
        for table in tables:
            assert [tuple(r) for r in uow.connection.execute('SELECT '+columns[table]+' FROM '+table)] == before[table]
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()
        assert uow.connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    registry = builtin_registry()
    app = Application(database, registry, policy=FixedPolicy())
    from context_ports import context_ports
    register_business_domains(registry, app, tmp_path, catalog=None, agent_resolver=None, **context_ports())
    # The installation owner's ordinary comments belong to the private channel, so a
    # teammate exercises the upgraded shared note writer.
    teammate = replace(context(), principal=Principal(TEAMMATE, 'human'))
    ok(invoke(app, 'tasks_comment', {'task_id': 'keep', 'text': 'After upgrade'}, revision=1, ctx=teammate))
    owner = replace(context(global_capabilities=frozenset({'read_global_context'})), business_id=None, review_business_id=None)
    ok(invoke(app, 'tasks_create', {'title': 'Global', 'id': 'global'}, ctx=owner))
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.connection.execute("INSERT INTO tasks SELECT * FROM tasks WHERE business_id IS NULL")
