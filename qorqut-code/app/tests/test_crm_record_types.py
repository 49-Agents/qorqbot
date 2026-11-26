"""A CRM row can be a record of an extension's record type: it is created through the row's ``create``, keeps the base type's
rules, and a column projecting one of the type's fields shows the record's value."""
import unittest

from test_record_types import VALUES, RecordTypeFixture, network_types
from qorqut_operations.tasks.record_types import FixedRecordTypes, RecordType, RecordTypes

COLUMNS = [{'id': 'record_name', 'name': 'Name', 'type': 'text', 'record_field': 'title'},
           {'id': 'closeness', 'name': 'Closeness', 'type': 'text', 'record_field': 'fields.closeness'},
           {'id': 'last_contact', 'name': 'Last contact', 'type': 'date', 'record_field': 'fields.last_contact'}]


class CRMRecordTypeTests(RecordTypeFixture):
    def setUp(self):
        super().setUp()
        self.table = self.ok(self.invoke(self.lead, 'tables_create', {'title': 'People', 'table_kind': 'crm',
                                                                      'columns': COLUMNS}))['table']

    def add(self, who=None, **create):
        return self.invoke(who or self.lead, 'crm_entries_add', {'table_id': self.table['id'], 'create': create},
                           revision_of=('data_tables', self.table['id']))

    def entries(self, **extra):
        return self.ok(self.invoke(self.lead, 'crm_entries_list', {'table_id': self.table['id'], **extra}))['items']

    def test_a_row_is_created_as_a_record_of_the_type(self):
        entry = self.ok(self.add(title='Ayla Demir', record_type='connection', fields=dict(VALUES)))['entry']
        record = entry['record']
        self.assertEqual((record['task_type'], record['customer_kind'], record['record_type']), ('customer', 'person', 'connection'))
        self.assertEqual(dict(record['fields']), VALUES)
        self.assertEqual(self.ok(self.add(title='Ben', record_type='connection'))['entry']['record']['record_type'], 'connection')
        self.refused(self.add(title='Cem', record_type='connection', fields={'closeness': 'best'}),
                     'Custom task field has wrong type: closeness')
        self.refused(self.add(title='Cem', task_type='story', record_type='connection'), 'A Connection is a customer record')

    def test_a_column_projecting_a_types_field_shows_the_records_value(self):
        self.ok(self.add(title='Ayla Demir', record_type='connection', fields=dict(VALUES)))
        self.ok(self.add(title='Ben'))
        cells = {item['record']['title']: dict(item['cells']) for item in self.entries()}
        self.assertEqual(cells['Ayla Demir'], {'record_name': 'Ayla Demir', 'closeness': 'friendly', 'last_contact': '2026-11-20'})
        self.assertEqual(cells['Ben'], {'record_name': 'Ben', 'closeness': None, 'last_contact': None})
        self.assertEqual([item['record']['title'] for item in self.entries(query='friendly')], ['Ayla Demir'])

    def test_a_record_type_on_another_base(self):
        types = [*network_types().types.values(),
                 RecordType('chapter', 'stories', 'story', 'Chapter', 'Chapters'),
                 RecordType('milestone', 'plans', 'task', 'Milestone', 'Milestones')]
        self.tasks.record_types = FixedRecordTypes(RecordTypes(types))
        record = self.ok(self.add(title='Opening', record_type='chapter'))['entry']['record']
        self.assertEqual((record['task_type'], record['record_type']), ('story', 'chapter'))
        # A row is a record, not a piece of work, whichever way its base type is named.
        self.refused(self.add(title='Launch', record_type='milestone'),
                     'A Milestone is based on a Task, which is a piece of work, so it cannot be a CRM row.')
        self.refused(self.add(title='Launch', task_type='task', record_type='milestone'), 'A Milestone is based on a Task')
        self.refused(self.add(title='Launch', task_type='task'), 'A CRM row is a record, not a piece of work')
        self.assertEqual(self.ok(self.add(self.owner, title='Launch', record_type='milestone'))['entry']['record']['task_type'],
                         'task')

    def test_a_row_of_a_type_switched_off(self):
        self.ok(self.add(title='Ayla Demir', record_type='connection', fields=dict(VALUES)))
        self.switch(False)
        cells = {item['record']['title']: dict(item['cells']) for item in self.entries()}
        # Undefined in the record's scope once the type is off: the projection shows nothing, and the value stays on the record.
        self.assertEqual(cells['Ayla Demir']['closeness'], None)
        self.assertEqual(self.entries()[0]['record']['fields']['closeness'], 'friendly')
        self.refused(self.add(title='Ben', record_type='connection'), 'Unknown record type: connection')


if __name__ == '__main__':
    unittest.main()
