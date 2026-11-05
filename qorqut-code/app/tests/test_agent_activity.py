"""Isolated coordination checks; no provider calls or agent sessions are launched."""
from datetime import datetime, timedelta, timezone
import json
import fcntl
import os
from pathlib import Path
import pytest

from bazarlyk.bootstrap import activity_hook as hook
from qorqut_operations.contracts import new_id
from test_service_transports import ServiceFixture


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return result.to_dict()['data']


def test_scoped_concurrent_work_presence_and_progress(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    s = ServiceFixture(tmp_path, full=True)
    app, activity = s.runtime.application, s.runtime.activity
    business = s.fixture.business_id
    ctx = s.runtime.resolver.invocation_context(s.agent)
    now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    activity.clock = lambda: now
    def call(name, args, revision=None):
        return app.invoke(ctx, name, args, {'idempotency_key': new_id(), 'expected_revision': revision})
    session, other = new_id(), new_id()
    assert ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]['state'] == 'unknown'
    for sid, runtime in ((session, 'claude-code'), (other, 'codex')):
        assert activity.register_session(ctx, s.registrations[s.agent.id], sid, runtime)
    with TestClient(s.web) as client:
        started=client.post('/api/agent/bootstrap', headers={'Authorization':'Bearer '+s.tokens[s.agent.id]},
            json={'registration_id':s.registrations[s.agent.id], 'session_id':session, 'runtime':'claude-code', 'event':'startup'})
        assert started.status_code==200, started.text
        assert started.json()['data']['activity_available']
        assert 'TEAM CURRENT WORK' in started.json()['data']['work_briefing']['rendered']
        assert 'TECHNICAL INCIDENTS' not in started.json()['data']['work_briefing']['rendered']
        response=client.get('/api/me/agents/activity?agent_id='+s.agent.id)
        assert response.status_code==200, response.text
        assert response.json()['data']['agents'][0]['agent_id']==s.agent.id
        assert client.get('/api/me/agents/activity?limit=201').status_code==422
    first = ok(call('tasks_create', {'title': 'Install pixel', 'assignee_ids': [s.agent.id]}))['task']
    parent = ok(call('tasks_create', {'title': 'Supply pixel ID', 'assignee_ids': [s.manager.id]}))['task']
    ok(call('tasks_update', {'task_id': first['id'], 'changes': {'blocked_by': [parent['id']]}}, 1))
    items = [{'business_id': business, 'task_id': first['id'], 'summary': 'Prepare event integration'},
             {'business_id': business, 'summary': 'Review unrelated deployment'}]
    work = ok(call('agent_work_update', {'session_id': session, 'expected_work_revision': 1, 'items': items}))
    assert work['work_revision'] == 2
    assert call('agent_work_update', {'session_id': session, 'expected_work_revision': 1, 'items': []}).error['code'] == 'revision_conflict'
    denied = call('agent_work_update', {'session_id': session, 'expected_work_revision': 2,
        'items': [{'business_id': None, 'summary': 'Unauthorized Global work'}]})
    assert denied.error['code'] == 'forbidden'
    with s.fixture.db.read() as u:
        events_before = u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0]
    workspace = Path(s.fixture.resolver.resolve_agent_config(s.agent).data['workspace'])
    registration = workspace / '.bazarlyk/registration.json'
    monkeypatch.setattr(hook, 'session_owner', lambda *_: hook.process_identity(os.getpid()))
    hook.record({'hook_event_name': 'PreToolUse', 'session_id': session, 'cwd': str(workspace)}, registration, 'claude-code', now=now.timestamp())
    hook.record({'hook_event_name': 'Stop', 'session_id': other, 'cwd': str(workspace)}, registration, 'codex', now=now.timestamp())
    activity.sample()
    snapshot = ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]
    assert snapshot['state'] == 'running'
    assert {entry['state'] for entry in snapshot['sessions']} == {'running', 'idle'}
    active = next(entry for entry in snapshot['sessions'] if entry['session_id'] == session)
    assert active['inbox_owner'] and not active['work_stale']
    assert snapshot['inbox']['state'] == 'not_started'
    assert len(active['items']) == 2 and active['items'][0]['blocked'] and not active['items'][1]['blocked']
    assert active['items'][0]['blockers'][0]['assignee_ids'] == [s.manager.id]
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0] == events_before
    assert first['id'] in ok(call('tasks_get', {'task_id': parent['id']}))['task']['blocks']
    # A former task grant must not expose either its title or authored work description.
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_get': 'deny'}}})
    ctx = s.runtime.resolver.invocation_context(s.agent)
    redacted = ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]
    assert all(not item.get('task_id') for entry in redacted['sessions'] for item in entry['items'])
    assert call('tasks_notes_list', {'task_id': first['id']}).status == 'error'
    now += timedelta(minutes=4)
    assert ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]['state'] == 'unknown'
    # An open shell is insufficient: the recorded native owner has exited.
    state_path = workspace / '.bazarlyk/activity' / (session + '.json')
    state = json.loads(state_path.read_text()); state['owner'] = '99999999:1'
    hook.write_state(state_path, state)
    activity.sample()
    observed = ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]
    assert next(entry for entry in observed['sessions'] if entry['session_id'] == session)['state'] == 'offline'
    now += timedelta(hours=2)
    observed = ok(call('agents_activity_list', {'agent_id': s.agent.id}))['agents'][0]
    assert next(entry for entry in observed['sessions'] if entry['session_id'] == session)['work_stale']


def test_progress_notes_pagination_refs_and_dependency_delivery(tmp_path):
    from test_signoff_inputs import FORM, binding
    s = ServiceFixture(tmp_path, full=True)
    app, business = s.runtime.application, s.fixture.business_id
    def call(name, args, revision=None, actor=None):
        ctx = s.runtime.resolver.invocation_context(actor) if actor else s.fixture.ctx
        return ok(app.invoke(ctx, name, args, {'idempotency_key': new_id(), 'expected_revision': revision}))
    task = call('tasks_create', {'title': 'Dependent', 'assignee_ids': [s.agent.id]})['task']
    parent = call('tasks_create', {'title': 'Prerequisite', 'assignee_ids': [s.manager.id]})['task']
    signoff = call('signoffs_input_create', {'request_key': 'pixel-choice', 'form': FORM})['signoff']
    call('tasks_update', {'task_id': task['id'], 'changes': {'blocked_by': [parent['id']]}}, 1)
    # The business agent writes the referencing comment: shared comments are the team's,
    # and the installation owner's own comments belong to the private channel.
    call('tasks_comment', {'task_id': task['id'], 'text': 'Waiting for the prerequisite',
        'note_refs': [{'type': 'task', 'id': parent['id']}, {'type': 'signoff', 'id': signoff['id']}]}, 2,
        actor=s.agent)
    # Seed older historical notes directly: only pagination behavior needs exercising here.
    with s.fixture.db.transaction() as u:
        for index in range(22):
            u.repository('task_notes', business_id=business).insert({'id': new_id(), 'task_id': task['id'],
                'body': 'Finding ' + str(index), 'created_by': s.fixture.owner.id,
                'created_at': f'2026-01-{index+1:02}T12:00:00Z'})
    detail = call('tasks_get', {'task_id': task['id']})['task']
    assert detail['note_count'] == 23 and len(detail['notes']) == 20 and detail['notes_next_cursor']
    assert detail['notes'][-1]['refs'][0]['label'] == 'Prerequisite'
    assert detail['notes'][-1]['refs'][1]['id'] == signoff['id']
    older = call('tasks_notes_list', {'task_id': task['id'], 'cursor': detail['notes_next_cursor']})
    assert len(older['notes']) == 3 and not older['next_cursor']
    assert len({n['id'] for n in detail['notes'] + older['notes']}) == 23
    assert app.invoke(s.fixture.ctx, 'tasks_notes_list', {'task_id': parent['id'], 'cursor': detail['notes_next_cursor']}).status == 'error'
    listed = next(t for t in call('tasks_list', {})['tasks'] if t['id'] == task['id'])
    assert listed['note_count'] == 23 and not listed['notes']
    call('tasks_complete', {'task_id': parent['id']}, 1)
    assert not call('tasks_get', {'task_id': task['id']})['task']['unresolved_blockers']
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id '
            "WHERE e.source_id=? AND d.agent_id=? AND e.event_type='updated'", (parent['id'],s.agent.id)).fetchone()[0] == 1
    ok(s.runtime.signoffs.respond(s.fixture.ctx, signoff['id'], notes='Use the first option', selected_choices=['a'], **binding(signoff)))
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id '
            "WHERE e.source_id=? AND d.agent_id=? AND e.event_type='response_submitted'", (signoff['id'],s.agent.id)).fetchone()[0] == 1
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'signoffs_get': 'deny'}}})
    ctx=s.runtime.resolver.invocation_context(s.agent)
    notes=ok(app.invoke(ctx, 'tasks_notes_list', {'task_id':task['id']}))['notes']
    assert notes[-1]['refs'][1] == {'unavailable':True}


@pytest.mark.parametrize('executable,argv,runtime,expected', [
    ('/usr/bin/claude', ['claude'], 'claude-code', True),
    ('/opt/node_modules/@anthropic-ai/claude-code/bin/claude.exe', ['claude'], 'claude-code', True),
    ('/opt/node_modules/@anthropic-ai/.claude-code-pNdwdcmv/bin/claude.exe (deleted)', ['claude'], 'claude-code', True),
    ('/home/user/.local/share/claude/versions/2.1.270', ['claude'], 'claude-code', True),
    ('/usr/bin/node', ['node', '/opt/node_modules/@anthropic-ai/claude-code/cli.js'], 'claude-code', True),
    ('/usr/bin/node', ['node', '/tmp/app.js'], 'claude-code', False),
    ('/usr/bin/claude.exe', ['claude'], 'codex', False),
])
def test_claude_session_owner_through_hook_shell(tmp_path, monkeypatch, executable, argv, runtime, expected):
    proc = tmp_path / 'proc'
    for pid, parent, exe, args in ((20, 10, '/bin/sh', ['sh']), (10, 1, executable, argv)):
        process = proc / str(pid)
        process.mkdir(parents=True)
        (process / 'exe').symlink_to(exe)
        (process / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in args) + b'\0')
        fields = ['S', str(parent)] + ['0'] * 17 + ['12345']
        (process / 'stat').write_text(f'{pid} (process) ' + ' '.join(fields))
    monkeypatch.setattr(hook, 'PROC_ROOT', proc)
    monkeypatch.setattr(hook.os, 'getppid', lambda: 20)
    assert hook.session_owner(runtime, new_id()) == ('10:12345' if expected else None)


def test_native_process_identity_and_codex_thread_lock(tmp_path):
    identity=hook.process_identity(os.getpid())
    assert hook.owner_alive(identity)
    assert not hook.owner_alive(identity.split(':')[0]+':0')
    with (tmp_path/'thread.lock').open('w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stat=os.fstat(stream.fileno())
        owner=f'{identity}:{stream.fileno()}:{stat.st_dev}:{stat.st_ino}'
        assert hook.owner_alive(owner)
        fcntl.flock(stream, fcntl.LOCK_UN)
        assert not hook.owner_alive(owner)


def test_action_reminders_are_throttled_and_missing_runtime_is_unknown(tmp_path, monkeypatch):
    workspace = tmp_path / 'agent'; (workspace / '.bazarlyk').mkdir(parents=True)
    registration = workspace / '.bazarlyk/registration.json'
    registration.write_text(json.dumps({'workspace': str(workspace), 'agent_id': new_id(), 'registration_id': new_id()}))
    session = new_id()
    monkeypatch.setattr(hook, 'session_owner', lambda *_: hook.process_identity(os.getpid()))
    event = {'session_id':session, 'cwd':str(workspace), 'hook_event_name':'PostToolUse'}
    assert hook.record(event, registration, 'codex', now=1000) == {}
    assert hook.record(event, registration, 'codex', now=1900)['hookSpecificOutput']['additionalContext']
    assert hook.record(event, registration, 'codex', now=1901) == {}
    hook.write_state(workspace / '.bazarlyk/activity' / (session+'.feedback.json'), {'work_updated_at':2700})
    assert hook.record(event, registration, 'codex', now=2800) == {}
    monkeypatch.setattr(hook, 'session_owner', lambda *_: None)
    assert hook.record(event, registration, 'codex', now=4000) == {}
    assert json.loads((workspace / '.bazarlyk/activity' / (session+'.json')).read_text())['state'] == 'unknown'
