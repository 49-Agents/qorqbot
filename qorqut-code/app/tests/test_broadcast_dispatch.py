"""Scheduling and recovery guarantees using temporary installations and fake input."""
from dataclasses import replace
from datetime import timedelta

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from test_broadcast_contract import NOW, definition, setup_service


def invoke(s, op, args, revision=None, ctx=None):
    result = s.runtime.application.invoke(ctx or s.fixture.ctx, op, args,
        {'idempotency_key': new_id(), **({'expected_revision': revision} if revision else {})})
    assert result.status == 'ok', result.to_dict()
    return result.data


def record(s):
    with s.fixture.db.read() as uow:
        return dict(uow.connection.execute('SELECT * FROM broadcasts').fetchone())


def test_interval_keeps_anchor_and_skips_repeated_dst_hour():
    dispatch = BroadcastDispatcher(None, None)
    timing = {'kind': 'interval', 'at': NOW.isoformat(), 'every_seconds': 900}
    assert dispatch._next(timing, NOW + timedelta(minutes=37)) == '2030-01-02T12:45:00Z'
    from datetime import datetime, timezone
    # 01:30 occurs twice on this New York fall-back day; schedule only once.
    cron = {'kind': 'cron', 'schedule': '30 1 * * *', 'timezone': 'America/New_York'}
    assert dispatch._next(cron, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == '2026-11-02T06:30:00Z'


def test_interval_without_start_waits_one_interval():
    from types import SimpleNamespace
    from bazarlyk.inbox.broadcasts import Broadcasts
    broadcasts = Broadcasts(SimpleNamespace(app=None), clock=lambda: NOW)
    assert broadcasts._timing({'kind': 'interval', 'every': 15, 'unit': 'minutes'})['next'] == '2030-01-02T12:15:00Z'


def test_fanout_limit_manual_run_and_restart_recovery(tmp_path, monkeypatch):
    s = setup_service(tmp_path)
    clock = [NOW]
    s.dispatcher.clock = lambda: clock[0]
    # A Global broadcast fans out to the business agent and Ordubashy.
    owner = replace(s.fixture.ctx, business_id=None, review_business_id=None)
    invoke(s, 'broadcasts_create', {'scope': 'global', 'definition': definition(s.agent.id,
        recipients=[s.agent.id, s.manager.id], max_occurrences=2,
        timing={'kind': 'interval', 'every': 15, 'unit': 'minutes', 'at': NOW.isoformat()})}, ctx=owner)
    # Crash after durable occurrence claim but before delivery.
    deliver = s.dispatcher._deliver
    with monkeypatch.context() as patch:
        patch.setattr(s.dispatcher, '_deliver', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
        with pytest.raises(RuntimeError, match='crash'):
            s.dispatcher.tick()
    s.dispatcher.tick()
    row = record(s)
    assert row['occurrence_count'] == 1
    assert row['next_due_at'] == '2030-01-02T12:15:00Z'
    # Replayed/concurrent delivery work sees terminal results and inserts nothing.
    deliver(row['id'], 1)
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 2
    invoke(s, 'broadcasts_send_now', {'scope': 'global', 'broadcast_id': row['id']}, row['revision'], ctx=owner)
    s.dispatcher.tick()
    row = record(s)
    assert row['occurrence_count'] == 1
    assert row['next_due_at'] == '2030-01-02T12:15:00Z'
    clock[0] += timedelta(hours=2, minutes=7)
    s.dispatcher.tick()
    row = record(s)
    assert row['status'] == 'paused' and row['occurrence_count'] == 2
    assert row['next_due_at'] == '2030-01-02T14:15:00Z'
    s.dispatcher.tick()
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 6


def test_input_uncertain_boundary_is_not_replayed(tmp_path, monkeypatch):
    s = setup_service(tmp_path)
    writes = []
    class FakeTerminals:
        def input_existing(self, business, recipient, text):
            writes.append((business, recipient, text))
            raise RuntimeError('connection lost after write')
    s.dispatcher.terminals = FakeTerminals()
    invoke(s, 'broadcasts_create', {'definition': definition(s.agent.id, delivery='input')})
    s.dispatcher.tick()
    s.dispatcher.tick()
    row = record(s)
    s.dispatcher._deliver(row['id'], 1)
    assert len(writes) == 1
    with s.fixture.db.read() as uow:
        result = dict(uow.connection.execute('SELECT * FROM broadcast_deliveries').fetchone())
        assert result['state'] == 'uncertain'
        assert uow.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 0


def test_sender_permission_revocation_prevents_later_delivery(tmp_path):
    s = setup_service(tmp_path)
    ctx = s.runtime.resolver.invocation_context(s.agent)
    invoke(s, 'broadcasts_create', {'definition': definition(s.agent.id)}, ctx=ctx)
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'broadcasts_create': 'deny'}}})
    s.dispatcher.tick()
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT state FROM broadcast_deliveries').fetchone()[0] == 'denied'
        assert uow.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 0


def test_manager_edit_authors_future_instruction_without_impersonation(tmp_path):
    s = setup_service(tmp_path)
    invoke(s, 'broadcasts_create', {'definition': definition(s.agent.id)})
    row = record(s)
    manager = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id)
    invoke(s, 'broadcasts_update', {'broadcast_id': row['id'],
        'definition': definition(s.agent.id, message='Manager instruction')}, row['revision'], ctx=manager)
    s.dispatcher.tick()
    with s.fixture.db.read() as uow:
        message = dict(uow.connection.execute('SELECT * FROM agent_messages').fetchone())
        assert message['sender_id'] == s.manager.id
        assert message['body'] == 'Manager instruction'
    assert record(s)['created_by'] == s.fixture.owner.id
