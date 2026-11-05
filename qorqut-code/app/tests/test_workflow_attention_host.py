"""Synthetic host workflow synchronization; no provider or live store."""
import json
import sqlite3
from types import SimpleNamespace
from dataclasses import dataclass

import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.agentic import attention as module
from bazarlyk.agentic.attention import WorkflowAttention


class Repository:
    def __init__(self, rows): self.rows = rows
    def get(self, key): return self.rows.get(key)


class Attention:
    def __init__(self):
        self.focus = {'type': 'workflow', 'id': 'run', 'business_id': None}
        self.generation = 3
        self.cleared = []
        self.focus_calls = []
    def state(self, uow, agent): return {'focus_json': self.focus, 'focus_generation': self.generation}
    def clear_focus(self, uow, agent, reason): self.cleared.append(reason); self.focus = None
    def authorized_task(self, uow, ctx, agent, task): return {'task': {'created_at': 'birth'}, 'membership': {}}
    def set_focus(self, uow, ctx, agent, **values):
        self.focus_calls.append(values)
        return values['expected_focus_generation'] == self.generation


def setup():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE attention_workflows(agent_id TEXT,run_id TEXT,revision INTEGER,consumer_id TEXT,
        work_task_json TEXT,workflow_ref TEXT,workflow_name TEXT,blocked_since TEXT,blocker_key TEXT,
        blocker_reason TEXT NOT NULL DEFAULT '',resume_context TEXT NOT NULL DEFAULT '',related_refs_json TEXT DEFAULT '[]',
        detached_at TEXT,paused INTEGER NOT NULL DEFAULT 0,last_event_ordinal INTEGER NOT NULL DEFAULT 0,updated_at TEXT,
        PRIMARY KEY(agent_id,run_id))''')
    db.execute('''CREATE TABLE attention_reminders(agent_id TEXT,kind TEXT,workflow_run_id TEXT,blocked_since TEXT,
        state TEXT,emission_started_at TEXT,updated_at TEXT,revision INTEGER)''')
    member = {'id': 'm', 'agent_id': 'agent', 'task_id': 'task', 'task_business_id': None,
              'task_created_at': 'birth', 'revision': 1}
    uow = SimpleNamespace(connection=db, repository=lambda name: Repository({'m': member} if name == 'attention_memberships' else {}))
    attention = Attention()
    host = WorkflowAttention(SimpleNamespace(application=SimpleNamespace(ordubashy_attention=attention),
                                             agentic_layer=SimpleNamespace(instance_id='instance')), key=b'x' * 32)
    host.caller = lambda ctx, consumer, generation, uow: ctx
    ctx = SimpleNamespace(principal_id='agent', session_registration_id='registration')
    snapshot = {'agent_id': 'agent', 'registration_id': 'registration', 'instance_id': 'instance', 'consumer_id': 'consumer', 'binding_generation': 1,
                'task': {'type': 'task', 'id': 'task', 'business_id': None}, 'membership_id': 'm',
                'membership_revision': 1, 'task_created_at': 'birth', 'focus_generation': 3, 'intent': 'Prepare reply'}
    return host, attention, uow, ctx, snapshot, member


def plan(snapshot, operation, state='running', ordinal=1):
    evidence = {'run_id': 'run', 'workflow_ref': 'sample/flow', 'state': state, 'ordinal': ordinal, 'current_ordinal': ordinal}
    payload = {**snapshot, 'operation': operation, 'operation_id': 'operation',
               'run_id': None if operation == 'workflows.start' else 'run', 'captured_at': '2026-01-01T00:00:00Z'}
    return SimpleNamespace(arguments={'context': module.sign_context(b'x' * 32, payload), 'operation': operation,
                                      'operation_id': 'operation', 'run_id': 'run'},
                           changes=[SimpleNamespace(action='sync_workflow_attention', values={'evidence': evidence})])


def test_focus_race_and_stale_event_do_not_steal_focus():
    host, service, uow, ctx, snapshot, member = setup()
    service.generation = 4
    service.focus = {'type': 'task', 'id': 'owner-selected', 'business_id': None}
    assert host.apply(ctx, plan(snapshot, 'workflows.start'), uow)['outcome'] == 'attention_skipped'
    assert not service.focus_calls
    assert service.focus['id'] == 'owner-selected'
    assert host.apply(ctx, plan(snapshot, 'workflows.start'), uow)['reason'] == 'stale_event'
    assert not service.focus_calls


def test_removed_membership_cannot_be_recreated_and_pause_clears_own_focus():
    host, service, uow, ctx, snapshot, member = setup()
    member['id'] = 'replacement'
    assert host.apply(ctx, plan(snapshot, 'workflows.start'), uow)['reason'] == 'membership_changed'
    assert uow.connection.execute('SELECT COUNT(*) FROM attention_workflows').fetchone()[0] == 0
    member['id'] = 'm'
    host.apply(ctx, plan(snapshot, 'workflows.start'), uow)
    member['id'] = 'replacement'
    host.apply(ctx, plan(snapshot, 'workflows.pause', 'paused', 2), uow)
    row = uow.connection.execute('SELECT * FROM attention_workflows').fetchone()
    assert row['paused'] == 1 and row['detached_at'] and service.cleared == ['workflow_paused']


def test_moved_membership_keeps_identity_and_uses_current_scope():
    host, service, uow, ctx, snapshot, member = setup()
    member['task_business_id'] = 'destination'
    member['revision'] = 2
    assert host.apply(ctx, plan(snapshot, 'workflows.start'), uow)['outcome'] == 'attention_synchronized'
    assert service.focus_calls[0]['task']['business_id'] == 'destination'


def test_old_start_cannot_refocus_or_consume_new_resume():
    host, service, uow, ctx, snapshot, member = setup()
    old = plan(snapshot, 'workflows.start', ordinal=1)
    old.changes[0].values['evidence']['current_ordinal'] = 2
    host.apply(ctx, old, uow)
    assert service.focus_calls == []
    host.apply(ctx, plan(snapshot, 'workflows.resume', ordinal=2), uow)
    assert len(service.focus_calls) == 1


def test_pause_invalidates_only_the_cleared_blocking_episode():
    host, service, uow, ctx, snapshot, member = setup()
    host.apply(ctx, plan(snapshot, 'workflows.start'), uow)
    uow.connection.execute("UPDATE attention_workflows SET blocked_since='episode' WHERE run_id='run'")
    uow.connection.executemany('INSERT INTO attention_reminders VALUES(?,?,?,?,?,?,?,?)', [
        ('agent', 'workflow_detach', 'run', 'episode', 'pending', None, 'old', 1),
        ('agent', 'workflow_detach', 'other', 'episode', 'pending', None, 'old', 1)])
    service.focus = {'type': 'task', 'id': 'other', 'business_id': None}
    host.apply(ctx, plan(snapshot, 'workflows.pause', 'paused', 2), uow)
    assert [tuple(row) for row in uow.connection.execute('SELECT workflow_run_id,state FROM attention_reminders ORDER BY workflow_run_id')] == [
        ('other', 'pending'), ('run', 'cancelled')]
    assert not service.cleared


def test_stale_native_binding_is_rejected_by_current_inbox_owner(monkeypatch):
    @dataclass
    class Context:
        principal_kind: str = 'agent'
        source: str = 'mcp'
        session_registration_id: str = 'registration'
        delegation_binding_id: object = None
        delegation_assignment_id: object = None
        principal_id: str = 'agent'
        principal: str = 'agent'
        inbox_session_id: str = 'other-session'
        inbox_generation: int = 1
        business_id: object = None
        review_business_id: object = None
    service = Attention()
    app = SimpleNamespace(ordubashy_attention=service, current_context=lambda ctx, uow: ctx)
    def session_owner(ctx, consumer, uow):
        assert ctx.inbox_session_id == consumer and ctx.inbox_generation == 1
        raise DomainError('inbox_generation_stale', 'stale')
    resolver = SimpleNamespace(profile=lambda principal, connection: (SimpleNamespace(agent_id='agent'), {'workspace': '/workspace'}))
    runtime = SimpleNamespace(application=app, agentic_layer=SimpleNamespace(instance_id='instance'), resolver=resolver,
                              inbox=SimpleNamespace(session_owner=session_owner))
    connection = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {
        'principal_id': 'agent', 'workspace': '/workspace', 'credential_id': 'credential', 'id': 'registration'}))
    monkeypatch.setattr(module, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    monkeypatch.setattr(module, 'current_agent_credential', lambda *args, **kwargs: None)
    with pytest.raises(DomainError) as error:
        WorkflowAttention(runtime, key=b'x' * 32).caller(Context(), 'current-session', 1, SimpleNamespace(connection=connection))
    assert error.value.code == 'inbox_generation_stale'


def test_session_owner_must_match_registration_and_workspace(monkeypatch):
    @dataclass
    class Context:
        principal_kind: str = 'agent'
        source: str = 'mcp'
        session_registration_id: str = 'registration'
        delegation_binding_id: object = None
        delegation_assignment_id: object = None
        principal_id: str = 'agent'
        principal: str = 'agent'
        inbox_session_id: object = None
        inbox_generation: object = None
        business_id: object = None
        review_business_id: object = None
    binding = {'registration_id': 'registration', 'agent_id': 'agent', 'session_id': 'session', 'workspace': '/workspace'}
    app = SimpleNamespace(ordubashy_attention=Attention(), current_context=lambda ctx, uow: ctx)
    resolver = SimpleNamespace(profile=lambda principal, connection: (SimpleNamespace(agent_id='agent'), {'workspace': '/workspace'}))
    runtime = SimpleNamespace(application=app, agentic_layer=SimpleNamespace(instance_id='instance'), resolver=resolver,
                              inbox=SimpleNamespace(session_owner=lambda ctx, consumer, uow: {'binding_json': json.dumps(binding)}))
    connection = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: {
        'principal_id': 'agent', 'workspace': '/workspace', 'credential_id': 'credential', 'id': 'registration'}))
    monkeypatch.setattr(module, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    monkeypatch.setattr(module, 'current_agent_credential', lambda *args, **kwargs: None)
    service = WorkflowAttention(runtime, key=b'x' * 32)
    uow = SimpleNamespace(connection=connection)
    assert service.caller(Context(), 'session', 1, uow).inbox_generation == 1
    binding['workspace'] = '/other'
    with pytest.raises(DomainError) as error:
        service.caller(Context(), 'session', 1, uow)
    assert error.value.code == 'registration_conflict'


def test_signed_context_rejects_tampering_and_operation_substitution(tmp_path):
    key = module.signing_key(tmp_path / 'runtime')
    assert module.signing_key(tmp_path / 'runtime') == key
    host, service, uow, ctx, snapshot, member = setup()
    payload = {**snapshot, 'operation': 'workflows.start', 'operation_id': 'start-1',
               'run_id': None, 'captured_at': '2026-01-01T00:00:00Z'}
    signed = module.sign_context(key, payload)
    assert module.verify_context(key, signed, 'workflows.start', 'start-1', 'run') == payload
    changed = {**signed, 'payload': {**payload, 'focus_generation': 9}}
    with pytest.raises(DomainError): module.verify_context(key, changed, 'workflows.start', 'start-1', 'run')
    with pytest.raises(DomainError): module.verify_context(key, signed, 'workflows.start', 'other', 'run')
