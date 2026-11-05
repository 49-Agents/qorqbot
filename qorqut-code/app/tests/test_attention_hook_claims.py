import sqlite3
import io
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest

from attention_runtime_test_support import companions
from bazarlyk.agents.attention_delivery import HookClaims, record_attention_activity
from bazarlyk.bootstrap.activity_context import emit_claimed_output


def test_hook_claim_fencing_and_uncertain_receipt_recovery(tmp_path):
    conn = sqlite3.connect(tmp_path / 'claims.sqlite3')
    conn.row_factory = sqlite3.Row
    _, schema = companions()
    for statement in schema:
        conn.execute(statement)
    conn.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,"
                 "created_at,updated_at) VALUES ('one','agent','focus',3,1,'due','due','pending','now','now')")
    uow = SimpleNamespace(connection=conn)
    deliveries = []
    claims = HookClaims(lambda u, row, consumer, generation, now: consumer == 'session' and generation == 7,
                        lambda u, row, at: deliveries.append((row['id'], at)))
    t0 = '2026-10-29T12:00:00+00:00'
    expired = '2026-10-29T12:01:01+00:00'
    first = claims.claim(uow, 'one', 'session', 7, t0)
    assert claims.claim(uow, 'one', 'session', 7, t0) is None
    assert claims.recover_expired(uow, expired) == (1, 0)
    assert not claims.begin(uow, first, expired)
    second = claims.claim(uow, 'one', 'session', 7, expired)
    assert claims.begin(uow, second, expired)
    assert claims.recover_expired(uow, '2026-10-29T12:02:02+00:00') == (0, 1)
    assert claims.claim(uow, 'one', 'session', 7, expired) is None
    receipt = {**second, 'delivered_at': '2026-10-29T12:01:02+00:00'}
    assert not claims.settle(uow, {**receipt, 'claim_token': first['claim_token']}, '2026-10-29T12:02:03+00:00')
    assert claims.settle(uow, receipt, '2026-10-29T12:02:03+00:00')
    assert claims.settle(uow, receipt, '2026-10-29T12:02:03+00:00')
    assert deliveries == [('one', receipt['delivered_at'])]


def test_hook_output_is_receipted_only_after_complete_flush(tmp_path):
    path = tmp_path / 'receipts' / 'occurrence.json'
    seen = []
    output = {'hookSpecificOutput': {'hookEventName': 'PostToolUse',
              'additionalContext': 'recovery context\nattention reminder'}}
    receipt = {'id': 'one', 'claim_token': 'token', 'registration_id': 'registration'}
    args = dict(receipt=receipt, begin=lambda: True, settle=seen.append,
                clock=lambda: '2026-10-29T12:01:02+00:00')
    class ShortStream(io.StringIO):
        def write(self, text):
            super().write(text[:1])
            return 1
    with pytest.raises(OSError):
        emit_claimed_output(output, ShortStream(), path, **args)
    assert not path.exists() and not seen
    class FailedFlush(io.StringIO):
        def flush(self):
            raise OSError('flush failed')
    with pytest.raises(OSError):
        emit_claimed_output(output, FailedFlush(), path, **args)
    assert not path.exists() and not seen
    stream = io.StringIO()
    assert emit_claimed_output(output, stream, path, **args)
    assert 'recovery context' in stream.getvalue() and 'attention reminder' in stream.getvalue()
    assert seen[0]['delivered_at'] == '2026-10-29T12:01:02+00:00'
    assert path.is_file() and path.stat().st_mode & 0o077 == 0
    def failed_settle(_):
        raise OSError('host unavailable')
    assert emit_claimed_output(output, io.StringIO(), path, **{**args, 'settle': failed_settle})
    assert path.is_file()


def test_activity_duplicate_watermarks_never_refresh_deadlines():
    attention = SimpleNamespace(state=lambda uow, agent: {'last_agent_activity_at': '2026-10-29T12:00:00+00:00'})
    assert not record_attention_activity(None, 'agent', 'agent_activity', '2026-10-29T12:00:00Z',
                                         'tool:original', attention=attention)
    assert not record_attention_activity(None, 'agent', 'agent_activity', '2026-10-29T11:00:00Z',
                                         'tool:older', attention=attention)
    with pytest.raises(ValueError):
        record_attention_activity(None, 'agent', 'agent_activity', '2026-10-29T12:00:00Z', '', attention=attention)


def test_coordinator_checkpoint_does_not_remove_finished_board_work(monkeypatch):
    from bazarlyk.agents import checkpoints, global_peers
    from bazarlyk.management import identity
    monkeypatch.setattr(identity, 'configured_ordubashy', lambda uow, require_codex: 'coordinator')
    monkeypatch.setattr(checkpoints, 'is_worker', lambda conn, agent: False)
    monkeypatch.setattr(global_peers, 'global_peer', lambda uow, agent: False)
    checks = checkpoints.AgentCheckpoints(SimpleNamespace(application=SimpleNamespace()))
    ctx = SimpleNamespace(principal_id='coordinator', business_id=None)
    row = {'items_json': [{'business_id': None, 'task_id': 'done'}], 'work_reviewed_turn': None,
           'work_reviewed_sequence': 0, 'session_id': 'session'}
    native = {'root_turn_id': 'turn', 'dirty_sequence': 1}
    issues, _ = checks._findings(ctx, row, native, SimpleNamespace(connection=None))
    assert issues == {}
    ctx.principal_id = 'other-worker'
    row['items_json'] = []
    issues, _ = checks._findings(ctx, row, native, SimpleNamespace(connection=None))
    assert [issue['code'] for issue in issues.values()] == ['work_review_missing']


def test_tool_hook_composes_recovery_and_reminder_before_emission(tmp_path, monkeypatch):
    from bazarlyk.bootstrap import activity_hook
    registration_id, session, occurrence, token = (str(uuid4()) for _ in range(4))
    path = tmp_path / 'registration.json'
    path.write_text(json.dumps({'registration_id': registration_id, 'workspace': str(tmp_path)}))
    claim = {'id': occurrence, 'claim_token': token, 'claim_consumer_id': session,
             'claim_binding_generation': 3, 'focus_generation': 2, 'revision': 4,
             'registration_id': registration_id, 'text': 'attention reminder'}
    calls = []
    def post(registration, body, **kwargs):
        calls.append(body['action'])
        return {'result': claim if body['action'] == 'claim' else True}
    monkeypatch.setattr(activity_hook, 'session_owner', lambda runtime, session: 'verified')
    monkeypatch.setattr(activity_hook, 'checkpoint_post', post)
    output = {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': 'recovery context'}}
    stream = io.StringIO()
    assert activity_hook.emit_attention({'hook_event_name': 'PostToolUse', 'session_id': session},
                                        path, 'codex', output, stream)
    assert json.loads(stream.getvalue())['hookSpecificOutput']['additionalContext'] == 'recovery context\n\nattention reminder'
    assert calls == ['claim', 'begin', 'settle']
    receipt = tmp_path / '.bazarlyk' / 'attention-receipts' / (occurrence + '-' + token + '.json')
    assert receipt.is_file()
