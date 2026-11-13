"""One record per fact: the Task holds the work, everything else follows it. One test per rule, over the real Task engine
with Todo boards, Focus, reminders, Tables and the CRM wired to it as the runtime wires them."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.agents.attention import Attention
from qorqut.composition.assembly import host_assembly
from qorqut.domains.records_health import RecordsHealth
from qorqut.domains.tasks.closure import TaskClosure
from qorqut.domains.tasks.ports import HostBoards, held_work
from qorqut.inbox.attention import AttentionBoard
from qorqut.reminders.service import ReminderService
from qorqut.service.changes import ChangeFeed
from qorqut_operations.application.engine import Application
from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.background.programs import Program, ProgramRegistry
from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal
from qorqut_operations.crm.operations import CRMOperations
from qorqut_operations.discussion.threads import OWNER_POST_LIMIT
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.signoffs.sources import SignoffSources
from qorqut_operations.storage.database import Database
from qorqut_operations.tables.operations import TableOperations
from qorqut_operations.tasks.lifecycle import CHILD_LIMIT
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.ports import TaskPorts

STAMP = '2026-11-10T12:00:00.000000Z'
NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
LATER = '2026-12-01T09:00:00.000000Z'


def profile(role):
    return json.dumps({'status': 'active', 'role': {'id': role},
                       'scope': {'kind': 'ecosystem', 'business_id': None, 'global_capabilities': ['read_global_context']}})


class Fixture(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.db = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.lead, self.worker = (str(uuid4()) for _ in range(3))
        with self.db.transaction() as uow:
            for identity, kind, name in ((self.owner, 'human', 'Owner'), (self.lead, 'agent', 'Lead'), (self.worker, 'agent', 'Worker')):
                uow.repository('principals').insert(dict(id=identity, kind=kind, display_name=name, active=1,
                                                         created_at=STAMP, updated_at=STAMP))
            for identity, role in ((self.lead, 'ordubashy'), (self.worker, 'global-worker')):
                uow.connection.execute('INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,'
                                       "created_at,updated_at) VALUES(?,?,'ecosystem',?,?,?,?,?)",
                                       (identity, identity, role, '/' + identity, profile(role), STAMP, STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()), owner_principal_id=self.owner,
                                                       management_coordinator_id=self.lead, created_at=STAMP, updated_at=STAMP))
        # The Task engine, with stand-ins only for the host knowledge these rules do not use.
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        self.app.policy.context_for = lambda agent, **_: self.ctx(agent)
        noop = lambda *a, **k: None
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), NS(resource=noop, resolve=noop, relationship_item=noop,
                                                                                    fingerprint=noop))], candidates=NS(candidates=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=TaskPorts(
            assignees=NS(protect=noop, owner_directed=lambda *a: False, worker_excluded=lambda *a: False),
            focus=NS(schema={}, creation_access=noop, present_created=noop), incidents=NS(link_task=noop),
            references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda *a: ((), ()), snooze_routes=lambda *a: ()), boards=HostBoards(self.app)))
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)
        # Todo boards, Focus and reminders, and the hook that keeps them in step with every Task write.
        inbox = NS(clock=lambda: NOW, session_owner=lambda *a: {'binding_json': json.dumps({'registration_id': 'reg'})})
        self.attention = Attention(NS(application=self.app, focus=NS(inbox=inbox, resolve=lambda ctx, ref, uow: ref))).register()
        self.app.ordubashy_attention = self.attention
        self.cards = object.__new__(AttentionBoard)
        self.cards.app, self.cards.legacy = self.app, NS(inbox=inbox)
        reminders = object.__new__(ReminderService)
        reminders.clock = lambda: NOW
        self.tasks.on_change = TaskClosure(self.attention, self.cards, reminders)
        RecordsHealth(clock=lambda: NOW).register(self.registry).bind(self.attention, self.cards)
        # Tables and the CRM; a table write queues the job that publishes its version, which never runs here.
        programs = ProgramRegistry()
        programs.register(Program('tables.global-version', 1, schema({'version_id': UUID}, ('version_id',)), noop,
                                  scope_kinds=('global',)))
        programs.bind_authority(NS(worker_id=lambda kind, uow: self.lead, require_worker=noop))
        tables = TableOperations(self.db, root, lambda business: root, clock=lambda: NOW, job_programs=programs)
        tables.register(self.registry)
        tables.bind_crm(CRMOperations(self.app, tables, self.tasks, items, clock=lambda: NOW).register(self.registry))
        self.app.signoffs = NS(sources=SignoffSources(NS(
            _context=lambda ctx, uow: ctx, _visibility=lambda ctx, uow: ('1=1', []), ports=NS(sources=object()),
            app=NS(authorize=noop, registry=NS(get=lambda name: name)))))

    # -- calls ----------------------------------------------------------------------------------------------------

    def ctx(self, who):
        if who == self.owner:
            return InvocationContext(Principal(who, 'human'), global_capabilities={'read_global_context'})
        return InvocationContext(Principal(who, 'agent'), global_capabilities={'read_global_context'}, profile_revision=1,
                                 session_registration_id='reg', inbox_session_id='session')

    def invoke(self, who, operation, arguments, revision_of=None):
        options = {} if operation.endswith(('_get', '_list')) else {'idempotency_key': str(uuid4())}
        table, key = revision_of or (('tasks', arguments['task_id']) if 'task_id' in arguments else (None, None))
        if options and table:
            with self.db.read() as uow:
                options['expected_revision'] = uow.connection.execute(f'SELECT revision FROM {table} WHERE id=?', (key,)).fetchone()[0]
        return self.app.invoke(self.ctx(who), operation, {'scope': 'global', **arguments}, options)

    def ok(self, result):
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data

    def refused(self, result, fragment):
        self.assertEqual((result.status, (result.error or {}).get('code')), ('error', 'validation_error'), result.to_dict())
        self.assertIn(fragment, result.error['message'])
        return result.error

    def task(self, who, title, **extra):
        return self.ok(self.invoke(who, 'tasks_create', {'title': title, **extra}))['task']

    def update(self, who, task, changes):
        return self.invoke(who, 'tasks_update', {'task_id': task['id'], 'changes': changes})

    def complete(self, who, task, **extra):
        return self.invoke(who, 'tasks_complete', {'task_id': task['id'], **extra})

    # -- reads ----------------------------------------------------------------------------------------------------

    def ref(self, task):
        return {'type': 'task', 'id': task['id'], 'business_id': None}

    def rows(self, sql, *params):
        with self.db.read() as uow:
            return [tuple(row) for row in uow.connection.execute(sql, params)]

    def board(self, agent):
        return [row[0] for row in self.rows('SELECT task_id FROM attention_memberships WHERE agent_id=? ORDER BY created_at,id', agent)]

    def stored(self, task):
        status, workers, fields = self.rows('SELECT status,worker_ids_json,fields_json FROM tasks WHERE id=?', task['id'])[0]
        typed = json.loads(fields or '{}').get('$task') or {}
        return {'status': status, 'workers': json.loads(workers or '[]'), 'status_line': typed.get('status_line', ''),
                'task_type': typed.get('task_type', 'task')}

    def card(self, task, reason=''):
        identity = str(uuid4())
        with self.db.transaction() as uow:
            uow.connection.execute(
                'INSERT INTO focus_cards(id,source_key,source_json,reason,position,archived,created_by,created_at,updated_at) '
                "VALUES(?,?,?,?,(SELECT count(*) FROM focus_cards),0,?,?,?)",
                (identity, 'source:global:task:' + task['id'], json.dumps(self.ref(task)), reason, self.owner, STAMP, STAMP))
        return identity


class TaskTests(Fixture):
    def test_an_agent_writes_work_only_by_the_rules(self):
        task = self.task(self.worker, 'Wait for the supplier')
        self.refused(self.update(self.worker, task, {'status': 'waiting'}), 'snoozed_until or due')
        self.ok(self.update(self.worker, task, {'status': 'waiting', 'snoozed_until': LATER}))
        self.refused(self.update(self.worker, task, {'status_line': f'See {uuid4()}'}), 'record identifier')
        parent = self.task(self.worker, 'Campaign')
        child = self.task(self.worker, 'Step one', parent_id=parent['id'])
        self.refused(self.invoke(self.worker, 'tasks_create', {'title': 'Step one', 'parent_id': parent['id']}), 'already exists')
        self.refused(self.complete(self.worker, parent), '1 open Task sit')
        self.ok(self.complete(self.worker, parent, children='close'))
        self.assertEqual(self.stored(child)['status'], 'done')
        self.refused(self.invoke(self.worker, 'tasks_create', {'title': 'Late step', 'parent_id': parent['id']}), 'parent Task is closed')
        # The owner decides for themself.
        self.ok(self.invoke(self.owner, 'tasks_create', {'title': 'Late step', 'parent_id': parent['id']}))

    def test_the_coordinator_hears_when_a_worker_stops_and_a_new_worker_is_told(self):
        with self.db.read() as uow:
            heard = lambda actor, before, after, new=(): held_work(uow, {'id': 'task', 'fields_json': '{}'},
                                                                   {'from': before, 'to': after, 'new_workers': list(new)}, actor)
            self.assertEqual(heard(self.lead, 'open', 'open', [self.worker]), {self.worker})
            for state in ('done', 'cancelled', 'blocked', 'waiting'):
                self.assertEqual(heard(self.worker, 'working', state), {self.lead})
            self.assertEqual(heard(self.worker, 'open', 'working'), set())
            self.assertEqual(heard(self.lead, 'working', 'done'), set())


class BoardTests(Fixture):
    def test_delegating_records_the_worker_lists_the_task_and_posts_the_brief(self):
        task = self.task(self.lead, 'Research suppliers')
        delegated = self.ok(self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                                      'brief': 'Three quotes by Friday.'}))['task']
        self.assertEqual(list(delegated['worker_ids']), [self.worker])
        self.assertIn('Three quotes by Friday.', [note['body'] for note in delegated['notes']])
        self.assertEqual(self.board(self.worker), [task['id']])

    def test_the_board_follows_the_tasks_workers(self):
        task = self.task(self.lead, 'Draft the offer', worker_ids=[self.worker])
        self.assertEqual(self.board(self.worker), [task['id']])
        self.ok(self.update(self.lead, task, {'worker_ids': [self.lead]}))
        self.assertEqual((self.board(self.worker), self.board(self.lead)), ([], [task['id']]))
        self.assertEqual([row[0] for row in self.rows('SELECT event_type FROM attention_membership_events WHERE agent_id=? '
                                                     'ORDER BY created_at,rowid', self.worker)], ['added', 'removed'])
        # todo_add is the same write: it records the worker on the Task.
        other = self.task(self.lead, 'Collect the invoices')
        self.ok(self.invoke(self.lead, 'todo_add', {'agent_id': self.worker, 'task': self.ref(other)}))
        self.assertEqual(self.stored(other)['workers'], [self.worker])

    def test_closing_with_a_result_cleans_up_everything_that_pointed_at_the_task(self):
        task = self.task(self.lead, 'Send the deck', worker_ids=[self.worker])
        card = self.card(task)
        with self.db.transaction() as uow:
            uow.connection.execute(
                'INSERT INTO reminders(id,creator_id,authority_principal_id,recipient_id,text,linked_item_json,state,created_at,'
                "updated_at) VALUES('own',?,?,?,'Check',?,'scheduled',?,?)",
                (self.worker, self.worker, self.worker, json.dumps(self.ref(task)), STAMP, STAMP))
        done = self.ok(self.complete(self.worker, task, result='Sent to the client on Tuesday'))['task']
        self.assertEqual(done['status_line'], 'Sent to the client on Tuesday')
        self.assertIn('Sent to the client on Tuesday', [note['body'] for note in done['notes']])
        self.assertEqual(self.board(self.worker), [])
        self.assertEqual(self.rows('SELECT archived FROM focus_cards WHERE id=?', card), [(1,)])
        self.assertEqual(self.rows("SELECT state FROM reminders WHERE id='own'"), [('cancelled',)])

    def test_a_task_cards_text_is_the_tasks_status_line(self):
        task = self.task(self.lead, 'Sign the lease')
        with self.db.transaction() as uow:
            self.cards.write_status_line(self.ctx(self.lead), self.ref(task), 'Waiting for the landlord', uow)
        self.assertEqual(self.stored(task)['status_line'], 'Waiting for the landlord')


class ThreadTests(Fixture):
    def test_the_owner_sees_results_and_posts_for_them_and_nothing_else(self):
        task = self.task(self.lead, 'Book the venue', worker_ids=[self.worker])
        comment = lambda text, **extra: self.invoke(self.worker, 'tasks_comment', {'task_id': task['id'], 'text': text, **extra})
        self.ok(comment('Checked three venues, none free on the 4th'))
        self.ok(comment('Do you want the 5th instead?', for_owner=True))
        self.refused(comment('x' * (OWNER_POST_LIMIT + 1), for_owner=True), 'at most 600 characters')
        self.refused(comment(f'Done, see {uuid4()}', for_owner=True), 'no record identifiers')
        self.ok(self.complete(self.worker, task, result='Booked for the 5th'))
        read = lambda **extra: self.ok(self.invoke(self.owner, 'tasks_notes_list', {'task_id': task['id'], 'limit': 50, **extra}))
        everything, owner = read(), read(audience='owner')
        self.assertEqual(len(everything['notes']), 3)
        self.assertEqual({note['body'] for note in owner['notes']}, {'Do you want the 5th instead?', 'Booked for the 5th'})
        self.assertEqual(owner['hidden'], 1)


class TargetListTests(Fixture):
    def setUp(self):
        super().setUp()
        self.table = self.ok(self.invoke(self.lead, 'tables_create', {'title': 'Suppliers', 'table_kind': 'crm'}))['table']

    def add(self, who, **arguments):
        return self.invoke(who, 'crm_entries_add', {'table_id': self.table['id'], **arguments},
                           revision_of=('data_tables', self.table['id']))

    def row(self, task):
        """A CRM row for an existing Task, written the way rows were before a row had to be a record."""
        with self.db.transaction() as uow:
            uow.connection.execute(
                'INSERT INTO crm_entries(business_id,id,table_id,record_task_id,record_business_id,record_namespace,position,'
                "cells_json,removed,created_by,updated_by,created_at,updated_at) VALUES(NULL,?,?,?,NULL,'global',"
                "(SELECT count(*) FROM crm_entries),'{}',0,?,?,?,?)", (str(uuid4()), self.table['id'], task['id'], self.lead, self.lead,
                                                                       STAMP, STAMP))

    def test_a_list_of_targets_is_rows_not_a_task_each(self):
        parent = self.task(self.lead, 'Contact the suppliers')
        for number in range(CHILD_LIMIT):
            self.task(self.lead, f'Supplier {number}', parent_id=parent['id'])
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'One more', 'parent_id': parent['id']}), 'belongs in a table')
        self.ok(self.invoke(self.lead, 'tasks_create', {'title': 'One more', 'parent_id': parent['id'], 'owner_requested': True}))
        self.assertEqual(self.ok(self.add(self.lead, create={'title': 'Northwind'}))['entry']['record']['task_type'], 'customer')
        self.refused(self.add(self.lead, create={'title': 'Apply to Northwind', 'task_type': 'task'}), 'a record, not a piece of work')
        self.refused(self.add(self.lead, record=self.ref(self.task(self.lead, 'Call Northwind'))), 'a piece of work')

    def test_each_row_says_where_its_decisions_stand(self):
        quiet, waiting, decided = (self.task(self.lead, name, task_type='customer') for name in ('Quiet', 'Waiting', 'Decided'))
        for record in (quiet, waiting, decided):
            self.row(record)
        for record, state, day in ((waiting, 'pending', '05'), (decided, 'answered', '02'), (decided, 'applied', '03')):
            self.signoff(record, state, f'2026-11-{day}T09:00:00.000000Z')
        items = self.ok(self.invoke(self.lead, 'crm_entries_list', {'table_id': self.table['id']}))['items']
        found = {item['record']['title']: item['decisions'] for item in items}
        self.assertEqual(dict(found['Quiet']), {'state': 'none', 'pending': 0, 'last': None})
        self.assertEqual((found['Waiting']['state'], found['Waiting']['pending']), ('waiting_on_owner', 1))
        self.assertEqual((found['Decided']['state'], found['Decided']['last']['state'], found['Decided']['last']['selected_choices']),
                         ('decided', 'applied', ('yes',)))

    def signoff(self, record, state, at):
        identity = str(uuid4())
        decision = None if state == 'pending' else json.dumps({'kind': 'answer', 'at': at, 'by': self.owner, 'selected_choices': ['yes']})
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,operation_version,"
                "proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,policy_binding_json,affected_scope_json,"
                "state,updated_sequence,decision_json,created_at,updated_at,category) VALUES(?,NULL,'global',?,'owner_input_request',"
                "1,1,'hash',x'00','{\"title\":\"Approve\"}','[]','{}','{}',?,(SELECT coalesce(max(updated_sequence),0)+1 FROM signoffs),"
                "?,?,?,'input_request')", (identity, self.lead, state, decision, at, STAMP))
            uow.connection.execute(
                "INSERT INTO signoff_source_links(id,signoff_id,source_type,source_id,source_business_id,source_namespace,created_by,"
                "created_at) VALUES(?,?,'task',?,NULL,'global',?,?)", (str(uuid4()), identity, record['id'], self.lead, at))

    def test_health_lists_what_disagrees_and_reconcile_fixes_it(self):
        listed = self.task(self.lead, 'Apply to Northwind', worker_ids=[self.lead])
        self.row(listed)
        stray = self.task(self.lead, 'Closed, still on a board')
        self.ok(self.complete(self.lead, stray))
        with self.db.transaction() as uow:
            self.attention.enter(uow, self.worker, self.ref(stray), stray['created_at'], self.worker, STAMP)
        carded = self.task(self.lead, 'Card with its own text', worker_ids=[self.lead])
        self.card(carded, 'Waiting on the notary')
        # A worker reads what Ordubashy reads, and reconciling still belongs to the owner and the coordinator.
        self.ok(self.invoke(self.worker, 'records_health', {}))
        refused = self.invoke(self.worker, 'records_reconcile', {}).error
        self.assertIn(refused['code'], {'forbidden', 'policy_denied'})
        report = self.ok(self.invoke(self.owner, 'records_health', {}))
        self.assertEqual({name: check['count'] for name, check in report['checks'].items() if check['count']},
                         {'todo_on_closed_task': 1, 'crm_record_typed_as_work': 1})
        self.assertEqual({name: item['count'] for name, item in report['repairs'].items() if item['count']},
                         {'remove_entry': 1, 'set_status_line': 1, 'retype_crm_record': 1})
        fixed = self.ok(self.invoke(self.lead, 'records_reconcile', {}))
        self.assertEqual((fixed['health']['disagreements'], list(fixed['skipped'])), (0, []))
        self.assertEqual((self.stored(listed)['task_type'], self.stored(carded)['status_line'], self.board(self.worker)),
                         ('customer', 'Waiting on the notary', []))


if __name__ == '__main__':
    unittest.main()
