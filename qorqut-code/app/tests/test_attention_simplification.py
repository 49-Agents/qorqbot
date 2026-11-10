"""Attention work stays scoped; membership mutations return durable small receipts."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
import sqlite3
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


if __name__ == '__main__':
    unittest.main()
