"""A worker's message to Ordubashy is bounded, and Ordubashy is told when a worker's session cannot take input."""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut.agents import unreachable
from qorqut.inbox.messages import MessageHost, WORKER_MESSAGE_LIMIT
from qorqut.native_chat.nudges import paced
from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox.messages import Messages

COORDINATOR, SCOUT, COMPASS, APPLIER, OWNER = (f'00000000-0000-4000-8000-00000000000{n}' for n in range(1, 6))
WORKERS = {SCOUT, COMPASS}
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
SCHEMA = (
    'CREATE TABLE agent_profiles(agent_id TEXT PRIMARY KEY,name TEXT,status TEXT,revision INTEGER)',
    'CREATE TABLE inbox_nudge_needs(agent_id TEXT PRIMARY KEY,active INTEGER,reason TEXT,updated_at TEXT)',
    'CREATE TABLE inbox_nudge_attempts(agent_id TEXT,sequence INTEGER,state TEXT,reason TEXT,prepared_at TEXT)',
    'CREATE TABLE inbox_events(id INTEGER PRIMARY KEY AUTOINCREMENT,source_type TEXT,source_id TEXT,event_type TEXT,'
    'summary TEXT,created_at TEXT)',
    'CREATE TABLE inbox_deliveries(agent_id TEXT,event_id INTEGER,acknowledged_at TEXT,required INTEGER)',
)


def stamp(value):
    return value.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def ago(**age):
    return stamp(NOW - timedelta(**age))


class UnreachableWorkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        for statement in SCHEMA:
            self.conn.execute(statement)
        for agent, name in ((COORDINATOR, 'ordubashy'), (SCOUT, 'scout'), (COMPASS, 'compass'), (APPLIER, 'learning-applier')):
            self.conn.execute('INSERT INTO agent_profiles VALUES(?,?,?,1)', (agent, name, 'active'))
        self.uow, self.now, self.coordinator, self.sent = NS(connection=self.conn), NOW, COORDINATOR, []

        def emit(uow, *, source_type, source_id, event_type, summary, required, **_):
            self.sent.append(list(required))
            return self.conn.execute('INSERT INTO inbox_events(source_type,source_id,event_type,summary,created_at) VALUES(?,?,?,?,?)',
                                     (source_type, source_id, event_type, summary, stamp(self.now))).lastrowid
        for target, value in (('emit_event', emit), ('configured_ordubashy', lambda uow: self.coordinator),
                              ('is_worker', lambda connection, agent: agent in WORKERS)):
            patch.object(unreachable, target, value).start()
        self.addCleanup(patch.stopall)

    def need(self, agent, active=1, reason=None, updated=None):
        self.conn.execute('INSERT OR REPLACE INTO inbox_nudge_needs VALUES(?,?,?,?)', (agent, active, reason, updated or ago(hours=5)))

    def attempts(self, agent, *rows):
        """Each row is ``(state, reason, minutes ago)``, oldest first."""
        start = self.conn.execute('SELECT COALESCE(MAX(sequence),0) FROM inbox_nudge_attempts WHERE agent_id=?', (agent,)).fetchone()[0]
        for offset, (state, reason, minutes) in enumerate(rows, 1):
            self.conn.execute('INSERT INTO inbox_nudge_attempts VALUES(?,?,?,?,?)', (agent, start + offset, state, reason, ago(minutes=minutes)))

    def told(self):
        return [row['agent_id'] for row in unreachable.reconcile(self.uow, now=self.now)]

    def test_a_worker_blocked_for_a_while_is_reported_once_with_the_repair(self):
        self.need(SCOUT)
        self.attempts(SCOUT, ('submitted', 'processing_not_confirmed', 90), ('deferred', 'composer_not_empty', 25),
                      ('deferred', 'composer_not_empty', 1))
        self.conn.executemany('INSERT INTO inbox_deliveries VALUES(?,?,?,1)', [(SCOUT, 1, None), (SCOUT, 2, None), (SCOUT, 3, ago(hours=1))])
        self.assertEqual(self.told(), [SCOUT])
        self.assertEqual(self.sent, [[COORDINATOR]])
        event = dict(self.conn.execute('SELECT * FROM inbox_events').fetchone())
        self.assertEqual((event['source_type'], event['source_id'], event['event_type']), ('agent_profiles', SCOUT, 'unreachable'))
        for part in ('scout cannot receive inbox notices since 2026-11-12 11:35 UTC', 'its input box holds unsent text',
                     '2 inbox items waiting', 'agent_terminal_status', 'agent_terminal_restart', 'reassign its open work'):
            self.assertIn(part, event['summary'])
        self.assertLessEqual(len(event['summary']), 500)
        # The same stretch is not reported again on the next pass, only after a full period.
        self.assertEqual(self.told(), [])
        self.now = NOW + unreachable.REPEAT_AFTER - timedelta(minutes=1)
        self.assertEqual(self.told(), [])
        self.now = NOW + unreachable.REPEAT_AFTER + timedelta(minutes=1)
        self.assertEqual(self.told(), [SCOUT])

    def test_a_short_stretch_a_busy_turn_and_a_recovered_worker_are_not_reported(self):
        self.need(SCOUT)
        self.attempts(SCOUT, ('deferred', 'composer_unrecognized', 10))
        self.assertEqual(self.told(), [])
        self.need(COMPASS)
        self.attempts(COMPASS, ('deferred', 'composer_busy', 60), ('deferred', 'composer_busy', 2))
        self.assertEqual(self.told(), [])
        # A notice that got through ends the stretch, and a superseded reservation says nothing either way.
        self.attempts(SCOUT, ('submitted', 'processing_not_confirmed', 3), ('stale', 'coverage_handled', 2))
        self.now = NOW + timedelta(hours=2)
        self.assertEqual(self.told(), [])

    def test_nothing_pending_means_nothing_to_report(self):
        self.need(SCOUT, active=0)
        self.attempts(SCOUT, ('deferred', 'composer_modal', 300), ('deferred', 'composer_modal', 1))
        self.assertEqual(self.told(), [])
        self.assertIsNone(unreachable.blocked(self.conn, COMPASS, NOW))

    def test_a_worker_without_a_session_is_reported(self):
        self.need(SCOUT, reason='offline', updated=ago(minutes=30))
        self.need(COMPASS, reason='offline', updated=ago(minutes=5))
        self.assertEqual(self.told(), [SCOUT])
        self.assertIn('it has no running session', self.conn.execute('SELECT summary FROM inbox_events').fetchone()[0])

    def test_a_new_stretch_after_a_recovery_is_reported_within_the_period(self):
        self.need(SCOUT)
        self.attempts(SCOUT, ('deferred', 'composer_unrecognized', 40))
        self.assertEqual(self.told(), [SCOUT])
        self.now = NOW + timedelta(hours=1)
        self.conn.execute('INSERT INTO inbox_nudge_attempts VALUES(?,?,?,?,?)', (SCOUT, 2, 'submitted', None, stamp(NOW + timedelta(minutes=5))))
        self.conn.execute('INSERT INTO inbox_nudge_attempts VALUES(?,?,?,?,?)', (SCOUT, 3, 'failed', None, stamp(NOW + timedelta(minutes=30))))
        self.assertEqual(self.told(), [SCOUT])
        self.assertIn(': failed.', self.conn.execute('SELECT summary FROM inbox_events ORDER BY id DESC').fetchone()[0])

    def test_only_permanent_workers_are_reported_and_only_to_a_configured_coordinator(self):
        for agent in (APPLIER, COORDINATOR, SCOUT):
            self.need(agent)
            self.attempts(agent, ('deferred', 'composer_unrecognized', 120))
        self.assertEqual(self.told(), [SCOUT])
        self.conn.execute('DELETE FROM inbox_events')
        self.coordinator = None
        self.assertEqual(self.told(), [])


class WorkerMessageLimitTests(unittest.TestCase):
    def setUp(self):
        for target, value in (('qorqut.agents.worker_access.is_worker', lambda connection, agent: agent in WORKERS),
                              ('qorqut.management.identity.configured_ordubashy', lambda uow: COORDINATOR)):
            patch(target, value).start()
        self.addCleanup(patch.stopall)
        self.host = MessageHost.__new__(MessageHost)
        self.uow = NS(connection=None)

    def limit(self, sender, recipient, kind='agent'):
        return self.host.message_limit(NS(principal_id=sender, principal_kind=kind), recipient, self.uow)

    def test_only_a_workers_message_to_ordubashy_is_bounded(self):
        self.assertEqual(self.limit(SCOUT, COORDINATOR), WORKER_MESSAGE_LIMIT)
        self.assertIsNone(self.limit(COORDINATOR, SCOUT))
        self.assertIsNone(self.limit(SCOUT, COMPASS))
        self.assertIsNone(self.limit(APPLIER, COORDINATOR))
        self.assertIsNone(self.limit(OWNER, COORDINATOR, kind='human'))

    def flow(self, host):
        messages = Messages.__new__(Messages)
        messages.app, messages.host = NS(current_context=lambda ctx, uow: ctx), host
        messages.peer = lambda ctx, recipient, uow, **_: 'peer'
        return messages

    def test_a_longer_message_is_refused_whole_and_names_where_the_detail_goes(self):
        ctx = NS(principal_id=SCOUT, principal_kind='agent', business_id=None)
        host = NS(binding=lambda ctx, uow: None, validate_recipient=lambda *a: None, message_limit=self.host.message_limit)
        send = lambda text, to=COORDINATOR: self.flow(host).validate(ctx, {'message': text, 'to_agent_id': to}, self.uow)
        self.assertEqual(send('x' * WORKER_MESSAGE_LIMIT)[1], 'peer')
        self.assertEqual(send('x' * 5000, to=COMPASS)[1], 'peer')
        with self.assertRaises(DomainError) as refused:
            send('x' * (WORKER_MESSAGE_LIMIT + 1))
        self.assertEqual(refused.exception.code, 'validation_error')
        for part in ('at most 1200 characters', 'this one has 1201', 'on the Task', 'linked file'):
            self.assertIn(part, refused.exception.message)
        # A host without the rule bounds nothing.
        del host.message_limit
        self.assertEqual(send('x' * 5000)[1], 'peer')


class PacedTests(unittest.TestCase):
    def test_a_paced_duty_runs_once_per_period_and_again_after_an_abandoned_unit(self):
        clock, calls = [0.0], []

        class Abandoned(BaseException):
            pass

        def duty(uow):
            calls.append(uow)
            if uow == 'read':
                raise Abandoned()
        run = paced(60, duty, monotonic=lambda: clock[0])
        with self.assertRaises(Abandoned):
            run('read')
        run('write')
        run('again')
        self.assertEqual(calls, ['read', 'write'])
        clock[0] = 61.0
        run('later')
        self.assertEqual(calls, ['read', 'write', 'later'])


if __name__ == '__main__':
    unittest.main()
