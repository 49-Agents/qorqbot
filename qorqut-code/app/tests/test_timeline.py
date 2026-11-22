"""Timelines of Tasks and Projects: filing, what holds now, questions, who may read and file, and a Task move. Global has no
timeline: a call that names it is answered with advice, and rows filed on it before stay in the store unread."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_operations.application.plans import PlanBuilder
from qorqut_operations.contracts import DomainError, PolicyDecision, plain
from qorqut_operations.tasks.relocation import Relocation
from qorqut_operations.timeline import state as timeline_state

from qorqut.domains.tasks.relocation import HostRelocation
from qorqut.domains.timeline import NO_GLOBAL, NO_GLOBAL_READ, Timeline

from test_single_source import Fixture

DAY = '2026-11-13T10:00:00.000000Z'


class TimelineTests(Fixture):
    reach = frozenset()

    def setUp(self):
        super().setUp()
        self.tool = Timeline(NS(application=self.app, registry=self.registry)).register()
        self.business = str(uuid4())
        self.current = frozenset({self.business})
        with self.db.transaction() as uow:
            uow.repository('businesses').insert({'id': self.business, 'name': 'Example project', 'slug': 'example',
                                                 'workspace': 'example', 'hq_path': 'hq'})
        self.work = self.task(self.lead, 'Pay creators')
        self.item = {'type': 'task', 'id': self.work['id'], 'business_id': None}
        self.project = {'type': 'project', 'id': self.business, 'business_id': self.business}
        self.global_item = {'type': 'global', 'id': 'global', 'business_id': None}
        patcher = patch('qorqut.domains.timeline.timestamp', lambda: DAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, who):
        # The coordinator reaches every current project; the worker only the ones in ``reach``.
        return replace(super().ctx(who), allowed_business_ids=self.current if who == self.lead else self.reach)

    def inside(self, title):
        """A Task created in the project, as the item that names its timeline."""
        made = self.app.invoke(replace(self.ctx(self.lead), business_id=self.business), 'tasks_create', {'title': title},
                               {'idempotency_key': str(uuid4())})
        return {'type': 'task', 'id': self.ok(made)['task']['id'], 'business_id': self.business}

    def ok(self, result):
        return plain(super().ok(result))

    def file(self, who, item, changes, **more):
        return self.app.invoke(self.ctx(who), 'timeline_file', {'scope': 'global', 'item': item, 'changes': changes, **more},
                               {'idempotency_key': str(uuid4())})

    def filed(self, item, changes, **more):
        return self.ok(self.file(self.lead, item, changes, **more))

    def asked(self, item, changes):
        """Questions an older filing left open. A new question is a question Signoff, so these are written as storage holds them."""
        from qorqut_operations.timeline import filing
        with self.db.transaction() as uow:
            return {'filed': filing.write(uow.connection, item, changes, [], created_by=self.lead, at=DAY)}

    def get(self, who, item, **more):
        return self.app.invoke(self.ctx(who), 'timeline_get', {'scope': 'global', 'item': item, **more}, {})

    def read(self, item, who=None, **more):
        return self.ok(self.get(who or self.lead, item, **more))

    def code(self, result):
        self.assertEqual(result.status, 'error', result.to_dict())
        return result.error['code']

    def add(self, kind, text, item=None):
        return self.filed(item or self.item, [{'action': 'add', 'kind': kind, 'text': text}])['filed']['entries'][0]

    # -- filing and what holds now ---------------------------------------------------------------------------------

    def test_an_add_holds_and_the_state_carries_the_ids(self):
        result = self.filed(self.item, [{'action': 'add', 'kind': 'goal', 'text': 'Pay every creator.'},
                                        {'action': 'add', 'kind': 'decision', 'text': 'One provider only.'},
                                        {'action': 'add', 'kind': 'fact', 'text': 'The hold was cleared.', 'refs': [{'type': 'task', 'id': 'x'}]},
                                        {'action': 'add', 'kind': 'progress', 'text': '9 of 14 paid.'}],
                            sources=[{'quote': 'Pay one provider only.', 'ref': 'thread:1'}])
        held = self.read(self.item)['state']
        self.assertEqual(held['revision'], 4)
        self.assertEqual((held['goal']['text'], held['progress']['text']), ('Pay every creator.', '9 of 14 paid.'))
        self.assertEqual([(row['id'], row['text']) for row in held['decisions']], [(result['filed']['entries'][1], 'One provider only.')])
        self.assertEqual([row['text'] for row in held['facts']], ['The hold was cleared.'])
        self.assertEqual(result['revision'], 4)
        self.assertEqual(self.read(self.item)['text'], result['text'])
        stored = self.rows('SELECT sources_json,refs_json,created_by FROM timeline_entries ORDER BY sequence')
        self.assertEqual(stored[0][0], '[{"quote": "Pay one provider only.", "ref": "thread:1"}]')
        self.assertEqual((stored[2][1], stored[0][2]), ('[{"type": "task", "id": "x"}]', self.lead))

    def test_a_removal_takes_the_entry_out_and_the_history_keeps_both(self):
        keep, gone = self.add('decision', 'Keep this.'), self.add('decision', 'Drop this.')
        self.filed(self.item, [{'action': 'remove', 'entry_id': gone, 'reason': 'No longer true.'}])
        self.assertEqual([row['id'] for row in self.read(self.item)['state']['decisions']], [keep])
        entries = self.read(self.item, history=True)['entries']
        self.assertEqual([(row['action'], row['text']) for row in entries], [('add', 'Keep this.'), ('add', 'Drop this.'), ('remove', 'No longer true.')])
        self.assertEqual(entries[1]['removed_by'], entries[2]['id'])
        self.assertEqual(entries[2]['removes_id'], gone)
        for again in ([{'action': 'remove', 'entry_id': gone}], [{'action': 'remove', 'entry_id': 'nothing'}],
                      [{'action': 'remove', 'entry_id': gone}, {'action': 'remove', 'entry_id': gone}]):
            self.assertEqual(self.code(self.file(self.lead, self.item, again)), 'validation_error')
        other = self.task(self.lead, 'Other work')
        foreign = self.add('fact', 'Elsewhere.', {'type': 'task', 'id': other['id'], 'business_id': None})
        self.assertEqual(self.code(self.file(self.lead, self.item, [{'action': 'remove', 'entry_id': foreign}])), 'validation_error')

    def test_a_later_goal_and_a_later_progress_note_hold(self):
        self.add('goal', 'First goal.')
        second = self.add('goal', 'Second goal.')
        self.add('progress', 'Started.')
        self.add('progress', 'Half done.')
        held = self.read(self.item)['state']
        self.assertEqual((held['goal']['text'], held['progress']['text']), ('Second goal.', 'Half done.'))
        # A replaced goal is gone for good: removing the current one leaves none, and never brings the older one back.
        self.filed(self.item, [{'action': 'remove', 'entry_id': second}])
        held = self.read(self.item)['state']
        self.assertEqual((held['goal'], held['progress']['text']), (None, 'Half done.'))
        self.assertNotIn('Goal:', self.read(self.item)['text'])
        self.add('goal', 'Third goal.')
        self.assertEqual(self.read(self.item)['state']['goal']['text'], 'Third goal.')

    def test_an_add_that_repeats_what_stands_is_skipped(self):
        fact, goal = self.add('fact', 'The hold was cleared.'), self.add('goal', 'Pay every creator.')
        again = self.filed(self.item, [{'action': 'add', 'kind': 'fact', 'text': '  the hold was  cleared. '},
                                       {'action': 'add', 'kind': 'goal', 'text': 'Pay every creator.'},
                                       {'action': 'add', 'kind': 'decision', 'text': 'The hold was cleared.'},
                                       {'action': 'add', 'kind': 'fact', 'text': 'New.'}, {'action': 'add', 'kind': 'fact', 'text': 'New.'}])
        self.assertEqual([(row['kind'], row['stands_as']) for row in again['filed']['skipped'][:2]], [('fact', fact), ('goal', goal)])
        self.assertEqual((len(again['filed']['entries']), len(again['filed']['skipped'])), (2, 3))
        self.assertEqual([row['text'] for row in again['state']['facts']], ['The hold was cleared.', 'New.'])
        # A removed entry, and a goal that was replaced, no longer stand: filing the same words again adds them.
        self.filed(self.item, [{'action': 'remove', 'entry_id': fact}])
        self.add('goal', 'Another goal.')
        back = self.filed(self.item, [{'action': 'add', 'kind': 'fact', 'text': 'The hold was cleared.'},
                                      {'action': 'add', 'kind': 'goal', 'text': 'Pay every creator.'}])
        self.assertEqual((len(back['filed']['entries']), back['filed']['skipped']), (2, []))
        # Nothing but repeats: nothing is appended and the revision stays.
        before = self.read(self.item)['revision']
        self.assertEqual(self.filed(self.item, [{'action': 'add', 'kind': 'fact', 'text': 'New.'}])['revision'], before)

    def test_a_question_is_answered_by_a_filed_entry_or_dropped(self):
        result = self.asked(self.item, [{'action': 'ask', 'text': 'Dollars or local currency?', 'blocking': True},
                                        {'action': 'ask', 'text': 'Which month next?', 'blocking': False, 'raised_by': 'owner'}])
        first, second = result['filed']['questions']
        questions = self.read(self.item)['state']['questions']
        self.assertEqual([(row['id'], row['blocking'], row['raised_by']) for row in questions],
                         [(first, True, 'coordinator'), (second, False, 'owner')])
        answered = self.filed(self.item, [{'action': 'answer', 'question_id': first, 'kind': 'decision', 'text': 'Pay in dollars.'}])
        held = answered['state']
        self.assertEqual([row['text'] for row in held['decisions']], ['Pay in dollars.'])
        self.assertEqual([row['id'] for row in held['questions']], [second])
        row = self.rows('SELECT state,answer_entry_id,revision,sequence,closed_at FROM timeline_questions WHERE id=?', first)[0]
        self.assertEqual(row, ('answered', answered['filed']['entries'][0], 2, 3, DAY))
        self.assertEqual(self.rows('SELECT question_id FROM timeline_entries')[0][0], first)
        self.filed(self.item, [{'action': 'drop', 'question_id': second}])
        self.assertEqual(self.read(self.item)['state']['questions'], [])
        for closed in ({'action': 'drop', 'question_id': first}, {'action': 'drop', 'question_id': second},
                       {'action': 'answer', 'question_id': second, 'kind': 'fact', 'text': 'Late.'},
                       {'action': 'drop', 'question_id': 'nothing'}):
            self.assertEqual(self.code(self.file(self.lead, self.item, [closed])), 'validation_error')
        self.assertEqual(self.code(self.file(self.lead, self.item, [{'action': 'answer', 'question_id': first, 'kind': 'goal', 'text': 'x'}])),
                         'validation_error')
        # The closed questions stay readable on the first page of the history, latest closed first.
        closed = self.read(self.item, history=True)['closed_questions']
        self.assertEqual([(row['id'], row['state'], row['answer_entry_id']) for row in closed],
                         [(second, 'dropped', None), (first, 'answered', answered['filed']['entries'][0])])
        self.assertEqual(closed[1]['text'], 'Dollars or local currency?')

    def test_one_sequence_orders_entries_and_question_changes_and_is_the_revision(self):
        self.add('fact', 'One.')
        asked = self.asked(self.item, [{'action': 'ask', 'text': 'Why?', 'blocking': False}])['filed']['questions'][0]
        self.assertEqual(self.read(self.item)['revision'], 2)
        self.filed(self.item, [{'action': 'drop', 'question_id': asked}])
        self.assertEqual(self.read(self.item)['revision'], 3)
        self.add('fact', 'Four.')
        # Another item draws on the same sequence, and its own revision is the highest of its own rows.
        elsewhere = {'type': 'task', 'id': self.task(self.lead, 'Elsewhere')['id'], 'business_id': None}
        self.add('fact', 'Elsewhere.', elsewhere)
        self.assertEqual((self.read(self.item)['revision'], self.read(elsewhere)['revision']), (4, 5))
        self.assertEqual(self.read(self.project)['revision'], 0)

    def test_entries_are_never_edited_or_deleted_and_only_the_project_follows_a_move(self):
        self.add('fact', 'Fixed.')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE timeline_entries SET business_id=?', (self.business,))
        for sql in ("UPDATE timeline_entries SET text='Changed'", "UPDATE timeline_entries SET kind='goal'",
                    'UPDATE timeline_entries SET sequence=99', 'DELETE FROM timeline_entries'):
            with self.assertRaises(DomainError):
                with self.db.transaction() as uow:
                    uow.connection.execute(sql)

    # -- limits ----------------------------------------------------------------------------------------------------

    def test_limits(self):
        add = {'action': 'add', 'kind': 'fact', 'text': 'x'}
        refused = [[{**add, 'text': 'x' * 401}], [{**add, 'text': ''}], [], [add] * 11, [{**add, 'kind': 'note'}],
                   [{'action': 'ask', 'text': 'Asked on the timeline?', 'blocking': True}], [{'action': 'remove', 'entry_id': 'a', 'extra': 1}]]
        for changes in refused:
            self.assertEqual(self.code(self.file(self.lead, self.item, changes)), 'validation_error', changes)
        quote = {'quote': 'q', 'ref': 'm'}
        self.assertEqual(self.code(self.file(self.lead, self.item, [add], sources=[{**quote, 'quote': 'q' * 1001}])), 'validation_error')
        self.assertEqual(self.code(self.file(self.lead, self.item, [add], sources=[quote] * 11)), 'validation_error')
        for item in ({**self.item, 'type': 'story'}, {**self.global_item, 'id': 'other'}, {**self.project, 'business_id': None},
                     {**self.item, 'id': 'missing'}):
            self.assertIn(self.code(self.file(self.lead, item, [add])), ('validation_error', 'not_found'))
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(0,)])
        self.filed(self.item, [{**add, 'text': 'x' * 400}] * 10, sources=[quote] * 10)

    def test_the_standing_state_over_three_thousand_characters_warns_and_never_refuses(self):
        for number in range(6):
            self.assertNotIn('warning', self.filed(self.item, [{'action': 'add', 'kind': 'fact', 'text': str(number) * 400}]))
        result = self.filed(self.item, [{'action': 'add', 'kind': 'fact', 'text': 'y' * 400}])
        self.assertIn('Remove the entries that no longer hold', result['warning'])
        self.assertGreater(len(self.read(self.item)['text']), 3000)
        gone = [row['id'] for row in result['state']['facts'][:2]]
        again = self.filed(self.item, [{'action': 'remove', 'entry_id': identity} for identity in gone])
        self.assertNotIn('warning', again)

    # -- the text form ---------------------------------------------------------------------------------------------

    def test_the_state_text_form(self):
        self.assertEqual(timeline_state.render(self.state(self.item), 'Pay creators'), 'Task "Pay creators"')
        goal = self.add('goal', 'Pay every approved creator by 20 November.')
        decision = self.add('decision', 'Pay through one provider only.')
        fact = self.add('fact', 'The hold on creator R. was cleared.')
        self.add('progress', '9 of 14 paid; 5 wait for invoices.')
        asked = self.asked(self.item, [{'action': 'ask', 'text': 'Pay creator F. in dollars?', 'blocking': True},
                                       {'action': 'ask', 'text': 'Pay on Fridays?', 'blocking': False}])['filed']['questions']
        child = self.task(self.lead, 'Subtask', parent_id=self.work['id'])
        self.assertEqual(self.read(self.item)['text'], '\n'.join([
            'Task "Pay creators" — what holds now (revision 6)',
            'Goal: Pay every approved creator by 20 November.',
            'Decisions:',
            f'- [{decision}] Pay through one provider only. (13 Nov)',
            'Facts:',
            f'- [{fact}] The hold on creator R. was cleared. (13 Nov)',
            'Progress: 9 of 14 paid; 5 wait for invoices. (13 Nov)',
            'Open questions for the owner:',
            f'- [{asked[0]}, blocks] Pay creator F. in dollars?',
            f'- [{asked[1]}] Pay on Fridays?',
            'Further reading: 1 subtask (timeline_get with history)']))
        self.assertEqual(goal, self.read(self.item)['state']['goal']['id'])
        links = self.read(self.item, history=True)['links']
        self.assertEqual([(row['id'], row['title']) for row in links['subtasks']], [(child['id'], 'Subtask')])
        self.assertEqual((links['approvals'], links['reports']), ([], []))

    def state(self, item):
        with self.db.read() as uow:
            return timeline_state.state(uow.connection, item)

    def test_history_pages_oldest_first(self):
        for number in range(5):
            self.add('fact', f'Fact {number}.')
        first = self.read(self.item, history=True, limit=2)
        self.assertEqual([row['text'] for row in first['entries']], ['Fact 0.', 'Fact 1.'])
        second = self.read(self.item, history=True, limit=2, cursor=first['next_cursor'])
        self.assertEqual([row['text'] for row in second['entries']], ['Fact 2.', 'Fact 3.'])
        self.assertNotIn('links', second)
        last = self.read(self.item, history=True, limit=2, cursor=second['next_cursor'])
        self.assertEqual(([row['text'] for row in last['entries']], last['next_cursor']), (['Fact 4.'], None))

    # -- who files and who reads -----------------------------------------------------------------------------------

    def test_only_the_coordinator_in_person_files(self):
        change = [{'action': 'add', 'kind': 'fact', 'text': 'x'}]
        self.assertEqual(self.code(self.file(self.worker, self.item, change)), 'forbidden')
        self.assertEqual(self.code(self.file(self.owner, self.item, change)), 'forbidden')
        delegated = replace(self.ctx(self.lead), delegation_binding_id=str(uuid4()), delegation_assignment_id=str(uuid4()),
                            delegation_generation=1)
        result = self.app.invoke(delegated, 'timeline_file', {'scope': 'global', 'item': self.item, 'changes': change},
                                 {'idempotency_key': str(uuid4())})
        self.assertEqual(self.code(result), 'forbidden')
        dashboard = replace(self.ctx(self.lead), source='dashboard')
        self.assertEqual(self.code(self.app.invoke(dashboard, 'timeline_file', {'scope': 'global', 'item': self.item, 'changes': change},
                                                   {'idempotency_key': str(uuid4())})), 'forbidden')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(0,)])
        self.assertEqual(self.read(self.item)['revision'], 0)

    def test_a_task_timeline_is_read_with_the_task(self):
        self.add('fact', 'Visible.')
        self.assertEqual(self.read(self.item, self.worker)['state']['facts'][0]['text'], 'Visible.')
        self.app.policy = NS(resolve=lambda ctx, descriptor, *a: PolicyDecision(
            'deny' if descriptor.name == 'tasks_get' and ctx.principal_id == self.worker else 'allow', 'test'),
            context_for=self.app.policy.context_for)
        self.assertEqual(self.code(self.get(self.worker, self.item)), 'not_found')
        self.assertEqual(self.read(self.item)['revision'], 1)
        self.assertEqual(self.code(self.get(self.lead, {**self.item, 'id': 'missing'})), 'not_found')

    def test_a_task_in_a_project_needs_the_callers_reach_of_that_project(self):
        inside = self.inside('Inside')
        self.add('fact', 'Inside the project.', inside)
        self.assertEqual(self.read(inside)['state']['facts'][0]['text'], 'Inside the project.')
        self.assertEqual(self.code(self.get(self.worker, inside)), 'not_found')
        self.reach = frozenset({self.business})
        self.assertEqual(self.read(inside, self.worker)['state']['facts'][0]['text'], 'Inside the project.')

    def test_approval_and_report_titles_are_for_the_coordinator_and_the_owner(self):
        approval = str(uuid4())
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,operation_version,"
                "proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,policy_binding_json,affected_scope_json,"
                "state,updated_sequence,created_at,updated_at,category) VALUES(?,NULL,'global',?,'owner_input_request',1,1,'hash',x'00',"
                "'{\"title\":\"Send the offer to Northwind\"}','[]','{}','{}','pending',1,?,?,'input_request')", (approval, self.lead, DAY, DAY))
            uow.connection.execute(
                "INSERT INTO signoff_source_links(id,signoff_id,source_type,source_id,source_business_id,source_namespace,created_by,"
                "created_at) VALUES(?,?,'task',?,NULL,'global',?,?)", (str(uuid4()), approval, self.work['id'], self.lead, DAY))
        full = {'type': 'signoff', 'id': approval, 'title': 'Send the offer to Northwind', 'state': 'pending'}
        self.assertEqual(self.read(self.item, history=True)['links']['approvals'], [full])
        self.assertEqual(self.read(self.item, self.owner, history=True)['links']['approvals'], [full])
        self.assertEqual(self.read(self.item, self.worker, history=True)['links']['approvals'],
                         [{'type': 'signoff', 'id': approval, 'state': 'pending'}])
        self.add('fact', 'Offer drafted.')
        self.assertIn('Further reading: 1 approval', self.read(self.item, self.worker)['text'])

    def test_nothing_is_filed_on_an_archived_project(self):
        inside = self.inside('Inside')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE businesses SET archived_at=? WHERE id=?', (DAY, self.business))
        # An archived project is no longer in anyone's reach, so the call is refused before anything is checked...
        self.current = frozenset()
        change = [{'action': 'add', 'kind': 'fact', 'text': 'Late.'}]
        for item in (self.project, inside):
            self.assertEqual(self.code(self.file(self.lead, item, change)), 'not_found', item)
            # ...and a filing prepared while the project was current is not applied after it was archived.
            with self.assertRaises(DomainError) as refused, self.db.transaction() as uow:
                self.tool.file(self.lead, {'item': item, 'changes': change}, uow)
            self.assertEqual(refused.exception.code, 'business_archived')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(0,)])

    def test_a_project_timeline_needs_a_participant(self):
        self.add('decision', 'Ship in spring.', self.project)
        self.assertEqual(self.code(self.get(self.worker, self.project)), 'not_found')
        self.reach = frozenset({self.business})
        self.assertEqual(self.read(self.project, self.worker)['state']['decisions'][0]['text'], 'Ship in spring.')
        self.assertTrue(self.read(self.project, self.worker)['text'].startswith('Project "Example project"'))

    # -- Global has no timeline --------------------------------------------------------------------------------------

    def old_global_row(self, text='Filed before Global lost its timeline.'):
        """A row filed on Global before it lost its timeline, written as the store layer still accepts it."""
        return self.asked(self.global_item, [{'action': 'add', 'kind': 'decision', 'text': text}])['filed']['entries'][0]

    def refused(self, result, message):
        self.assertEqual(self.code(result), 'validation_error', result.to_dict())
        self.assertEqual(result.error['message'], message)

    def test_reading_global_is_answered_with_advice_for_every_caller(self):
        self.old_global_row()
        for who in (self.lead, self.worker, self.owner):
            self.refused(self.get(who, self.global_item), NO_GLOBAL_READ)
            self.refused(self.get(who, self.global_item, history=True), NO_GLOBAL_READ)

    def test_filing_on_global_is_refused_with_advice_after_the_filer_check(self):
        change = [{'action': 'add', 'kind': 'decision', 'text': 'Reply in plain words.'}]
        self.refused(self.file(self.lead, self.global_item, change), NO_GLOBAL)
        self.assertIn('agentic_change_propose', NO_GLOBAL)
        # Who is not the coordinator is told so first, whatever item the call names.
        self.assertEqual(self.code(self.file(self.worker, self.global_item, change)), 'forbidden')
        self.assertEqual(self.code(self.file(self.owner, self.global_item, change)), 'forbidden')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(0,)])

    def test_a_filing_held_for_review_on_global_ends_refused_when_it_is_approved(self):
        change = [{'action': 'add', 'kind': 'decision', 'text': 'Reply in plain words.'}]
        with self.assertRaises(DomainError) as refused, self.db.transaction() as uow:
            self.tool.file(self.lead, {'item': self.global_item, 'changes': change}, uow)
        self.assertEqual((refused.exception.code, refused.exception.message), ('validation_error', NO_GLOBAL))
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(0,)])

    def test_global_is_no_item_that_exists(self):
        with self.db.read() as uow:
            self.assertIsNone(self.tool.exists(self.global_item, uow))
            self.assertEqual(self.tool.exists(self.item, uow), 'Pay creators')

    def test_a_row_filed_on_global_earlier_stays_in_the_store_and_is_never_read(self):
        entry = self.old_global_row()
        self.assertEqual(self.state(self.global_item)['decisions'][0]['id'], entry)
        self.refused(self.get(self.lead, self.global_item), NO_GLOBAL_READ)
        # A Task's own read carries nothing of it.
        self.add('fact', 'Task fact.')
        self.assertNotIn('Filed before', str(self.read(self.item, history=True)))
        self.assertNotIn('Filed before', self.read(self.item)['text'])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries WHERE item_type=?', 'global'), [(1,)])

    # -- a Task move -----------------------------------------------------------------------------------------------

    def test_a_task_that_moves_takes_its_timeline_along(self):
        entry = self.add('fact', 'Moves with the Task.')
        self.asked(self.item, [{'action': 'ask', 'text': 'Where next?', 'blocking': False}])
        stay = self.task(self.lead, 'Stays')
        self.add('fact', 'Stays behind.', {'type': 'task', 'id': stay['id'], 'business_id': None})
        self.old_global_row('Global stays.')
        relocation = Relocation(None, self.business, (self.work['id'],), (self.work['id'],))
        adapter = HostRelocation(self.app)
        with self.db.transaction() as uow:
            self.assertEqual(adapter.inspect(uow, relocation).counts['timeline_rows'], 2)
            adapter.apply(uow, relocation, DAY)
        self.assertEqual(self.rows('SELECT id,business_id FROM timeline_entries WHERE item_id=?', self.work['id']), [(entry, self.business)])
        self.assertEqual(self.rows('SELECT business_id FROM timeline_questions'), [(self.business,)])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries WHERE business_id IS NULL'), [(2,)])
        moved = {**self.item, 'business_id': self.business}
        self.assertEqual(self.state(moved)['facts'][0]['text'], 'Moves with the Task.')
        self.assertEqual(self.state(self.item)['revision'], 0)

    # -- preparing a filing for review -------------------------------------------------------------------------------

    def test_removals_and_answers_can_pin_their_target_rows(self):
        gone = self.add('decision', 'Old.')
        asked = self.asked(self.item, [{'action': 'ask', 'text': 'Q?', 'blocking': True}])['filed']['questions'][0]
        changes = [{'action': 'add', 'kind': 'fact', 'text': 'New.'}, {'action': 'remove', 'entry_id': gone},
                   {'action': 'answer', 'question_id': asked, 'kind': 'fact', 'text': 'Because.'}]
        with self.db.read() as uow:
            title, pins = self.tool.check(self.ctx(self.lead), {'item': self.item, 'changes': changes}, uow)
            builder = PlanBuilder(self.ctx(self.lead), 'timeline_file', {}, 'File')
            self.tool.pin(builder, self.item, pins)
        self.assertEqual(title, 'Pay creators')
        targets = {(target.resource.resource_type, target.resource.resource_id): (target.exists, target.revision)
                   for target in builder.targets.values()}
        self.assertEqual(targets, {('timeline_entries', gone): (True, None), ('timeline_questions', asked): (True, 1)})
        # The same filing made stale by another one: the second is refused rather than applied twice.
        self.filed(self.item, [{'action': 'drop', 'question_id': asked}])
        self.assertEqual(self.code(self.file(self.lead, self.item, changes)), 'validation_error')
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries'), [(1,)])


if __name__ == '__main__':
    unittest.main()
