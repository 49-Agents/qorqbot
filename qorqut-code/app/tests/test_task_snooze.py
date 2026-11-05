"""Snooze is shared deferral metadata, not a task lifecycle or scheduling system."""
from datetime import datetime, timezone
import json

import pytest
from starlette.testclient import TestClient

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.domains.tasks import TaskOperations
from bazarlyk.domains.tasks.snooze import ACTIVE_SQL
from bazarlyk.storage import Database
from foundation_support import BUSINESS, HUMAN, FixedPolicy, context, database_at
from test_domain_tasks import invoke
from test_global_tasks import setup, call, ok

START = datetime(2030, 1, 1, tzinfo=timezone.utc)
END = datetime(2030, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def domain(tmp_path):
    db = database_at(tmp_path)
    clock = [START]
    tasks = TaskOperations(clock=lambda: clock[0])
    registry = builtin_registry()
    app = Application(db, registry, policy=FixedPolicy())
    tasks.register(registry, app)
    return db, app, clock


def test_timed_snooze_preserves_state_and_expires_without_writes(domain):
    db, app, clock = domain
    ok(invoke(app, 'tasks_create', {'id':'parent', 'title':'Parent'}))
    original = ok(invoke(app, 'tasks_create', {'id':'task', 'title':'Task', 'status':'blocked',
        'due':'2030-01-01', 'assignee_ids':[HUMAN], 'blocked_by':['parent'], 'note':'Keep this'}))['task']
    args = {'task_id':'task', 'changes':{'snooze':{'until':'2030-01-02T03:00:00+03:00'}}}
    task = ok(invoke(app, 'tasks_update', args, revision=1, key='snooze'))['task']
    assert task['snooze'] == {'until':'2030-01-02T00:00:00.000000Z'} and task['snoozed']
    for field in ('status','due','assignee_ids','blocked_by','unresolved_blockers','notes','created_at'):
        assert task[field] == original[field]
    assert [t['id'] for t in ok(app.invoke(context(), 'tasks_list', {}))['tasks']] == ['parent']
    assert [t['id'] for t in ok(app.invoke(context(), 'tasks_list', {'snooze_filter':'snoozed'}))['tasks']] == ['task']
    assert len(ok(app.invoke(context(), 'tasks_list', {'snooze_filter':'all'}))['tasks']) == 2
    assert invoke(app, 'tasks_update', args, revision=1).error['code'] == 'revision_conflict'
    assert ok(invoke(app, 'tasks_update', args, revision=1, key='snooze'))['task']['revision'] == 2
    with db.read() as u:
        before = u.connection.execute('SELECT total_changes()').fetchone()[0]
        assert [r[0] for r in u.connection.execute('SELECT t.id FROM tasks t WHERE '+ACTIVE_SQL, ('2030-01-01T00:00:00.000000Z',))] == ['parent']
        assert u.connection.execute('SELECT total_changes()').fetchone()[0] == before
        stored = dict(u.connection.execute("SELECT * FROM tasks WHERE id='task'").fetchone())
    clock[0] = END  # Boundary is inclusive: equal means no longer snoozed.
    current = ok(app.invoke(context(), 'tasks_get', {'task_id':'task'}))['task']
    assert not current['snoozed'] and current['revision'] == 2 and current['snooze'] == task['snooze']
    assert not ok(app.invoke(context(), 'tasks_list', {'snooze_filter':'snoozed'}))['tasks']
    with db.read() as u:
        assert dict(u.connection.execute("SELECT * FROM tasks WHERE id='task'").fetchone()) == stored
        assert not u.repository('jobs', business_id=BUSINESS).list()
    # Inspect capture before the event materializer drains it at commit.
    with db.transaction() as u:
        u.repository('tasks', business_id=BUSINESS).update('task', {'snooze_indefinite':1,'snoozed_until':None}, expected_revision=2)
        captures = u.connection.execute("SELECT after_json FROM inbox_changes WHERE source_type='tasks' AND action='update'").fetchall()
        assert any(json.loads(r[0]).get('snooze_indefinite') == 1 for r in captures)


def test_indefinite_unsnooze_completion_archive_permissions_and_compact_receipt(domain):
    _, app, clock = domain
    task = ok(invoke(app, 'tasks_create', {'id':'one', 'title':'One', 'snooze':{'indefinite':True}}))['task']
    from bazarlyk.transport.mcp_presentation import present
    # Presentation must retain the deferred category and its deadline.
    compact = present('tasks_create', {'status':'ok','data':{'task':task}})['data']['task']
    assert compact['snooze'] == {'indefinite':True} and compact['snoozed']
    app.policy.overrides['tasks_update'] = 'deny'
    assert invoke(app, 'tasks_update', {'task_id':'one', 'changes':{'snooze':None}}, revision=1).error['code'] == 'policy_denied'
    app.policy.overrides.clear()
    task = ok(invoke(app, 'tasks_update', {'task_id':'one', 'changes':{'snooze':None}}, revision=1))['task']
    assert task['snooze'] is None and not task['snoozed']
    task = ok(invoke(app, 'tasks_update', {'task_id':'one', 'changes':{'snooze':{'until':'2030-01-02T00:00:00Z'}}}, revision=2))['task']
    task = ok(invoke(app, 'tasks_complete', {'task_id':'one'}, revision=3))['task']
    clock[0] = END
    assert not ok(app.invoke(context(), 'tasks_list', {'include_closed':False}))['tasks']
    assert ok(app.invoke(context(), 'tasks_get', {'task_id':'one'}))['task']['closed']
    ok(invoke(app, 'tasks_update', {'task_id':'one', 'changes':{'status':'open','archived':True}}, revision=4))
    assert not ok(app.invoke(context(), 'tasks_list', {}))['tasks']
    assert len(ok(app.invoke(context(), 'tasks_list', {'include_archived':True}))['tasks']) == 1


@pytest.mark.parametrize('value', [{}, {'until':'bad'}, {'until':'2030-01-02T00:00:00'},
    {'until':'2030-01-01T00:00:00Z'}, {'until':'2029-01-01T00:00:00Z'}, {'indefinite':False},
    {'until':'2030-01-02T00:00:00Z','indefinite':True}, {'indefinite':True,'extra':1}, True])
def test_invalid_snooze_is_atomic(domain, value):
    db, app, _ = domain
    result = invoke(app, 'tasks_create', {'id':'bad','title':'Bad','snooze':value})
    assert result.status == 'error', result.to_dict()
    with db.read() as u:
        assert u.repository('tasks', business_id=BUSINESS).get('bad') is None


@pytest.mark.parametrize('global_scope', [False, True])
def test_scoped_snooze_bootstrap_owner_counts_and_http(setup, global_scope):
    s = setup
    ctx = s.orductx if global_scope else s.orductx.select_business(s.fixture.business_id)
    task = ok(call(s, 'tasks_create', {'title':'Follow up', 'assignee_ids':[s.ordu.id, s.fixture.owner.id],
        'snooze':{'indefinite':True}}, ctx=ctx))['task']
    assert not ok(call(s, 'tasks_list', {}, ctx=ctx))['tasks']
    assert ok(call(s, 'tasks_list', {'snooze_filter':'snoozed'}, ctx=ctx))['tasks'][0]['id'] == task['id']
    attention = ok(call(s, 'agent_context_get', {}, ctx=ctx))['attention']
    assert not any(a['id'] == task['id'] and a.get('reason') == 'current_work' for a in attention)
    from qorqut_operations.signoffs.notifications import SignoffNotifications
    scope_id = None if global_scope else s.fixture.business_id
    counts = SignoffNotifications(s.app.signoffs).overview(s.owner)['businesses']
    assert next(b['tasks'] for b in counts if b['id'] == scope_id) == 0
    with TestClient(s.web) as client:
        if not global_scope:
            assert any(t['id'] == task['id'] for t in client.get('/api/me/tasks').json()['tasks'])
            assert not client.get('/api/biz/widget/tasks').json()
            rows = client.get('/api/biz/widget/tasks?all=1').json()
            assert any(t['id'] == task['id'] and t['snoozed'] for t in rows)
        else:
            assert not client.get('/api/me/tasks').json()['tasks']
    task = ok(call(s, 'tasks_update', {'task_id':task['id'], 'changes':{'snooze':None}}, ctx=ctx, revision=1))['task']
    counts = SignoffNotifications(s.app.signoffs).overview(s.owner)['businesses']
    assert next(b['tasks'] for b in counts if b['id'] == scope_id) == 1
    assert any(a['id'] == task['id'] for a in ok(call(s, 'agent_context_get', {}, ctx=ctx))['attention'])


def test_snooze_migration_preserves_global_and_business_records(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, 'migrations', staticmethod(lambda: migrations[:46]))
        db = database_at(tmp_path)
        with db.transaction() as u:
            for scope in (None, BUSINESS):
                u.repository('tasks', business_id=scope).insert({'id':'same','title':'Keep','status':'open','created_by':HUMAN})
                u.repository('task_notes', business_id=scope).insert({'id':'note','task_id':'same','body':'Evidence','created_by':HUMAN})
    db.initialize()
    with db.read() as u:
        assert not u.connection.execute('PRAGMA foreign_key_check').fetchall()
        for scope in (None, BUSINESS):
            row = u.repository('tasks', business_id=scope).get('same')
            assert row['snoozed_until'] is None and row['snooze_indefinite'] == 0 and row['revision'] == 1
            assert u.repository('task_notes', business_id=scope).get('note')['body'] == 'Evidence'


def test_expiry_refreshes_memory_and_attention_without_changing_revision(setup):
    s = setup
    clock = [START]
    memory = s.runtime.domains.working_memory
    memory.clock = memory.sources.clock = s.runtime.domains.tasks.clock = lambda: clock[0]
    task = ok(call(s, 'tasks_create', {'title':'Check tomorrow', 'snooze':{'until':'2030-01-02T00:00:00Z'}}))['task']
    source = {'type':'task','id':task['id']}
    first = ok(call(s, 'working_memory_get', {'observe':[source]}))['observations'][0]
    assert first['snoozed']
    assert not any(a.get('reason') == 'current_work' for a in ok(call(s, 'agent_context_get', {}))['attention'])
    clock[0] = END
    second = ok(call(s, 'working_memory_get', {'observe':[source]}))['observations'][0]
    assert not second['snoozed'] and first['token'] != second['token']
    assert any(a['id'] == task['id'] for a in ok(call(s, 'agent_context_get', {}))['attention'])
    clock[0] = datetime(2030, 1, 3, tzinfo=timezone.utc)
    third = ok(call(s, 'working_memory_get', {'observe':[source]}))['observations'][0]
    assert second['token'] == third['token']
    assert ok(call(s, 'tasks_get', {'task_id':task['id']}))['task']['revision'] == task['revision']


def test_prepared_snooze_can_expire_before_approval_without_reopening_or_retiming(setup):
    s = setup
    clock = [START]
    s.runtime.domains.tasks.clock = lambda: clock[0]
    task = ok(call(s, 'tasks_create', {'title':'Prepared deferral'}))['task']
    s.fixture.update(s.ordu, {'tool_policy':{'overrides':{'tasks_update':'signoff'}}})
    s.orductx = s.runtime.resolver.invocation_context(s.ordu)
    result = call(s, 'tasks_update', {'task_id':task['id'], 'changes':{'snooze':{'until':'2030-01-02T00:00:00Z'}}}, revision=1)
    assert result.status == 'signoff_required', result.to_dict()
    clock[0] = END
    service = s.app.signoffs
    row = ok(service.get(s.owner, result.signoff['id']))['signoff']
    from qorqut_operations.contracts import new_id
    approved = service.review(s.owner, row['id'], decision='approve', proposal_revision=row['proposal_revision'],
        payload_hash=row['payload_hash'], expected_status_revision=row['status_revision'], idempotency_key=new_id())
    assert approved.status == 'accepted', approved.to_dict()
    assert service.run_pending() == 1
    task = ok(call(s, 'tasks_get', {'task_id':task['id']}))['task']
    assert task['revision'] == 2 and not task['snoozed'] and task['status'] == 'open'
    assert task['snooze'] == {'until':'2030-01-02T00:00:00.000000Z'}


def test_snooze_filters_are_bound_to_pagination(domain):
    _, app, _ = domain
    for id in ('one','two','three'):
        ok(invoke(app, 'tasks_create', {'id':id,'title':id,'snooze':{'indefinite':True}}))
    args = {'snooze_filter':'snoozed','limit':1}
    first = ok(app.invoke(context(), 'tasks_list', args))
    second = ok(app.invoke(context(), 'tasks_list', {**args,'cursor':first['next_cursor']}))
    assert first['tasks'][0]['id'] != second['tasks'][0]['id']
    assert app.invoke(context(), 'tasks_list', {**args,'snooze_filter':'all','cursor':first['next_cursor']}).status == 'error'
