"""Tasks that hold stored fields no definition names (older data) can still be written; undefined fields cannot be set."""
import json
import unittest

from test_single_source import Fixture

LEGACY = {'_migration_source': {'table': 'old_tasks', 'row': 7}, 'background': 'Earlier context', 'next_step': None}


class LegacyFieldTests(Fixture):
    def legacy_task(self):
        task = self.task(self.owner, 'Old work')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE tasks SET fields_json=? WHERE id=?', (json.dumps(LEGACY), task['id']))
        return task

    def stored(self, task):
        return self.rows('SELECT fields_json FROM tasks WHERE id=?', task['id'])[0][0]

    def test_status_change_keeps_legacy_fields(self):
        task = self.legacy_task()
        before = self.stored(task)
        self.ok(self.update(self.owner, task, {'status': 'cancelled'}))
        self.assertEqual(self.stored(task), before)
        self.assertEqual(json.loads(self.stored(task)), LEGACY)

    def test_other_edits_keep_legacy_fields(self):
        task = self.legacy_task()
        self.ok(self.update(self.owner, task, {'title': 'Old work, renamed'}))
        self.assertEqual(json.loads(self.stored(task)), LEGACY)

    def test_setting_an_undefined_field_is_refused(self):
        task = self.legacy_task()
        self.refused(self.update(self.owner, task, {'fields': {**LEGACY, 'background': 'Changed'}}), 'Unknown custom task field: background')
        self.refused(self.update(self.owner, task, {'fields': {**LEGACY, 'invented': 'x'}}), 'Unknown custom task field: invented')
        self.assertEqual(json.loads(self.stored(task)), LEGACY)

    def test_undefined_field_on_a_normal_task_is_refused(self):
        task = self.task(self.owner, 'Normal work')
        self.refused(self.update(self.owner, task, {'fields': {'invented': 'x'}}), 'Unknown custom task field: invented')
        self.refused(self.invoke(self.owner, 'tasks_create', {'title': 'Other', 'fields': {'invented': 'x'}}),
                     'Unknown custom task field: invented')

    def test_normal_task_is_unaffected(self):
        task = self.task(self.owner, 'Normal work')
        self.ok(self.update(self.owner, task, {'status': 'cancelled'}))
        self.assertEqual(json.loads(self.stored(task)), {})


if __name__ == '__main__':
    unittest.main()
