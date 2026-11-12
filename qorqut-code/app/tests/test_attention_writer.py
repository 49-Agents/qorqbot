"""Attention's routine checks leave SQLite's writer alone until they have something to record.

Every tool call asks whether a reminder is due, and a background pass asks the same for every board every few
seconds. Nearly all of those find nothing to do. These cases run them against a real store while a second connection
holds the writer, as another process or a long unit of this one would: a check that issued even one write statement,
changing nothing, would wait for that writer and fail here.
"""
from contextlib import nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut.agents.attention import Attention
from qorqut.agents.attention_delivery import AttentionRuntime
from qorqut.composition.assembly import host_assembly
from qorqut.native_chat.nudges import NudgeBackend
from qorqut.service.changes import ChangeFeed
from qorqut_operations.storage.database import Database

NOW = '2026-11-09T12:00:00.000000Z'
A_MINUTE_AGO = '2026-11-09T11:59:00.000000Z'
AN_HOUR_AGO = '2026-11-09T11:00:00.000000Z'
BUSY_TIMEOUT = 0.5   # how long a unit that does need the writer waits for it here
HELD = 0.15          # how long the other writer keeps it in the cases that let a unit through


class AttentionWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'operations.sqlite3'
        self.db = Database(self.path, assembly=host_assembly(ChangeFeed()), busy_timeout=BUSY_TIMEOUT).initialize_target()
        self.addCleanup(self.db.close)
        self.runtime = NS(database=self.db, application=NS(ensure_writes_open=lambda _: None,
            writer_admission=NS(writer=nullcontext)), focus=NS(inbox=NS()),
            native_chat=NS(arbiter=NS(locks=lambda _: nullcontext())))
        self.attention = Attention(self.runtime)
        self.runtime.application.ordubashy_attention = self.attention
        self.delivery = AttentionRuntime(self.runtime)
        self.delivery._now = lambda: NOW
        self.delivery._verified_consumer = Mock(return_value=True)
        self.delivery._text = lambda *_: 'Current task reminder'
        self.attention.board_mode = Mock(return_value='normal')
        self.attention.reminder_eligible = Mock(return_value=True)
        self.claim_unit = self.delivery._claim_due = Mock(wraps=self.delivery._claim_due)
        self.chat_unit = self.delivery._claim_chat = Mock(wraps=self.delivery._claim_chat)
        self.nudges = NudgeBackend(self.runtime, None, None, None, None, None, reconcile=self.delivery.reconcile)
        self.boards = []
        self.addCleanup(patch.stopall)
        patch('qorqut.agents.attention.enrolled_board', return_value=True).start()
        patch('qorqut.agents.attention.enrolled_boards', side_effect=lambda _: list(self.boards)).start()

    def raw(self):
        """A connection of another process, as far as SQLite's locks are concerned (and no foreign key checks, so a
        fixture needs only the rows its case reads)."""
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.create_function('inbox_actor', 0, lambda: None)   # the store's triggers name whoever wrote a row
        self.addCleanup(connection.close)
        return connection

    def board(self, agent, *, focused_since=AN_HOUR_AGO):
        connection = self.raw()
        connection.execute('INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,'
            "created_at,updated_at) VALUES(?,?,'ecosystem',?,?,'{}',?,?)", (agent, agent, agent, '/' + agent, NOW, NOW))
        focus = json.dumps({'type': 'task', 'id': 'task'}) if focused_since else None
        connection.execute('INSERT INTO agent_attention(agent_id,focus_json,focus_generation,focus_started_at,updated_at) '
            'VALUES(?,?,1,?,?)', (agent, focus, focused_since, NOW))
        connection.execute("INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation) "
            "VALUES(?,?,'{\"id\":\"binding\"}',1)", (agent, 'session-' + agent))
        connection.execute("INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime) "
            "VALUES(?,?,?,'claude-code')", (agent, 'session-' + agent, 'reg-' + agent))
        self.boards.append(agent)

    def expired_claim(self, agent, sequence, *, begun):
        identifier = f'{agent}-{sequence}'
        self.raw().execute('INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,'
            'state,route,claim_token,claim_consumer_id,claim_binding_generation,claim_expires_at,emission_started_at,'
            "created_at,updated_at) VALUES(?,?,'focus',1,?,?,?,'claimed','hook',?,?,1,?,?,?,?)",
            (identifier, agent, sequence, AN_HOUR_AGO, AN_HOUR_AGO, 'token-' + identifier, 'session-' + agent,
             AN_HOUR_AGO, AN_HOUR_AGO if begun else None, AN_HOUR_AGO, AN_HOUR_AGO))
        return identifier

    def reminders(self, where='1'):
        return [dict(row) for row in self.raw().execute('SELECT * FROM attention_reminders WHERE ' + where + ' ORDER BY id')]

    def hold_writer(self, release_after=None):
        """Another writer takes SQLite's write lock: for good, or for ``release_after`` seconds."""
        holder = self.raw()
        holder.execute('BEGIN IMMEDIATE')
        release = lambda: holder.in_transaction and holder.execute('ROLLBACK')
        self.addCleanup(release)
        if release_after is not None:
            timer = threading.Timer(release_after, release)
            timer.start()
            self.addCleanup(timer.join)
        return release

    def claim(self, agent):
        return self.delivery.hook_claim(agent, 'reg-' + agent, 'session-' + agent)

    def background_pass(self):
        """The pass reports its failures as warnings and carries on, so a pass that had to wait for the writer and
        gave up would otherwise look like one that had nothing to do."""
        with self.assertNoLogs('qorqut.native_chat.nudges', level='WARNING'):
            self.nudges._run_hook()

    # ----- the after-tool-call hook -----------------------------------------------------------------------------------

    def test_tool_call_with_no_reminder_due_does_not_wait_for_the_writer(self):
        self.board('worker', focused_since=A_MINUTE_AGO)
        self.assertIsNone(self.claim('worker'))        # the first call arms the clock and records the occurrence
        self.assertEqual([row['state'] for row in self.reminders()], ['pending'])
        self.attention.reminder_eligible.reset_mock()
        self.hold_writer()
        started = time.monotonic()
        for _ in range(3):
            self.assertIsNone(self.claim('worker'))
        self.assertLess(time.monotonic() - started, BUSY_TIMEOUT)
        self.assertEqual(self.claim_unit.call_count, 4)                # no call had to be run a second time
        self.attention.reminder_eligible.assert_not_called()           # authority is not established for nothing
        self.attention.board_mode.assert_not_called()                  # nor is a Focused board's mode derived

    def test_due_reminder_is_claimed_exactly_once_when_another_writer_is_active(self):
        self.board('worker')
        self.hold_writer(release_after=HELD)
        claim = self.claim('worker')
        self.assertEqual(claim['kind'], 'focus')
        self.assertEqual(claim['text'], 'Current task reminder')
        self.assertEqual(self.claim_unit.call_count, 2)                # refused at its first write, then run again
        claimed = self.reminders("state='claimed'")
        self.assertEqual([(row['id'], row['claim_token'], row['route']) for row in claimed],
                         [(claim['id'], claim['claim_token'], 'hook')])
        self.assertEqual(len(self.reminders()), 1)
        self.assertIsNone(self.claim('worker'))                        # and it is not handed out a second time

    def test_tool_call_that_cannot_get_the_writer_reports_it_instead_of_dropping_the_reminder(self):
        from qorqut_operations.contracts import DomainError
        self.board('worker')
        release = self.hold_writer()
        with self.assertRaises(DomainError) as raised:
            self.claim('worker')
        self.assertEqual(raised.exception.code, 'busy')
        release()
        self.assertEqual(self.reminders(), [])                         # nothing half-recorded by the abandoned attempt
        self.assertEqual(self.claim('worker')['kind'], 'focus')

    # ----- the background pass ----------------------------------------------------------------------------------------

    def test_background_pass_with_nothing_to_record_does_not_wait_for_the_writer(self):
        self.board('focused', focused_since=A_MINUTE_AGO)
        self.board('unfocused', focused_since=None)
        self.background_pass()                         # the first pass arms both clocks and records both occurrences
        self.assertEqual(sorted(row['kind'] for row in self.reminders()), ['board_normal', 'focus'])
        before = self.reminders()
        self.hold_writer()
        started = time.monotonic()
        for _ in range(3):
            self.background_pass()
        self.assertLess(time.monotonic() - started, BUSY_TIMEOUT)
        self.assertEqual(self.reminders(), before)

    def test_background_pass_still_records_what_it_finds_when_another_writer_is_active(self):
        self.board('worker')
        lost = self.expired_claim('worker', 10, begun=False)
        begun = self.expired_claim('worker', 11, begun=True)
        self.hold_writer(release_after=HELD)
        self.background_pass()
        states = {row['id']: row['state'] for row in self.reminders()}
        self.assertEqual((states[lost], states[begun]), ('pending', 'uncertain'))
        self.assertEqual(len(states), 3)                               # plus the current occurrence, recorded once

    def test_occurrence_whose_deadline_moved_is_still_updated(self):
        self.board('worker', focused_since=A_MINUTE_AGO)
        self.background_pass()
        before = self.reminders()[0]
        with self.db.transaction() as uow:                             # the owner writes: chat must now wait longer
            from qorqut.agents.attention_delivery import clock_advice
            advice = clock_advice(uow, 'worker', self.attention, NOW, event='owner_message', event_at=NOW)
        self.background_pass()
        after = self.reminders()[0]
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['not_before'], advice['chat_not_before'])
        self.assertNotEqual(after['not_before'], before['not_before'])
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.hold_writer()
        self.background_pass()                                         # and the settled occurrence is left alone again

    # ----- the chat route ---------------------------------------------------------------------------------------------

    def chat(self, agent, session):
        with patch('qorqut.native_chat.store.live_session', return_value=session), \
                patch('qorqut.native_chat.store.waiting_inputs', return_value=[]), \
                patch('qorqut.native_chat.store.input_by_key', return_value=None):
            self.delivery.send_due({'agent_id': agent})

    def test_chat_pass_with_no_reminder_due_does_not_wait_for_the_writer(self):
        self.board('worker', focused_since=A_MINUTE_AGO)
        session = {'id': 'binding', 'conversation_id': 'session-worker'}
        self.runtime.native_chat.arbiter.admit_nudge = Mock()
        self.chat('worker', session)
        self.hold_writer()
        started = time.monotonic()
        for _ in range(3):
            self.chat('worker', session)
        self.assertLess(time.monotonic() - started, BUSY_TIMEOUT)
        self.assertEqual(self.chat_unit.call_count, 4)
        self.runtime.native_chat.arbiter.admit_nudge.assert_not_called()

    def test_due_chat_reminder_is_claimed_and_typed_once_when_another_writer_is_active(self):
        self.board('worker')
        self.background_pass()                                         # records the occurrence, due long ago
        session = {'id': 'binding', 'conversation_id': 'session-worker'}
        chat = self.runtime.native_chat
        chat.probe = NS(observe=Mock(return_value=object()))
        chat.registry = NS(reconcile=Mock(return_value=NS(live=True, session=session)))
        chat.arbiter.admit_nudge = Mock(return_value={})
        self.delivery._settle_chat = Mock()
        self.hold_writer(release_after=HELD)
        self.chat('worker', session)
        self.assertEqual(self.chat_unit.call_count, 2)                 # refused at the claim, then run again
        claimed = self.reminders("state='claimed'")
        self.assertEqual([(row['route'], row['claim_consumer_id']) for row in claimed], [('chat', 'session-worker')])
        chat.arbiter.admit_nudge.assert_called_once()
        self.assertEqual(chat.arbiter.admit_nudge.call_args.args[:3],
                         ('worker', 'attention:' + claimed[0]['id'], 'Current task reminder'))


if __name__ == '__main__':
    unittest.main()
