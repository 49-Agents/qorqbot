"""Qorqut's own compaction of the coordinator's conversation: when the handoff is asked for, what happens when it is filed or
not, how the conversation is replaced, and how a flow continues after the service or the session went away. The filing tool and
its storage are real; the managed session and the opener are stand-ins."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.native_chat import compaction
from qorqut.native_chat.compaction import CompactionController, limit, transcript_tail

from test_compaction_handoff import HandoffCase

OLD, NEW = 'aaaaaaaa-0000-4000-8000-000000000001', 'bbbbbbbb-0000-4000-8000-000000000002'


class Session:
    """One running session as its host answers for it."""

    def __init__(self, session, transcript=None):
        self.record, self.client, self.transcript = {'session_id': session}, self, transcript
        self.usage, self.phase, self.refuse, self.kind = None, 'idle', False, 'claude'
        self.sent, self.ledger, self.interrupts = [], {}, 0

    def status(self):
        return {'status': 'ok', 'state': 'ready', 'phase': self.phase, 'session_id': self.record['session_id'],
                'transcript_path': self.transcript, 'usage': self.usage, 'client': self.kind}

    def send(self, text, message_uuid, steer=False):
        if self.refuse:
            return {'status': 'rejected', 'reason': 'host_busy'}
        if message_uuid not in self.ledger:
            self.ledger[message_uuid] = False
            self.sent.append(text)
            self.phase = 'working'
        return {'status': 'ok'}

    def lookup(self, message_uuid, fence=False):
        return {'status': 'ok', 'seen': message_uuid in self.ledger, 'finished': bool(self.ledger.get(message_uuid))}

    def interrupt(self):
        self.interrupts += 1
        return {'status': 'ok'}

    def end_turn(self):
        self.ledger = {key: True for key in self.ledger}
        self.phase = 'idle'


class Managed:
    def __init__(self):
        self.host, self.closed = None, []

    def handle(self, agent_id):
        return self.host

    def close(self, agent_id, mode='graceful'):
        self.closed.append(mode)
        self.host = None
        return True


class Opener:
    """``SessionOpener.replace`` as the controller sees it (the real one is tested in ``test_managed_wake``)."""

    def __init__(self, managed):
        self.managed, self.replaced, self.texts = managed, [], []
        self.off, self.fail, self.session, self.on_close = False, None, NEW, None

    def replace(self, profile, closing, compose, key, *, close_seconds=45.0):
        if self.off:
            raise DomainError('agent_off', 'This agent is turned off')
        self.replaced.append((profile, closing, key))
        if self.managed.host is not None and self.managed.host.record['session_id'] == closing:
            self.managed.close(profile['agent_id'], 'graceful')
        if self.on_close:
            self.on_close()
        text = compose()
        if self.fail:
            raise DomainError(self.fail, 'The session could not be started', retryable=True)
        self.managed.host = Session(self.session)
        if self.session == closing:
            raise DomainError('not_replaced', 'The closed conversation was opened again', retryable=True)
        self.texts.append(text)
        return self.session


class NativeCompactionTests(HandoffCase):
    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        self.managed = Managed()
        self.opener = Opener(self.managed)
        self.managed.host = self.session = Session(OLD)
        self.runtime = NS(database=self.db, application=self.app, compaction_handoff=self.tool, paths=None,
                          native_chat=NS(managed=self.managed, opener=self.opener))
        with self.db.transaction() as uow:
            uow.connection.execute('PRAGMA defer_foreign_keys=ON')
            self.configure(uow)
        self.controller = self.make()

    def configure(self, uow):
        """The lead is the installation's configured coordinator."""
        from qorqut.management.identity import configured_ordubashy
        if configured_ordubashy(uow) != self.lead:
            self.fail('the fixture has another coordinator than the lead')

    def make(self, **more):
        controller = CompactionController(self.runtime, **{'mode': 'on', 'clock': lambda: self.clock,
                                                           'spawn': lambda target, *args: target(*args), **more})
        controller._context = lambda agent_id, uow: self.ctx(agent_id)
        controller._profile = lambda agent_id, uow: {'agent_id': agent_id}
        return controller

    def step(self, seconds=6.0, controller=None):
        self.clock += seconds
        self.now += timedelta(seconds=seconds)
        (controller or self.controller).step()

    def fill(self, tokens=310_000, window=1_000_000):
        self.session.usage = {'tokens': tokens, 'window': window, 'at': 'now'}

    def file_handoff(self):
        return self.filed([{'id': 'm1', 'status': 'partly', 'note': 'Liam is paid; Sanzida is not.', 'task_id': self.pay,
                            'next_step': 'Lead pays Sanzida'}])

    # -- when ---------------------------------------------------------------------------------------------------------

    def test_the_limit_is_the_earlier_of_seventy_percent_and_45k_before_the_end_of_the_working_window(self):
        self.assertEqual(limit(), 300_000)
        self.assertEqual(limit(1_000_000), 300_000)        # the model holds far more: the limit stands
        self.assertEqual(limit(200_000), 140_000)          # a smaller model limit wins
        self.assertEqual(limit(100_000), 55_000)           # 45,000 before the end comes before 70% here
        self.assertEqual(limit('unknown'), 300_000)

    def test_nothing_is_asked_below_the_limit_without_a_reading_or_while_it_is_off(self):
        self.say('Pay the creators today.', 10)
        self.step()                                        # the client has reported nothing yet
        self.fill(299_999)
        self.step()
        self.assertEqual((self.session.sent, self.rows()), ([], []))
        self.fill(300_000)
        off = self.make(mode='off')
        off.step()
        self.assertEqual(self.session.sent, [])
        self.assertFalse(CompactionController(self.runtime).enabled)     # off unless the service's environment says so
        self.step()
        self.assertEqual(len(self.session.sent), 1)

    def test_a_session_of_another_client_keeps_its_own_compaction(self):
        self.say('Pay the creators today.', 10)
        self.session.kind = 'codex'
        self.fill()
        self.step()
        self.assertEqual((self.session.sent, self.rows()), ([], []))

    def test_at_the_limit_the_handoff_is_asked_for_once_and_the_row_says_why(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        request, = self.session.sent
        self.assertTrue(request.startswith('<qorqut-handoff-request>\nYour context is nearly full. Stop the work'))
        self.assertTrue(request.endswith('m1 [2026-11-18 09:10 UTC, chat] Pay the creators today.\n</qorqut-handoff-request>'))
        row, = self.rows()
        self.assertEqual((row['reason'], row['filed_at'], row['request_json']), ('context', None, {
            'tokens': 310_000, 'window': 1_000_000, 'limit': 300_000, 'session_id': OLD, 'asks': 1}))
        for _ in range(5):
            self.step(1.0)
        self.assertEqual((len(self.session.sent), len(self.rows()), self.managed.closed), (1, 1, []))

    def test_a_request_the_host_does_not_take_is_sent_again_with_the_same_id(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.session.refuse = True
        self.step()
        self.step(1.0)
        self.assertEqual(self.session.sent, [])
        self.session.refuse = False
        self.step(1.0)
        self.step(1.0)
        self.assertEqual((len(self.session.sent), len(self.session.ledger), len(self.rows())), (1, 1, 1))

    # -- filed --------------------------------------------------------------------------------------------------------

    def test_once_filed_and_the_turn_has_ended_the_conversation_is_closed_and_a_fresh_one_gets_the_opening(self):
        self.say('Pay the creators today.', 10)
        self.session.transcript = self.transcript(
            {'type': 'user', 'message': {'role': 'user', 'content': 'Pay the creators today.'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Sent the transfer to Liam.'}]}})
        self.fill()
        self.step()
        self.file_handoff()
        self.step(1.0)                                     # filed, but the turn is still running
        self.assertEqual((self.managed.closed, self.opener.replaced), ([], []))
        self.session.end_turn()
        self.step(1.0)
        self.assertEqual(self.managed.closed, ['graceful'])
        (profile, closing, key), = self.opener.replaced
        row, = self.rows()
        self.assertEqual((profile, closing, key), ({'agent_id': self.lead}, OLD, 'compaction:' + row['id']))
        text, = self.opener.texts
        self.assertTrue(text.startswith('<qorqut-handoff>\nThis conversation continues your work.'))
        self.assertTrue(text.endswith('</qorqut-handoff>'))
        self.assertIn('[2026-11-18 09:10 UTC, chat] Pay the creators today.\n    PARTLY: Liam is paid; Sanzida is not. '
                      'Read: Task "Pay creators" (' + self.pay + '). Next step: Lead pays Sanzida', text)
        self.assertIn('THE LAST MESSAGES OF THE CLOSED CONVERSATION, WORD FOR WORD\nOWNER: Pay the creators today.\n'
                      'YOU: Sent the transfer to Liam.', text)
        self.assertIsNone(row['opened_at'])                # the replacement ran on its own thread; the next pass records it
        self.step(1.0)
        row, = self.rows()
        self.assertTrue(row['opened_at'])
        self.assertEqual(row['opened_json'], {'unanswered': []})
        self.assertIs(self.controller._flow, None)
        # The fresh conversation is small again: nothing more is asked of it.
        self.step()
        self.assertEqual((self.managed.host.sent, len(self.rows())), ([], 1))

    def test_a_turn_that_goes_on_after_the_filing_is_interrupted_and_then_replaced(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.file_handoff()
        self.step(1.0)
        self.step(compaction.STOP_SECONDS - 5)
        self.assertEqual((self.session.interrupts, self.managed.closed), (0, []))
        self.step(10.0)
        self.assertEqual((self.session.interrupts, self.managed.closed), (1, []))
        self.step(1.0)
        self.assertEqual(self.session.interrupts, 1)       # asked to stop once
        self.step(compaction.CLOSE_SECONDS)                # still running: it is closed whatever it does
        self.assertEqual((self.managed.closed, len(self.opener.replaced)), (['graceful'], 1))

    # -- not filed ----------------------------------------------------------------------------------------------------

    def test_a_turn_that_ends_without_a_filing_is_asked_once_more_and_then_closed_without_one(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.session.end_turn()
        self.step(1.0)
        first, second = self.session.sent
        self.assertTrue(second.startswith('<qorqut-handoff-request>\nYour turn ended and no handoff was filed. File it now'))
        self.assertIn('Your context is nearly full.', second)
        self.assertEqual((len(self.session.ledger), self.rows()[0]['request_json']['asks'], self.managed.closed), (2, 2, []))
        self.session.end_turn()
        self.step(1.0)                                     # given up; the turn has ended
        self.step(1.0)
        self.assertEqual((len(self.session.sent), self.managed.closed, len(self.opener.replaced)), (2, ['graceful'], 1))
        text = self.opener.texts[0]
        self.assertIn('[2026-11-18 09:10 UTC, chat] Pay the creators today.\n    NO STATUS: your handoff did not say', text)
        self.assertIn('NO HANDOFF WAS FILED before the previous conversation closed.', text)
        self.step(1.0)
        row, = self.rows()
        self.assertEqual((bool(row['opened_at']), row['opened_json']['unanswered']), (True, [self.said[0]['id']]))

    def test_a_request_that_is_neither_filed_nor_finished_is_given_up_on_after_a_while(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.step(compaction.ASK_SECONDS - 60)
        self.assertEqual(self.managed.closed, [])
        self.step(120.0)                                   # given up: the turn is still running
        self.step(compaction.STOP_SECONDS + 1)
        self.assertEqual(self.session.interrupts, 1)
        self.session.end_turn()
        self.step(1.0)
        self.assertEqual((self.managed.closed, len(self.opener.replaced), len(self.session.sent)), (['graceful'], 1, 1))

    def test_a_handoff_filed_before_it_was_asked_for_does_not_count_as_the_answer(self):
        self.say('Pay the creators today.', 10)
        self.file_handoff()                                # the agent filed on its own, an hour before the context filled
        self.now += timedelta(hours=1)
        self.say('And book the car in.', 90)
        self.fill()
        self.step()
        self.assertEqual(len(self.session.sent), 1)
        self.step(1.0)
        self.assertEqual(self.managed.closed, [])          # the old filing is not the answer to this request
        self.session.end_turn()
        self.step(1.0)
        self.session.end_turn()
        self.step(1.0)
        self.step(1.0)
        text = self.opener.texts[0]
        self.assertIn('PARTLY: Liam is paid; Sanzida is not.', text)
        self.assertIn('THIS HANDOFF WAS FILED AT 2026-11-18 11:00 UTC, BEFORE THE CONVERSATION ENDED.', text)
        self.assertIn('And book the car in.\n    NO STATUS', text)

    # -- the service or the session went away --------------------------------------------------------------------------

    def test_a_restarted_service_continues_the_flow_from_its_row_without_asking_twice(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        again = self.make()                                # the service restarted; the host, detached, kept running
        self.session.usage = None                          # even with no reading: the row says a handoff was asked for
        self.step(controller=again)
        self.step(1.0, again)
        self.assertEqual(len(self.session.sent), 1)
        self.file_handoff()
        self.session.end_turn()
        self.step(1.0, again)
        self.assertEqual((self.managed.closed, len(self.opener.replaced)), (['graceful'], 1))
        self.step(1.0, again)
        self.assertTrue(self.rows()[0]['opened_at'])

    def test_when_the_conversation_stops_running_the_flow_waits_and_continues_when_it_runs_again(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.managed.host = None                           # an idle close, a crash, the owner ending the session
        self.step(1.0)
        self.assertIs(self.controller._flow, None)
        self.step()
        self.assertEqual((self.opener.replaced, self.rows()[0]['opened_at']), ([], None))
        self.managed.host = self.session = Session(OLD)    # something opened the agent: the same conversation, resumed
        self.step()
        self.step(1.0)
        request, = self.session.sent                       # the new host never got the request: it is sent to it
        self.assertIn('Pay the creators today.', request)
        self.assertEqual(len(self.rows()), 1)
        self.file_handoff()
        self.session.end_turn()
        self.step(1.0)
        self.assertEqual(len(self.opener.replaced), 1)

    def test_another_conversation_is_not_given_a_request_that_was_made_for_the_closed_one(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.managed.host = self.session = Session(NEW)    # the owner started a new conversation meanwhile
        self.step(1.0)
        self.step()
        self.assertEqual((self.session.sent, self.managed.closed), ([], []))
        self.fill()                                        # much later it fills too: the waiting row is asked for again
        self.step()
        self.assertEqual(len(self.session.sent), 1)
        row, = self.rows()
        self.assertEqual(row['request_json']['session_id'], NEW)

    # -- the open ------------------------------------------------------------------------------------------------------

    def test_an_open_that_fails_is_tried_again_and_the_row_keeps_waiting_when_it_keeps_failing(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.file_handoff()
        self.session.end_turn()
        self.opener.fail = 'open_busy'
        self.step(1.0)
        self.assertEqual(len(self.opener.replaced), 1)
        self.step(5.0)                                     # not yet
        self.assertEqual(len(self.opener.replaced), 1)
        self.step(compaction.RETRY_SECONDS)
        self.step(compaction.RETRY_SECONDS)
        self.assertEqual(len(self.opener.replaced), 3)
        self.step(compaction.RETRY_SECONDS)
        self.assertEqual((len(self.opener.replaced), self.controller._flow, self.rows()[0]['opened_at']), (3, None, None))
        self.assertEqual(self.managed.closed, ['graceful'])         # the conversation was closed once; nothing ran after it

    def test_an_open_that_only_resumed_the_closed_conversation_is_not_recorded_as_replaced(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.file_handoff()
        self.session.end_turn()
        self.opener.session = OLD                          # what came up is the old conversation, resumed
        self.step(1.0)
        self.assertEqual(self.rows()[0]['opened_at'], None)
        self.opener.session = NEW
        self.step(compaction.RETRY_SECONDS)
        self.step(1.0)
        self.assertEqual((len(self.opener.replaced), bool(self.rows()[0]['opened_at'])), (2, True))

    def test_an_agent_the_owner_turned_off_is_not_opened_again(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.file_handoff()
        self.session.end_turn()
        self.opener.off = True
        self.step(1.0)
        self.step(compaction.RETRY_SECONDS)
        self.assertEqual((self.opener.replaced, self.controller._flow, self.rows()[0]['opened_at']), ([], None, None))

    def test_the_opening_is_written_after_the_close_so_a_message_that_reached_the_closing_conversation_is_in_it(self):
        self.say('Pay the creators today.', 10)
        self.fill()
        self.step()
        self.file_handoff()
        self.session.end_turn()
        self.now += timedelta(minutes=10)
        self.opener.on_close = lambda: self.say('And book the car in.', 125)       # typed while the conversation was closing
        self.step(1.0)
        text, = self.opener.texts
        self.assertIn("THE OWNER'S MESSAGES THAT ARRIVED WHILE YOU WERE FILING. You have not answered or acted on them.\n"
                      '[2026-11-18 11:05 UTC, chat] And book the car in.', text)

    # -- the client's own compaction ----------------------------------------------------------------------------------

    def test_only_in_the_only_mode_is_the_coordinators_session_started_without_the_clients_own_compaction(self):
        self.assertTrue(self.make(mode='only').replaces_client(self.lead))
        self.assertFalse(self.make(mode='only').replaces_client(self.worker))      # a worker keeps the client's
        self.assertFalse(self.make(mode='on').replaces_client(self.lead))          # on: the client's stays behind this one
        self.assertFalse(self.make(mode='off').replaces_client(self.lead))
        self.assertTrue(self.make(mode='only').enabled)
        with mock.patch.dict('os.environ', {compaction.ENVIRONMENT: ''}):
            self.assertEqual(compaction.mode_by_environment(), 'off')
        for value, mode in (('1', 'on'), ('on', 'on'), ('only', 'only'), (' ONLY ', 'only'), ('0', 'off'), ('never', 'off')):
            with mock.patch.dict('os.environ', {compaction.ENVIRONMENT: value}):
                self.assertEqual(compaction.mode_by_environment(), mode)

    # -- the last messages --------------------------------------------------------------------------------------------

    def transcript(self, *records):
        folder = Path(tempfile.mkdtemp(prefix='compaction tail '))
        self.addCleanup(__import__('shutil').rmtree, folder, True)
        path = folder / 'conversation.jsonl'
        path.write_text(''.join(json.dumps(record) + '\n' for record in records) + 'not json\n')
        return str(path)

    def test_the_tail_is_the_last_messages_before_the_request_word_for_word(self):
        said = lambda text: {'type': 'user', 'message': {'role': 'user', 'content': text}}
        does = lambda *blocks: {'type': 'assistant', 'message': {'content': list(blocks)}}
        path = self.transcript(
            said('Pay the creators today. The card is 4111 1111 1111 1111 and the password: hunter2.'),
            said('<qorqut-system-input id="abcdefgh" type="inbox_nudge">\nYou have new inbox events.\n</qorqut-system-input>'),
            {'type': 'user', 'isMeta': True, 'message': {'content': 'Caveat: a note of the client'}},
            does({'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': 'Paying Liam now.'},
                 {'type': 'tool_use', 'name': 'wise_transfer', 'input': {'to': 'Liam', 'memo': 'x' * 400}}),
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'accepted ' + 'y' * 1200}]}},
            {'type': 'assistant', 'isSidechain': True, 'message': {'content': [{'type': 'text', 'text': 'a subagent speaking'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't2', 'is_error': True,
                                                      'content': [{'type': 'text', 'text': 'no such payee'}]}]}},
            {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': 'And book the car in.'}},
            {'type': 'attachment', 'attachment': {'type': 'queued_command',
                                                  'prompt': '<qorqut-handoff-request>\nYour context is nearly full.\n</qorqut-handoff-request>'}},
            does({'type': 'text', 'text': 'Filing my handoff.'}))
        self.assertEqual(transcript_tail(path).split('\n'), [
            'OWNER: Pay the creators today. The card is [number removed] and the password: [removed]',
            'YOU: Paying Liam now.',
            'TOOL CALL: wise_transfer {"to": "Liam", "memo": "' + 'x' * 276 + ' …[cut]',
            'TOOL RESULT: accepted ' + 'y' * 991 + ' …[cut]',
            'TOOL RESULT: ERROR [{"type": "text", "text": "no such payee"}]',
            'OWNER: And book the car in.'])

    def test_the_tail_keeps_the_newest_messages_within_its_bounds_and_is_empty_without_a_transcript(self):
        records = [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': f'step {number} ' + 'z' * 900}]}}
                   for number in range(30)]
        tail = transcript_tail(self.transcript(*records))
        self.assertLessEqual(len(tail), compaction.TAIL_CAP)
        self.assertTrue(tail.endswith('step 29 ' + 'z' * 900))
        self.assertNotIn('step 17 ', tail)                 # twelve messages at most
        self.assertEqual(transcript_tail(None), '')
        self.assertEqual(transcript_tail('/nonexistent/conversation.jsonl'), '')
        self.assertEqual(transcript_tail(self.transcript({'type': 'response_item', 'payload': {'role': 'user'}})), '')
