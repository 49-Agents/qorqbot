from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
import sqlite3
import json
from types import SimpleNamespace

import pytest

from attention_runtime_test_support import companions
from bazarlyk.agents.attention_delivery import AttentionRuntime, record_attention_activity
from bazarlyk.management import identity
from bazarlyk.native_chat import store
from bazarlyk.native_chat.schema import SCHEMA as NATIVE_SCHEMA


def real_service(tmp_path):
    Attention, schema = companions()
    conn = sqlite3.connect(tmp_path / 'attention.sqlite3')
    conn.row_factory = sqlite3.Row
    for statement in schema:
        conn.execute(statement)
    uow = SimpleNamespace(connection=conn)
    service = Attention.__new__(Attention)
    service.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    service.board_mode = lambda u, agent: 'normal'
    conn.execute("INSERT INTO agent_attention(agent_id,board_mode,board_due_at,updated_at) "
                 "VALUES('agent','normal','2030-01-01T12:15:00.000000Z','2030-01-01T12:00:00.000000Z')")
    runtime = AttentionRuntime(SimpleNamespace(application=SimpleNamespace(ordubashy_attention=service)))
    runtime._now = lambda: '2030-01-01T12:20:00.000000Z'
    return conn, uow, service, runtime


def test_real_clock_and_service_record_source_activity(tmp_path):
    conn, uow, service, runtime = real_service(tmp_path)
    assert record_attention_activity(uow, 'agent', 'agent_activity', '2030-01-01T12:10:00Z',
                                     'tool:one', attention=service)
    state = service.state(uow, 'agent')
    assert state['last_agent_activity_at'] == '2030-01-01T12:10:00.000000Z'
    assert state['board_due_at'] == '2030-01-01T12:25:00.000000Z'
    assert not record_attention_activity(uow, 'agent', 'agent_activity', '2030-01-01T12:10:00+00:00',
                                         'tool:one', attention=service)


def test_real_schema_reuses_safely_cancelled_occurrence(tmp_path, monkeypatch):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "state,created_at,updated_at) VALUES ('one','agent','board_normal',0,0,"
                 "'2030-01-01T12:15:00.000000Z','2030-01-01T12:15:00.000000Z',"
                 "'cancelled','2030-01-01T12:00:00.000000Z','2030-01-01T12:00:00.000000Z')")
    runtime.reconcile(uow)
    row = conn.execute("SELECT * FROM attention_reminders WHERE id='one'").fetchone()
    assert row['state'] == 'pending'
    assert service.state(uow, 'agent')['reminder_sequence'] == 0


def test_real_clock_consumes_delayed_receipt_without_shortening_owner_deadline(tmp_path):
    conn, uow, service, runtime = real_service(tmp_path)
    conn.execute("UPDATE agent_attention SET board_due_at='2030-01-01T12:50:00.000000Z',"
                 "last_owner_message_at='2030-01-01T12:20:00.000000Z' WHERE agent_id='agent'")
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "state,created_at,updated_at) VALUES ('one','agent','board_normal',0,0,"
                 "'2030-01-01T12:15:00.000000Z','2030-01-01T12:15:00.000000Z',"
                 "'submitted','2030-01-01T12:00:00.000000Z','2030-01-01T12:00:00.000000Z')")
    row = conn.execute("SELECT * FROM attention_reminders WHERE id='one'").fetchone()
    runtime._delivered(uow, row, '2030-01-01T12:16:00.000000Z')
    state = service.state(uow, 'agent')
    assert state['reminder_sequence'] == 1
    assert state['board_due_at'] == '2030-01-01T12:50:00.000000Z'
    runtime._delivered(uow, row, '2030-01-01T12:16:00.000000Z')
    assert service.state(uow, 'agent')['reminder_sequence'] == 1


@pytest.mark.parametrize('native_state', ['submitted', 'uncertain'])
def test_cancelled_chat_claim_uses_native_evidence(tmp_path, monkeypatch, native_state):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    native = {'id': 'native', 'state': native_state, 'settled_at': '2030-01-01T12:16:00.000000Z',
              'updated_at': '2030-01-01T12:16:00.000000Z', 'reason': 'transport'}
    monkeypatch.setattr(store, 'input_by_key', lambda *args: native)
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "state,route,claim_token,claim_consumer_id,claim_binding_generation,claim_expires_at,created_at,updated_at) "
                 "VALUES('one','agent','board_normal',0,0,'2030-01-01T12:15:00.000000Z',"
                 "'2030-01-01T12:15:00.000000Z','cancelled','chat','token','consumer',1,"
                 "'2030-01-01T12:30:00.000000Z','now','now')")
    runtime.reconcile(uow)
    row = conn.execute("SELECT * FROM attention_reminders WHERE id='one'").fetchone()
    assert row['state'] == native_state
    assert service.state(uow, 'agent')['reminder_sequence'] == (1 if native_state == 'submitted' else 0)


def workflow(conn, run='run-one', blocked='2030-01-01T11:00:00+00:00', generation=2):
    conn.execute("INSERT INTO attention_workflows(agent_id,run_id,consumer_id,work_task_json,workflow_ref,workflow_name,"
                 "blocked_since,blocker_key,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 ('agent', run, 'consumer', '{}', run, 'Example', blocked, 'key', blocked))
    focus = json.dumps({'type': 'workflow', 'id': run, 'business_id': None})
    conn.execute("UPDATE agent_attention SET focus_json=?,focus_started_at=?,focus_generation=?,focus_due_at=NULL "
                 "WHERE agent_id='agent'", (focus, blocked, generation))


@pytest.mark.parametrize('state', ['submitted', 'uncertain', 'claimed'])
def test_same_block_episode_is_not_repeated_after_refocus(tmp_path, monkeypatch, state):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    blocked = '2030-01-01T11:00:00+00:00'
    workflow(conn, blocked=blocked)
    claim = ('token', 'consumer', 1, '2030-01-01T12:30:00.000000Z') if state == 'claimed' else (None,)*4
    conn.execute('INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,'
                 'workflow_run_id,blocked_since,state,claim_token,claim_consumer_id,claim_binding_generation,'
                 'claim_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 ('old', 'agent', 'workflow_detach', 1, 0, '2030-01-01T11:30:00.000000Z',
                  '2030-01-01T11:30:00.000000Z', 'run-one', blocked, state, *claim, blocked, blocked))
    runtime.reconcile(uow)
    assert conn.execute('SELECT count(*) FROM attention_reminders').fetchone()[0] == 1
    # A new episode is independent, even if its prior uncertain claim remains.
    conn.execute("UPDATE attention_workflows SET blocked_since='2030-01-01T11:10:00+00:00' WHERE run_id='run-one'")
    runtime.reconcile(uow)
    row = conn.execute("SELECT * FROM attention_reminders WHERE id<>'old'").fetchone()
    assert row['blocked_since'] == '2030-01-01T11:10:00+00:00'
    assert row['state'] == 'pending'


def test_new_episode_same_generation_does_not_retry_old_uncertain(tmp_path, monkeypatch):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    workflow(conn, blocked='2030-01-01T11:10:00+00:00')
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "workflow_run_id,blocked_since,state,created_at,updated_at) VALUES('old','agent','workflow_detach',2,0,"
                 "'2030-01-01T11:30:00.000000Z','2030-01-01T11:30:00.000000Z','run-one',"
                 "'2030-01-01T11:00:00+00:00','uncertain','now','now')")
    runtime.reconcile(uow)
    old = conn.execute("SELECT * FROM attention_reminders WHERE id='old'").fetchone()
    new = conn.execute("SELECT * FROM attention_reminders WHERE id<>'old'").fetchone()
    assert old['state'] == 'uncertain' and new['state'] == 'pending'
    assert new['sequence'] == 1 and new['blocked_since'] == '2030-01-01T11:10:00+00:00'


def test_different_run_with_same_block_time_and_late_old_receipt(tmp_path, monkeypatch):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    blocked = '2030-01-01T11:00:00+00:00'
    workflow(conn, run='run-two', blocked=blocked, generation=2)
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "workflow_run_id,blocked_since,state,created_at,updated_at) VALUES('old','agent','workflow_detach',1,0,"
                 "'2030-01-01T11:30:00.000000Z','2030-01-01T11:30:00.000000Z','run-one',?,"
                 "'submitted','now','now')", (blocked,))
    row = conn.execute("SELECT * FROM attention_reminders WHERE id='old'").fetchone()
    runtime._delivered(uow, row, '2030-01-01T11:31:00.000000Z')
    assert service.state(uow, 'agent')['reminder_sequence'] == 0
    runtime.reconcile(uow)
    new = conn.execute("SELECT * FROM attention_reminders WHERE id<>'old'").fetchone()
    assert new['workflow_run_id'] == 'run-two' and new['state'] == 'pending'


def test_cancelled_unemitted_block_occurrence_can_be_reused_for_reblock(tmp_path, monkeypatch):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    workflow(conn, blocked='2030-01-01T11:10:00+00:00')
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,"
                 "workflow_run_id,blocked_since,state,created_at,updated_at) VALUES('old','agent','workflow_detach',2,0,"
                 "'2030-01-01T11:30:00.000000Z','2030-01-01T11:30:00.000000Z','run-one',"
                 "'2030-01-01T11:00:00+00:00','cancelled','now','now')")
    runtime.reconcile(uow)
    row = conn.execute("SELECT * FROM attention_reminders WHERE id='old'").fetchone()
    assert row['state'] == 'pending' and row['blocked_since'] == '2030-01-01T11:10:00+00:00'
    assert row['due_at'] == '2030-01-01T11:40:00.000000Z'


@pytest.mark.parametrize('current_generation', [0, 2])
def test_late_definitive_native_receipt_skips_unresolved_backlog(tmp_path, monkeypatch, current_generation):
    conn, uow, service, runtime = real_service(tmp_path)
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'agent')
    conn.execute(next(statement for statement in NATIVE_SCHEMA if statement.startswith('CREATE TABLE native_inputs')))
    conn.execute('CREATE TABLE native_session_owners(agent_id TEXT, consumer_id TEXT, generation INTEGER, desired_state TEXT)')
    conn.execute("INSERT INTO native_session_owners VALUES('agent','consumer',1,'paused')")
    conn.execute('CREATE TABLE agent_activity(agent_id TEXT, session_id TEXT, registration_id TEXT)')
    conn.execute('UPDATE agent_attention SET focus_generation=? WHERE agent_id=?', (current_generation, 'agent'))
    for index in range(12):
        occurrence = 'r' + str(index)
        conn.execute('INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,'
            'state,route,claim_token,claim_consumer_id,claim_binding_generation,claim_expires_at,created_at,updated_at) '
            "VALUES(?, 'agent','board_normal',0,?,'2030-01-01T12:15:00.000000Z',"
            "'2030-01-01T12:15:00.000000Z','uncertain','chat',?,'consumer',1,"
            "'2030-01-01T12:30:00.000000Z','now',?)", (occurrence, index, 'token'+str(index), str(index).zfill(2)))
        conn.execute('INSERT INTO native_inputs(id,agent_id,session_id,origin,input_type,source_key,request_hash,state,'
            'sequence,created_at,updated_at,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('input'+str(index), 'agent', 'session', 'system', 'inbox_nudge', 'attention:'+occurrence, 'a'*64,
             'submitted' if index == 11 else 'uncertain', index+1, 'now', '2030-01-01T12:16:00.000000Z',
             '2030-01-01T12:16:00.000000Z'))
    # The definitive row is later than eleven unresolved rows, but is still
    # selected in the first bounded pass. Give it the currently consumable key.
    conn.execute("UPDATE attention_reminders SET sequence=100 WHERE id='r0'")
    conn.execute("UPDATE attention_reminders SET sequence=0 WHERE id='r11'")
    class Database:
        @contextmanager
        def read(self): yield uow
        @contextmanager
        def transaction(self, **kwargs): yield uow
    runtime.runtime = SimpleNamespace(database=Database(), application=SimpleNamespace(ensure_writes_open=lambda u: None),
        native_chat=SimpleNamespace(arbiter=SimpleNamespace(locks=lambda agent: nullcontext())))
    runtime.send_due({'agent_id': 'agent'})
    assert conn.execute("SELECT state FROM attention_reminders WHERE id='r11'").fetchone()[0] == 'submitted'
    assert conn.execute("SELECT count(*) FROM attention_reminders WHERE state='uncertain'").fetchone()[0] == 11
    assert service.state(uow, 'agent')['reminder_sequence'] == (1 if current_generation == 0 else 0)
