"""The boards and Focus cards follow the Task: a board entry is the Task's recorded worker, a Task card's text is the
Task's status line, and the one-time reconcile brings the records written before that into line."""
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace as NS
from uuid import uuid4

from qorqut.agents.attention import Attention
from qorqut.domains.records_health import RECONCILED, RecordsHealth
from qorqut.domains.tasks.closure import SEEN, TaskClosure, acknowledge_seen
from qorqut.domains.tasks.ports import HostBoards
from qorqut.inbox.attention import AttentionBoard
from qorqut_operations.contracts import InvocationContext, Principal

import test_task_lifecycle as base

NOW, STAMP = base.NOW, base.STAMP


class BoardFixture(base.Fixture):
    """The real Task engine with the real Todo boards wired to it the way the runtime wires them."""

    def setUp(self):
        super().setUp()
        self.app.policy.context_for = lambda agent, **_: self.ctx(agent)
        self.app.signoffs = NS(sources=NS(project_tasks=lambda ctx, tasks, uow: tasks))
        inbox = NS(clock=lambda: NOW, session_owner=lambda ctx, session, uow: {'binding_json': json.dumps({'registration_id': 'reg'})})
        runtime = NS(application=self.app, focus=NS(inbox=inbox, resolve=lambda ctx, ref, uow: {**ref, 'label': 'Task'}))
        self.attention = Attention(runtime).register()
        self.cards = object.__new__(AttentionBoard)
        self.cards.app, self.cards.legacy = self.app, NS(inbox=inbox)
        reminders = NS(release_task=lambda *a: 0)
        self.tasks.on_change = TaskClosure(self.attention, self.cards, reminders)
        self.health = RecordsHealth(clock=lambda: NOW).register(self.registry).bind(self.attention, self.cards)

    def ctx(self, who):
        if who == self.owner:
            return InvocationContext(Principal(who, 'human'), global_capabilities={'read_global_context'})
        return InvocationContext(Principal(who, 'agent'), global_capabilities={'read_global_context'}, profile_revision=1,
                                 session_registration_id='reg', inbox_session_id='session')

    def ok(self, result):
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data

    def ref(self, task):
        return {'type': 'task', 'id': task['id'], 'business_id': None}

    def board(self, agent):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute(
                'SELECT task_id FROM attention_memberships WHERE agent_id=? ORDER BY created_at,id', (agent,))]

    def stored(self, task):
        with self.db.read() as uow:
            row = uow.connection.execute('SELECT status,worker_ids_json,fields_json,revision FROM tasks WHERE id=?',
                                         (task['id'],)).fetchone()
        return {'status': row[0], 'workers': json.loads(row[1] or '[]'), 'revision': row[3],
                **{key: (json.loads(row[2] or '{}').get('$task') or {}).get(key, '') for key in ('status_line', 'next_step')}}

    def board_state(self, agent):
        with self.db.read() as uow:
            return self.attention.state(uow, agent)

    def entry(self, agent, task):
        """A board entry written the old way, by hand, with no change to the Task."""
        with self.db.transaction() as uow:
            return self.attention.enter(uow, agent, self.ref(task), task['created_at'], agent, STAMP)


class BoardsFollowTheTaskTests(BoardFixture):
    def test_the_recorded_worker_has_the_task_on_its_board_for_as_long_as_it_is_open(self):
        task = self.task(self.lead, 'Draft the offer')
        self.assertEqual((self.board(self.lead), self.board(self.worker)), ([], []))
        self.ok(self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker}))
        self.assertEqual(self.board(self.worker), [task['id']])
        self.ok(self.update(self.lead, task, {'worker_ids': [self.lead]}))
        self.assertEqual((self.board(self.lead), self.board(self.worker)), ([task['id']], []))
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': task['id']}))
        self.assertEqual(self.board(self.lead), [])
        self.ok(self.update(self.lead, task, {'status': 'open'}))
        self.assertEqual(self.board(self.lead), [task['id']])
        with self.db.read() as uow:
            history = [tuple(row) for row in uow.connection.execute(
                'SELECT agent_id,event_type,rationale FROM attention_membership_events ORDER BY rowid')]
        self.assertEqual(history, [(self.worker, 'added', ''), (self.worker, 'removed', 'No longer a recorded worker'),
                                   (self.lead, 'added', ''), (self.lead, 'removed', 'Task closed'), (self.lead, 'added', '')])

    def test_a_task_created_with_a_worker_is_on_that_board_and_a_person_gets_no_board(self):
        task = self.task(self.lead, 'Call the bank', worker_ids=[self.worker])
        self.assertEqual(self.board(self.worker), [task['id']])
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM attention_memberships').fetchone()[0], 1)

    def test_an_entry_written_before_boards_followed_the_task_stays_until_the_task_closes(self):
        task = self.task(self.lead, 'Old entry')
        self.entry(self.worker, task)
        self.ok(self.update(self.lead, task, {'next_step': 'Lead reviews it'}))
        self.assertEqual(self.board(self.worker), [task['id']])
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': task['id']}))
        self.assertEqual(self.board(self.worker), [])


class NamedWorkerTests(BoardFixture):
    """The Task engine tells the host's boards port about newly named Workers before the event is routed, and the
    closure follows the whole write afterwards. Wired together, as in the runtime, they must agree."""

    def setUp(self):
        super().setUp()
        self.app.ordubashy_attention = self.attention
        self.tasks.ports = replace(self.tasks.ports, boards=HostBoards(self.app))

    def history(self, agent, task):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute(
                "SELECT event_type FROM attention_membership_events WHERE agent_id=? AND task_id=? ORDER BY created_at,rowid",
                (agent, task['id']))]

    def test_a_named_worker_is_listed_once_and_leaves_when_the_task_names_someone_else(self):
        task = self.task(self.lead, 'Draft the offer', worker_ids=[self.worker])
        self.assertEqual(self.board(self.worker), [task['id']])
        self.assertEqual(self.history(self.worker, task), ['added'])
        self.ok(self.update(self.lead, task, {'worker_ids': [self.lead]}))
        self.assertEqual((self.board(self.worker), self.board(self.lead)), ([], [task['id']]))
        self.assertEqual(self.history(self.worker, task), ['added', 'removed'])


class TodoWritesTheWorkerTests(BoardFixture):
    def test_adding_a_task_to_todo_records_the_worker_and_removing_it_gives_the_work_up(self):
        task = self.task(self.lead, 'Review the contract')
        added = self.ok(self.invoke(self.worker, 'todo_add', {'task': self.ref(task)}))
        self.assertEqual(self.stored(task)['workers'], [self.worker])
        self.assertEqual(self.board(self.worker), [task['id']])
        # Adding it again changes nothing.
        again = self.ok(self.invoke(self.worker, 'todo_add', {'task': self.ref(task)}))
        self.assertEqual(again['membership_id'], added['membership_id'])
        self.assertEqual(self.stored(task)['revision'], 2)
        removed = self.ok(self.invoke(self.worker, 'todo_remove', {
            'membership_id': added['membership_id'], 'expected_membership_revision': added['membership_revision']}))
        self.assertTrue(removed['removed'])
        self.assertEqual((self.stored(task)['workers'], self.board(self.worker)), ([], []))

    def test_the_coordinator_puts_a_task_on_a_workers_board_by_the_same_write(self):
        task = self.task(self.lead, 'Collect the invoices')
        self.ok(self.invoke(self.lead, 'todo_add', {'agent_id': self.worker, 'task': self.ref(task)}))
        self.assertEqual((self.stored(task)['workers'], self.board(self.worker), self.board(self.lead)),
                         ([self.worker], [task['id']], []))

    def test_a_closed_task_is_not_added(self):
        task = self.task(self.lead, 'Finished')
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': task['id']}))
        self.refused(self.invoke(self.worker, 'todo_add', {'task': self.ref(task)}), 'reopen it')

    def test_focusing_on_an_open_task_starts_it_and_asks_for_no_sentence(self):
        task = self.task(self.lead, 'Write the summary', worker_ids=[self.worker])
        ref = self.ref(task)
        focused = self.ok(self.invoke(self.worker, 'attention_focus_set', {
            'focus': ref, 'task': ref, 'expected_revision': self.board_state(self.worker)['revision']}))
        self.assertEqual(focused['attention']['state'], 'focused')
        self.assertEqual(focused['attention']['intent'], '')
        self.assertEqual(self.stored(task)['status'], 'working')
        self.ok(self.update(self.worker, task, {'next_step': 'Worker drafts the first page'}))
        shown = self.ok(self.invoke(self.lead, 'todo_get', {'agent_id': self.worker, 'projection': 'focus'}))
        self.assertEqual(shown['attention']['next_step'], 'Worker drafts the first page')
        # A Task that is already past open keeps its state.
        self.ok(self.update(self.worker, task, {'status': 'waiting', 'snoozed_until': base.LATER}))
        self.ok(self.invoke(self.worker, 'attention_focus_set', {
            'focus': ref, 'task': ref, 'intent': 'Check the reply', 'expected_revision': self.board_state(self.worker)['revision']}))
        self.assertEqual(self.stored(task)['status'], 'waiting')
        self.assertEqual(self.board_state(self.worker)['intent'], 'Check the reply')


class CoordinatorReadTests(BoardFixture):
    def test_the_coordinator_reads_the_open_work_it_does_not_hold(self):
        mine = self.task(self.lead, 'Mine', worker_ids=[self.lead])
        held = self.task(self.lead, 'Held', worker_ids=[self.worker], status_line='Waiting on the supplier', next_step='Worker calls Monday')
        unowned = self.task(self.lead, 'Nobody has this')
        closed = self.task(self.lead, 'Closed')
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': closed['id']}))
        self.task(self.lead, 'A customer', task_type='customer')
        board = self.ok(self.invoke(self.lead, 'todo_get', {'projection': 'summary'}))
        self.assertEqual([item['task']['id'] for item in board['items']], [mine['id']])
        self.assertEqual(board['held_by_others']['count'], 1)
        entry = dict(board['held_by_others']['tasks'][0])
        self.assertEqual((entry['task_ref']['id'], entry['title'], list(entry['workers']), entry['status_line'], entry['next_step']),
                         (held['id'], 'Held', ['Worker'], 'Waiting on the supplier', 'Worker calls Monday'))
        self.assertEqual((board['unowned']['count'], board['unowned']['tasks'][0]['task_ref']['id']), (1, unowned['id']))
        own = self.ok(self.invoke(self.worker, 'todo_get', {'projection': 'summary'}))
        self.assertEqual([item['task']['id'] for item in own['items']], [held['id']])
        self.assertNotIn('unowned', own)
        self.assertNotIn('held_by_others', self.ok(self.invoke(self.lead, 'todo_get', {'projection': 'focus'})))


class OneRecordTests(BoardFixture):
    def test_closing_with_a_result_writes_it_once_as_the_status_line_and_the_closing_note(self):
        task = self.task(self.worker, 'Send the deck')
        done = self.ok(self.invoke(self.worker, 'tasks_complete', {'task_id': task['id'], 'result': 'Sent to the client on Tuesday'}))
        self.assertEqual((done['task']['status'], done['task']['status_line']), ('done', 'Sent to the client on Tuesday'))
        self.assertIn('Sent to the client on Tuesday', [note['body'] for note in done['task']['notes']])
        other = self.task(self.worker, 'Send the invoice')
        noted = self.ok(self.invoke(self.worker, 'tasks_complete', {'task_id': other['id'], 'result': 'Paid', 'note': 'Receipt is in the folder'}))
        self.assertEqual([note['body'] for note in noted['task']['notes'] if note.get('body')], ['Receipt is in the folder'])
        self.assertEqual(noted['task']['status_line'], 'Paid')

    def test_a_task_cards_text_is_written_to_the_task(self):
        task = self.task(self.lead, 'Sign the lease')
        with self.db.transaction() as uow:
            self.cards.write_status_line(self.ctx(self.lead), self.ref(task), '  Waiting for  the landlord ', uow)
        self.assertEqual((self.stored(task)['status_line'], self.stored(task)['revision']), ('Waiting for the landlord', 2))
        with self.db.transaction() as uow:
            self.cards.write_status_line(self.ctx(self.lead), self.ref(task), 'Waiting for the landlord', uow)
        self.assertEqual(self.stored(task)['revision'], 2)

    def test_writing_a_task_acknowledges_what_the_writer_had_seen_of_it(self):
        raw = sqlite3.connect(self.db.path)
        self.addCleanup(raw.close)
        cases = (('state', 'updated', 3, None), ('assigned', 'assignment_changed,updated', 2, None), ('comment', 'updated,note_added', 3, None),
                 ('claimed', 'updated', 3, 'claimed'), ('handled', 'updated', 3, 'handled'), ('newer', 'updated', 4, None),
                 ('elsewhere', 'updated', 1, None))
        events = {}
        for name, kind, revision, claim in cases:
            events[name] = raw.execute(
                "INSERT INTO inbox_events(source_type,source_id,source_revision,actor_id,event_type,summary,created_at) "
                "VALUES('tasks',?,?,?,?,'Task',?)", ('other' if name == 'elsewhere' else 'task', revision, self.lead, kind, STAMP)).lastrowid
            for agent in (self.worker, self.lead):
                raw.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (agent, events[name]))
            if claim:
                raw.execute('INSERT INTO operational_event_claims(event_id,assignment_id,execution_generation,state,lease_until,'
                            'handled_at,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?)',
                            (events[name], 'assignment', claim, STAMP if claim == 'claimed' else None,
                             STAMP if claim == 'handled' else None, STAMP, STAMP))
        before = {'row': {'id': 'task', 'business_id': None, 'revision': 3}}
        self.assertEqual(acknowledge_seen(NS(connection=raw), before, self.worker, lambda: NOW), 3)
        self.assertEqual(acknowledge_seen(NS(connection=raw), before, self.worker, lambda: NOW), 0)
        self.assertEqual(acknowledge_seen(NS(connection=raw), None, self.worker, lambda: NOW), 0)
        seen = {row[0]: row[1] for row in raw.execute('SELECT event_id,outcome FROM inbox_deliveries WHERE agent_id=?', (self.worker,))}
        self.assertEqual({name for name, event in events.items() if seen[event] == SEEN}, {'state', 'assigned', 'handled'})
        self.assertEqual(raw.execute('SELECT count(*) FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NOT NULL',
                                     (self.lead,)).fetchone()[0], 0)
        raw.rollback()


class ReconcileTests(BoardFixture):
    def card(self, task, reason):
        identity = str(uuid4())
        with self.db.transaction() as uow:
            position = uow.connection.execute('SELECT count(*) FROM focus_cards').fetchone()[0]
            uow.connection.execute(
                'INSERT INTO focus_cards(id,source_key,source_json,reason,position,archived,created_by,created_at,updated_at) '
                'VALUES(?,?,?,?,?,0,?,?,?)', (identity, 'source:global:task:' + task['id'], json.dumps(self.ref(task)), reason,
                                              position, self.owner, STAMP, STAMP))
        return identity

    def test_the_report_lists_the_repairs_and_reconcile_applies_exactly_those(self):
        unrecorded = self.task(self.lead, 'On a board, no worker')
        closed = self.task(self.lead, 'Closed, still on a board')
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': closed['id']}))
        self.entry(self.worker, unrecorded)
        self.entry(self.worker, closed)
        aligned = self.task(self.lead, 'Already right', worker_ids=[self.worker], status_line='Kept as it is')
        short, long = self.task(self.lead, 'Short card'), self.task(self.lead, 'Long card')
        cards = {'short': self.card(short, 'Waiting on\nthe notary'), 'long': self.card(long, 'x' * 245),
                 'kept': self.card(aligned, 'An older card text')}
        report = self.ok(self.invoke(self.owner, 'records_health', {}))['repairs']
        self.assertEqual({name: item['count'] for name, item in report.items()},
                         {'record_worker': 1, 'remove_entry': 1, 'set_status_line': 1, 'status_line_needs_rewrite': 1,
                          'retype_crm_record': 0})
        self.assertEqual(report['record_worker']['examples'][0]['task_id'], unrecorded['id'])
        self.assertEqual(report['set_status_line']['examples'][0]['text'], 'Waiting on the notary')

        self.assertEqual(self.invoke(self.worker, 'records_reconcile', {}).error['code'], 'forbidden')
        result = self.ok(self.invoke(self.owner, 'records_reconcile', {}))
        self.assertEqual(dict(result['applied']), {'record_worker': 1, 'remove_entry': 1, 'set_status_line': 1, 'retype_crm_record': 0})
        self.assertEqual(list(result['skipped']), [])
        self.assertEqual([item['task_id'] for item in result['status_line_needs_rewrite']], [long['id']])

        self.assertEqual(self.stored(unrecorded)['workers'], [self.worker])
        self.assertEqual(sorted(self.board(self.worker)), sorted([unrecorded['id'], aligned['id']]))
        self.assertEqual(self.stored(short)['status_line'], 'Waiting on the notary')
        self.assertEqual(self.stored(aligned)['status_line'], 'Kept as it is')
        with self.db.read() as uow:
            reasons = dict(uow.connection.execute('SELECT id,reason FROM focus_cards').fetchall())
            told = uow.connection.execute(
                "SELECT d.outcome FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id WHERE d.agent_id=? AND e.source_id=?",
                (self.worker, unrecorded['id'])).fetchall()
        self.assertEqual((reasons[cards['short']], reasons[cards['long']], reasons[cards['kept']]), ('', 'x' * 245, 'An older card text'))
        self.assertTrue(all(row[0] == RECONCILED for row in told))
        after = self.ok(self.invoke(self.owner, 'records_health', {}))['repairs']
        self.assertEqual({name: item['count'] for name, item in after.items()},
                         {'record_worker': 0, 'remove_entry': 0, 'set_status_line': 0, 'status_line_needs_rewrite': 1,
                          'retype_crm_record': 0})
        # Running it again changes nothing.
        again = self.ok(self.invoke(self.owner, 'records_reconcile', {}))
        self.assertEqual(dict(again['applied']), {'record_worker': 0, 'remove_entry': 0, 'set_status_line': 0, 'retype_crm_record': 0})


if __name__ == '__main__':
    import unittest
    unittest.main()
