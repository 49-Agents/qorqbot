"""The coordinator's reminders back off (5, then 60, then 180 minutes) while nothing changes; a change starts over."""
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut.agents.attention_delivery import _backoff
from qorqut_agentic.behavior.attention_clock import advance

START = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
at = lambda minutes: (START + timedelta(minutes=minutes)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class ClockBackoffTests(unittest.TestCase):
    def setUp(self):
        self.state = {'board_mode': 'normal', 'focus_generation': 1}

    def step(self, minutes, *, event=None, backoff=None, focused=True, mode='normal'):
        state = dict(self.state, **({'delivered_kind': 'focus' if focused else 'board_' + mode} if event == 'delivered' else {}))
        result = advance(state, now=at(minutes), mode=mode, focused=focused, focus_generation=1, focus_started_at=at(0),
                         event=event, backoff=backoff)
        self.state = {key: result[key] for key in ('board_mode', 'board_due_at', 'focus_due_at', 'last_agent_activity_at',
                                                    'last_owner_message_at', 'focus_generation', 'reminder_sequence')}
        return result['due_at']

    def test_focus_reminders_run_five_sixty_then_one_hundred_eighty_minutes(self):
        quiet = lambda previous: {'previous_delivery_at': previous, 'changed_at': at(0)}
        self.assertEqual(self.step(0, backoff=quiet(None)), at(5))
        self.assertEqual(self.step(5, event='delivered', backoff=quiet(None)), at(65))
        self.assertEqual(self.step(65, event='delivered', backoff=quiet(at(5))), at(245))
        self.assertEqual(self.step(245, event='delivered', backoff=quiet(at(65))), at(425))

    def test_a_change_brings_the_next_reminder_forward_and_starts_over(self):
        self.step(0, backoff={'previous_delivery_at': None, 'changed_at': at(0)})
        self.step(5, event='delivered', backoff={'previous_delivery_at': None, 'changed_at': at(0)})
        self.step(65, event='delivered', backoff={'previous_delivery_at': at(5), 'changed_at': at(0)})
        changed = {'previous_delivery_at': at(65), 'changed_at': at(100)}
        self.assertEqual(self.step(101, backoff=changed), at(105))
        self.assertEqual(self.step(105, event='delivered', backoff=changed), at(165))        # starts over at 60
        # A reminder that came early because of a change no longer remembered also starts over.
        self.assertEqual(self.step(165, event='delivered', backoff={'previous_delivery_at': at(160), 'changed_at': None}), at(225))

    def test_an_unfocused_board_backs_off_the_same_way(self):
        quiet = lambda previous: {'previous_delivery_at': previous, 'changed_at': None}
        self.assertEqual(self.step(0, focused=False, backoff=quiet(None)), at(3))
        self.assertEqual(self.step(3, event='delivered', focused=False, backoff=quiet(None)), at(63))
        self.assertEqual(self.step(63, event='delivered', focused=False, backoff=quiet(at(3))), at(243))

    def test_a_worker_keeps_the_five_minute_focus_interval(self):
        self.step(0)
        self.assertEqual(self.step(5, event='delivered'), at(10))
        self.assertEqual(self.step(10, event='delivered'), at(15))


class BackoffInputTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript('''
            CREATE TABLE attention_reminders (agent_id TEXT, kind TEXT, state TEXT, delivered_at TEXT);
            CREATE TABLE inbox_events (id INTEGER PRIMARY KEY, actor_id TEXT, created_at TEXT);
            CREATE TABLE inbox_deliveries (agent_id TEXT, event_id INTEGER, required INTEGER);''')
        self.uow = NS(connection=self.conn)
        patch('qorqut.management.identity.configured_ordubashy', return_value='coordinator').start()
        self.addCleanup(patch.stopall)
        self.conn.execute("INSERT INTO attention_reminders VALUES('coordinator','focus','submitted',?)", (at(60),))
        self.conn.execute("INSERT INTO attention_reminders VALUES('coordinator','focus','cancelled',?)", (at(90),))
        self.state = {'last_owner_message_at': at(10), 'focus_started_at': at(0), 'last_agent_activity_at': at(61)}

    def read(self, event=None, moment=120, agent='coordinator'):
        return _backoff(self.uow, agent, self.state, event, at(moment))

    def test_only_the_coordinator_backs_off(self):
        self.assertIsNone(self.read(agent='worker'))

    def test_a_short_reply_to_a_reminder_is_not_a_change_but_later_work_is(self):
        self.assertEqual(self.read(), {'previous_delivery_at': at(60), 'changed_at': at(10)})
        self.state['last_agent_activity_at'] = at(64)
        self.assertEqual(self.read()['changed_at'], at(64))
        self.assertEqual(self.read('agent_activity', 100)['changed_at'], at(100))

    def test_an_owner_message_and_someone_elses_inbox_event_are_changes(self):
        self.assertEqual(self.read('owner_message', 110)['changed_at'], at(110))
        self.conn.execute("INSERT INTO inbox_events VALUES(1,'worker',?)", (at(80),))
        self.conn.execute("INSERT INTO inbox_events VALUES(2,'coordinator',?)", (at(95),))
        self.conn.execute("INSERT INTO inbox_events VALUES(3,'worker',?)", (at(99),))
        self.conn.executemany("INSERT INTO inbox_deliveries VALUES('coordinator',?,?)", ((1, 1), (2, 1), (3, 0)))
        self.assertEqual(self.read()['changed_at'], at(80))                # not his own event, nor one he only follows

    def test_the_reminder_being_delivered_is_not_its_own_predecessor(self):
        self.assertIsNone(self.read('delivered', 60)['previous_delivery_at'])


if __name__ == '__main__':
    unittest.main()
