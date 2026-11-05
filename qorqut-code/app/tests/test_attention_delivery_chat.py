from contextlib import contextmanager, nullcontext
import sqlite3
from types import SimpleNamespace
import pytest

from attention_runtime_test_support import companions
from bazarlyk.agents import attention_delivery
from bazarlyk.management import identity
from bazarlyk.native_chat import store
from bazarlyk.native_chat.schema import SCHEMA as NATIVE_SCHEMA
from bazarlyk.native_chat.arbiter import StaleSession


@pytest.mark.parametrize('hook_state', ['none', 'uncertain', 'settled', 'paused', 'stopped', 'offline', 'revoked'])
def test_chat_rechecks_owner_delay_before_typing(tmp_path, monkeypatch, hook_state):
    conn = sqlite3.connect(tmp_path / 'chat.sqlite3')
    conn.row_factory = sqlite3.Row
    _, schema = companions()
    for statement in schema:
        conn.execute(statement)
    conn.execute(next(statement for statement in NATIVE_SCHEMA if statement.startswith('CREATE TABLE native_inputs')))
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,"
                 "created_at,updated_at) VALUES ('one','agent','focus',1,1,'due','due','pending','now','now')")
    for index in range(12):
        conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,"
                     "created_at,updated_at) VALUES(?, 'agent','focus',0,?,'due','due','pending','old','old')",
                     ('old'+str(index), index))
    conn.execute('CREATE TABLE native_session_owners (agent_id TEXT, consumer_id TEXT, generation INTEGER, desired_state TEXT)')
    conn.execute("INSERT INTO native_session_owners VALUES ('agent','consumer',7,'running')")
    uow = SimpleNamespace(connection=conn)
    class Database:
        @contextmanager
        def transaction(self, **kwargs):
            yield uow
        @contextmanager
        def read(self):
            # No prior hook receipts in this synthetic transport scenario.
            class ReadConnection:
                def execute(self, sql, params):
                    return SimpleNamespace(fetchall=lambda: [])
            yield SimpleNamespace(connection=ReadConnection())
    eligible = {'chat': True}
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    monkeypatch.setattr(store, 'waiting_inputs', lambda conn, agent: [])
    monkeypatch.setattr(store, 'live_session', lambda conn, agent: {'id': 'binding', 'conversation_id': 'consumer'})
    monkeypatch.setattr(store, 'input_by_key', lambda conn, agent, key: None)
    monkeypatch.setattr(attention_delivery, 'clock_advice', lambda *args, **kwargs:
        {'kind': 'focus', 'due_at': 'due', 'reminder_sequence': 1, 'chat_eligible': eligible['chat']})
    stopped = []
    def admit(agent, source, text, *, on_checkpoint, **kwargs):
        eligible['chat'] = False  # An owner message arrived while this input was queued.
        try:
            on_checkpoint()
        except StaleSession:
            stopped.append(source)
        return SimpleNamespace()
    arbiter = SimpleNamespace(locks=lambda agent: nullcontext(), admit_nudge=admit)
    chat = SimpleNamespace(arbiter=arbiter, probe=SimpleNamespace(observe=lambda profile: object()),
        registry=SimpleNamespace(reconcile=lambda profile, observed:
                                 SimpleNamespace(live=True, session={'id': 'binding'})))
    attention = SimpleNamespace(reminder_eligible=lambda *args, **kwargs: True,
                                state=lambda uow, agent: {'focus_json': None, 'focus_generation': 1})
    app = SimpleNamespace(ordubashy_attention=attention, ensure_writes_open=lambda uow: None)
    runtime = SimpleNamespace(application=app, database=Database(), native_chat=chat)
    delivery = attention_delivery.AttentionRuntime(runtime)
    if hook_state in ('paused', 'stopped', 'offline', 'revoked'):
        if hook_state in ('paused', 'stopped'):
            conn.execute('UPDATE native_session_owners SET desired_state=?', (hook_state,))
        elif hook_state == 'offline':
            monkeypatch.setattr(store, 'live_session', lambda conn, agent: None)
        else:
            attention.reminder_eligible = lambda *args, **kwargs: False
        delivery.send_due({'agent_id': 'agent'})
        assert not stopped
        assert tuple(conn.execute("SELECT state,route FROM attention_reminders WHERE id='one'").fetchone()) == ('pending', None)
        return
    # The hook and chat compete for the same occurrence. A begun hook with no
    # receipt stays uncertain after restart and cannot be emitted by chat.
    if hook_state in ('uncertain', 'settled'):
        delivery.hooks.eligible = lambda *args: True
        settled = []
        delivery.hooks.delivered = lambda uow, row, at: settled.append((row['id'], at))
        claim = delivery.hooks.claim(uow, 'one', 'consumer', 7, '2030-01-01T12:00:00Z')
        assert delivery.hooks.begin(uow, claim, '2030-01-01T12:00:00Z')
        assert delivery.hooks.recover_expired(uow, '2030-01-01T12:01:01Z') == (0, 1)
        if hook_state == 'settled':
            receipt = {**claim, 'delivered_at': '2030-01-01T12:00:01Z'}
            assert delivery.hooks.settle(uow, receipt, '2030-01-01T12:01:02Z')
            assert delivery.hooks.settle(uow, receipt, '2030-01-01T12:01:02Z')
            assert settled == [('one', receipt['delivered_at'])]
        delivery.send_due({'agent_id': 'agent'})
        assert not stopped
        assert conn.execute("SELECT state FROM attention_reminders WHERE id='one'").fetchone()[0] == (
            'submitted' if hook_state == 'settled' else 'uncertain')
        return
    delivery.send_due({'agent_id': 'agent'})
    assert stopped == ['attention:one']
    row = conn.execute('SELECT state,route FROM attention_reminders WHERE id=\'one\'').fetchone()
    assert tuple(row) == ('pending', None)


def test_hook_claim_selects_current_generation_not_old_backlog(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / 'hook.sqlite3')
    conn.row_factory = sqlite3.Row
    _, schema = companions()
    for statement in schema:
        conn.execute(statement)
    conn.execute('CREATE TABLE native_session_owners(agent_id TEXT,consumer_id TEXT,generation INTEGER)')
    conn.execute('CREATE TABLE agent_activity(agent_id TEXT,session_id TEXT,registration_id TEXT)')
    conn.execute("INSERT INTO native_session_owners VALUES('agent','consumer',1)")
    conn.execute("INSERT INTO agent_activity VALUES('agent','consumer','registration')")
    for index in range(12):
        conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,"
                     "created_at,updated_at) VALUES(?, 'agent','focus',1,?,'due','due','pending','old','old')",
                     ('old'+str(index), index))
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,"
                 "created_at,updated_at) VALUES('current','agent','focus',2,1,'due','due','pending','now','now')")
    uow = SimpleNamespace(connection=conn)
    class Database:
        @contextmanager
        def transaction(self, **kwargs): yield uow
    attention = SimpleNamespace(state=lambda u, agent: {'focus_generation': 2, 'focus_json': None})
    app = SimpleNamespace(ordubashy_attention=attention, ensure_writes_open=lambda u: None,
                          writer_admission=SimpleNamespace(writer=lambda: nullcontext()))
    runtime = attention_delivery.AttentionRuntime(SimpleNamespace(application=app, database=Database(),
        native_chat=SimpleNamespace(arbiter=SimpleNamespace(locks=lambda agent: nullcontext()))))
    runtime._verified_consumer = lambda *args: True
    runtime.reconcile = lambda u: None
    runtime.hooks.eligible = lambda *args: True
    monkeypatch.setattr(attention_delivery, 'clock_advice', lambda *args, **kwargs:
        {'kind': 'focus', 'due_at': 'due', 'reminder_sequence': 1})
    claim = runtime.hook_claim('agent', 'registration', 'consumer')
    assert claim['id'] == 'current'
