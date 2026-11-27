"""A record type an extension adds: a record of it is a Task of the type's base that holds ``record_type`` and the type's values
as custom fields. Its fields follow the custom-field rules; switched off or removed, the record keeps its type and values."""
from dataclasses import replace
import json
from pathlib import Path
import unittest
from uuid import uuid4

import yaml

from test_single_source import Fixture, STAMP
from qorqut_operations.contracts import plain
from qorqut_operations.tasks import lifecycle
from qorqut_operations.tasks.record_types import FixedRecordTypes, RecordType, RecordTypes

NETWORK = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"
VALUES = {'met_at': 'Lisbon, Web Summit', 'closeness': 'friendly', 'last_contact': '2026-11-20'}


def network_types():
    """The record types of the ``network`` example, as its ``records.yaml`` declares them."""
    entries = yaml.safe_load((NETWORK / 'plugin' / 'records.yaml').read_text(encoding='utf-8'))
    return RecordTypes(RecordType.from_entry('network', entry) for entry in entries)


class RecordTypeFixture(Fixture):
    ON = True  # False leaves the Task service as it is without any extension

    def setUp(self):
        super().setUp()
        if self.ON:
            self.switch(True)

    def switch(self, on):
        """The provider the host would install: the ``network`` type while the extension is on, none once it is off."""
        self.tasks.record_types = FixedRecordTypes(network_types()) if on else FixedRecordTypes()

    def connection(self, title='Ayla Demir', **extra):
        return self.task(self.lead, title, record_type='connection', fields=dict(VALUES), **extra)

    def read(self, task, who=None):
        return self.ok(self.invoke(who or self.lead, 'tasks_get', {'task_id': task['id']}))['task']

    def listed(self, **filters):
        return [task['id'] for task in self.ok(self.invoke(self.lead, 'tasks_list', filters))['tasks']]

    def stored(self, task):
        return json.loads(self.rows('SELECT fields_json FROM tasks WHERE id=?', task['id'])[0][0])

    def owner_in(self, project):
        ctx = self.ctx(self.owner)
        if project is None:
            return ctx
        return replace(ctx, allowed_project_ids=frozenset({project}), project_id=project, review_project_id=project)

    def configure(self, fields, project=None):
        scope = {} if project else {'scope': 'global'}
        current = self.ok(self.app.invoke(self.owner_in(project), 'tasks_config_get', scope, {}))
        options = {'idempotency_key': str(uuid4()), **({'expected_revision': current['revision']}
                                                       if current['revision'] is not None else {})}
        return self.app.invoke(self.owner_in(project), 'tasks_config_update', {**scope, 'config': {'v': 1, 'fields': fields}},
                               options)

    def project(self):
        identity = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('projects').insert({'id': identity, 'slug': 'p' + identity[:8], 'name': 'Alpha', 'workspace': identity[:8]})
        return identity

    def move(self, task, project):
        """Move a Global Task into ``project``: its blockers, or the Task as read there."""
        ctx = replace(self.ctx(self.owner), allowed_project_ids=frozenset({project}))
        with self.db.read() as uow:
            inspection = self.tasks.relocation.inspect(ctx, uow, None, project, [task['id']])
        if inspection.blockers:
            return inspection.blockers
        with self.db.transaction() as uow:
            self.tasks.relocation.apply(ctx, uow, None, project, [task['id']], inspection.fingerprint(self.owner),
                                        move_id=str(uuid4()), at=STAMP)
        return self.ok(self.app.invoke(self.owner_in(project), 'tasks_get', {'task_id': task['id']}, {}))['task']

    def hold(self, task, **values):
        """Write custom values straight into the stored Task, as older data the current definitions do not name."""
        with self.db.transaction() as uow:
            stored = json.loads(uow.connection.execute('SELECT fields_json FROM tasks WHERE id=?', (task['id'],)).fetchone()[0])
            uow.connection.execute('UPDATE tasks SET fields_json=? WHERE id=?', (json.dumps({**stored, **values}), task['id']))


class RecordTypeTests(RecordTypeFixture):
    def test_a_record_type_alone_makes_a_record_of_its_base(self):
        task = self.connection()
        shown = self.read(task)
        self.assertEqual((shown['task_type'], shown['customer_kind'], shown['record_type']), ('customer', 'person', 'connection'))
        self.assertEqual(dict(shown['fields']), VALUES)
        stored = self.stored(task)
        self.assertEqual((stored['$task']['task_type'], stored['$task']['record_type']), ('customer', 'connection'))
        self.assertEqual({key: stored[key] for key in VALUES}, VALUES)
        self.assertEqual(self.listed(record_type='connection'), [task['id']])
        self.assertIn(task['id'], self.listed(task_type='customer'))
        with self.db.read() as uow:
            row = uow.repository('tasks', project_id=None).get(task['id'])
        self.assertFalse(lifecycle.work(row))

    def test_the_base_type_must_agree(self):
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ayla', 'task_type': 'story', 'record_type': 'connection'}),
                     'A Connection is a customer record')
        self.assertEqual(self.connection(task_type='customer')['task_type'], 'customer')

    def test_values_follow_the_custom_field_rules(self):
        create = lambda **fields: self.invoke(self.lead, 'tasks_create', {'title': 'Ayla', 'record_type': 'connection',
                                                                           'fields': {**VALUES, **fields}})
        self.refused(create(closeness='best'), 'Custom task field has wrong type: closeness')
        self.refused(create(met_at=3), 'Custom task field has wrong type: met_at')
        self.refused(create(last_contact='soon'), 'Custom date field is invalid: last_contact')
        self.refused(create(shoe_size=42), 'Unknown custom task field: shoe_size')
        task = self.connection()
        self.refused(self.update(self.lead, task, {'fields': {**VALUES, 'closeness': 'best'}}),
                     'Custom task field has wrong type: closeness')

    def test_a_types_field_is_unknown_on_any_other_task(self):
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Plain', 'fields': {'met_at': 'x'}}),
                     'Unknown custom task field: met_at')
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ben', 'task_type': 'customer', 'fields': {'met_at': 'x'}}),
                     'Unknown custom task field: met_at')

    def test_an_unknown_record_type_is_refused(self):
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ben', 'record_type': 'colleague'}),
                     'Unknown record type: colleague')
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ben', 'record_type': 'Not An Id'}), 'arguments are invalid')

    def test_the_type_changes_both_ways_and_the_values_stay(self):
        ben = self.task(self.lead, 'Ben', task_type='customer')
        self.ok(self.update(self.lead, ben, {'record_type': 'connection', 'fields': dict(VALUES)}))
        self.assertEqual(self.read(ben)['record_type'], 'connection')
        self.ok(self.update(self.lead, ben, {'record_type': None}))
        shown = self.read(ben)
        self.assertEqual((shown['task_type'], shown['record_type'], dict(shown['fields'])), ('customer', None, VALUES))
        self.assertNotIn('record_type', self.stored(ben)['$task'])
        self.ok(self.update(self.lead, ben, {'record_type': 'connection'}))
        self.assertEqual(self.read(ben)['record_type'], 'connection')

    def test_a_change_of_base_type_ends_the_record_type(self):
        task = self.connection()
        self.ok(self.update(self.lead, task, {'task_type': 'story', 'customer_kind': None}))
        shown = self.read(task)
        self.assertEqual((shown['task_type'], shown['record_type'], dict(shown['fields'])), ('story', None, VALUES))
        self.refused(self.update(self.lead, task, {'record_type': 'connection', 'task_type': 'story'}),
                     'A Connection is a customer record')

    def test_the_configuration_lists_the_callers_types(self):
        listed = plain(self.ok(self.invoke(self.lead, 'tasks_config_get', {}))['record_types'])
        self.assertEqual(listed, [{'id': 'connection', 'label': 'Connection', 'plural': 'Connections', 'based_on': 'customer',
                                   'extension': 'network', 'fields': [
                                       {'id': 'met_at', 'label': 'Where we met', 'type': 'string'},
                                       {'id': 'closeness', 'label': 'Closeness', 'type': 'enum',
                                        'values': ['close', 'friendly', 'distant']},
                                       {'id': 'last_contact', 'label': 'Last contact', 'type': 'date'}]}])
        self.switch(False)
        self.assertEqual(plain(self.ok(self.invoke(self.lead, 'tasks_config_get', {}))['record_types']), [])

    def test_a_record_of_a_type_switched_off_keeps_its_type_and_values(self):
        task = self.connection()
        self.switch(False)
        shown = self.read(task)
        self.assertEqual((shown['task_type'], shown['record_type'], dict(shown['fields'])), ('customer', 'connection', VALUES))
        self.ok(self.update(self.lead, task, {'title': 'Ayla Demir Yilmaz'}))
        self.ok(self.complete(self.lead, task))
        self.refused(self.update(self.lead, task, {'fields': {**VALUES, 'closeness': 'close'}}), 'Unknown custom task field: closeness')
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ben', 'record_type': 'connection'}),
                     'Unknown record type: connection')
        self.assertEqual(self.listed(record_type='connection'), [task['id']])
        self.switch(True)
        self.ok(self.update(self.lead, task, {'fields': {**VALUES, 'closeness': 'close'}}))
        self.assertEqual(self.read(task)['fields']['closeness'], 'close')

    def test_the_configuration_changes_in_a_scope_holding_such_records(self):
        task = self.connection()
        self.switch(False)
        self.ok(self.configure({'priority': {'type': 'int'}}))
        self.ok(self.update(self.lead, task, {'fields': {**VALUES, 'priority': 2}}))
        # The record's kept values are not judged again, even under a definition the change removes.
        self.ok(self.configure({}))
        self.assertEqual(dict(self.read(task)['fields']), {**VALUES, 'priority': 2})
        # On an ordinary Task, removing a definition it still uses stays refused.
        self.ok(self.configure({'priority': {'type': 'int'}}))
        self.task(self.lead, 'Plain', fields={'priority': 1})
        self.refused(self.configure({}), 'Unknown custom task field: priority')
        self.switch(True)
        self.ok(self.configure({'priority': {'type': 'int'}, 'rank': {'type': 'int'}}))

    def test_a_scope_field_with_a_types_field_id(self):
        # The project names closeness too, as a number; while the type is on, the type's definition applies to its records.
        self.ok(self.configure({'closeness': {'type': 'int'}, 'priority': {'type': 'int'}}))
        task = self.connection()
        self.switch(False)
        # An edit of something else keeps the value that was right for the type.
        self.ok(self.update(self.lead, task, {'title': 'Ayla Demir Yilmaz'}))
        self.ok(self.update(self.lead, task, {'fields': {**VALUES, 'priority': 2}}))
        self.assertEqual(dict(self.read(task)['fields']), {**VALUES, 'priority': 2})
        # A changed value is judged by what applies now: the project's definition.
        self.refused(self.update(self.lead, task, {'fields': {**VALUES, 'priority': 2, 'closeness': 'close'}}),
                     'Custom task field has wrong type: closeness')
        # A configuration change that adds or changes a field leaves the record as it is.
        self.ok(self.configure({'closeness': {'type': 'enum', 'values': ['near', 'far']}, 'priority': {'type': 'int'},
                                'rank': {'type': 'int'}}))
        self.ok(self.complete(self.lead, task))
        self.assertEqual(self.read(task)['fields']['closeness'], 'friendly')
        self.refused(self.update(self.lead, task, {'fields': {**VALUES, 'priority': 2, 'closeness': 3}}),
                     'Custom task field has wrong type: closeness')
        self.ok(self.update(self.lead, task, {'fields': {**VALUES, 'priority': 2, 'closeness': 'near'}}))

    def test_a_moved_record_keeps_its_type(self):
        project = self.project()
        task = self.task(self.owner, 'Ayla Demir', record_type='connection', fields=dict(VALUES))
        shown = self.move(task, project)
        self.assertEqual((shown['project_id'], shown['record_type'], dict(shown['fields'])), (project, 'connection', VALUES))

    def test_a_record_moves_while_its_type_is_off(self):
        project = self.project()
        # The destination names closeness differently; the record keeps the value it was given under its type.
        self.ok(self.configure({'closeness': {'type': 'int'}}, project))
        task = self.connection()
        self.switch(False)
        shown = self.move(task, project)
        self.assertEqual((shown['project_id'], shown['record_type'], dict(shown['fields'])), (project, 'connection', VALUES))


class NoRecordTypeTests(RecordTypeFixture):
    ON = False

    def test_without_an_extension_there_are_no_record_types(self):
        task = self.task(self.lead, 'Plain')
        self.assertIsNone(self.ok(self.invoke(self.lead, 'tasks_get', {'task_id': task['id']}))['task']['record_type'])
        self.assertNotIn('$task', json.loads(self.rows('SELECT fields_json FROM tasks WHERE id=?', task['id'])[0][0]))
        self.assertEqual(plain(self.ok(self.invoke(self.lead, 'tasks_config_get', {}))['record_types']), [])
        self.refused(self.invoke(self.lead, 'tasks_create', {'title': 'Ben', 'record_type': 'connection'}),
                     'Unknown record type: connection')


    def test_values_no_definition_names_are_judged_as_before(self):
        # Without record types a configuration change and a move judge a Task's values exactly as they did before.
        task = self.task(self.lead, 'Plain')
        self.hold(task, met_at='Lisbon')
        self.refused(self.configure({'priority': {'type': 'int'}}), 'Unknown custom task field: met_at')
        self.ok(self.update(self.lead, task, {'title': 'Plain too'}))
        project = self.project()
        self.ok(self.configure({'met_at': {'type': 'int'}}, project))
        blockers = self.move(task, project)
        self.assertEqual([item['code'] for item in blockers], ['fields_invalid'])
        self.assertIn('Custom task field has wrong type: met_at', blockers[0]['message'])

    def test_fields_that_are_not_an_object_are_refused_as_before(self):
        task = self.task(self.lead, 'Plain')
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET fields_json='[]' WHERE id=?", (task['id'],))
        self.refused(self.configure({'priority': {'type': 'int'}}), 'Task fields must be an object')


if __name__ == '__main__':
    unittest.main()
