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
from qorqut.agents.attention_delivery import AttentionRuntime, HookClaims, clock_advice
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

    def write_if_needed(self, body, **_):
        with self.transaction() as uow:
            return body(uow)


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
        self.assertEqual({call.args[1] for call in self.attention.board_mode.call_args_list} - {'requesting'}, set())

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

    def test_background_pass_skips_a_board_that_cannot_be_read(self):
        # One board whose mode could not be derived used to raise out of the write unit that every board and every
        # timed duty shares, on every pass. It is now rolled back and skipped alone.
        for agent in ('broken', 'idle'):
            self.agent(agent)
        self.conn.execute('UPDATE agent_attention SET focus_json=NULL,focus_generation=2,focus_started_at=NULL')
        self.conn.commit()

        def mode(_uow, agent):
            if agent == 'broken':
                self.conn.execute("UPDATE agent_attention SET board_mode='reassess' WHERE agent_id='broken'")
                raise DomainError('business_archived', 'This project is archived')
            return 'normal'
        self.attention.board_mode = Mock(side_effect=mode)
        with patch('qorqut.agents.attention.enrolled_boards', return_value=['broken', 'idle']):
            with self.assertLogs('qorqut.native_chat.nudges', 'WARNING') as logs:
                with self.database.transaction() as uow:
                    self.delivery.reconcile(uow)
        self.assertEqual([record.getMessage() for record in logs.records],
                         ['Board reminder reconcile for broken failed and was skipped: business_archived'])
        reminders = {row['agent_id']: row['kind'] for row in self.conn.execute('SELECT agent_id,kind FROM attention_reminders')}
        self.assertEqual(reminders, {'idle': 'board_normal'})
        modes = dict(self.conn.execute('SELECT agent_id,board_mode FROM agent_attention').fetchall())
        self.assertEqual(modes, {'broken': 'empty', 'idle': 'normal'})    # the failed board's partial write is undone

    def test_background_pass_still_stops_when_writes_are_closed(self):
        self.agent('first')
        self.conn.execute('UPDATE agent_attention SET focus_json=NULL,focus_generation=2,focus_started_at=NULL')
        self.attention.board_mode = Mock(side_effect=DomainError('maintenance', 'Writes are unavailable'))
        with patch('qorqut.agents.attention.enrolled_boards', return_value=['first']):
            with self.assertRaises(DomainError):
                self.delivery.reconcile(NS(connection=self.conn))

    def test_archiving_a_project_takes_its_tasks_off_every_board(self):
        for agent in ('first', 'second'):
            self.agent(agent)
        entries = (('m1', 'first', 'project', 'one'), ('m2', 'first', 'project', 'two'), ('m3', 'first', 'other', 'one'),
                   ('m4', 'first', None, 'one'), ('m5', 'second', 'project', 'one'))
        for identifier, agent, business, task in entries:
            self.conn.execute('INSERT INTO attention_memberships(id,agent_id,task_business_id,task_id,task_created_at,'
                              'created_at) VALUES(?,?,?,?,?,?)', (identifier, agent, business, task, EARLIER, EARLIER))
        work = json.dumps({'type': 'task', 'id': 'one', 'business_id': 'project'})
        self.conn.execute("UPDATE agent_attention SET work_task_json=? WHERE agent_id='first'", (work,))
        self.conn.execute("UPDATE agent_attention SET work_task_json=? WHERE agent_id='second'",
                          (json.dumps({'type': 'task', 'id': 'one', 'business_id': 'other'}),))
        self.conn.execute('INSERT INTO attention_workflows(agent_id,run_id,consumer_id,work_task_json,workflow_ref,'
                          "workflow_name,updated_at) VALUES('first','run','session-first',?,'ref','Example',?)", (work, EARLIER))
        before = {row['agent_id']: row['revision'] for row in self.conn.execute('SELECT * FROM agent_attention')}
        with self.database.transaction() as uow:
            self.assertEqual(self.attention.release_project(uow, 'project', 'owner'), 3)
        left = [row[0] for row in self.conn.execute('SELECT id FROM attention_memberships ORDER BY id')]
        self.assertEqual(left, ['m3', 'm4'])
        history = [tuple(row) for row in self.conn.execute('SELECT membership_id,agent_id,task_business_id,task_id,actor_id,'
            'event_type,rationale FROM attention_membership_events ORDER BY membership_id')]
        self.assertEqual(history, [(identifier, agent, 'project', task, 'owner', 'removed', 'Project archived')
                                   for identifier, agent, business, task in entries if business == 'project'])
        first, second = (self.attention.state(NS(connection=self.conn), agent) for agent in ('first', 'second'))
        self.assertEqual((first['focus_json'], first['work_task_json']), (None, None))     # its focus was on that project
        self.assertIsNotNone(second['focus_json'])                                         # a focus elsewhere is kept
        self.assertTrue(all(state['revision'] > before[state['agent_id']] for state in (first, second)))
        self.assertIsNotNone(self.conn.execute("SELECT detached_at FROM attention_workflows WHERE run_id='run'").fetchone()[0])
        with self.database.transaction() as uow:
            self.assertEqual(self.attention.release_project(uow, 'project', 'owner'), 0)   # nothing left to remove

    def test_registration_and_native_consumer_checks_still_precede_work(self):
        self.agent('first')
        self.assertIsNone(self.delivery.hook_claim('first', 'wrong', 'session-first'))
        self.assertIsNone(self.delivery.hook_claim('first', 'reg-first', 'wrong-session'))
        self.delivery._verified_consumer.return_value = False
        self.assertIsNone(self.delivery.hook_claim('first', 'reg-first', 'session-first'))
        self.attention.board_mode.assert_not_called()
        # The clock was not advanced and no occurrence recorded for a caller that is not this board's session.
        self.assertEqual(self.conn.execute('SELECT count(*) FROM attention_reminders').fetchone()[0], 0)
        self.assertIsNone(self.conn.execute("SELECT focus_due_at FROM agent_attention WHERE agent_id='first'").fetchone()[0])

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

    def test_clock_poll_does_not_write_unchanged_values_but_records_real_events(self):
        self.agent('first')
        uow = NS(connection=self.conn)
        first = clock_advice(uow, 'first', self.attention, NOW)
        before = self.conn.total_changes
        again = clock_advice(uow, 'first', self.attention, NOW)
        self.assertEqual(again, first)
        self.assertEqual(self.conn.total_changes, before)
        event = clock_advice(uow, 'first', self.attention, NOW, event='agent_activity', event_at=NOW)
        self.assertGreater(self.conn.total_changes, before)
        self.assertEqual(self.attention.state(uow, 'first')['last_agent_activity_at'], NOW)
        before = self.conn.total_changes
        self.assertEqual(clock_advice(uow, 'first', self.attention, NOW,
                                     event='agent_activity', event_at=NOW), event)
        self.assertEqual(self.conn.total_changes, before)
        delivered = clock_advice(uow, 'first', self.attention, NOW,
                                 event='delivered', delivered_kind='focus')
        self.assertEqual(delivered['reminder_sequence'], first['reminder_sequence'] + 1)
        self.assertEqual(self.attention.state(uow, 'first')['focus_due_at'], delivered['focus_due_at'])

    def test_board_mode_is_derived_only_for_an_unfocused_board(self):
        # Deriving the mode authorizes and reads every Task on the board. A Focused board's clock takes nothing from
        # it, so it is not derived; the mode stored meanwhile may be out of date, and leaving focus must not trust it.
        self.agent('first')
        self.conn.execute("UPDATE agent_attention SET board_mode='reassess' WHERE agent_id='first'")
        uow = NS(connection=self.conn)
        focused = clock_advice(uow, 'first', self.attention, NOW)
        self.assertEqual((focused['kind'], focused['board_due_at']), ('focus', None))
        self.attention.board_mode.assert_not_called()
        self.assertEqual(self.attention.state(uow, 'first')['board_mode'], 'reassess')
        self.conn.execute("UPDATE agent_attention SET focus_json=NULL,focus_generation=2,focus_started_at=NULL "
                          "WHERE agent_id='first'")
        unfocused = clock_advice(uow, 'first', self.attention, NOW)
        self.attention.board_mode.assert_called_once_with(uow, 'first')
        self.assertEqual((unfocused['kind'], unfocused['board_mode']), ('board_normal', 'normal'))
        self.assertEqual(unfocused['board_due_at'], '2026-11-09T12:03:00.000000Z')   # armed from now, in the real mode
        self.assertEqual(self.attention.state(uow, 'first')['board_mode'], 'normal')

    def test_claim_checks_the_deadline_before_establishing_authority(self):
        # Both must hold for a claim. The deadline is a few reads; authority resolves policy and reads the Task.
        self.agent('first')
        self.attention.reminder_eligible = Mock(return_value=True)
        uow = NS(connection=self.conn)
        self.delivery.reconcile_board(uow, 'first', NOW)
        row = self.conn.execute("SELECT * FROM attention_reminders WHERE agent_id='first'").fetchone()
        self.assertFalse(self.delivery._hook_eligible(uow, row, 'session-first', 1, '2026-11-09T11:04:00.000000Z'))
        self.attention.reminder_eligible.assert_not_called()
        self.assertTrue(self.delivery._hook_eligible(uow, row, 'session-first', 1, NOW))
        self.attention.reminder_eligible.assert_called_once()
        self.attention.reminder_eligible.return_value = False
        self.assertFalse(self.delivery._hook_eligible(uow, row, 'session-first', 1, NOW))
        with patch('qorqut.agents.attention.enrolled_board', return_value=False):
            before = self.conn.total_changes
            self.assertFalse(self.delivery._hook_eligible(uow, row, 'session-first', 1, NOW))
            self.assertEqual(self.conn.total_changes, before)       # a board that is gone has no clock to advance


class TodoReceiptTests(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.uow = NS(connection=self.database.connection)
        self.ctx = NS(principal_id='owner', principal_kind='owner')
        self.runtime = NS(application=NS(), focus=NS(inbox=NS()), database=self.database)
        self.attention = Attention(self.runtime)
        self.attention.clock = lambda: datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        self.attention.context = lambda ctx, args, uow: (ctx, 'board')
        self.attention.alias_authorize = Mock()
        self.attention.read = Mock(side_effect=AssertionError('full board read inside mutation'))
        # The board's agent is already the Task's recorded worker, so adding writes the entry and nothing on the Task.
        self.task = {'created_at': EARLIER, 'revision': 3, 'worker_ids': ['board'], 'archived': False}
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
        self.assertNotIn('agent_hint', result)
        instructions.render.assert_not_called()
        self.task['worker_ids'] = []
        hint = self.attention.mutation_hint(self.ctx, {'task': self.ref}, 'todo_add')
        self.assertIn('attention-board-transition', hint)
        self.assertIn('attention-next-actor', hint)
        self.attention.read.assert_not_called()

    def test_advisory_reads_current_authorized_task_and_skips_owner_calls(self):
        self.assertIsNone(self.attention.mutation_hint(self.ctx, {}, 'todo_add'))
        self.ctx = NS(principal_kind='agent', principal_id='board')
        self.attention.board_task.side_effect = DomainError('forbidden', 'Task no longer available')
        with self.assertRaises(DomainError):
            self.attention.mutation_hint(self.ctx, {'task': self.ref}, 'todo_add')
        # No resolver exists in this fixture: denied task reads precede rendering.


if __name__ == '__main__':
    unittest.main()
