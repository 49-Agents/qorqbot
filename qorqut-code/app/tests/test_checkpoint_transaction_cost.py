"""Instruction IO precedes checkpoint transactions; authority and claims stay transactional."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from qorqut.agents.checkpoints import AgentCheckpoints
from qorqut.composition.schema_ddl import OWNER_SCHEMA
from qorqut_operations.contracts import DomainError


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        for statement in OWNER_SCHEMA['incidents']:
            if statement.startswith('CREATE TABLE agent_checkpoint_findings'):
                self.connection.execute(statement)
        self.connection.execute('CREATE TABLE native_session_owners(agent_id,consumer_id,generation)')
        self.agent, self.registration, self.session, self.turn = (str(uuid4()) for _ in range(4))
        self.ctx = NS(principal_id=self.agent, principal_kind='agent', business_id=None, inbox_generation=1)
        self.connection.execute('INSERT INTO native_session_owners VALUES(?,?,1)', (self.agent, self.session))
        self.row = {'session_id': self.session, 'registration_id': self.registration, 'runtime': 'claude-code',
                    'items_json': [], 'work_reviewed_turn': None, 'work_reviewed_sequence': 0}
        def repository(name, **kwargs):
            return NS(get=lambda *args, **kw: {'principal_id': self.agent}
                      if name == 'machine_registrations' else self.row)
        self.uow = NS(connection=self.connection, repository=repository)
        self.units = []
        self.active = None
        self.before_write = lambda: None
        @contextmanager
        def unit(kind):
            if kind == 'write':
                self.before_write()
            self.units.append(kind)
            self.active = kind
            self.connection.execute('BEGIN IMMEDIATE' if kind == 'write' else 'BEGIN')
            try:
                yield self.uow
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise
            finally:
                self.active = None
        database = NS(read=lambda: unit('read'), transaction=lambda: unit('write'))
        self.instructions = NS(render=lambda key, **values: key)
        def read_instructions():
            self.assertIsNone(self.active, 'Instruction IO must happen before the transaction')
            self.assertFalse(self.connection.in_transaction)
            return self.instructions
        self.load = Mock(side_effect=read_instructions)
        self.app = NS(database=database, policy=NS(agent_resolver=NS(
            require_agentic_view=lambda: NS(runtime_instructions=self.load))),
            current_context=Mock(side_effect=lambda ctx, uow: ctx), ensure_writes_open=Mock())
        self.checkpoints = AgentCheckpoints(NS(application=self.app, activity=NS(writer=nullcontext)),
            clock=lambda: datetime(2026, 11, 10, tzinfo=timezone.utc))
        self.native = {'turn_id': self.turn, 'root_turn_id': self.turn, 'dirty_sequence': 1}
        for target, value in [('qorqut.agents.checkpoints.native_checkpoint', self.native),
                              ('qorqut.management.identity.configured_ordubashy', 'coordinator'),
                              ('qorqut.agents.checkpoints.is_worker', False)]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self, **kwargs):
        return self.checkpoints.check(self.ctx, self.registration, self.session, self.turn, **kwargs)

    def test_one_instruction_read_for_both_transactions_and_correction_is_claimed_once(self):
        first = self.check(allow_correction=True, claim_id=str(uuid4()))
        self.assertEqual(first['correction'], 'completion-records')
        self.assertEqual(self.units, ['read', 'write'])
        self.load.assert_called_once()
        self.assertEqual(self.app.current_context.call_count, 2)
        self.app.ensure_writes_open.assert_called_once()
        self.load.reset_mock()
        self.units.clear()
        second = self.check(allow_correction=True, claim_id=str(uuid4()))
        self.assertIsNone(second['correction'])
        self.assertTrue(second['issues'][0]['correction_attempted'])
        self.assertEqual(self.units, ['read'])
        self.load.assert_called_once()
        self.assertEqual(self.connection.execute('SELECT count(*) FROM agent_checkpoint_findings').fetchone()[0], 1)

    def test_changed_source_is_rechecked_before_writing_or_claiming(self):
        def reviewed():
            self.row.update(work_reviewed_turn=self.turn, work_reviewed_sequence=1)
        self.before_write = reviewed
        result = self.check(allow_correction=True, claim_id=str(uuid4()))
        self.assertEqual(result['issues'], [])
        self.assertIsNone(result['correction'])
        self.assertEqual(self.connection.execute('SELECT count(*) FROM agent_checkpoint_findings').fetchone()[0], 0)
        self.load.assert_called_once()

    def test_session_supersession_between_read_and_write_still_refuses_claim(self):
        self.before_write = lambda: self.connection.execute(
            'UPDATE native_session_owners SET consumer_id=?,generation=2', (str(uuid4()),))
        with self.assertRaises(DomainError) as caught:
            self.check(allow_correction=True, claim_id=str(uuid4()))
        self.assertEqual(caught.exception.code, 'inbox_session_superseded')
        self.assertEqual(self.connection.execute('SELECT count(*) FROM agent_checkpoint_findings').fetchone()[0], 0)

    def test_unobserved_turn_does_not_load_instructions_or_open_transactions(self):
        self.native['turn_id'] = str(uuid4())
        self.assertEqual(self.check()['state'], 'unobserved')
        self.load.assert_not_called()
        self.assertEqual(self.units, [])

    def test_instruction_failure_never_opens_a_claim_transaction(self):
        self.load.side_effect = DomainError('agentic_unavailable', 'Recovery required')
        with self.assertRaises(DomainError):
            self.check()
        self.assertEqual(self.units, [])
