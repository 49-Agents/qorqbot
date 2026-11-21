"""A CRM entry names its record by type and id; the record's project is found from the id when it is not stated."""
from dataclasses import replace
import unittest
from uuid import uuid4

from test_single_source import Fixture, STAMP


class CRMRecordReferenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.alpha, self.beta = sorted(str(uuid4()) for _ in range(2))
        with self.db.transaction() as uow:
            for project, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta')):
                uow.repository('projects').insert({'id': project, 'slug': name.lower(), 'name': name, 'workspace': name.lower()})
        self.reach = frozenset({self.alpha})
        self.table = self.ok(self.invoke(self.lead, 'tables_create', {'title': 'Suppliers', 'table_kind': 'crm'}))['table']
        self.in_alpha = self.customer(self.alpha, 'Alpha supplier')
        self.in_beta = self.customer(self.beta, 'Beta supplier')
        self.in_global = self.customer(None, 'Global supplier')

    def ctx(self, who):
        ctx = super().ctx(who)
        return ctx if who == self.owner else replace(ctx, allowed_project_ids=self.reach)

    def customer(self, project, title):
        """A customer Task made by an agent that reaches every project."""
        everywhere = replace(super().ctx(self.lead), allowed_project_ids=frozenset({self.alpha, self.beta}))
        arguments = {'title': title, 'task_type': 'customer', **({'scope': 'global'} if project is None else {})}
        result = self.app.invoke(replace(everywhere, project_id=project, review_project_id=project), 'tasks_create', arguments,
                                 {'idempotency_key': str(uuid4())})
        return self.ok(result)['task']['id']

    def add(self, record):
        return self.invoke(self.lead, 'crm_entries_add', {'table_id': self.table['id'], 'record': record},
                           revision_of=('data_tables', self.table['id']))

    def entries(self):
        return self.rows('SELECT record_task_id,record_project_id,record_namespace FROM crm_entries ORDER BY record_task_id')

    def test_a_record_without_a_project_is_added_with_the_project_that_holds_it(self):
        for identity in (self.in_alpha, self.in_global):
            self.ok(self.add({'type': 'task', 'id': identity}))
        self.assertEqual(sorted(self.entries()), sorted([(self.in_alpha, self.alpha, 'project:' + self.alpha),
                                                         (self.in_global, None, 'global')]))

    def test_what_is_stored_is_the_same_as_when_the_project_is_stated(self):
        stated = self.ok(self.add({'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha}))
        again = self.add({'type': 'task', 'id': self.in_alpha})
        # The same record is the same entry: the second call finds the first one, whatever way it named the record.
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(self.entries(), [(self.in_alpha, self.alpha, 'project:' + self.alpha)])
        self.assertEqual(again.status, 'ok', again.to_dict())
        self.assertEqual(again.data['entry']['id'], stated['entry']['id'])

    def test_a_record_outside_the_reach_is_answered_like_an_unknown_id(self):
        unknown = self.add({'type': 'task', 'id': str(uuid4())})
        self.assertEqual(unknown.status, 'error')
        hidden = self.add({'type': 'task', 'id': self.in_beta})
        self.assertEqual((hidden.status, hidden.error), (unknown.status, unknown.error))
        self.assertEqual(self.entries(), [])

    def test_a_stated_project_or_global_is_checked_as_given(self):
        self.assertEqual(self.add({'type': 'task', 'id': self.in_alpha, 'project_id': None}).status, 'error')
        self.assertEqual(self.add({'type': 'task', 'id': self.in_global, 'project_id': self.alpha}).status, 'error')
        self.assertEqual(self.entries(), [])
        self.ok(self.add({'type': 'task', 'id': self.in_global, 'project_id': None}))
        self.assertEqual(self.entries(), [(self.in_global, None, 'global')])

    def test_the_operation_completes_the_record_and_requires_no_project(self):
        descriptor = self.registry.get('crm_entries_add')
        self.assertIsNotNone(descriptor.complete_references)
        self.assertEqual(tuple(descriptor.input_schema['properties']['record']['required']), ('type', 'id'))


if __name__ == '__main__':
    unittest.main()
