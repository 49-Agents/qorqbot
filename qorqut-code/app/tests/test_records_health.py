"""The record health report counts each way a second record drifts from its Task, over the real table definitions."""
from datetime import datetime, timezone
import json
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut.composition import attention, schema_ddl
from qorqut.composition.catalog import builtin_registry
from qorqut.composition.policy import CORE_DEFAULT_ALLOWED
from qorqut.domains.records_health import CHECKS, RecordsHealth, disagreements, summary
from qorqut.reminders import schema as reminders
from qorqut_operations.contracts import DomainError
from qorqut_operations.crm import schema as crm
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.tasks import schema as tasks

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
RECENT = '2026-11-11T11:00:00.000000Z'
OLD = '2026-11-01T00:00:00.000000Z'
TABLES = ('businesses', 'installation', 'tasks', 'attention_memberships', 'reminders', 'focus_cards', 'crm_entries')


def definitions():
    owner = [statement for group in schema_ddl.OWNER_SCHEMA.values() for statement in group]
    for statement in (*CORE_SCHEMA, *tasks.SCHEMA, *attention.SCHEMA, *reminders.SCHEMA, *crm.SCHEMA, *owner):
        if any(statement.lstrip().startswith(f'CREATE TABLE {name} ') for name in TABLES):
            yield statement


class RecordsHealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        created = [self.conn.execute(statement) for statement in definitions()]
        self.assertEqual(len(created), len(TABLES))
        self.conn.execute("INSERT INTO businesses(id,slug,workspace,hq_path,name,created_at,updated_at,archived_at) "
                          "VALUES('live','live','w1','h1','Live',?,?,NULL),('gone','gone','w2','h2','Gone',?,?,?)",
                          (OLD, OLD, OLD, OLD, OLD))

    def task(self, identity, status='open', *, business='live', workers=(), parent=None, kind='task', updated=RECENT,
             archived=0, snoozed=None, due=None):
        fields = {'$task': {'task_type': kind, 'overview': '', 'email_conversation_id': None}} if kind else {}
        self.conn.execute(
            'INSERT INTO tasks(business_id,id,title,status,due,worker_ids_json,fields_json,archived,created_by,created_at,'
            'updated_at,snoozed_until,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (business, identity, 'Task ' + identity, status, due, json.dumps(list(workers)), json.dumps(fields), archived,
             'owner', OLD, updated, snoozed, parent))

    def todo(self, agent, identity, business='live'):
        self.conn.execute('INSERT INTO attention_memberships(id,agent_id,task_business_id,task_id,task_created_at,created_at) '
                          'VALUES(?,?,?,?,?,?)', (f'{agent}-{identity}', agent, business, identity, OLD, OLD))

    def counts(self):
        report = disagreements(self.conn, NOW)
        return {name: item['count'] for name, item in report['checks'].items() if item['count']}, report

    def test_a_consistent_installation_reports_nothing(self):
        self.task('held', 'working', workers=['worker'])
        self.task('parked', 'waiting', workers=['worker'], snoozed='2026-11-20T00:00:00.000000Z')
        self.task('finished', 'done', workers=['worker'])
        self.todo('worker', 'held')
        counts, report = self.counts()
        self.assertEqual(counts, {})
        self.assertEqual(report['disagreements'], 0)
        self.assertEqual(report['totals'], {'open_work_tasks': 2, 'todo_entries': 1})
        self.assertEqual(set(report['checks']), set(CHECKS))
        self.assertEqual((summary(report), report['summary']), ([], []))

    def test_todo_entries_are_checked_against_the_task_they_name(self):
        self.task('closed', 'done', workers=['worker'])
        self.task('cancelled', 'cancelled', workers=['worker'])
        self.task('other', workers=['someone-else'])
        self.task('mine', workers=['worker'])
        self.task('shelved', workers=['worker'], archived=1)
        self.task('orphaned', business='gone', workers=['worker'])
        self.task('global', business=None, workers=['worker'])
        for identity in ('closed', 'cancelled', 'other', 'mine', 'shelved'):
            self.todo('worker', identity)
        self.todo('worker', 'orphaned', 'gone')
        self.todo('worker', 'global', None)
        counts, report = self.counts()
        self.assertEqual(counts['todo_on_closed_task'], 2)
        self.assertEqual(counts['todo_on_archived_task'], 2)
        self.assertEqual(counts['todo_holder_not_worker'], 1)
        self.assertEqual(report['checks']['todo_holder_not_worker']['examples'],
                         [{'business_id': 'live', 'task_id': 'other', 'title': 'Task other', 'status': 'open',
                           'agent_id': 'worker'}])

    def test_work_tasks_are_checked_for_holders_parents_and_wake_times(self):
        self.task('parent', 'done', workers=['worker'])
        self.task('child', workers=['worker'], parent='parent')
        self.task('unowned')
        self.task('stalled', 'working', workers=['worker'], updated=OLD)
        self.task('busy', 'working', workers=['worker'])
        self.task('adrift', 'blocked', workers=['worker'], updated=OLD)
        self.task('dated', 'waiting', workers=['worker'], due='2026-11-20')
        self.task('in-gone', business='gone', workers=['worker'])
        # Records of another kind are not work, whatever their state.
        self.task('customer', 'waiting', kind='customer', updated=OLD)
        self.task('story', kind='story')
        self.task('legacy', kind=None, workers=['worker'])
        counts, _ = self.counts()
        self.assertEqual(counts, {'open_under_closed_parent': 1, 'open_without_worker': 1, 'working_untouched': 1,
                                  'parked_without_wake_time': 1, 'parked_untouched': 1, 'open_in_archived_project': 1})

    def test_reminders_focus_cards_and_crm_rows_are_checked_against_stored_status(self):
        self.task('closed', 'done', workers=['worker'])
        self.task('open', workers=['worker'])
        self.task('target', workers=['worker'])
        self.task('client', kind='customer')
        for identity, linked, state, occurrence in (('r1', 'closed', 'scheduled', None), ('r2', 'closed', 'due', 'o1'),
                                                    ('r3', 'closed', 'dismissed', None), ('r4', 'open', 'scheduled', None)):
            self.conn.execute(
                'INSERT INTO reminders(id,creator_id,authority_principal_id,recipient_id,text,linked_item_json,state,'
                'current_occurrence_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identity, 'owner', 'owner', 'owner', 'Check', json.dumps({'type': 'task', 'id': linked, 'business_id': 'live'}),
                 state, occurrence, OLD, OLD))
        for position, (identity, linked, archived) in enumerate((('c1', 'closed', 0), ('c2', 'closed', 1), ('c3', 'open', 0))):
            self.conn.execute(
                'INSERT INTO focus_cards(id,source_key,source_json,position,archived,created_by,created_at,updated_at) '
                'VALUES(?,?,?,?,?,?,?,?)', (identity, identity, json.dumps({'type': 'task', 'id': linked, 'business_id': 'live'}),
                                          position, archived, 'owner', OLD, OLD))
        for position, (identity, record, removed) in enumerate((('e1', 'target', 0), ('e2', 'client', 0), ('e3', 'open', 1))):
            self.conn.execute(
                'INSERT INTO crm_entries(business_id,id,table_id,record_task_id,record_business_id,record_namespace,position,'
                'removed,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('live', identity, 'table', record, 'live', 'business:live', position, removed, 'owner', 'owner', OLD, OLD))
        counts, report = self.counts()
        self.assertEqual(counts, {'reminder_on_closed_task': 2, 'focus_card_on_closed_task': 1, 'crm_record_typed_as_work': 1})
        self.assertEqual(report['checks']['focus_card_on_closed_task']['examples'][0]['card_id'], 'c1')
        self.assertEqual(report['checks']['crm_record_typed_as_work']['examples'][0]['table_id'], 'table')
        self.assertEqual(summary(report), ['2 — Live reminders linked to a done or cancelled Task',
                                           '1 — Focus cards still shown for a done or cancelled Task',
                                           '1 — CRM row records stored as open work Tasks'])

    def test_examples_are_bounded(self):
        for index in range(8):
            self.task(f'unowned-{index}')
        _, report = self.counts()
        self.assertEqual(report['checks']['open_without_worker']['count'], 8)
        self.assertEqual(len(report['checks']['open_without_worker']['examples']), 5)

    def test_only_the_owner_and_the_coordinator_read_it(self):
        self.conn.execute("INSERT INTO installation(id,installation_id,owner_principal_id,management_coordinator_id,created_at,"
                          "updated_at) VALUES('service','00000000-0000-4000-8000-000000000001','owner','coordinator',?,?)",
                          (OLD, OLD))
        installation = {'owner_principal_id': 'owner', 'management_coordinator_id': 'coordinator'}
        uow = NS(connection=self.conn, repository=lambda name: NS(get=lambda _: installation))
        service = RecordsHealth(clock=lambda: NOW)
        with patch('qorqut.management.identity.ordubashy_profile', return_value=True):
            for principal in ('owner', 'coordinator'):
                self.assertEqual(service.read(NS(principal_id=principal), {}, None, uow)['disagreements'], 0)
            with self.assertRaises(DomainError) as refused:
                service.read(NS(principal_id='worker'), {}, None, uow)
        self.assertEqual(refused.exception.code, 'forbidden')

    def test_it_registers_as_a_global_read_every_role_may_call(self):
        registry = builtin_registry()
        RecordsHealth().register(registry)
        descriptor = registry.get('records_health')
        self.assertEqual((descriptor.effect_kind, descriptor.scope_kinds, descriptor.supports_signoff),
                         ('read', frozenset({'global'}), False))
        self.assertIn('records_health', CORE_DEFAULT_ALLOWED)


if __name__ == '__main__':
    unittest.main()
