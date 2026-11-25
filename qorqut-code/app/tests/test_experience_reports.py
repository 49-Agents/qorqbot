"""What an experience extraction reads: the report of conversations and the two tool-call reports."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import new_id

from qorqut.experience import conversations
from qorqut.native_archive import CaptureError

import test_experience
import test_owner_inputs as inputs
from private_thread_runtime import create, invoke, reply

at = inputs.at

FORM = {'title': 'Choose a date', 'explanation': 'Which date works for the call?', 'kind': 'question', 'selection_mode': 'single',
        'choices': [{'id': 'mon', 'label': 'Monday', 'explanation': 'The 16th.'}, {'id': 'tue', 'label': 'Tuesday', 'explanation': 'The 17th.'}]}


class Transcript(inputs.Archive):
    """An archived conversation that also holds tool calls."""

    def record(self, value):
        offset = len(self.records) * 10
        self.records.append(NS(kind='json', value=value, offset=offset, length=10, end=offset + 10))

    def call(self, tool, arguments, when, *, error=None):
        identity = new_id()
        self.record({'type': 'assistant', 'timestamp': when, 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': identity, 'name': tool, 'input': arguments}]}})
        self.record({'type': 'user', 'timestamp': when, 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': identity, 'is_error': error is not None, 'content': error or 'ok'}]}})


class ReportCase(test_experience.ExperienceCase):
    # The fixtures of the owner's-messages tests, borrowed without bringing their tests into this module.
    turn, said, write, stamp_thread = (getattr(inputs.OwnerInputsTests, name) for name in ('turn', 'said', 'write', 'stamp_thread'))
    session = staticmethod(inputs.OwnerInputsTests.session)

    def setUp(self):
        super().setUp()
        self.runtime.native_chat.indexer.archive = self.archive
        self.write('DROP TRIGGER owner_conversation_messages_no_update', ())
        for identity, name in ((self.lead, 'Ordubashy'), (self.worker, 'Atlas')):
            self.write('UPDATE principals SET display_name=? WHERE id=?', (name, identity))
        # One extraction run over a period that holds the fixed times below and whatever is written now.
        self.run_id = new_id()
        self.write("INSERT INTO experience_runs(id,kind,state,slot,window_start,window_end,agent_id,attempts,queued_at,started_at) "
                   "VALUES(?,'extraction','running',?,?,?,?,1,?,?)",
                   (self.run_id, at(23), '2026-11-12T00:00:00.000000Z', '2100-01-01T00:00:00.000000Z', self.applier.principal_id,
                    at(23), at(23)))

    def archive(self, profile):
        if profile['agent_id'] not in self.archives:
            raise CaptureError('unavailable', 'The archive of this conversation is not there')
        return self.archives[profile['agent_id']]

    def pages(self):
        found, cursor = [], None
        while True:
            page = self.ok('experience_conversations', {'run_id': self.run_id, **({'cursor': cursor} if cursor else {})})
            found.append(page)
            cursor = page.get('next_cursor')
            if cursor is None:
                return found


class ConversationsReportTests(ReportCase):
    def test_the_report_is_one_timeline_of_what_the_owner_wrote_and_decided_and_what_was_answered(self):
        asked, _ = self.turn(self.lead, 'Send the Linden contract today.', at(10))
        sent = self.said(self.lead, 'Sent to Linden at 10:04.', at(10, 5))
        late, _ = self.turn(self.worker, 'Why is the report late again?', at(11))
        long = self.said(self.worker, 'Because ' + 'the export failed. ' * 400, at(11, 2))
        self.said(self.worker, 'A reply to a reminder, not to the owner.', at(11, 30), origin='nudge')
        private, _ = self.turn(self.lead, 'my card number is zq-secret-77', at(12), redacted=True)
        self.said(self.lead, 'I have noted the card number zq-secret-77.', at(12, 1))
        chat = create(self.runtime, 'Stop asking me to send files you can send.')
        reply(self.runtime, chat, 'Understood. I will send them myself.')
        self.stamp_thread('Stop asking me to send files you can send.', at(13))
        self.stamp_thread('Understood. I will send them myself.', at(13, 1))
        question = invoke(self.runtime, 'signoffs_input_create', {'request_key': 'call-date', 'form': FORM}, ctx=self.runtime.worker)['signoff']
        self.answer(question['id'], selected_choices=['tue'], notes='Tuesday, and stop asking twice.')

        page, = self.pages()
        text = page['report']
        positions = [text.index(part) for part in (
            f'[10:00] OWNER to Ordubashy in chat   id {asked}\nSend the Linden contract today.',
            f'[10:05] ORDUBASHY replied to the owner in chat   id {sent}\nSent to Linden at 10:04.',
            f'[11:00] OWNER to Atlas in chat   id {late}\nWhy is the report late again?',
            f'[11:02] ATLAS replied to the owner in chat   id {long}\nBecause the export failed.',
            f'[12:00] OWNER to Ordubashy in chat   id {private}\n(a possibly private input: its text is withheld)',
            'OWNER in the thread of signoff "A record"',
            'Stop asking me to send files you can send.',
            'ORDUBASHY in the thread of signoff "A record"',
            'Understood. I will send them myself.',
            'OWNER ANSWERED the Signoff "Choose a date" proposed by Atlas',
            'chose: Tuesday',
            'the words: Tuesday, and stop asking twice.')]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(text.startswith('## Thursday 2026-11-12 (UTC)\n\n[10:00] OWNER'))
        # Every record is named with how to open it.
        self.assertIn(f'open: signoff {chat["id"]}', text)
        self.assertIn(f'id signoff:{question["id"]}   open: signoff {question["id"]}', text)
        # A long reply is cut, saying how to read the rest; the owner's words are whole.
        self.assertIn(f'more characters. Read all of it with experience_message_get id {long}]', text)
        whole = self.ok('experience_message_get', {'id': long})['item']
        self.assertEqual((whole['chars'], whole['truncated'], whole['agent']['name']), (len('Because ' + 'the export failed. ' * 400), False, 'Atlas'))
        self.assertEqual(self.ok('experience_message_get', {'id': asked})['item']['text'], 'Send the Linden contract today.')
        self.refused('experience_message_get', {'id': 'signoff:' + question['id']}, says='signoffs_get')
        # Left out: a reply to something other than the owner, what followed a possibly private input, and the note that
        # only repeats the words of a decision.
        self.assertNotIn('reminder', text)
        self.assertNotIn('zq-secret-77', text)  # not hex, so no record id in the report can contain it
        self.assertEqual(text.count('stop asking twice'), 1)
        self.assertEqual(([gap['code'] for gap in page['gaps']], page['shown']), (['private'], {'owner': 4, 'reply': 3, 'decision': 1}))
        self.assertEqual((page['entries_in_period'], page['from_entry'], page['to_entry']), (9, 1, 9))
        self.assertEqual(page['end_token'], conversations.end_token(self.row(self.run_id)))

    def test_the_report_comes_in_pages_and_only_the_last_one_ends_it(self):
        for minute in range(6):
            self.turn(self.lead, f'Message number {minute}. ' + 'x' * 300, at(9, minute))
        with patch.object(conversations, 'PAGE_CHARACTERS', 700):
            pages = self.pages()
        self.assertEqual([(page['from_entry'], page['to_entry'], 'end_token' in page) for page in pages],
                         [(1, 2, False), (3, 4, False), (5, 6, True)])
        self.assertEqual([page['report'].count('Message number') for page in pages], [2, 2, 2])
        self.assertTrue(all(page['report'].startswith('## Thursday 2026-11-12 (UTC)') for page in pages))
        self.refused('experience_conversations', {'run_id': self.run_id, 'cursor': '99'}, says='beyond the end')

    def test_the_report_is_the_learning_appliers_alone_and_belongs_to_an_extraction(self):
        for ctx in (self.runtime.owner, self.runtime.coordinator, self.runtime.worker):
            self.refused('experience_conversations', {'run_id': self.run_id}, ctx=ctx, code='forbidden')
            self.refused('experience_tool_problems', {'run_id': self.run_id}, ctx=ctx, code='forbidden')
        self.refused('experience_conversations', {'run_id': 'missing'}, code='not_found')
        self.write("UPDATE experience_runs SET kind='review', slot='x' WHERE id=?", (self.run_id,))
        self.refused('experience_conversations', {'run_id': self.run_id}, says='experience extraction run')


class ToolReportTests(ReportCase):
    def conversation(self, agent):
        self.archives[agent] = archive = Transcript()
        with self.runtime.database.transaction() as uow:
            self.session(uow.connection, agent)
        return archive

    def test_failed_calls_are_grouped_by_what_the_error_says_and_repeats_are_listed(self):
        atlas, lead = self.conversation(self.worker), self.conversation(self.lead)
        atlas.call('Bash', {'command': 'jq .name package.json'}, at(9), error='bash: jq: command not found')
        atlas.call('Bash', {'command': 'jq .version package.json'}, at(9, 1), error='bash: jq: command not found')
        atlas.call('Bash', {'command': 'cat package.json'}, at(9, 2))
        lead.call('Bash', {'command': 'jq . a.json'}, at(10), error='bash: jq: command not found')
        lead.call('tasks_get', {'task_id': 'T1'}, at(10, 1), error='Task does not exist')
        for minute in (5, 6, 7):
            lead.call('tasks_list', {'scope': 'global'}, at(10, minute))
        lead.call('Bash', {'command': 'jq . old.json'}, '2026-11-01T10:00:00.000000Z', error='bash: jq: command not found')   # before the period
        report = self.ok('experience_tool_problems', {'run_id': self.run_id})
        self.assertEqual((report['conversations_read'], report['calls'], report['failed_calls'], report['groups_total']), (2, 8, 4, 2))
        self.assertEqual(report['failed_calls_by_agent'], {'Atlas': 2, 'Ordubashy': 2})
        first, second = report['groups']
        self.assertEqual((first['tool'], first['count'], first['agents'], first['sessions'], first['recovered']),
                         ('Bash', 3, ['Atlas', 'Ordubashy'], 2, 2))
        self.assertIn('command not found', first['signature'])
        self.assertEqual((second['tool'], second['count'], second['examples'][0]['error']), ('tasks_get', 1, 'Task does not exist'))
        repeated, = report['repeated_calls']
        self.assertEqual((repeated['agent'], repeated['tool'], repeated['count'], repeated['failed']), ('Ordubashy', 'tasks_list', 3, 0))
        self.assertEqual(report['gaps'], [])

    def test_repeated_sequences_of_calls_are_counted_across_conversations(self):
        atlas, lead = self.conversation(self.worker), self.conversation(self.lead)
        for archive, minutes in ((atlas, (1, 5)), (lead, (9,))):
            for minute in minutes:
                archive.call('tables_get', {'table_id': f't{minute}'}, at(9, minute))
                archive.call('tables_rows_list', {'table_id': f't{minute}'}, at(9, minute))
                archive.call('tables_row_update', {'table_id': f't{minute}', 'row': 1}, at(9, minute))
                archive.add(f'Next one ({minute}).', new_id())      # the owner speaks: the next calls are another piece of work
        report = self.ok('experience_tool_patterns', {'run_id': self.run_id})
        # The same sequence is counted twice: by the tools' names and by what each call does to what.
        by_name, = (found for found in report['patterns'] if found['projection'] == 'tool')
        by_shape, = (found for found in report['patterns'] if found['projection'] == 'shape')
        self.assertEqual((by_name['sequence'], by_name['occurrences'], by_name['sessions'], by_name['agents']),
                         (['tables_get', 'tables_rows_list', 'tables_row_update'], 3, 2, ['Atlas', 'Ordubashy']))
        self.assertEqual((by_shape['sequence'], by_shape['occurrences']), (['read:table_id', 'list:table_id', 'edit:row,table_id'], 3))
        self.assertEqual((report['conversations_read'], report['calls']), (2, 9))

    def test_a_conversation_that_cannot_be_read_is_named_as_a_gap(self):
        self.conversation(self.worker).call('Bash', {'command': 'true'}, at(9))
        with self.runtime.database.transaction() as uow:
            self.session(uow.connection, self.lead)       # captured, but its archive is not there
        report = self.ok('experience_tool_problems', {'run_id': self.run_id})
        self.assertEqual((report['conversations_read'], [(gap['code'], gap['agent'], gap['count']) for gap in report['gaps']]),
                         (1, [('conversation_unreadable', 'Ordubashy', 1)]))


if __name__ == '__main__':
    unittest.main()
