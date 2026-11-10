"""Attention work stays scoped; membership mutations return durable small receipts."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut.agents.attention import Attention
from qorqut.agents.attention_delivery import AttentionRuntime, HookClaims
from qorqut.composition.attention import SCHEMA
from qorqut_operations.contracts import DomainError

NOW = '2026-11-09T12:00:00.000000Z'
EARLIER = '2026-11-09T11:00:00.000000Z'


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        for statement in SCHEMA:
            self.connection.execute(statement)
        self.connection.executescript('''
            CREATE TABLE native_session_owners (agent_id TEXT PRIMARY KEY, consumer_id TEXT, generation INTEGER);
            CREATE TABLE agent_activity (agent_id TEXT, session_id TEXT, registration_id TEXT);
        ''')

    @contextmanager
    def transaction(self, **_):
        with self.connection:
            yield NS(connection=self.connection)

    read = transaction


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.conn = self.database.connection
        self.runtime = NS(database=self.database, application=NS(ensure_writes_open=lambda _: None,
            writer_admission=NS(writer=nullcontext)), focus=NS(inbox=NS()),
            native_chat=NS(arbiter=NS(locks=lambda _: nullcontext())))
        self.attention = Attention(self.runtime)
        self.runtime.application.ordubashy_attention = self.attention
        self.delivery = AttentionRuntime(self.runtime)
        self.delivery._now = lambda: NOW
        self.delivery._verified_consumer = Mock(return_value=True)
        self.delivery._text = lambda *_: 'Current task reminder'
        self.attention.board_mode = Mock(return_value='normal')
        self.attention.reminder_eligible = lambda *_a, **_k: True
        self.addCleanup(patch.stopall)
        patch('qorqut.agents.attention.enrolled_board', return_value=True).start()

    def agent(self, agent):
        self.conn.execute('INSERT INTO agent_attention(agent_id,focus_json,focus_generation,focus_started_at,updated_at) '
                          'VALUES(?,?,1,?,?)', (agent, json.dumps({'type': 'task', 'id': 'task'}), EARLIER, EARLIER))
        self.conn.execute('INSERT INTO native_session_owners VALUES(?,?,1)', (agent, 'session-' + agent))
        self.conn.execute('INSERT INTO agent_activity VALUES(?,?,?)', (agent, 'session-' + agent, 'reg-' + agent))

    def expired(self, agent, sequence, begun=False):
        identifier = f'{agent}-{sequence}'
        self.conn.execute('INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,'
            'state,route,claim_token,claim_consumer_id,claim_binding_generation,claim_expires_at,emission_started_at,'
            'created_at,updated_at) VALUES(?,?,\'focus\',1,?,?,?,\'claimed\',\'hook\',?,?,1,?,?,?,?)',
            (identifier, agent, sequence, EARLIER, EARLIER, 'token-' + identifier, 'session-' + agent,
             EARLIER, EARLIER if begun else None, EARLIER, EARLIER))

    def row(self, identifier):
        return dict(self.conn.execute('SELECT * FROM attention_reminders WHERE id=?', (identifier,)).fetchone())

    def independent_writer_renderer(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = directory.name + '/operations.sqlite3'
        self.conn.commit()
        connection = sqlite3.connect(path, timeout=.05)
        connection.row_factory = sqlite3.Row
        self.conn.backup(connection)
        connection.execute('CREATE TABLE independent_write (value TEXT)')
        connection.commit()
        self.database.connection = self.conn = connection
        self.addCleanup(connection.close)

        def render(*_):
            # This is a separate connection to the same real SQLite database.
            # Before the fix, the reminder claim still held its writer lock.
            with sqlite3.connect(path, timeout=.05) as other:
                other.execute("INSERT INTO independent_write VALUES ('unblocked')")
            return 'Current task reminder'
        self.delivery._text = render

    def test_hook_rendering_does_not_hold_database_writer(self):
        self.agent('first')
        self.independent_writer_renderer()
        claim = self.delivery.hook_claim('first', 'reg-first', 'session-first')
        self.assertEqual(claim['text'], 'Current task reminder')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM independent_write').fetchone()[0], 1)

    def test_claim_invalidated_during_render_cannot_begin_emission(self):
        self.agent('first')
        def render(*_):
            self.conn.execute("UPDATE attention_reminders SET state='cancelled' WHERE agent_id='first'")
            return 'Superseded reminder'
        self.delivery._text = render
        claim = self.delivery.hook_claim('first', 'reg-first', 'session-first')
        self.assertFalse(self.delivery.hook_begin('first', 'reg-first', claim))

    def test_chat_rendering_does_not_hold_database_writer(self):
        self.agent('first')
        self.expired('first', 1)
        self.conn.execute("UPDATE attention_reminders SET state='pending',route=NULL,claim_token=NULL,"
                          "claim_consumer_id=NULL,claim_binding_generation=NULL,claim_expires_at=NULL")
        self.conn.execute("ALTER TABLE native_session_owners ADD COLUMN desired_state TEXT DEFAULT 'running'")
        self.conn.execute('CREATE TABLE native_inputs (id TEXT, agent_id TEXT, source_key TEXT, state TEXT, '
                          'reason TEXT, evidence_json TEXT, attempt INTEGER)')
        self.independent_writer_renderer()
        session = {'id': 'binding', 'conversation_id': 'session-first'}
        chat = self.runtime.native_chat
        chat.probe = NS(observe=Mock(return_value=object()))
        chat.registry = NS(reconcile=Mock(return_value=NS(live=True, session=session)))
        chat.arbiter.admit_nudge = Mock(return_value={})
        self.delivery.todo_adjustment.send_due = Mock(return_value=False)
        self.delivery._settle_chat = Mock()
        advice = {'chat_eligible': True, 'kind': 'focus', 'due_at': EARLIER, 'reminder_sequence': 1}
        with patch('qorqut.agents.attention_delivery.clock_advice', return_value=advice), \
                patch('qorqut.native_chat.store.waiting_inputs', return_value=[]), \
                patch('qorqut.native_chat.store.live_session', return_value=session), \
                patch('qorqut.native_chat.store.input_by_key', return_value=None):
            self.delivery.send_due({'agent_id': 'first'})
        self.assertEqual(self.conn.execute('SELECT count(*) FROM independent_write').fetchone()[0], 1)
        chat.arbiter.admit_nudge.assert_called_once()

    def test_hook_only_reconciles_requesting_board_and_its_expired_claims(self):
        self.agent('requesting')
        self.expired('requesting', 10)
        self.expired('requesting', 11, begun=True)
        for index in range(60):
            agent = f'unrelated-{index}'
            self.agent(agent)
            self.expired(agent, 10)
            self.expired(agent, 11, begun=True)
        before = [dict(row) for row in self.conn.execute("SELECT * FROM agent_attention WHERE agent_id<>'requesting'")]
        claim = self.delivery.hook_claim('requesting', 'reg-requesting', 'session-requesting')
        self.assertEqual(claim['text'], 'Current task reminder')
        self.assertEqual(self.row('requesting-10')['state'], 'pending')
        self.assertEqual(self.row('requesting-11')['state'], 'uncertain')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attention_reminders WHERE agent_id<>'requesting' "
                                          "AND state='claimed'").fetchone()[0], 120)
        after = [dict(row) for row in self.conn.execute("SELECT * FROM agent_attention WHERE agent_id<>'requesting'")]
        self.assertEqual(before, after)
        self.assertEqual({call.args[1] for call in self.attention.board_mode.call_args_list}, {'requesting'})

    def test_background_pass_still_recovers_every_board(self):
        for agent in ('first', 'idle'):
            self.agent(agent)
            self.expired(agent, 10)
            self.expired(agent, 11, begun=True)
        with patch('qorqut.agents.attention.enrolled_boards', return_value=['first', 'idle']):
            with self.database.transaction() as uow:
                self.delivery.reconcile(uow)
        for agent in ('first', 'idle'):
            self.assertEqual(self.row(agent + '-10')['state'], 'pending')
            self.assertEqual(self.row(agent + '-11')['state'], 'uncertain')
            self.assertIsNotNone(self.conn.execute('SELECT 1 FROM attention_reminders WHERE agent_id=? AND sequence=0',
                                                   (agent,)).fetchone())

    def test_registration_and_native_consumer_checks_still_precede_work(self):
        self.agent('first')
        self.assertIsNone(self.delivery.hook_claim('first', 'wrong', 'session-first'))
        self.assertIsNone(self.delivery.hook_claim('first', 'reg-first', 'wrong-session'))
        self.delivery._verified_consumer.return_value = False
        self.assertIsNone(self.delivery.hook_claim('first', 'reg-first', 'session-first'))
        self.attention.board_mode.assert_not_called()

    def test_begun_expired_claim_settles_exactly_once_from_durable_receipt(self):
        self.agent('first')
        claim = self.delivery.hook_claim('first', 'reg-first', 'session-first')
        delivered = Mock()
        claims = HookClaims(lambda *_: True, delivered)
        with self.database.transaction() as uow:
            self.assertTrue(claims.begin(uow, claim, NOW))
            claims.recover_expired(uow, '2026-11-09T12:02:00Z', agent_id='first')
            receipt = {**claim, 'delivered_at': NOW}
            self.assertFalse(claims.settle(uow, {**receipt, 'claim_token': 'different'}, '2026-11-09T12:03:00Z'))
            self.assertTrue(claims.settle(uow, receipt, '2026-11-09T12:03:00Z'))
            self.assertTrue(claims.settle(uow, receipt, '2026-11-09T12:04:00Z'))
        delivered.assert_called_once()


class TodoReceiptTests(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.uow = NS(connection=self.database.connection)
        self.ctx = NS(principal_id='owner', principal_kind='owner')
        self.runtime = NS(application=NS(), focus=NS(inbox=NS()))
        self.attention = Attention(self.runtime)
        self.attention.clock = lambda: datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.attention.context = lambda ctx, args, uow: (ctx, 'board')
        self.attention.alias_authorize = Mock()
        self.attention.read = Mock(side_effect=AssertionError('full board read inside mutation'))
        self.task = {'created_at': EARLIER, 'revision': 3, 'worker_ids': [], 'archived': False}
        self.attention.board_task = Mock(return_value=self.task)
        self.ref = {'type': 'task', 'id': 'selected', 'business_id': None}
        self.addCleanup(patch.stopall)
        self.event = patch('qorqut.agents.attention.emit_event').start()

    def apply(self, name='todo_add', **extra):
        return self.attention.apply(self.ctx, NS(operation_name=name, arguments={'task': self.ref, **extra}), self.uow)

    def test_large_board_add_and_duplicate_only_return_selected_membership(self):
        for index in range(60):
            self.uow.connection.execute('INSERT INTO attention_memberships(id,agent_id,task_id,task_created_at,created_at) '
                'VALUES(?,\'board\',?,?,?)', (str(index), f'unrelated-{index}', EARLIER, EARLIER))
        result = self.apply()
        self.assertEqual(set(result), {'agent_id', 'membership_id', 'membership_revision', 'task', 'revision'})
        self.assertEqual(result['task'], self.ref)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['membership_revision'], 1)
        self.assertEqual(self.apply('attention_add'), result)
        self.assertEqual(self.uow.connection.execute('SELECT count(*) FROM attention_membership_events').fetchone()[0], 1)
        self.event.assert_called_once()
        self.assertTrue(all(call.args[2] == self.ref for call in self.attention.board_task.call_args_list))
        self.attention.read.assert_not_called()

    def test_expected_revision_and_task_authorization_remain_enforced(self):
        self.apply()
        with self.assertRaises(DomainError) as raised:
            self.apply(expected_revision=0)
        self.assertEqual(raised.exception.code, 'revision_conflict')
        self.attention.board_task.side_effect = DomainError('forbidden', 'Task is unavailable')
        with self.assertRaises(DomainError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, 'forbidden')
        self.event.assert_called_once()

    def test_agent_guidance_is_preserved_without_loading_the_board(self):
        self.ctx = NS(principal_kind='agent', principal_id='board')
        instructions = NS(render=Mock(side_effect=lambda name: name + '\n'))
        self.runtime.resolver = NS(require_agentic_view=lambda: NS(runtime_instructions=lambda: instructions))
        result = self.apply()
        self.assertIn('attention-board-transition', result['agent_hint'])
        self.assertIn('attention-next-actor', result['agent_hint'])
        self.attention.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
