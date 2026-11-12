"""A thread message is for the owner or it is a working note, and a reader can ask for the owner's part only."""
import json
from uuid import uuid4

from qorqut_operations.discussion.threads import OWNER_POST_LIMIT

import test_task_lifecycle as base

STAMP = base.STAMP


class AudienceFixture(base.Fixture):
    def ok(self, result):
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data

    def comment(self, who, task, text, **extra):
        return self.invoke(who, 'tasks_comment', {'task_id': task['id'], 'text': text, **extra})

    def owner_says(self, task, text, at):
        """The owner's own message in the thread, as the dashboard writes it for a business Task."""
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,attribution_json,"
                "created_at) VALUES(NULL,?,'task',?,'comment',?,?,?,'{\"kind\":\"self\"}',?)",
                (str(uuid4()), task['id'], text, self.owner, self.owner, at))

    def notes(self, who, task, **arguments):
        return self.ok(self.invoke(who, 'tasks_notes_list', {'task_id': task['id'], 'limit': 50, **arguments}))

    def audiences(self, task):
        return [(note['body'], note['audience']) for note in self.notes(self.lead, task)['notes']]


class WritingTests(AudienceFixture):
    def test_an_agents_note_is_a_working_note_unless_it_is_posted_for_the_owner(self):
        task = self.task(self.lead, 'Book the venue')
        self.ok(self.comment(self.worker, task, 'Checked three venues, none free on the 4th'))
        self.ok(self.comment(self.worker, task, 'The venue is booked for the 5th', for_owner=True))
        self.assertEqual(self.audiences(task), [('Checked three venues, none free on the 4th', 'working'),
                                                ('The venue is booked for the 5th', 'owner')])
        with self.db.read() as uow:
            stored = [json.loads(row[0]) for row in uow.connection.execute(
                "SELECT attribution_json FROM thread_messages WHERE kind='comment' ORDER BY created_at,id")]
        self.assertEqual(stored, [{'kind': 'self'}, {'kind': 'self', 'audience': 'owner'}])

    def test_a_note_for_the_owner_is_short_and_names_no_record(self):
        task = self.task(self.lead, 'Report')
        self.refused(self.comment(self.worker, task, 'x' * (OWNER_POST_LIMIT + 1), for_owner=True), 'at most 600 characters')
        self.refused(self.comment(self.worker, task, f'Done, see {uuid4()}', for_owner=True), 'no record identifiers')
        # The same text is an ordinary working note.
        self.ok(self.comment(self.worker, task, f'Detail for the team: {uuid4()} ' + 'x' * 700))
        self.assertEqual(list(self.notes(self.lead, task, audience='owner')['notes']), [])

    def test_the_first_agent_note_after_the_owners_message_answers_it(self):
        task = self.task(self.lead, 'Choose the colour')
        self.owner_says(task, 'Which one do you suggest?', '2020-01-01T00:00:00.000000Z')
        self.assertEqual(self.audiences(task), [('Which one do you suggest?', 'owner')])
        # The owner's message is the newest comment only once it is stamped after the engine's own clock.
        self.owner_says(task, 'And when?', '2999-01-01T00:00:00.000000Z')
        other = self.task(self.lead, 'Pick the font')
        self.owner_says(other, 'Serif or not?', '2020-01-01T00:00:00.000000Z')
        self.ok(self.comment(self.lead, other, 'Serif reads better in print'))
        self.ok(self.comment(self.lead, other, 'Asked the designer for samples'))
        self.assertEqual(self.audiences(other), [('Serif or not?', 'owner'), ('Serif reads better in print', 'owner'),
                                                 ('Asked the designer for samples', 'working')])
        quiet = self.task(self.lead, 'Pick the paper')
        self.owner_says(quiet, 'Matte?', '2020-01-01T00:00:00.000000Z')
        self.ok(self.comment(self.lead, quiet, 'Noting the supplier list for the team', for_owner=False))
        self.assertEqual(self.audiences(quiet)[-1], ('Noting the supplier list for the team', 'working'))

    def test_a_relayed_owner_statement_is_the_owners_and_is_answered_elsewhere(self):
        task = self.task(self.lead, 'Plan the trip')
        self.ok(self.comment(self.lead, task, 'Owner wants to leave on Friday', relay={
            'kind': 'explicit_owner_statement', 'owner_id': self.owner, 'statement_text': 'Leave on Friday'}))
        self.ok(self.comment(self.lead, task, 'Looking at Friday departures'))
        self.assertEqual(self.audiences(task), [('Owner wants to leave on Friday', 'owner'), ('Looking at Friday departures', 'working')])

    def test_closing_the_work_is_for_the_owner_and_a_delegation_brief_is_not(self):
        task = self.task(self.lead, 'File the form')
        self.ok(self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                             'brief': 'Use the 2026 version of the form'}))
        self.ok(self.invoke(self.worker, 'tasks_complete', {'task_id': task['id'], 'result': 'Filed on Monday'}))
        self.assertEqual(self.audiences(task), [('Use the 2026 version of the form', 'working'), ('Filed on Monday', 'owner')])


class ReadingTests(AudienceFixture):
    def setUp(self):
        super().setUp()
        self.item = self.task(self.lead, 'Launch the page')
        self.ok(self.comment(self.worker, self.item, 'Working note one'))
        self.ok(self.comment(self.worker, self.item, 'Page is live', for_owner=True))
        self.ok(self.comment(self.worker, self.item, 'Working note two'))
        self.ok(self.update(self.worker, self.item, {'status': 'working'}))
        self.ok(self.comment(self.worker, self.item, 'Do you want the banner too?', for_owner=True))

    def test_every_reader_gets_everything_unless_it_asks_for_the_owners_part(self):
        everything = self.notes(self.lead, self.item)
        self.assertEqual(len(everything['notes']), 4)
        self.assertNotIn('hidden', everything)
        owner = self.notes(self.owner, self.item, audience='owner')
        self.assertEqual([note['body'] for note in owner['notes']], ['Page is live', 'Do you want the banner too?'])
        self.assertEqual(owner['hidden'], 2)
        self.assertEqual(self.notes(self.owner, self.item, audience='all')['notes'], everything['notes'])

    def test_the_task_read_returns_the_owners_notes_and_how_many_were_left_out(self):
        full = self.ok(self.invoke(self.owner, 'tasks_get', {'task_id': self.item['id']}))['task']
        self.assertEqual(len(full['notes']), 4)
        self.assertNotIn('notes_hidden', full)
        owner = self.ok(self.invoke(self.owner, 'tasks_get', {'task_id': self.item['id'], 'audience': 'owner'}))['task']
        self.assertEqual({note['body'] for note in owner['notes']}, {'Page is live', 'Do you want the banner too?'})
        self.assertEqual((owner['notes_hidden'], owner['note_count']), (2, 4))

    def test_the_whole_thread_counts_its_system_entries_as_working_notes(self):
        thread = self.ok(self.invoke(self.owner, 'tasks_thread_get', {'task_id': self.item['id'], 'limit': 50}))
        system = [note for note in thread['notes'] if note['entry_kind'] == 'system']
        self.assertTrue(system)
        self.assertTrue(all(note['audience'] == 'working' for note in system))
        owner = self.ok(self.invoke(self.owner, 'tasks_thread_get', {'task_id': self.item['id'], 'limit': 50, 'audience': 'owner'}))
        self.assertEqual([note['body'] for note in owner['notes']], ['Page is live', 'Do you want the banner too?'])
        self.assertEqual(owner['hidden'], len(thread['notes']) - 2)

    def test_paging_through_the_owners_part_skips_working_notes(self):
        first = self.notes(self.owner, self.item, audience='owner', limit=1)
        self.assertEqual([note['body'] for note in first['notes']], ['Do you want the banner too?'])
        second = self.notes(self.owner, self.item, audience='owner', limit=1, cursor=first['next_cursor'])
        self.assertEqual([note['body'] for note in second['notes']], ['Page is live'])
        self.assertIsNone(second['next_cursor'])


if __name__ == '__main__':
    import unittest
    unittest.main()
