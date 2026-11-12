"""A list of targets is rows of a table: no work Task per target, and each row says where its decisions stand."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from uuid import uuid4

from qorqut.domains.records_health import RETYPED
from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.background.programs import Program, ProgramRegistry
from qorqut_operations.crm.operations import CRMOperations
from qorqut_operations.signoffs.sources import SignoffSources
from qorqut_operations.tables.operations import TableOperations
from qorqut_operations.tasks.lifecycle import CHILD_LIMIT

import test_derived_todo as boards
import test_task_lifecycle as base

NOW, STAMP = base.NOW, base.STAMP


class ChildLimitTests(base.Fixture):
    def setUp(self):
        super().setUp()
        self.parent = self.task(self.lead, 'Contact the suppliers')
        for number in range(CHILD_LIMIT):
            self.task(self.lead, f'Supplier {number}', parent_id=self.parent['id'])

    def create(self, who, title, **extra):
        return self.invoke(who, 'tasks_create', {'scope': 'global', 'title': title, **extra})

    def test_an_agent_cannot_keep_a_long_list_as_tasks_under_one_parent(self):
        refused = self.create(self.lead, 'Supplier 10', parent_id=self.parent['id'])
        self.refused(refused, 'belongs in a table')
        self.assertEqual(dict(refused.error['details']), {'parent_id': self.parent['id'], 'open_children': CHILD_LIMIT})
        # Moving an existing Task under the full parent is the same list by another route.
        loose = self.task(self.lead, 'Supplier 11')
        self.refused(self.update(self.lead, loose, {'parent_id': self.parent['id']}), 'belongs in a table')
        # Closed children and records do not count, and other writes of the children are not stopped.
        self.assertEqual(self.create(self.lead, 'A contact', parent_id=self.parent['id'], task_type='customer').status, 'ok')
        self.assertEqual(self.update(self.lead, loose, {'status': 'working'}).status, 'ok')

    def test_the_owner_can_ask_for_separate_tasks_and_a_person_is_not_stopped(self):
        self.assertEqual(self.create(self.lead, 'Supplier 10', parent_id=self.parent['id'], owner_requested=True).status, 'ok')
        self.assertEqual(self.create(self.owner, 'Supplier 11', parent_id=self.parent['id']).status, 'ok')

    def test_a_closed_child_makes_room(self):
        child = self.task(self.lead, 'Under another parent', parent_id=self.task(self.lead, 'Other')['id'])
        with self.db.read() as uow:
            first = uow.connection.execute('SELECT id FROM tasks WHERE parent_id=? ORDER BY title LIMIT 1',
                                           (self.parent['id'],)).fetchone()[0]
        self.assertEqual(self.invoke(self.lead, 'tasks_complete', {'task_id': first}).status, 'ok')
        self.assertEqual(self.update(self.lead, child, {'parent_id': self.parent['id']}).status, 'ok')


class CrmFixture(boards.BoardFixture):
    """The board fixture with real Tables and a real CRM over the same Task engine, and Signoff sources read from the store."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        # A table write queues the job that publishes its version; the job itself is never run here.
        programs = ProgramRegistry()
        programs.register(Program('tables.global-version', 1, schema({'version_id': UUID}, ('version_id',)), lambda *a: None,
                                  scope_kinds=('global',)))
        programs.bind_authority(NS(worker_id=lambda kind, uow: self.lead, require_worker=lambda *a: None))
        self.tables = TableOperations(self.db, root, lambda business: root, clock=lambda: NOW, job_programs=programs)
        self.tables.register(self.registry)
        items = self.tasks.items
        self.crm = CRMOperations(self.app, self.tables, self.tasks, items, clock=lambda: NOW).register(self.registry)
        self.tables.bind_crm(self.crm)
        allow = NS(_context=lambda ctx, uow: ctx, _visibility=lambda ctx, uow: ('1=1', []), ports=NS(sources=object()),
                   app=NS(authorize=lambda *a, **k: None, registry=NS(get=lambda name: name)))
        self.app.signoffs = NS(sources=SignoffSources(allow))
        self.table = self.ok(self.invoke(self.lead, 'tables_create', {'scope': 'global', 'title': 'Suppliers',
                                                                        'table_kind': 'crm'}))['table']

    def add(self, who, **arguments):
        with self.db.read() as uow:
            revision = uow.connection.execute('SELECT revision FROM data_tables WHERE id=?', (self.table['id'],)).fetchone()[0]
        return self.app.invoke(self.ctx(who), 'crm_entries_add', {'scope': 'global', 'table_id': self.table['id'], **arguments},
                               {'idempotency_key': str(uuid4()), 'expected_revision': revision})

    def entries(self, who):
        return list(self.ok(self.invoke(who, 'crm_entries_list', {'scope': 'global', 'table_id': self.table['id']}))['items'])

    def row(self, task, by=None):
        """A CRM row for an existing Task, written the way rows were before a row had to be a record."""
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO crm_entries(business_id,id,table_id,record_task_id,record_business_id,record_namespace,position,"
                "cells_json,removed,created_by,updated_by,created_at,updated_at) "
                "VALUES(NULL,?,?,?,NULL,'global',(SELECT count(*) FROM crm_entries),'{}',0,?,?,?,?)",
                (str(uuid4()), self.table['id'], task['id'], by or self.lead, by or self.lead, STAMP, STAMP))

    def signoff(self, task, state, at, *, title='Approve the offer', decided=True):
        identity = str(uuid4())
        with self.db.transaction() as uow:
            sequence = uow.connection.execute('SELECT coalesce(max(updated_sequence),0)+1 FROM signoffs').fetchone()[0]
            uow.connection.execute(
                "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,operation_name,"
                "operation_version,proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,"
                "policy_binding_json,affected_scope_json,state,updated_sequence,decision_json,created_at,updated_at,category) "
                "VALUES(?,NULL,'global',?,'owner_input_request',1,1,'hash',x'00',?,'[]','{}','{}',?,?,?,?,?,'input_request')",
                (identity, self.lead, json.dumps({'title': title}), state, sequence,
                 json.dumps({'kind': 'answer', 'at': at, 'by': self.owner, 'selected_choices': ['yes'], 'notes': 'Not shown'})
                 if decided else None, at, STAMP))
            uow.connection.execute(
                "INSERT INTO signoff_source_links(id,signoff_id,source_type,source_id,source_business_id,source_namespace,"
                "created_by,created_at) VALUES(?,?,'task',?,NULL,'global',?,?)", (str(uuid4()), identity, task['id'], self.lead, at))
        return identity


class RowsAreRecordsTests(CrmFixture):
    def test_an_agents_new_row_is_a_customer_record_and_never_a_work_task(self):
        added = self.ok(self.add(self.lead, create={'title': 'Northwind'}))
        self.assertEqual(added['entry']['record']['task_type'], 'customer')
        self.refused(self.add(self.lead, create={'title': 'Apply to Northwind', 'task_type': 'task'}), 'a record, not a piece of work')
        # A person arranging the table by hand is not stopped.
        self.assertEqual(self.ok(self.add(self.owner, create={'title': 'By hand', 'task_type': 'task'}))['entry']['record']['task_type'],
                         'task')

    def test_an_agent_cannot_turn_a_piece_of_work_into_a_row(self):
        work = self.task(self.lead, 'Call Northwind')
        self.refused(self.add(self.lead, record=self.ref(work)), 'a piece of work')
        contact = self.task(self.lead, 'Northwind', task_type='customer')
        self.assertEqual(self.add(self.lead, record=self.ref(contact)).status, 'ok')
        # A work Task that already is a row is still read and written as that row.
        self.row(work)
        self.assertEqual(self.add(self.lead, record=self.ref(work)).status, 'ok')
        self.assertEqual(len(self.entries(self.lead)), 2)


class DecisionStateTests(CrmFixture):
    def test_each_row_says_where_its_decisions_stand(self):
        quiet, waiting, decided = (self.task(self.lead, title, task_type='customer') for title in ('Quiet', 'Waiting', 'Decided'))
        for task in (quiet, waiting, decided):
            self.row(task)
        self.signoff(waiting, 'answered', '2026-11-01T09:00:00.000000Z', title='An earlier question')
        asked = self.signoff(waiting, 'pending', '2026-11-05T09:00:00.000000Z', title='Send the offer?', decided=False)
        self.signoff(decided, 'answered', '2026-11-02T09:00:00.000000Z', title='First answer')
        latest = self.signoff(decided, 'applied', '2026-11-03T09:00:00.000000Z', title='Latest answer')
        self.signoff(decided, 'cancelled', '2026-11-04T09:00:00.000000Z', title='Withdrawn before an answer', decided=False)
        found = {item['record']['title']: dict(item['decisions']) for item in self.entries(self.lead)}
        self.assertEqual(found['Quiet'], {'state': 'none', 'pending': 0, 'last': None})
        self.assertEqual((found['Waiting']['state'], found['Waiting']['pending']), ('waiting_on_owner', 1))
        self.assertEqual(found['Waiting']['last']['title'], 'An earlier question')
        self.assertEqual([request['id'] for request in self.entries(self.lead)[1]['record']['input_requests']], [asked])
        self.assertEqual(found['Decided']['state'], 'decided')
        self.assertEqual(dict(found['Decided']['last']), {
            'type': 'signoff', 'id': latest, 'business_id': None, 'title': 'Latest answer', 'state': 'applied',
            'decided_at': '2026-11-03T09:00:00.000000Z', 'kind': 'answer', 'selected_choices': ('yes',)})

    def test_a_decision_the_reader_may_not_see_is_reported_without_its_content(self):
        task = self.task(self.lead, 'Private', task_type='customer')
        self.row(task)
        self.signoff(task, 'answered', '2026-11-02T09:00:00.000000Z')
        self.app.signoffs.sources.service._visibility = lambda ctx, uow: ('1=0', [])
        self.assertEqual(dict(self.entries(self.lead)[0]['decisions']), {'state': 'decided', 'pending': 0,
                                                                         'last': {'unavailable': True}})


class RetypeTests(CrmFixture):
    def test_reconcile_stores_open_work_typed_rows_as_customer_records(self):
        row = self.task(self.lead, 'Apply to Northwind', worker_ids=[self.worker])
        closed = self.task(self.lead, 'Apply to Southwind')
        self.ok(self.invoke(self.lead, 'tasks_complete', {'task_id': closed['id']}))
        work = self.task(self.lead, 'Write the applications', worker_ids=[self.worker])
        for task in (row, closed):
            self.row(task)
        report = self.ok(self.invoke(self.owner, 'records_health', {}))
        self.assertEqual(report['repairs']['retype_crm_record']['count'], 1)
        self.assertEqual(report['checks']['crm_record_typed_as_work']['count'], 1)

        # Only the named repair runs.
        only = self.ok(self.invoke(self.lead, 'records_reconcile', {'repairs': ['remove_entry']}))
        self.assertEqual(dict(only['applied'])['retype_crm_record'], 0)
        result = self.ok(self.invoke(self.lead, 'records_reconcile', {'repairs': ['retype_crm_record']}))
        self.assertEqual((dict(result['applied'])['retype_crm_record'], result['retype_remaining'], list(result['skipped'])),
                         (1, 0, []))
        self.assertEqual(result['health']['checks']['crm_record_typed_as_work']['count'], 0)

        with self.db.read() as uow:
            typed = {title: (json.loads(fields or '{}').get('$task') or {}).get('task_type', 'task')
                     for title, fields in uow.connection.execute('SELECT title,fields_json FROM tasks')}
            told = uow.connection.execute(
                "SELECT d.outcome FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id WHERE e.source_id=? "
                "AND e.source_revision=(SELECT revision FROM tasks WHERE id=?)", (row['id'], row['id'])).fetchall()
        self.assertEqual((typed['Apply to Northwind'], typed['Apply to Southwind'], typed['Write the applications']),
                         ('customer', 'task', 'task'))
        self.assertTrue(all(outcome == RETYPED for (outcome,) in told))
        # The record keeps its worker, state and thread; only what it is called changed.
        self.assertEqual((self.stored(row)['workers'], self.stored(row)['status']), ([self.worker], 'open'))
        self.assertEqual(sorted(self.board(self.worker)), sorted([row['id'], work['id']]))
        again = self.ok(self.invoke(self.lead, 'records_reconcile', {}))
        self.assertEqual(dict(again['applied'])['retype_crm_record'], 0)

    def test_a_parent_holding_a_long_list_of_tasks_is_reported(self):
        parent = self.task(self.lead, 'Contact the suppliers')
        for number in range(CHILD_LIMIT + 1):
            self.task(self.owner, f'Supplier {number}', parent_id=parent['id'])
        check = self.ok(self.invoke(self.owner, 'records_health', {}))['checks']['parent_with_many_open_children']
        self.assertEqual((check['count'], check['examples'][0]['task_id'], check['examples'][0]['open_children']),
                         (1, parent['id'], CHILD_LIMIT + 1))


if __name__ == '__main__':
    import unittest
    unittest.main()
