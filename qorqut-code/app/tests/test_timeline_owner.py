"""The owner's view of timelines: one item for a thread or the Global block. Its open questions are question Signoffs (see test_timeline_review)."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_operations.contracts import plain

from qorqut.domains.timeline import Timeline
from qorqut.domains.timeline_owner import TimelineOwner
from qorqut.inbox.thread_views import timeline_part

import test_timeline
from test_single_source import Fixture


class OwnerViewTests(Fixture):
    reach = frozenset()
    # The filing helpers of the timeline tests, without running those tests again.
    file, filed, code, add, read, get = (getattr(test_timeline.TimelineTests, name) for name in
                                         ('file', 'filed', 'code', 'add', 'read', 'get'))

    def ok(self, result):
        return plain(Fixture.ok(self, result))

    def ctx(self, who):
        return replace(Fixture.ctx(self, who), allowed_project_ids=frozenset({self.project_id, self.other}) if who == self.lead else self.reach)

    def setUp(self):
        Fixture.setUp(self)
        self.tool = Timeline(NS(application=self.app, registry=self.registry)).register()
        self.project_id = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('projects').insert({'id': self.project_id, 'name': 'Example project', 'slug': 'example',
                                                 'workspace': 'example', 'hq_path': 'hq'})
        self.other = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('projects').insert({'id': self.other, 'name': 'Closed project', 'slug': 'closed',
                                                 'workspace': 'closed', 'hq_path': 'hq2'})
            uow.repository('project_memberships', project_id=self.project_id).insert({'principal_id': self.owner, 'is_owner': True})
        self.work = self.task(self.lead, 'Pay creators')
        self.item = {'type': 'task', 'id': self.work['id'], 'project_id': None}
        self.project = {'type': 'project', 'id': self.project_id, 'project_id': self.project_id}
        self.global_item = {'type': 'global', 'id': 'global', 'project_id': None}
        patcher = patch('qorqut.domains.timeline.timestamp', lambda: test_timeline.DAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = NS(application=self.app, registry=self.registry, timeline=self.tool)
        self.owner_view = TimelineOwner(self.runtime).register()

    def dash(self, who):
        return replace(self.ctx(who), source='dashboard', allowed_project_ids=frozenset({self.project_id}))

    def view(self, who, item, **more):
        return self.app.invoke(self.dash(who), 'timeline_view', {'scope': 'global', 'item': item, **more}, {})

    def test_the_owner_reads_the_state_and_the_history_of_every_kind_of_item(self):
        self.add('goal', 'Pay everyone.')
        gone = self.add('decision', 'Old way.')
        self.filed(self.item, [{'action': 'remove', 'entry_id': gone, 'reason': 'Replaced.'}],
                   sources=[{'quote': 'Do it the new way.', 'ref': 'thread:3'}])
        self.add('fact', 'Project fact.', self.project)
        self.add('progress', 'Started.', self.global_item)
        shown = self.ok(self.view(self.owner, self.item))
        self.assertEqual((shown['title'], shown['state']['goal']['text'], shown['state']['decisions']), ('Pay creators', 'Pay everyone.', []))
        self.assertNotIn('entries', shown)
        history = self.ok(self.view(self.owner, self.item, history=True))
        self.assertEqual([(row['action'], row.get('removed_by') is not None) for row in history['entries']],
                         [('add', False), ('add', True), ('remove', False)])
        self.assertEqual(history['entries'][2]['sources'], [{'quote': 'Do it the new way.', 'ref': 'thread:3'}])
        self.assertEqual(history['links'], {'subtasks': [], 'approvals': [], 'reports': []})
        self.assertEqual(self.ok(self.view(self.owner, self.project))['state']['facts'][0]['text'], 'Project fact.')
        self.assertEqual(self.ok(self.view(self.owner, self.global_item))['state']['progress']['text'], 'Started.')

    def test_the_thread_view_attaches_the_timeline_to_tasks_and_projects_only_when_readable(self):
        self.add('fact', 'Kept.')
        with self.db.read() as uow:
            service = NS(app=self.app)
            self.assertEqual(timeline_part(service, self.dash(self.owner), self.item, uow)['timeline']['state']['facts'][0]['text'], 'Kept.')
            self.assertEqual(timeline_part(service, self.dash(self.owner), self.project, uow)['timeline']['state']['facts'], [])
            self.assertEqual(timeline_part(service, self.dash(self.owner), {**self.item, 'id': 'missing'}, uow), {})
            self.assertEqual(timeline_part(NS(app=NS()), self.dash(self.owner), self.item, uow), {})

    def test_only_the_owner_reads_them_and_the_coordinator_files_but_never_on_the_dashboard(self):
        self.assertEqual(self.code(self.view(self.lead, self.item)), 'forbidden')
        self.assertEqual(self.code(self.view(self.worker, self.item)), 'forbidden')
        descriptor = self.registry.get('timeline_view')
        self.assertEqual((descriptor.owner_only, set(descriptor.exposure), descriptor.effect_kind), (True, {'dashboard'}, 'read'))
        # The owner answers a question in the sign-off queue: no question operation of its own is left on the dashboard.
        for name in ('timeline_questions_list', 'timeline_question_answer'):
            self.assertEqual(self.registry.known(name), False)
        self.assertNotIn('dashboard', self.registry.get('timeline_file').exposure)



    def test_the_owner_reads_the_timeline_of_a_task_inside_a_project(self):
        inside = test_timeline.TimelineTests.inside(self, 'Inside task')
        self.add('fact', 'Held in the project.', inside)
        with self.db.read() as uow:
            seen = timeline_part(NS(app=self.app), self.dash(self.owner), inside, uow)
        self.assertEqual(seen['timeline']['state']['facts'][0]['text'], 'Held in the project.')
        self.assertEqual(self.ok(self.view(self.owner, inside, history=True))['entries'][0]['text'], 'Held in the project.')

    def test_history_carries_removed_by_for_the_whole_item_and_the_closed_questions(self):
        gone = self.add('decision', 'Old way.')
        self.filed(self.item, [{'action': 'remove', 'entry_id': gone, 'reason': 'Replaced.'}],
                   sources=[{'quote': 'New way, please.', 'ref': 'thread:4'}])
        self.add('fact', 'Later fact.')
        asked = test_timeline.TimelineTests.asked(self, self.item, [{'action': 'ask', 'text': 'Which month?', 'blocking': False}])['filed']['questions'][0]
        self.filed(self.item, [{'action': 'drop', 'question_id': asked}])
        first = self.ok(self.view(self.owner, self.item, history=True, limit=1))
        self.assertIsNotNone(first['entries'][0]['removed_by'])
        self.assertEqual([(row['text'], row['state']) for row in first['closed_questions']], [('Which month?', 'dropped')])


if __name__ == '__main__':
    unittest.main()
