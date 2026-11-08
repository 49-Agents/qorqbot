"""Synthetic inactivity passes through the real input arbiter and append-only ledger."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut_agentic.behavior.instructions import from_catalog
from qorqut_agentic.behavior.todo_adjustment import due
from bazarlyk.agents.todo_adjustment import TodoAdjustment
from bazarlyk.agents.attention_delivery import AttentionRuntime
from bazarlyk.native_chat.arbiter import InputArbiter
from bazarlyk.native_chat import schema, store
from bazarlyk.native_chat.contracts import InputRequest, TYPE_NUDGE
from bazarlyk.native_chat.probe import Observation, ProbeResult
from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome

AGENT = 'ordubashy'
START = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE principals (id TEXT PRIMARY KEY);
            INSERT INTO principals VALUES ('ordubashy');
            CREATE TABLE native_session_owners (agent_id TEXT PRIMARY KEY, consumer_id TEXT, generation INTEGER, desired_state TEXT);
            CREATE TABLE attention_memberships (agent_id TEXT);
            INSERT INTO attention_memberships VALUES ('ordubashy');
        ''')
        for ddl in schema.SCHEMA:
            self.connection.execute(ddl)
        self.connection.commit()

    @contextmanager
    def read(self):
        yield NS(connection=self.connection)

    @contextmanager
    def transaction(self, **_):
        with self.connection:
            yield NS(connection=self.connection)


class AdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.now = START
        self.database = Database()
        self.addCleanup(self.database.connection.close)
        self.state = dict(focus_generation=1, last_agent_activity_at=self.stamp(START - timedelta(minutes=16)),
                          last_owner_message_at=None)
        self.working, self.live, self.allowed = False, True, True
        self.coordinator = AGENT
        self.outcome = OwnerInputOutcome('submitted', 'processing_not_confirmed')
        self.before_checkpoint = lambda: None
        self.after_checkpoint = lambda: None
        self.sent = []
        self.profile = {'agent_id': AGENT}
        root = files('qorqut_agentic').joinpath('resources/defaults/operations/runtime-instructions')
        self.instructions = from_catalog(lambda path: root.joinpath(path).read_text())
        resolver = NS(require_agentic_view=lambda: NS(runtime_instructions=lambda: self.instructions))
        attention = NS(state=lambda *_: self.state, reminder_eligible=self.eligible)
        self.runtime = NS(database=self.database, application=NS(ordubashy_attention=attention,
            ensure_writes_open=lambda _: None, policy=NS(agent_resolver=resolver)))
        self.new_session()
        probe = NS(observe=lambda _: ProbeResult('live' if self.live else 'session_end', self.observation()))
        registry = NS(reconcile=lambda *_: NS(live=self.live, session=self.session if self.live else None))
        adapter = NS(name='synthetic', submit=self.submit)
        arbiter = InputArbiter(self.runtime, probe, registry, {'terminal': adapter, 'codex_queue': adapter})
        arbiter._profile = lambda _: self.profile
        self.runtime.native_chat = NS(probe=probe, registry=registry, arbiter=arbiter)
        self.review = TodoAdjustment(self.runtime, clock=lambda: self.stamp())
        self.addCleanup(patch.stopall)
        patch('bazarlyk.agents.todo_adjustment.configured_ordubashy', lambda _: self.coordinator).start()
        patch('bazarlyk.native_chat.store.utc_now', lambda: self.stamp()).start()

    def stamp(self, value=None):
        return (value or self.now).isoformat().replace('+00:00', 'Z')

    def new_session(self, provider='claude'):
        generation = getattr(self, 'session', {}).get('generation', 0) + 1
        self.session = dict(id=f'session-{generation}', agent_id=AGENT, provider=provider,
            conversation_id=f'conversation-{generation}', generation=generation,
            transport='terminal' if provider == 'claude' else 'codex_queue', process_pid=123,
            process_started='456', owner=None, transcript_path='/synthetic/transcript',
            observed_at=self.stamp(self.now - timedelta(hours=1)), verified_at=self.stamp(), ended_at=None)
        keys = list(self.session)
        self.database.connection.execute('INSERT INTO native_sessions (' + ','.join(keys) + ') VALUES ('
            + ','.join('?' for _ in keys) + ')', list(self.session.values()))
        self.database.connection.execute('INSERT OR REPLACE INTO native_session_owners VALUES (?,?,?,?)',
            (AGENT, self.session['conversation_id'], generation, 'running'))
        self.database.connection.commit()

    def observation(self):
        s = self.session
        return Observation(AGENT, 'claude-code' if s['provider'] == 'claude' else 'codex', s['provider'],
            s['conversation_id'], s['transcript_path'], s['process_pid'], s['process_started'],
            s['transport'], s['owner'], working=self.working)

    def eligible(self, uow, agent, **_):
        owner = uow.connection.execute('SELECT * FROM native_session_owners WHERE agent_id=?', (agent,)).fetchone()
        return self.allowed and self.coordinator == agent and owner['desired_state'] == 'running'

    def submit(self, profile, session, text, *, steer, echo, checkpoint, **_):
        self.assertFalse(steer)  # Daily cleanup must never interrupt or steer a running turn.
        self.assertTrue(echo)
        self.before_checkpoint()
        checkpoint()
        self.after_checkpoint()
        if self.outcome.status != 'deferred':
            self.sent.append(text)
        return self.outcome

    def rows(self):
        return [dict(r) for r in self.database.connection.execute('SELECT * FROM native_inputs ORDER BY sequence')]

    def send(self):
        return self.review.send_due(self.profile)

    def test_first_idle_pass_then_exact_24_hours_across_restart_and_session_change(self):
        self.assertTrue(self.send())
        first = self.rows()[0]
        self.assertEqual(first['state'], 'submitted')
        self.assertIsNone(first['body'])  # Cadence survives normal body scrubbing.
        self.assertIn('Run a careful Todo list adjustment', self.sent[0])
        self.assertIn('owner_requested=true', self.sent[0])
        self.review = TodoAdjustment(self.runtime, clock=lambda: self.stamp())
        self.now += timedelta(hours=24) - timedelta(microseconds=1)
        self.new_session('codex')
        self.assertFalse(self.send())
        self.now += timedelta(microseconds=1)
        self.assertTrue(self.send())
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(first['source_key'], self.rows()[-1]['source_key'])
        self.send()
        self.assertEqual(len(self.sent), 2)

    def test_waits_for_inactivity_and_owner_buffer_after_daily_deadline(self):
        self.send()
        self.now += timedelta(hours=25)
        self.state['last_agent_activity_at'] = self.stamp(self.now - timedelta(minutes=14))
        self.assertFalse(self.send())
        self.now += timedelta(minutes=1)
        self.state['last_owner_message_at'] = self.stamp()
        self.assertFalse(self.send())
        self.now += timedelta(minutes=30)
        self.assertTrue(self.send())
        self.assertEqual(len(self.sent), 2)

    def test_busy_offline_paused_empty_and_unauthorized_skip_without_receipts(self):
        self.working = True
        self.assertFalse(self.send())
        self.working, self.live = False, False
        self.assertFalse(self.send())
        self.live = True
        self.database.connection.execute("UPDATE native_session_owners SET desired_state='paused'")
        self.assertFalse(self.send())
        self.database.connection.execute("UPDATE native_session_owners SET desired_state='running'")
        self.allowed = False
        self.assertFalse(self.send())
        self.allowed = True
        self.coordinator = 'someone-else'
        self.assertFalse(self.send())
        self.coordinator = AGENT
        self.database.connection.execute('DELETE FROM attention_memberships')
        self.assertFalse(self.send())
        self.assertEqual(self.rows(), [])

    def test_done_only_board_and_stale_focus_can_be_adjusted(self):
        # Eligibility deliberately needs membership, not an open/working status.
        self.state.update(focus_json={'type': 'task', 'id': 'completed-task'})
        self.assertTrue(self.send())
        self.assertEqual(len(self.sent), 1)

    def test_busy_or_revoked_at_checkpoint_is_never_typed(self):
        self.before_checkpoint = lambda: setattr(self, 'working', True)
        self.send()
        self.assertEqual(self.rows(), [])
        self.working = False
        self.before_checkpoint = lambda: setattr(self, 'allowed', False)
        self.send()
        self.assertEqual(self.rows(), [])

    def test_changed_session_at_checkpoint_is_never_typed(self):
        self.before_checkpoint = self.new_session
        self.send()
        self.assertEqual(self.rows(), [])

    def test_known_not_typed_failure_retries_exact_text_without_spending_day(self):
        self.outcome = OwnerInputOutcome('deferred', 'composer_not_empty')
        self.send()
        failed = self.rows()[0]
        self.assertEqual(failed['state'], 'failed')
        self.instructions.document['messages']['todo-adjustment']['body'] = 'New authored instruction'
        self.outcome = OwnerInputOutcome('submitted', 'processing_not_confirmed')
        self.send()
        self.assertEqual(self.rows()[-1]['attempt'], 2)
        self.assertIn(failed['body'], self.sent[0])
        self.assertEqual(len(self.sent), 1)

    def test_unknown_delivery_is_not_replayed_but_next_day_is_independent(self):
        self.outcome = OwnerInputOutcome('uncertain', 'paste_unverified')
        self.send()
        first = self.rows()[0]
        self.now += timedelta(hours=23)
        self.assertFalse(self.send())
        self.assertEqual(len(self.sent), 1)
        self.now += timedelta(hours=1)
        self.outcome = OwnerInputOutcome('submitted', 'processing_not_confirmed')
        self.assertTrue(self.send())
        self.assertNotEqual(first['source_key'], self.rows()[-1]['source_key'])
        self.assertEqual(self.rows()[0]['state'], 'uncertain')

    def test_inflight_crash_waits_for_native_recovery(self):
        def crash():
            raise KeyboardInterrupt()
        self.after_checkpoint = crash
        with self.assertRaises(KeyboardInterrupt):
            self.send()
        self.assertEqual(self.rows()[0]['state'], 'submitting')
        self.now += timedelta(days=2)
        self.assertFalse(self.send())
        self.assertEqual(len(self.rows()), 1)

    def test_uncertain_ordinary_reminder_does_not_block_daily_adjustment(self):
        request = InputRequest(AGENT, TYPE_NUDGE, 'attention:old-review', 'Earlier board review')
        row = store.insert_input(self.database.connection, self.session, request, store.request_hash(request),
                                 state='submitting')
        store.settle_input(self.database.connection, row['id'], 'uncertain', 'paste_unverified', {}, ('submitting',))
        self.assertTrue(self.send())
        self.assertEqual([r['state'] for r in self.rows()], ['uncertain', 'submitted'])
        self.assertEqual(len(self.sent), 1)

    def test_waiting_owner_input_precedes_housekeeping(self):
        request = InputRequest(AGENT, TYPE_NUDGE, 'test-waiting', 'Pending input', wait=True)
        store.insert_input(self.database.connection, self.session, request, store.request_hash(request), state='waiting')
        self.assertFalse(self.send())
        self.assertEqual(len(self.rows()), 1)

    def test_missing_authored_instruction_leaves_ordinary_nudges_available(self):
        del self.instructions.document['messages']['todo-adjustment']
        self.assertFalse(self.send())
        self.assertEqual(self.rows(), [])

    def test_daily_invitation_preempts_regular_attention_for_same_idle_period(self):
        attention = AttentionRuntime(self.runtime)
        attention.todo_adjustment = self.review
        attention.send_due(self.profile)
        attention.send_due(self.profile)
        self.assertEqual(len(self.sent), 1)


class TimingTests(unittest.TestCase):
    def test_timezone_offsets_and_new_session_idle_boundary(self):
        self.assertFalse(due('2026-11-09T12:00:00Z', session_started_at='2026-11-09T11:50:00Z',
            last_nudge_at='2026-11-08T08:00:00-04:00'))
        self.assertTrue(due('2026-11-09T12:05:00Z', session_started_at='2026-11-09T11:50:00Z',
            last_nudge_at='2026-11-08T08:00:00-04:00'))


if __name__ == '__main__':
    unittest.main()
