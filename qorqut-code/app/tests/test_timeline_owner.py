"""The owner's view of timelines: one item for a thread or the Global block, and every open question, blocking first."""
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
        return replace(Fixture.ctx(self, who), allowed_business_ids=frozenset({self.business, self.other}) if who == self.lead else self.reach)

    def setUp(self):
        Fixture.setUp(self)
        self.tool = Timeline(NS(application=self.app, registry=self.registry)).register()
        self.business = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('businesses').insert({'id': self.business, 'name': 'Example project', 'slug': 'example',
                                                 'workspace': 'example', 'hq_path': 'hq'})
        self.other = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('businesses').insert({'id': self.other, 'name': 'Closed project', 'slug': 'closed',
                                                 'workspace': 'closed', 'hq_path': 'hq2'})
            uow.repository('business_memberships', business_id=self.business).insert({'principal_id': self.owner, 'is_owner': True})
        self.work = self.task(self.lead, 'Pay creators')
        self.item = {'type': 'task', 'id': self.work['id'], 'business_id': None}
        self.project = {'type': 'project', 'id': self.business, 'business_id': self.business}
        self.global_item = {'type': 'global', 'id': 'global', 'business_id': None}
        patcher = patch('qorqut.domains.timeline.timestamp', lambda: test_timeline.DAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = NS(application=self.app, registry=self.registry, timeline=self.tool)
        self.owner_view = TimelineOwner(self.runtime).register()

    def dash(self, who):
        return replace(self.ctx(who), source='dashboard', allowed_business_ids=frozenset({self.business}))

    def view(self, who, item, **more):
        return self.app.invoke(self.dash(who), 'timeline_view', {'scope': 'global', 'item': item, **more}, {})

    def questions(self, who, **more):
        return self.app.invoke(self.dash(who), 'timeline_questions_list', {'scope': 'global', **more}, {})

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
        self.assertEqual(self.code(self.questions(self.worker)), 'forbidden')
        for name in ('timeline_view', 'timeline_questions_list'):
            descriptor = self.registry.get(name)
            self.assertEqual((descriptor.owner_only, set(descriptor.exposure), descriptor.effect_kind), (True, {'dashboard'}, 'read'))
        self.assertNotIn('dashboard', self.registry.get('timeline_file').exposure)

    def test_every_open_question_is_listed_blocking_first_with_its_item(self):
        self.filed(self.item, [{'action': 'ask', 'text': 'Task question?', 'blocking': False}])
        self.filed(self.project, [{'action': 'ask', 'text': 'Project question?', 'blocking': True}])
        closed = self.filed(self.global_item, [{'action': 'ask', 'text': 'Closed?', 'blocking': True}])['filed']['questions'][0]
        self.filed(self.global_item, [{'action': 'drop', 'question_id': closed}])
        # A Project the owner takes no part in is left out of the list.
        self.filed({'type': 'project', 'id': self.other, 'business_id': self.other}, [{'action': 'ask', 'text': 'Hidden?', 'blocking': True}])
        self.filed(self.global_item, [{'action': 'ask', 'text': 'Global question?', 'blocking': True, 'raised_by': 'owner'}])
        self.filed(self.item, [{'action': 'ask', 'text': 'Second task question?', 'blocking': True}])
        listed = self.ok(self.questions(self.owner))['items']
        self.assertEqual([(row['text'], row['blocking'], row['source']['type'], row['title']) for row in listed],
                         [('Project question?', True, 'project', 'Example project'), ('Global question?', True, 'global', 'Global'),
                          ('Second task question?', True, 'task', 'Pay creators'), ('Task question?', False, 'task', 'Pay creators')])
        self.assertEqual(self.code(self.view(self.owner, {'type': 'project', 'id': self.other, 'business_id': self.other})), 'not_found')
        self.assertEqual(listed[0]['source'], self.project)
        self.assertEqual(listed[2]['source'], self.item)
        self.assertEqual([row['business_name'] for row in listed], ['Example project', 'Global', 'Global', 'Global'])
        self.assertEqual(len(self.ok(self.questions(self.owner, limit=2))['items']), 2)
        self.assertTrue(self.ok(self.questions(self.owner, limit=2))['truncated'])

    def test_truncated_counts_only_questions_the_owner_can_read(self):
        self.filed(self.item, [{'action': 'ask', 'text': 'One?', 'blocking': False}])
        self.filed(self.global_item, [{'action': 'ask', 'text': 'Two?', 'blocking': False}])
        self.filed({'type': 'project', 'id': self.other, 'business_id': self.other}, [{'action': 'ask', 'text': 'Hidden?', 'blocking': False}])
        exact = self.ok(self.questions(self.owner, limit=2))
        self.assertEqual((len(exact['items']), exact['truncated']), (2, False))
        self.assertEqual(self.ok(self.questions(self.owner))['truncated'], False)
        self.assertEqual(self.ok(self.questions(self.owner, limit=1))['truncated'], True)

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
        asked = self.filed(self.item, [{'action': 'ask', 'text': 'Which month?', 'blocking': False}])['filed']['questions'][0]
        self.filed(self.item, [{'action': 'drop', 'question_id': asked}])
        first = self.ok(self.view(self.owner, self.item, history=True, limit=1))
        self.assertIsNotNone(first['entries'][0]['removed_by'])
        self.assertEqual([(row['text'], row['state']) for row in first['closed_questions']], [('Which month?', 'dropped')])

if __name__ == '__main__':
    unittest.main()
