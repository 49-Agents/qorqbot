"""A read-and-acknowledge report may belong to a Task, and that link is not the Task's wait on the owner."""
from dataclasses import replace
import unittest

from qorqut_operations.contracts import new_id, plain

import test_signoff_acknowledge as acknowledge
from test_signoff_acknowledge import REPORT


class ReportTaskTests(unittest.TestCase):
    setUp = acknowledge.AcknowledgeTests.setUp
    invoke = acknowledge.AcknowledgeTests.invoke
    task = acknowledge.AcknowledgeTests.task
    agent = acknowledge.AcknowledgeTests.agent
    coordinator = acknowledge.AcknowledgeTests.coordinator
    refused = acknowledge.AcknowledgeTests.refused

    GLOBAL = {'id': 'work', 'project_id': None}

    def post(self, ctx, key='daily-report', *, scope='global', **extra):
        return self.app.invoke(ctx, 'signoffs_input_create', {'scope': scope, 'request_key': key, 'form': REPORT, **extra},
                               {'idempotency_key': new_id()})

    def posted(self, ctx, **arguments):
        result = self.post(ctx, **arguments)
        self.assertEqual(result.status, 'ok', result.error)
        return plain(result.data)['signoff']

    def link(self, identity):
        with self.db.read() as uow:
            row = uow.connection.execute('SELECT * FROM signoff_report_tasks WHERE signoff_id=?', (identity,)).fetchone()
        return dict(row) if row else None

    def test_a_report_without_a_task_is_created_and_reads_without_one(self):
        ctx = self.coordinator()
        signoff = self.posted(ctx)
        self.assertIsNone(signoff['report_task'])
        self.assertIsNone(self.link(signoff['id']))
        self.assertIsNone(self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']['report_task'])
        # The same report again is the same request; naming a Task the report does not have is not.
        self.assertEqual(self.posted(ctx)['id'], signoff['id'])

    def test_a_malformed_task_is_still_refused(self):
        ctx = self.coordinator()
        for task in ({'project_id': None}, {'id': '', 'project_id': None}, 'work', None):
            self.assertNotEqual(self.post(ctx, report_task=task).status, 'ok', task)

    def test_a_report_with_a_readable_task_is_created_and_read_back_with_it(self):
        ctx = self.coordinator()
        signoff = self.posted(ctx, report_task=self.GLOBAL)
        self.assertEqual(signoff['report_task'], {'type': 'task', 'id': 'work', 'project_id': None, 'title': 'Example work'})
        got = self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']
        self.assertEqual(got['report_task'], signoff['report_task'])
        listed = self.invoke('signoffs_list', {'states': ['pending']})['items']
        self.assertEqual([row['report_task']['id'] for row in listed], ['work'])
        self.assertEqual(self.link(signoff['id'])['task_id'], 'work')
        # A closed Task is fine: reports follow finished work.
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET status='done' WHERE project_id IS NULL AND id='work'")
        self.assertEqual(self.posted(ctx, key='second', report_task=self.GLOBAL)['report_task']['id'], 'work')

    def test_a_missing_or_unreadable_task_is_refused(self):
        ctx = self.coordinator()
        self.refused(self.post(ctx, report_task={'id': 'nothing', 'project_id': None}), 'validation_error')
        self.policy.modes['tasks_get'] = 'deny'
        self.refused(self.post(ctx, report_task=self.GLOBAL), 'validation_error')

    def test_the_task_and_the_report_share_a_scope(self):
        ctx = self.coordinator()
        self.refused(self.post(ctx, report_task={'id': 'project-work', 'project_id': self.project}), 'validation_error')
        project = replace(ctx, project_id=self.project, review_project_id=self.project)
        self.refused(self.post(project, scope='project', report_task=self.GLOBAL), 'validation_error')
        signoff = self.posted(project, scope='project', report_task={'id': 'project-work', 'project_id': self.project})
        self.assertEqual((signoff['review_project_id'], signoff['report_task']['project_id'], signoff['report_task']['title']),
                         (self.project, self.project, 'Example work'))

    def test_one_task_holds_many_reports(self):
        ctx = self.coordinator()
        first = self.posted(ctx, key='one', report_task=self.GLOBAL)
        second = self.posted(ctx, key='two', report_task=self.GLOBAL)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual((self.link(first['id'])['task_id'], self.link(second['id'])['task_id']), ('work', 'work'))

    def test_a_new_report_replaces_the_old_one_and_carries_its_own_task(self):
        ctx = self.coordinator()
        with self.db.transaction() as uow:
            uow.repository('tasks', project_id=None).insert({'id': 'other', 'title': 'Other work', 'status': 'working', 'created_by': self.owner})
        first = self.posted(ctx, report_task=self.GLOBAL)
        # The same report again is the same request; the same key for another Task is a conflict, not a silent relink.
        self.assertEqual(self.posted(ctx, report_task=self.GLOBAL)['id'], first['id'])
        self.refused(self.post(ctx, report_task={'id': 'other', 'project_id': None}), 'request_conflict')
        self.refused(self.post(ctx), 'request_conflict')
        replacement = self.app.invoke(ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': 'daily-report',
            'form': {**REPORT, 'title': 'Daily report, next day'}, 'report_task': {'id': 'other', 'project_id': None}},
            {'idempotency_key': new_id()})
        self.assertEqual(replacement.status, 'ok', replacement.error)
        second = plain(replacement.data)['signoff']
        self.assertEqual(second['preview']['replaces_id'], first['id'])
        self.assertEqual((self.link(first['id'])['task_id'], self.link(second['id'])['task_id']), ('work', 'other'))
        self.assertEqual(second['report_task']['title'], 'Other work')

    def test_the_link_follows_the_task_when_it_moves(self):
        from qorqut.domains.tasks.relocation import HostRelocation
        from qorqut_operations.tasks.relocation import Relocation
        signoff = self.posted(self.coordinator(), report_task=self.GLOBAL)
        relocation = Relocation(None, self.project, ('work',), ('work',))
        adapter = HostRelocation(self.app)
        with self.db.transaction() as uow:
            self.assertEqual(adapter.inspect(uow, relocation).counts['report_links'], 1)
            adapter.apply(uow, relocation, self.signoffs.clock())
        moved = self.link(signoff['id'])
        self.assertEqual((moved['task_project_id'], moved['revision']), (self.project, 2))
        got = self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']
        # The report stays where it was posted; its Task is now elsewhere and is named, without a title from the old scope.
        self.assertEqual(got['report_task'], {'type': 'task', 'id': 'work', 'project_id': self.project, 'title': None})

    def test_a_report_never_makes_its_task_wait_and_cannot_be_a_source(self):
        ctx = self.coordinator()
        signoff = self.posted(ctx, report_task=self.GLOBAL)
        self.assertFalse(self.task()['input_needed'])
        self.refused(self.post(ctx, key='again', report_task=self.GLOBAL, source=self.source), 'validation_error')
        linked = self.app.invoke(ctx, 'signoffs_source_link', {'scope': 'global', 'source': self.source, 'signoff_id': signoff['id']},
                                 {'idempotency_key': new_id()})
        self.refused(linked, 'validation_error')
        self.assertEqual(self.task()['status'], 'working')

    def test_only_a_report_takes_a_task_and_an_old_report_reads_without_one(self):
        ctx = self.coordinator()
        question = {'title': 'Choose a date', 'kind': 'question', 'explanation': 'Which date?', 'selection_mode': 'none'}
        self.refused(self.app.invoke(ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': 'q', 'form': question,
            'report_task': self.GLOBAL}, {'idempotency_key': new_id()}), 'validation_error')
        signoff = self.posted(ctx, report_task=self.GLOBAL)
        with self.db.transaction() as uow:
            uow.connection.execute('DELETE FROM signoff_report_tasks')
        got = self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']
        self.assertIsNone(got['report_task'])

    # -- the Task is named by its id ----------------------------------------------------------------------------------

    def test_a_task_without_a_project_gets_the_project_that_holds_it(self):
        ctx = self.coordinator()
        stated = self.posted(ctx, report_task=self.GLOBAL)
        bare = self.posted(ctx, key='bare', report_task={'id': 'work'})
        self.assertEqual(bare['report_task'], stated['report_task'])
        self.assertEqual({key: value for key, value in self.link(bare['id']).items() if key in ('task_id', 'task_project_id')},
                         {'task_id': 'work', 'task_project_id': None})
        project = replace(ctx, project_id=self.project, review_project_id=self.project)
        scoped = self.posted(project, scope='project', report_task={'id': 'project-work'})
        self.assertEqual((scoped['report_task']['project_id'], self.link(scoped['id'])['task_project_id']),
                         (self.project, self.project))

    def test_a_task_of_another_scope_is_refused_as_when_its_project_is_stated(self):
        ctx = self.coordinator()
        refused = self.post(ctx, report_task={'id': 'project-work'})
        self.refused(refused, 'validation_error')
        self.assertEqual(refused.error, self.post(ctx, report_task={'id': 'project-work', 'project_id': self.project}).error)
        self.refused(self.post(ctx, key='stated', report_task={'id': 'work', 'project_id': self.project}), 'validation_error')

    def test_a_task_outside_the_reach_is_answered_like_an_unknown_id(self):
        ctx = self.coordinator()
        outside = replace(ctx, allowed_project_ids=frozenset())
        unknown = self.post(outside, report_task={'id': 'nothing'})
        self.refused(unknown, 'validation_error')
        self.assertEqual(self.post(outside, key='hidden', report_task={'id': 'project-work'}).error, unknown.error)


if __name__ == '__main__':
    unittest.main()
