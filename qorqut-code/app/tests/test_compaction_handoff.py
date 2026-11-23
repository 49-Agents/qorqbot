"""The coordinator's compaction handoff: what a filing writes, what it refuses, what the next conversation starts with and
which messages the handoff after it answers for. Over the real Task engine; the owner's messages are a list in the test."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

from qorqut_operations.contracts import plain

from qorqut.composition.delegation import HOST_REVIEW_EXCEPTIONS
from qorqut.domains.compaction_handoff import CompactionHandoff

from test_single_source import Fixture

START = datetime(2026, 11, 18, 9, tzinfo=timezone.utc)
STOPPED = {'doing': 'Paying the creators one by one', 'last_action': 'Sent the Wise transfer to Liam; it was accepted',
           'next_action': 'Send the transfer to Sanzida', 'waiting_on': 'Nothing'}


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class HandoffCase(Fixture):
    """The filing tool over the real Task engine, with the owner's messages as a list. Holds no test of its own."""

    def setUp(self):
        super().setUp()
        self.now = START + timedelta(hours=2)
        self.said = []
        self.business = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('businesses').insert({'id': self.business, 'name': 'Example project', 'slug': 'example',
                                                 'workspace': 'example', 'hq_path': 'hq'})
        runtime = NS(application=self.app, registry=self.registry, domains=NS(tasks=self.tasks),
                     owner_inputs=NS(addressed=self.addressed))
        self.tool = CompactionHandoff(runtime, clock=lambda: self.now).register()
        self.app.review_exceptions = HOST_REVIEW_EXCEPTIONS      # as the service composes the engine
        self.pay = self.task(self.lead, 'Pay creators')['id']
        made = self.app.invoke(replace(self.ctx(self.lead), business_id=self.business), 'tasks_create', {'title': 'Fix the car'},
                               {'idempotency_key': str(uuid4())})
        self.car = self.ok(made)['task']['id']

    def ctx(self, who):
        return replace(super().ctx(who), allowed_business_ids={self.business} if who == self.lead else ())

    # -- the owner's messages and the calls -------------------------------------------------------------------------

    def addressed(self, ctx, uow, *, since, until, ids=(), limit=100):
        return [message for message in self.said if since <= message['at'] < until or message['id'] in ids]

    def say(self, text, minutes, *, thread=None):
        identity = ('thread:' if thread else 'chat:') + str(uuid4())
        place = {'type': 'task', 'id': thread, 'business_id': None, 'title': 'Pay creators'} if thread else None
        self.said.append({'id': identity, 'at': at(minutes), 'channel': 'thread' if thread else 'chat', 'place': place,
                          'text': text, 'redacted': False})
        return identity

    def file(self, messages, who=None, **more):
        arguments = {'scope': 'global', 'messages': messages, 'stopped': STOPPED, 'workers': 'Scout holds nothing from me.', **more}
        return self.app.invoke(self.ctx(who or self.lead), 'compaction_handoff_file', arguments, {'idempotency_key': str(uuid4())})

    def filed(self, messages, **more):
        return plain(self.ok(self.file(messages, **more)))

    def form(self):
        return plain(self.ok(self.app.invoke(self.ctx(self.lead), 'compaction_handoff_get', {'scope': 'global'}, {})))['messages']

    def opening(self, tail=''):
        with self.db.read() as uow:
            return self.tool.opening(self.ctx(self.lead), uow, tail=tail)

    def open(self, **ids):
        """Start the next conversation from the waiting handoff, as the host does (``closed`` and ``session`` name the two)."""
        opening = self.opening()
        with self.db.transaction() as uow:
            self.tool.opened(uow, opening['id'], opening['unanswered'], **ids)
        return opening['text']

    def seen(self, task_id, business=None):
        with self.db.read() as uow:
            row = uow.repository('tasks', business_id=business).get(task_id)
            notes = [line['body'] for line in uow.connection.execute(
                "SELECT body FROM thread_messages WHERE item_id=? AND kind='comment' ORDER BY created_at, id", (task_id,))]
        return (row['fields_json'].get('$task') or {}).get('next_step'), notes

    def rows(self):
        with self.db.read() as uow:
            return [uow.repository('compactions').get(row['id']) for row in
                    uow.connection.execute('SELECT id FROM compactions ORDER BY requested_at, id')]


class CompactionHandoffTests(HandoffCase):
    # -- filing -----------------------------------------------------------------------------------------------------

    def test_filing_writes_each_note_and_next_step_onto_its_task_and_keeps_the_handoff(self):
        self.say('Pay the creators today.', 10)
        self.say('And book the car in.', 20)
        result = self.filed(
            [{'id': 'm1', 'status': 'partly', 'note': 'Liam is paid; Sanzida is not.', 'task_id': self.pay, 'next_step': 'Lead pays Sanzida'},
             {'id': 'm2', 'status': 'done', 'note': 'Booked for Friday.'}],
            tasks=[{'task_id': self.car, 'note': 'The shop confirmed Friday 10:00.', 'next_step': 'Owner drops the car off Friday'}],
            stopped={**STOPPED, 'task_id': self.pay})
        self.assertEqual((result['filed'], result['messages']), (True, 2))
        self.assertEqual(result['tasks'], [{'task_id': self.pay, 'set': 'note and next step'}, {'task_id': self.car, 'set': 'note and next step'}])
        # One note per Task holds everything the handoff said about it; the owner's words are not copied into it.
        step, notes = self.seen(self.pay)
        self.assertEqual(step, 'Lead pays Sanzida')
        self.assertEqual(notes, ["Handoff: the owner's message of 2026-11-18 09:10 UTC is partly. Liam is paid; Sanzida is not.\n"
                                 'Handoff: the conversation stopped here. Step in progress: Paying the creators one by one. '
                                 'Last action and its result: Sent the Wise transfer to Liam; it was accepted. '
                                 'Next action: Send the transfer to Sanzida. Waiting on: Nothing.'])
        # A Task in a project is found by its id alone.
        self.assertEqual(self.seen(self.car, self.business), ('Owner drops the car off Friday', ['Handoff: The shop confirmed Friday 10:00.']))
        row, = self.rows()
        filing = row['filing_json']
        self.assertEqual((row['reason'], row['agent_id'], row['opened_at']), ('agent', self.lead, None))
        self.assertEqual([(entry['handle'], entry['status'], entry['task'] and entry['task']['id']) for entry in filing['messages']],
                         [('m1', 'partly', self.pay), ('m2', 'done', None)])
        self.assertEqual(filing['stopped']['task'], {'id': self.pay, 'business_id': None, 'title': 'Pay creators'})
        self.assertEqual(filing['tasks'][0]['task'], {'id': self.car, 'business_id': self.business, 'title': 'Fix the car'})
        self.assertNotIn('Pay the creators today', str(filing))

    def test_a_closed_task_gets_the_note_and_keeps_its_next_step(self):
        self.say('Pay the creators today.', 10)
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': self.pay, 'result': 'All paid'}))
        result = self.filed([{'id': 'm1', 'status': 'done', 'note': 'All paid.', 'task_id': self.pay, 'next_step': 'Nothing'}])
        self.assertEqual(result['tasks'], [{'task_id': self.pay, 'set': 'note'}])
        step, notes = self.seen(self.pay)
        self.assertFalse(step)
        self.assertEqual(notes[-1], "Handoff: the owner's message of 2026-11-18 09:10 UTC is done. All paid.")

    def test_a_filing_is_refused_with_every_reason_and_writes_nothing(self):
        self.say('Pay the creators today.', 10)
        self.say('And book the car in.', 20)
        self.say('Find me a dentist.', 30)
        refusal = self.refused(self.file(
            [{'id': 'm1', 'status': 'remaining', 'note': 'Not started.'},
             {'id': 'm3', 'status': 'partly', 'note': 'Asked two.', 'task_id': 'zzzzz'},
             {'id': 'm9', 'status': 'done', 'note': 'Nothing.'}]), 'The handoff was not filed.')
        self.assertEqual(plain(refusal['details'])['problems'], [
            'm1 is remaining and names no Task: give task_id, and create the Task first if none holds this work.',
            'm9 is not a listed message.',
            'No entry for: m2 (2026-11-18 09:20 UTC, chat: And book the car in.).',
            'Task zzzzz: no Task has this id'])
        self.assertEqual((self.rows(), self.seen(self.pay)), ([], (None, [])))

    def test_only_the_coordinator_in_person_files_or_reads_the_form(self):
        self.say('Pay the creators today.', 10)
        for who in (self.worker, self.owner):
            for operation, arguments in (('compaction_handoff_file', {'messages': [], 'stopped': STOPPED, 'workers': ''}),
                                         ('compaction_handoff_get', {})):
                result = self.app.invoke(self.ctx(who), operation, {'scope': 'global', **arguments}, {'idempotency_key': str(uuid4())})
                self.assertEqual((result.status, result.error['code']), ('error', 'forbidden'))
        self.assertEqual(self.rows(), [])

    def test_filing_again_before_the_next_conversation_replaces_the_filing(self):
        self.say('Pay the creators today.', 10)
        self.filed([{'id': 'm1', 'status': 'remaining', 'note': 'Not started.', 'task_id': self.pay}])
        self.filed([{'id': 'm1', 'status': 'done', 'note': 'All paid.'}])
        row, = self.rows()
        self.assertEqual([entry['status'] for entry in row['filing_json']['messages']], ['done'])

    def test_a_handoff_filed_unasked_answers_up_to_the_newest_message_it_gives_a_status(self):
        self.say('Pay the creators today.', 10)
        self.say('And book the car in.', 20)
        late = self.say('Find me a dentist.', 30)
        self.refused(self.file([{'id': 'm1', 'status': 'done', 'note': 'Paid.'}, {'id': 'm3', 'status': 'done', 'note': 'Found.'}]),
                     'No entry for: m2')
        self.filed([{'id': 'm1', 'status': 'done', 'note': 'Paid.'}, {'id': 'm2', 'status': 'done', 'note': 'Booked.'}])
        opening = self.opening()
        self.assertIn("THE OWNER'S MESSAGES THAT ARRIVED WHILE YOU WERE FILING. You have not answered or acted on them.\n"
                      '[2026-11-18 09:30 UTC, chat] Find me a dentist.', opening['text'])
        self.assertEqual(opening['unanswered'], [])
        self.open()
        # It is a new message of the next period, listed once.
        self.assertEqual([(message['id'], message['text'], message['open']) for message in self.form()], [('m1', 'Find me a dentist.', None)])
        self.assertTrue(late)

    # -- the next conversation --------------------------------------------------------------------------------------

    def test_the_opening_quotes_the_owner_and_points_at_the_tasks_the_filing_wrote(self):
        self.say('Pay the creators today.\nLiam first.', 10)
        self.say('And book the car in.', 20, thread=self.pay)
        self.ok(self.invoke(self.lead, 'tasks_delegate', {'task_id': self.pay, 'worker_id': self.worker}))
        self.filed([{'id': 'm1', 'status': 'partly', 'note': 'Liam is paid; Sanzida is not.', 'task_id': self.pay, 'next_step': 'Lead pays Sanzida'},
                    {'id': 'm2', 'status': 'done', 'note': 'Booked for Friday.'}],
                   tasks=[{'task_id': self.car, 'note': 'The shop confirmed Friday 10:00.', 'next_step': 'Owner drops the car off Friday'}],
                   stopped={**STOPPED, 'task_id': self.pay, 'also_open': [{'task_id': self.car, 'pending': 'Ask the shop for the total'}]},
                   workers='Worker holds Pay creators and is waiting for the bank details.', not_recorded='The shop prefers calls.')
        text = self.opening(tail='YOU: Sent the transfer to Liam.')['text']
        for part in (
                'nothing of it is carried over except what follows',
                "THE OWNER'S MESSAGES TO YOU, IN THEIR EXACT WORDS, AND WHERE EACH STANDS BY YOUR HANDOFF\n"
                '[2026-11-18 09:10 UTC, chat] Pay the creators today.\nLiam first.\n'
                f'    PARTLY: Liam is paid; Sanzida is not. Read: Task "Pay creators" ({self.pay}). Next step: Lead pays Sanzida\n'
                '[2026-11-18 09:20 UTC, thread on task "Pay creators"] And book the car in.\n'
                '    DONE: Booked for Friday.',
                f'WHERE YOU STOPPED\nTask: "Pay creators" ({self.pay})\nStep in progress: Paying the creators one by one\n'
                'Last action and its result: Sent the Wise transfer to Liam; it was accepted\nNext action: Send the transfer to Sanzida\n'
                f'Waiting on: Nothing\nAlso started and not finished: "Fix the car" ({self.car}): Ask the shop for the total',
                'OTHER TASKS YOU WROTE A NOTE ON WHEN YOU STOPPED (the note is on each Task)\n'
                f'- "Fix the car" ({self.car}). Next step: Owner drops the car off Friday',
                'YOUR WORKERS\nWorker holds Pay creators and is waiting for the bank details.\n'
                f'Open Tasks with a worker now, from the records:\n- Worker: "Pay creators" ({self.pay})',
                'NOT ON ANY RECORD\nThe shop prefers calls.',
                'THE LAST MESSAGES OF THE CLOSED CONVERSATION, WORD FOR WORD\nYOU: Sent the transfer to Liam.'):
            self.assertIn(part, text)
        self.assertNotIn('NO HANDOFF WAS FILED', text)
        self.assertNotIn('RULES THE OWNER STATED', text)

    def test_an_open_message_is_listed_again_until_a_handoff_marks_it_done(self):
        first = self.say('Pay the creators today.', 10)
        self.say('And book the car in.', 20)
        self.filed([{'id': 'm1', 'status': 'partly', 'note': 'Liam is paid; Sanzida is not.', 'task_id': self.pay, 'next_step': 'Lead pays Sanzida'},
                    {'id': 'm2', 'status': 'done', 'note': 'Booked for Friday.'}])
        self.open()
        self.now += timedelta(hours=5)
        self.say('Find me a dentist.', 200)
        listed = self.form()
        self.assertEqual([(message['id'], message['text']) for message in listed], [('m1', 'Pay the creators today.'), ('m2', 'Find me a dentist.')])
        self.assertEqual(listed[0]['open'], {'status': 'partly', 'note': 'Liam is paid; Sanzida is not.', 'next_step': 'Lead pays Sanzida',
                                             'task': {'id': self.pay, 'business_id': None, 'title': 'Pay creators'}})
        self.filed([{'id': 'm1', 'status': 'done', 'note': 'Everyone is paid.', 'task_id': self.pay},
                    {'id': 'm2', 'status': 'remaining', 'note': 'Not started.', 'task_id': self.car}])
        text = self.open()
        self.assertIn('Pay the creators today.\n    DONE: Everyone is paid.', text)
        self.now += timedelta(hours=5)
        self.assertEqual([message['text'] for message in self.form()], ['Find me a dentist.'])
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(first)

    def test_a_handoff_that_was_asked_for_answers_the_list_fixed_when_it_was_asked(self):
        self.say('Pay the creators today.', 10)
        with self.db.transaction() as uow:
            identity, request = self.tool.ask(self.ctx(self.lead), uow, context={'used_tokens': 231000, 'window_tokens': 330000})
        self.assertIn("The owner's messages to give a status:\nm1 [2026-11-18 09:10 UTC, chat] Pay the creators today.", request)
        self.say('Find me a dentist.', 150)          # written after the request, before the filing
        self.now += timedelta(minutes=40)
        self.assertEqual([message['text'] for message in self.form()], ['Pay the creators today.'])
        result = self.filed([{'id': 'm1', 'status': 'done', 'note': 'Paid.'}])
        self.assertEqual(result['compaction_id'], identity)
        row, = self.rows()
        self.assertEqual((row['reason'], row['request_json']), ('context', {'used_tokens': 231000, 'window_tokens': 330000}))
        self.assertIn('ARRIVED WHILE YOU WERE FILING. You have not answered or acted on them.\n[2026-11-18 11:30 UTC, chat] Find me a dentist.',
                      self.open())
        self.assertEqual([message['text'] for message in self.form()], ['Find me a dentist.'])

    def test_a_conversation_closed_without_a_filing_still_starts_from_the_owners_words(self):
        self.say('Pay the creators today.', 10)
        with self.db.transaction() as uow:
            self.tool.ask(self.ctx(self.lead), uow)
        text = self.open()
        self.assertIn('[2026-11-18 09:10 UTC, chat] Pay the creators today.\n    NO STATUS: your handoff did not say where this stands.', text)
        self.assertIn('NO HANDOFF WAS FILED before the previous conversation closed.', text)
        self.now += timedelta(hours=5)
        # The message never got a status, so the next handoff still answers for it.
        self.assertEqual([(message['text'], message['open']['status']) for message in self.form()], [('Pay the creators today.', 'remaining')])

    def test_an_opened_handoff_names_the_closed_and_the_fresh_conversation_and_the_fresh_one_leads_back_to_the_closed(self):
        def compact(**ids):
            with self.db.transaction() as uow:
                self.tool.ask(self.ctx(self.lead), uow)
            self.open(**ids)
            self.now += timedelta(hours=1)

        def continued(conversation, who=None):
            with self.db.read() as uow:
                return self.tool.continued(uow, who or self.lead, conversation)

        compact(closed='first', session='second')
        compact(closed='second', session='third')
        first, second = self.rows()
        self.assertEqual(first['opened_json'], {'unanswered': [], 'closed': 'first', 'session': 'second'})
        self.assertEqual(continued('second'), {'conversation_id': 'first', 'at': first['opened_at']})
        self.assertEqual(continued('third'), {'conversation_id': 'second', 'at': second['opened_at']})
        self.assertIsNone(continued('first'))                   # no compaction opened it
        self.assertIsNone(continued('elsewhere'))
        self.assertIsNone(continued('second', self.worker))     # another agent's conversation of that id
        # A row that names no conversation, or the same one twice, leads nowhere.
        compact()
        compact(closed='fourth', session='fourth')
        compact(closed='', session='fifth')
        self.assertEqual(self.rows()[2]['opened_json'], {'unanswered': [], 'closed': None, 'session': None})
        self.assertEqual([continued(conversation) for conversation in (None, 'fourth', 'fifth')], [None, None, None])
        # The newest row decides when two name the same fresh conversation.
        compact(closed='other', session='third')
        self.assertEqual(continued('third'), {'conversation_id': 'other', 'at': self.rows()[-1]['opened_at']})

    def test_the_opening_names_the_owner_rules_the_agent_filed_until_an_approved_proposal_names_them(self):
        with self.db.transaction() as uow:
            run = uow.connection.execute
            run('PRAGMA defer_foreign_keys=ON')
            run("INSERT INTO experience_clusters(id,label,created_by,created_at) VALUES('filed-by-agents','Filed by agents',?,'x')", (self.lead,))

            def filed(identity, who, kind, text, at, in_run=None):
                run('INSERT INTO experience_observations(id,run_id,cluster_id,kind,subject,text,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (identity, in_run, 'filed-by-agents', kind, 'all agents', text, who, at))
            filed('a', self.lead, 'owner_complaint', 'Ask every question  in a Signoff,\nnever in chat.', '2026-11-23T10:00:00Z')
            filed('b', self.lead, 'agent_failure', 'I guessed an identifier.', '2026-11-23T10:01:00Z')
            filed('c', self.worker, 'owner_complaint', 'Never send a LinkedIn note.', '2026-11-23T10:02:00Z')
            filed('d', self.lead, 'owner_complaint', 'x' * 500, '2026-11-23T10:03:00Z')
            filed('e', self.lead, 'owner_complaint', 'Seen by the extraction, not filed by the agent.', '2026-11-23T10:04:00Z', in_run='r1')
            self.assertEqual(self.tool.rules(uow, self.lead), ['Ask every question in a Signoff, never in chat.', 'x' * 399 + '…'])
            self.assertEqual(self.tool.rules(uow, self.worker), ['Never send a LinkedIn note.'])
            # A proposal that only waits leaves the rule listed; an approved one that names it takes it off the list.
            run("INSERT INTO agentic_delta_proposals(id,number,title,problem,change,state,created_by,created_at,updated_at) "
                "VALUES('p',1,'Questions as Signoffs','Asked in chat.','Ask in a Signoff.','waiting_for_approval',?,'x','x')", (self.lead,))
            run("INSERT INTO agentic_delta_proposal_links(proposal_id,target_type,target_id) VALUES('p','observation','a')")
            self.assertEqual(len(self.tool.rules(uow, self.lead)), 2)
            run("UPDATE agentic_delta_proposals SET state='approved', decided_at='x' WHERE id='p'")
            self.assertEqual(self.tool.rules(uow, self.lead), ['x' * 399 + '…'])
            for table in ('agentic_delta_proposal_links', 'agentic_delta_proposals', 'experience_observations', 'experience_clusters'):
                run(f'DELETE FROM {table}')
