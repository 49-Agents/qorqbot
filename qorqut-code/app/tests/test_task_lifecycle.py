"""The Task is the one record of a piece of work: its lines, its write rules, delegation and what closing it cleans up."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.agents.attention import Attention
from qorqut.composition.assembly import host_assembly
from qorqut.composition.catalog import builtin_registry, install_contribution
from qorqut.composition.policy import resolve_operation_policy
from qorqut.domains.tasks.closure import TaskClosure
from qorqut.domains.tasks.ports import held_work
from qorqut.inbox.attention import AttentionBoard
from qorqut.reminders.service import ReminderService
from qorqut.service.changes import ChangeFeed
from qorqut.transport.mcp_presentation import present
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import lifecycle
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.ports import TaskPorts
from qorqut_operations.tasks.snooze import responsible

STAMP = '2026-11-10T12:00:00.000000Z'
NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
LATER = '2026-12-01T09:00:00.000000Z'


def profile(role):
    return json.dumps({'status': 'active', 'role': {'id': role},
                       'scope': {'kind': 'ecosystem', 'business_id': None, 'global_capabilities': ['read_global_context']}})


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.lead, self.worker = (str(uuid4()) for _ in range(3))
        with self.db.transaction() as uow:
            for identity, kind, name in ((self.owner, 'human', 'Owner'), (self.lead, 'agent', 'Lead'),
                                         (self.worker, 'agent', 'Worker')):
                uow.repository('principals').insert(dict(id=identity, kind=kind, display_name=name, active=1,
                                                         created_at=STAMP, updated_at=STAMP))
            for identity, role in ((self.lead, 'ordubashy'), (self.worker, 'global-worker')):
                uow.connection.execute(
                    'INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,created_at,'
                    "updated_at) VALUES(?,?,'ecosystem',?,?,?,?,?)",
                    (identity, identity, role, '/' + identity, profile(role), STAMP, STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                owner_principal_id=self.owner, management_coordinator_id=self.lead, created_at=STAMP, updated_at=STAMP))
        self.routed, self.changes = [], []
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        ports = TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *a: False, worker_excluded=lambda *a: False),
            focus=NS(schema={}, creation_access=noop, present_created=noop), incidents=NS(link_task=noop),
            references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda uow, business, task, actor, change=None: (self.routed.append(change), ((), ()))[1],
                       snooze_routes=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=ports)
        self.tasks.on_change = lambda ctx, uow, before, after: self.changes.append(
            (after['row']['id'], before['row']['status'] if before else None, after['row']['status']))
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)

    def ctx(self, who):
        kind = 'human' if who == self.owner else 'agent'
        return InvocationContext(Principal(who, kind), global_capabilities={'read_global_context'},
                                 profile_revision=None if kind == 'human' else 1)

    def invoke(self, who, operation, arguments):
        options = {'idempotency_key': str(uuid4())} if not operation.endswith(('_get', '_list')) else {}
        if options and 'task_id' in arguments:
            with self.db.read() as uow:
                options['expected_revision'] = uow.connection.execute('SELECT revision FROM tasks WHERE id=?',
                                                                      (arguments['task_id'],)).fetchone()[0]
        return self.app.invoke(self.ctx(who), operation, {'scope': 'global', **arguments}, options)

    def task(self, who, title, **extra):
        result = self.invoke(who, 'tasks_create', {'title': title, **extra})
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data['task']

    def update(self, who, task, changes):
        return self.invoke(who, 'tasks_update', {'task_id': task['id'], 'changes': changes})

    def refused(self, result, fragment):
        self.assertEqual(result.status, 'error', result.to_dict())
        self.assertEqual(result.error['code'], 'validation_error', result.to_dict())
        self.assertIn(fragment, result.error['message'])
        return result.error

    def status(self, task_id):
        with self.db.read() as uow:
            return uow.connection.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()[0]


class TaskLineTests(Fixture):
    def test_the_lines_are_kept_on_the_task_and_returned_wherever_it_is_read(self):
        task = self.task(self.worker, 'Send the report', status_line='  Draft is  with the reviewer ', next_step='Worker sends it Friday')
        self.assertEqual((task['status_line'], task['next_step']), ('Draft is with the reviewer', 'Worker sends it Friday'))
        updated = self.update(self.worker, task, {'status_line': 'Sent'})
        self.assertEqual(updated.data['task']['status_line'], 'Sent')
        self.assertEqual(updated.data['task']['next_step'], 'Worker sends it Friday')
        receipt = present('tasks_update', updated.to_dict(), 'compact', arguments={})
        self.assertEqual(receipt['data']['task']['status_line'], 'Sent')
        listed = self.invoke(self.worker, 'tasks_list', {}).data['tasks']
        self.assertEqual(listed[0]['status_line'], 'Sent')

    def test_an_empty_line_stores_nothing(self):
        task = self.task(self.worker, 'Plain')
        self.assertEqual((task['status_line'], task['next_step'], task['stalled']), ('', '', False))
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT fields_json FROM tasks').fetchone()[0], '{}')
        self.update(self.worker, task, {'next_step': 'Call back'})
        cleared = self.update(self.worker, task, {'next_step': ''})
        self.assertEqual(cleared.data['task']['next_step'], '')
        with self.db.read() as uow:
            self.assertNotIn('next_step', uow.connection.execute('SELECT fields_json FROM tasks').fetchone()[0])

    def test_a_line_is_short_and_the_status_line_names_no_record(self):
        task = self.task(self.worker, 'Checked')
        self.refused(self.update(self.worker, task, {'next_step': 'x' * 241}), 'at most 240')
        self.refused(self.update(self.worker, task, {'status_line': f'See {uuid4()} for detail'}), 'record identifier')
        self.assertEqual(self.update(self.worker, task, {'next_step': f'Reply on {uuid4()}'}).status, 'ok')

    def test_working_untouched_for_a_day_reads_as_stalled(self):
        row = {'status': 'working', 'updated_at': '2026-11-10T11:00:00.000000Z', 'fields_json': {}}
        now = '2026-11-11T12:00:00.000000Z'
        self.assertTrue(lifecycle.stalled(row, now))
        self.assertFalse(lifecycle.stalled({**row, 'updated_at': '2026-11-11T08:00:00.000000Z'}, now))
        self.assertFalse(lifecycle.stalled({**row, 'status': 'waiting'}, now))
        self.assertFalse(lifecycle.stalled({**row, 'fields_json': {'$task': {'task_type': 'customer'}}}, now))


class WriteRuleTests(Fixture):
    def test_an_agent_parks_work_only_with_a_time_to_come_back(self):
        task = self.task(self.worker, 'Wait for the supplier')
        self.refused(self.update(self.worker, task, {'status': 'waiting'}), 'snoozed_until or due')
        self.refused(self.update(self.worker, task, {'status': 'blocked'}), 'blocked_by')
        self.assertEqual(self.update(self.worker, task, {'status': 'waiting', 'due': '2026-12-01'}).status, 'ok')
        # Already parked: a later edit is not asked for the time again.
        self.assertEqual(self.update(self.worker, task, {'due': None, 'next_step': 'Chase'}).status, 'ok')
        snoozed = self.task(self.worker, 'Wait for the bank')
        self.assertEqual(self.update(self.worker, snoozed, {'status': 'waiting', 'snoozed_until': LATER}).status, 'ok')
        blocker = self.task(self.worker, 'Sign the contract')
        blocked = self.task(self.worker, 'Start delivery')
        self.assertEqual(self.update(self.worker, blocked, {'status': 'blocked', 'blocked_by': [blocker['id']]}).status, 'ok')

    def test_the_rules_are_for_agents_writing_work(self):
        owned = self.task(self.owner, 'Owner parks it')
        self.assertEqual(self.update(self.owner, owned, {'status': 'waiting'}).status, 'ok')
        record = self.task(self.worker, 'A company', task_type='customer')
        self.assertEqual(self.update(self.worker, record, {'status': 'waiting'}).status, 'ok')

    def test_no_new_work_goes_under_a_closed_parent(self):
        parent = self.task(self.worker, 'Campaign')
        other = self.task(self.worker, 'Stray')
        self.assertEqual(self.invoke(self.worker, 'tasks_complete', {'task_id': parent['id']}).status, 'ok')
        self.refused(self.invoke(self.worker, 'tasks_create', {'title': 'Late step', 'parent_id': parent['id']}), 'parent Task is closed')
        self.refused(self.update(self.worker, other, {'parent_id': parent['id']}), 'parent Task is closed')
        self.assertEqual(self.invoke(self.owner, 'tasks_create', {'title': 'Owner adds one', 'parent_id': parent['id']}).status, 'ok')

    def test_an_agent_does_not_close_over_open_work(self):
        parent = self.task(self.worker, 'Campaign')
        first = self.task(self.worker, 'Step one', parent_id=parent['id'])
        second = self.task(self.worker, 'Step two', parent_id=parent['id'])
        nested = self.task(self.worker, 'Step two, part one', parent_id=second['id'])
        record = self.task(self.worker, 'A company', task_type='customer', parent_id=parent['id'])
        self.invoke(self.worker, 'tasks_complete', {'task_id': first['id']})
        error = self.refused(self.invoke(self.worker, 'tasks_complete', {'task_id': parent['id']}), '1 open Task sit')
        self.assertEqual(list(error['details']['open_child_ids']), [second['id']])
        self.refused(self.update(self.worker, parent, {'status': 'cancelled'}), 'children: close')
        self.changes.clear()
        closed = self.invoke(self.worker, 'tasks_complete', {'task_id': parent['id'], 'status': 'cancelled', 'children': 'close'})
        self.assertEqual(closed.status, 'ok', closed.to_dict())
        self.assertEqual(closed.data['task']['id'], parent['id'])
        self.assertEqual([self.status(item['id']) for item in (parent, first, second, nested, record)],
                         ['cancelled', 'done', 'cancelled', 'cancelled', 'open'])
        # Each closed Task reaches the host hook and keeps its own thread entry.
        self.assertEqual(self.changes, [(parent['id'], 'open', 'cancelled'), (second['id'], 'open', 'cancelled'),
                                        (nested['id'], 'open', 'cancelled')])
        with self.db.read() as uow:
            entries = uow.connection.execute("SELECT item_id FROM thread_messages WHERE kind='system' AND body LIKE "
                                             "'State changed from open to cancelled.%'").fetchall()
        self.assertEqual({row[0] for row in entries}, {parent['id'], second['id'], nested['id']})

    def test_the_owner_closes_whatever_they_decide(self):
        parent = self.task(self.owner, 'Campaign')
        child = self.task(self.owner, 'Step', parent_id=parent['id'])
        self.assertEqual(self.invoke(self.owner, 'tasks_complete', {'task_id': parent['id']}).status, 'ok')
        self.assertEqual(self.status(child['id']), 'open')

    def test_a_second_open_task_with_the_same_title_is_refused(self):
        parent = self.task(self.worker, 'Campaign')
        first = self.task(self.worker, 'Apply to the  Fund')
        error = self.refused(self.invoke(self.worker, 'tasks_create', {'title': 'apply to the fund'}), 'already exists')
        self.assertEqual(dict(error['details']), {'existing_task_id': first['id']})
        self.task(self.worker, 'Apply to the Fund', allow_duplicate=True)
        self.task(self.worker, 'Apply to the Fund', parent_id=parent['id'])
        self.task(self.worker, 'Apply to the Fund', parent_id=parent['id'], task_type='customer')
        self.task(self.owner, 'Campaign')
        closed = self.task(self.worker, 'Monthly report')
        self.invoke(self.worker, 'tasks_complete', {'task_id': closed['id']})
        self.task(self.worker, 'Monthly report')


class DelegationTests(Fixture):
    def test_delegating_records_the_worker_and_posts_the_brief_on_the_task(self):
        task = self.task(self.lead, 'Research suppliers', worker_ids=[self.lead])
        self.routed.clear()
        result = self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                           'brief': 'Three quotes by Friday.'})
        self.assertEqual(result.status, 'ok', result.to_dict())
        self.assertEqual(list(result.data['task']['worker_ids']), [self.worker])
        self.assertEqual([note['body'] for note in result.data['task']['notes'] if note.get('kind') != 'system'],
                         ['Three quotes by Friday.'])
        self.assertEqual(self.routed, [{'from': 'open', 'to': 'open', 'new_workers': [self.worker]}])

    def test_a_closed_task_is_reopened_before_it_is_delegated(self):
        task = self.task(self.lead, 'Finished')
        self.invoke(self.lead, 'tasks_complete', {'task_id': task['id']})
        self.refused(self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker}), 'reopen')

    def test_delegation_is_decided_by_the_task_update_grant(self):
        for mode in ('allow', 'deny'):
            decision = resolve_operation_policy('tasks_delegate', {'tasks_update': mode}, {}, self.registry,
                                                role_id='global-worker')
            self.assertEqual(decision.mode, mode)

    def test_the_host_catalog_takes_the_operation_and_its_arguments(self):
        registry = builtin_registry()
        install_contribution(registry, self.tasks.contribution())
        delegate = registry.get('tasks_delegate')
        self.assertEqual(set(delegate.input_schema['required']), {'task_id', 'worker_id'})
        self.assertIn('recorded worker', delegate.description)
        self.assertIn('allow_duplicate', registry.get('tasks_create').input_schema['properties'])
        self.assertEqual(list(registry.get('tasks_complete').input_schema['properties']['children']['enum']), ['close'])
        for name in ('status_line', 'next_step'):
            self.assertIn(name, registry.get('tasks_update').input_schema['properties']['changes']['properties'])

    def test_the_hook_sees_every_write_with_the_state_before_and_after(self):
        task = self.task(self.worker, 'Hooked')
        self.update(self.worker, task, {'status': 'working'})
        self.invoke(self.worker, 'tasks_complete', {'task_id': task['id']})
        self.assertEqual(self.changes, [(task['id'], None, 'open'), (task['id'], 'open', 'working'),
                                        (task['id'], 'working', 'done')])

    def test_a_snooze_wakes_the_recorded_worker_too(self):
        with self.db.read() as uow:
            task = {'business_id': None, 'id': 'absent', 'created_by': self.lead, 'worker_ids_json': json.dumps([self.worker])}
            self.assertEqual(responsible(uow, task), [self.lead, self.worker])


class HeldWorkRoutingTests(Fixture):
    def routes(self, actor, change, kind='task'):
        task = {'id': 'task', 'fields_json': json.dumps({'$task': {'task_type': kind}} if kind != 'task' else {})}
        with self.db.read() as uow:
            return held_work(uow, task, change, actor)

    def test_a_new_worker_is_told_the_task_is_theirs(self):
        self.assertEqual(self.routes(self.lead, {'from': 'open', 'to': 'open', 'new_workers': [self.worker]}), {self.worker})

    def test_the_coordinator_hears_when_another_agent_stops_working(self):
        for state in ('done', 'cancelled', 'blocked', 'waiting'):
            self.assertEqual(self.routes(self.worker, {'from': 'working', 'to': state, 'new_workers': []}), {self.lead})
        quiet = [(self.worker, {'from': 'open', 'to': 'working', 'new_workers': []}, 'task'),
                 (self.worker, {'from': 'working', 'to': 'working', 'new_workers': []}, 'task'),
                 (self.worker, {'from': None, 'to': 'done', 'new_workers': []}, 'task'),
                 (self.lead, {'from': 'working', 'to': 'done', 'new_workers': []}, 'task'),
                 (self.owner, {'from': 'working', 'to': 'done', 'new_workers': []}, 'task'),
                 (self.worker, {'from': 'working', 'to': 'done', 'new_workers': []}, 'customer')]
        for actor, change, kind in quiet:
            self.assertEqual(self.routes(actor, change, kind), set())


class ClosureTests(Fixture):
    """What the host does when a Task closes, over the real tables."""

    def state(self, status, before='working'):
        row = {'id': 'task', 'business_id': None, 'status': status, 'created_at': STAMP}
        return ({'row': {**row, 'status': before}} if before else None), {'row': row}

    def test_only_a_move_into_a_closed_state_cleans_up(self):
        calls = []
        closure = TaskClosure(NS(release_task=lambda *a: calls.append('todo') or 2),
                              NS(release_task=lambda *a: calls.append('focus') or True),
                              NS(release_task=lambda *a: calls.append('reminders') or 1))
        ctx = NS(principal_id=self.worker)
        self.assertEqual(closure(ctx, None, *self.state('done')), {'todo_entries': 2, 'focus_card': True, 'reminders': 1})
        self.assertEqual(calls, ['todo', 'focus', 'reminders'])
        for before, after in (('working', 'waiting'), ('done', 'cancelled'), ('open', 'working')):
            self.assertIsNone(closure(ctx, None, *self.state(after, before)))
        self.assertEqual(len(calls), 3)

    def test_the_task_leaves_every_todo_board_and_a_focus_on_it_is_cleared(self):
        attention = object.__new__(Attention)
        attention.clock = lambda: NOW
        ref = json.dumps({'type': 'task', 'id': 'task', 'business_id': None})
        with self.db.transaction() as uow:
            for agent, focus in ((self.lead, None), (self.worker, ref)):
                uow.connection.execute('INSERT INTO agent_attention(agent_id,focus_json,work_task_json,focus_generation,updated_at) '
                                       'VALUES(?,?,?,1,?)', (agent, focus, focus, STAMP))
                for task in ('task', 'other'):
                    uow.connection.execute('INSERT INTO attention_memberships(id,agent_id,task_business_id,task_id,task_created_at,'
                                           'created_at) VALUES(?,?,NULL,?,?,?)', (f'{agent}-{task}', agent, task, STAMP, STAMP))
            self.assertEqual(attention.release_task(uow, None, 'task', STAMP, self.worker), 2)
            self.assertEqual(attention.release_task(uow, None, 'task', STAMP, self.worker), 0)
            left = uow.connection.execute('SELECT task_id FROM attention_memberships ORDER BY id').fetchall()
            self.assertEqual({row[0] for row in left}, {'other'})
            self.assertEqual(len(left), 2)
            events = uow.connection.execute("SELECT event_type,rationale,actor_id FROM attention_membership_events").fetchall()
            self.assertEqual([tuple(row) for row in events], [('removed', 'Task closed', self.worker)] * 2)
            self.assertIsNone(attention.state(uow, self.worker)['focus_json'])

    def test_its_focus_card_is_archived(self):
        board = NS(legacy=NS(inbox=NS(clock=lambda: NOW)))
        with self.db.transaction() as uow:
            for position, (identity, key) in enumerate((('card', 'source:global:task:task'), ('kept', 'source:global:task:other'))):
                uow.connection.execute(
                    'INSERT INTO focus_cards(id,source_key,source_json,position,archived,created_by,created_at,updated_at) '
                    'VALUES(?,?,?,?,0,?,?,?)', (identity, key, json.dumps({'type': 'task', 'id': key.rsplit(':', 1)[1],
                                                                           'business_id': None}), position, self.owner, STAMP, STAMP))
            self.assertTrue(AttentionBoard.release_task(board, None, 'task', uow))
            self.assertFalse(AttentionBoard.release_task(board, None, 'task', uow))
            self.assertEqual([tuple(row) for row in uow.connection.execute('SELECT id,archived FROM focus_cards ORDER BY id')],
                             [('card', 1), ('kept', 0)])

    def test_only_reminders_agents_set_for_agents_are_cancelled(self):
        service = object.__new__(ReminderService)
        service.clock = lambda: NOW
        link = json.dumps({'type': 'task', 'id': 'task', 'business_id': None})
        cases = (('agent-own', self.worker, self.worker, link, 'scheduled'), ('agent-peer', self.lead, self.worker, link, 'due'),
                 ('for-owner', self.worker, self.owner, link, 'scheduled'), ('by-owner', self.owner, self.owner, link, 'scheduled'),
                 ('elsewhere', self.worker, self.worker, json.dumps({'type': 'task', 'id': 'other', 'business_id': None}), 'scheduled'),
                 ('finished', self.worker, self.worker, link, 'dismissed'))
        with self.db.transaction() as uow:
            for identity, creator, recipient, linked, state in cases:
                occurrence = 'occurrence-' + identity if state == 'due' else None
                uow.connection.execute(
                    'INSERT INTO reminders(id,creator_id,authority_principal_id,recipient_id,text,linked_item_json,state,'
                    'current_occurrence_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (identity, creator, creator, recipient, 'Check', linked, state, occurrence, STAMP, STAMP))
                if occurrence:
                    uow.connection.execute('INSERT INTO reminder_occurrences(id,reminder_id,recipient_id,generation,due_at,'
                                           'created_at) VALUES(?,?,?,1,?,?)', (occurrence, identity, recipient, STAMP, STAMP))
            self.assertEqual(service.release_task(uow, None, 'task'), 2)
            states = dict(uow.connection.execute('SELECT id,state FROM reminders').fetchall())
            self.assertEqual(uow.connection.execute('SELECT close_reason FROM reminder_occurrences').fetchone()[0], 'cancelled')
        self.assertEqual(states, {'agent-own': 'cancelled', 'agent-peer': 'cancelled', 'for-owner': 'scheduled',
                                  'by-owner': 'scheduled', 'elsewhere': 'scheduled', 'finished': 'dismissed'})


if __name__ == '__main__':
    unittest.main()
