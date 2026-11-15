"""A delegated Task's plan and the worker's queue: hand-over, step tools, who may do what, and the Todo order."""
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
from uuid import uuid4

from qorqut.agents.task_plans import TaskPlans
from qorqut_agentic.layer import AgenticLayer

import plan_template_package
from test_single_source import STAMP, Fixture, profile

PLAN = ['Find the three suppliers', 'Ask each for a quote', 'Put the quotes in the table']


class PlanFixture(Fixture):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        self.layer = AgenticLayer.initialize(base / 'tree', base / 'state')
        self.plans = TaskPlans(NS(application=self.app, agentic_layer=self.layer, domains=NS(tasks=self.tasks))).register()
        self.other = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.other, kind='agent', display_name='Other', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.connection.execute('INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,'
                                   "created_at,updated_at) VALUES(?,?,'ecosystem',?,?,?,?,?)",
                                   (self.other, self.other, 'other-worker', '/' + self.other, profile('global-worker'), STAMP, STAMP))

    def delegate(self, task, worker=None, **extra):
        return self.ok(self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': worker or self.worker,
                                                                 'brief': 'Three quotes by Friday.', **extra}))['task']

    def planned(self, title='Research suppliers', plan=PLAN, **extra):
        task = self.task(self.lead, title)
        self.delegate(task, plan=plan, **extra)
        return task

    def step(self, who, operation, task, **arguments):
        return self.invoke(who, 'tasks_step_' + operation, {'task_id': task['id'], **arguments})

    def read(self, who, task):
        return self.ok(self.invoke(who, 'tasks_get', {'task_id': task['id']}))['task']

    def states(self, steps):
        return [(step['id'], step['state']) for step in steps]

    def refused_as(self, result, code, fragment):
        self.assertEqual((result.status, (result.error or {}).get('code')), ('error', code), result.to_dict())
        self.assertIn(fragment, result.error['message'])

    def queue(self, agent):
        items = self.ok(self.invoke(self.lead if agent != self.lead else agent, 'attention_get',
                                    {'agent_id': agent, 'projection': 'summary'}))['items']
        return [item['task']['title'] for item in items]


class HandOverTests(PlanFixture):
    def test_a_delegated_task_carries_its_plan(self):
        task = self.planned()
        shown = self.read(self.worker, task)
        self.assertEqual(list(shown['plan']), PLAN)
        self.assertEqual(self.states(shown['plan_steps']), [('s1', 'pending'), ('s2', 'pending'), ('s3', 'pending')])
        self.assertEqual([step.get('current', False) for step in shown['plan_steps']], [True, False, False])
        self.assertEqual(shown['plan_steps'][0]['text'], PLAN[0])

    def test_a_quick_task_has_no_plan(self):
        task = self.task(self.lead, 'Rename the file')
        self.delegate(task)
        shown = self.read(self.worker, task)
        self.assertEqual(list(shown['plan']), [])
        self.assertNotIn('plan_steps', shown)
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'no plan')

    def test_the_thread_says_a_plan_was_set(self):
        task = self.planned()
        entries = [row[0] for row in self.rows("SELECT body FROM thread_messages WHERE item_id=? AND kind='system'", task['id'])]
        self.assertTrue(any('Plan set: 3 steps.' in entry for entry in entries), entries)

    def test_handing_over_again_without_a_plan_keeps_it(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.delegate(task, worker=self.other)
        shown = self.read(self.other, task)
        self.assertEqual(self.states(shown['plan_steps'])[:2], [('s1', 'completed'), ('s2', 'pending')])

    def test_a_new_plan_starts_from_its_first_step(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.delegate(task, plan=['Start over'])
        self.assertEqual(self.states(self.read(self.worker, task)['plan_steps']), [('s1', 'pending')])

    def test_an_empty_plan_removes_it(self):
        task = self.planned()
        self.delegate(task, plan=[])
        self.assertEqual(list(self.read(self.worker, task)['plan']), [])

    def test_a_plan_is_bounded(self):
        task = self.task(self.lead, 'Too much')
        result = self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker, 'plan': ['x'] * 31})
        self.assertEqual(result.status, 'error', result.to_dict())


class StepTests(PlanFixture):
    def test_the_worker_does_the_steps_in_order(self):
        task = self.planned()
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'comes after step s1')
        first = self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Found Acme, Bolt and Cord'))
        self.assertEqual(first['next_step'], {'id': 's2', 'text': PLAN[1]})
        self.assertFalse(first['plan_finished'])
        self.assertEqual(first['plan_steps'][0]['note'], 'Found Acme, Bolt and Cord')
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        last = self.ok(self.step(self.worker, 'done', task, step_id='s3'))
        self.assertTrue(last['plan_finished'])
        self.assertIsNone(last['next_step'])
        self.assertIn('tasks_complete', last['hint'])
        self.assertEqual(self.states(self.read(self.lead, task)['plan_steps']),
                         [('s1', 'completed'), ('s2', 'completed'), ('s3', 'completed')])

    def test_marking_a_step_does_not_touch_the_task(self):
        task = self.planned()
        before = self.rows('SELECT revision,updated_at FROM tasks WHERE id=?', task['id'])
        events = self.rows('SELECT count(*) FROM thread_messages WHERE item_id=?', task['id'])
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.assertEqual(self.rows('SELECT revision,updated_at FROM tasks WHERE id=?', task['id']), before)
        self.assertEqual(self.rows('SELECT count(*) FROM thread_messages WHERE item_id=?', task['id']), events)

    def test_a_finished_step_cannot_be_marked_again_with_other_words(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='First'))
        self.refused(self.step(self.worker, 'done', task, step_id='s1', note='Second'), 'already completed')

    def test_an_unknown_step_is_refused(self):
        task = self.planned()
        self.refused_as(self.step(self.worker, 'done', task, step_id='s9'), 'not_found', 'no such step')

    def test_only_the_tasks_worker_or_the_lead_works_the_plan(self):
        task = self.planned()
        self.refused_as(self.step(self.other, 'done', task, step_id='s1'), 'forbidden', 'recorded worker')
        self.ok(self.step(self.lead, 'done', task, step_id='s1'))
        self.ok(self.step(self.owner, 'done', task, step_id='s2'))

    def test_the_worker_adds_a_step(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        added = self.ok(self.step(self.worker, 'add', task, text='Check each supplier ships here', after='s1'))
        self.assertEqual([step['id'] for step in added['plan_steps']], ['s1', 's4', 's2', 's3'])
        self.assertEqual(added['next_step']['id'], 's4')
        self.assertTrue(added['plan_steps'][1]['added'])
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'comes after step s4')
        self.ok(self.step(self.worker, 'done', task, step_id='s4'))
        ended = self.ok(self.step(self.worker, 'add', task, text='Tell the lead the totals'))
        self.assertEqual(ended['plan_steps'][-1]['text'], 'Tell the lead the totals')

    def test_only_the_lead_or_the_owner_skips(self):
        task = self.planned()
        self.refused_as(self.step(self.worker, 'skip', task, step_id='s1', reason='Looks unnecessary'), 'forbidden',
                        "coordinator's decision")
        skipped = self.ok(self.step(self.lead, 'skip', task, step_id='s1', reason='The owner named the suppliers'))
        self.assertEqual(self.states(skipped['plan_steps'])[0], ('s1', 'skipped'))
        self.assertEqual(skipped['plan_steps'][0]['note'], 'The owner named the suppliers')
        self.assertEqual(skipped['next_step']['id'], 's2')
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.ok(self.step(self.owner, 'skip', task, step_id='s3', reason='Not needed'))
        self.assertTrue(all(step['state'] in ('completed', 'skipped') for step in self.read(self.worker, task)['plan_steps']))

    def test_a_closed_task_takes_no_more_steps(self):
        task = self.planned()
        self.ok(self.complete(self.worker, task, result='The quotes are in the table.'))
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'closed')


class QueueTests(PlanFixture):
    def test_the_queue_keeps_the_hand_over_order(self):
        for title in ('First', 'Second', 'Third'):
            self.delegate(self.task(self.lead, title))
        self.assertEqual(self.queue(self.worker), ['First', 'Second', 'Third'])

    def test_next_goes_to_the_front(self):
        for title in ('First', 'Second'):
            self.delegate(self.task(self.lead, title))
        self.delegate(self.task(self.lead, 'Urgent'), position='next')
        self.assertEqual(self.queue(self.worker), ['Urgent', 'First', 'Second'])

    def test_handing_a_task_to_the_same_worker_again_keeps_its_place(self):
        first, second = (self.task(self.lead, title) for title in ('First', 'Second'))
        self.delegate(first)
        self.delegate(second)
        self.delegate(first, brief='One more detail.')
        self.assertEqual(self.queue(self.worker), ['First', 'Second'])
        self.delegate(first, position='last')
        self.assertEqual(self.queue(self.worker), ['Second', 'First'])


class ThreadTests(PlanFixture):
    def context(self, task):
        from qorqut.inbox.thread_views import task_context
        service = NS(select=lambda ctx, business, uow: ctx, app=self.app)
        with self.db.read() as uow:
            return task_context(service, self.ctx(self.lead), {'type': 'task', 'id': task['id'], 'business_id': None}, uow)

    def test_the_thread_shows_where_the_plan_stands(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Three found.'))
        steps = self.context(task)['plan_steps']
        self.assertEqual(self.states(steps), [('s1', 'completed'), ('s2', 'pending'), ('s3', 'pending')])
        self.assertEqual((steps[0]['note'], steps[1].get('current')), ('Three found.', True))

    def test_a_thread_without_a_plan_carries_none(self):
        task = self.task(self.lead, 'Rename the file')
        self.delegate(task)
        self.assertNotIn('plan_steps', self.context(task))


class TemplateTests(PlanFixture):
    REF = 'house-plans/publish-post'

    def setUp(self):
        super().setUp()
        plan_template_package.install(self.layer)

    def test_a_template_is_copied_into_the_tasks_plan(self):
        task = self.task(self.lead, 'Publish the spring post')
        self.delegate(task, template=self.REF)
        shown = self.read(self.worker, task)
        self.assertEqual(list(shown['plan']), plan_template_package.PUBLISH_STEPS)
        self.assertEqual(shown['plan_template'], self.REF)
        self.assertEqual(self.states(shown['plan_steps']), [('s1', 'pending'), ('s2', 'pending'), ('s3', 'pending')])
        entries = [row[0] for row in self.rows("SELECT body FROM thread_messages WHERE item_id=? AND kind='system'", task['id'])]
        self.assertTrue(any('Plan set: 3 steps from the template house-plans/publish-post.' in entry for entry in entries), entries)

    def test_a_plan_from_a_template_is_worked_like_any_plan(self):
        task = self.task(self.lead, 'Publish the spring post')
        self.delegate(task, template=self.REF)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        added = self.ok(self.step(self.worker, 'add', task, text='Check the picture credits.', after='s1'))
        self.assertEqual(self.states(added['plan_steps']),
                         [('s1', 'completed'), ('s4', 'pending'), ('s2', 'pending'), ('s3', 'pending')])
        skipped = self.ok(self.step(self.lead, 'skip', task, step_id='s2', reason='The owner gave the time already.'))
        self.assertEqual(skipped['next_step']['id'], 's4')

    def test_a_plan_and_a_template_together_are_refused(self):
        task = self.task(self.lead, 'Publish the spring post')
        result = self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                           'template': self.REF, 'plan': PLAN})
        self.refused(result, 'not both')

    def test_templates_a_plan_cannot_carry_are_refused_with_the_reason(self):
        task = self.task(self.lead, 'Publish the spring post')
        for ref, code, fragment in (('house-plans/nothing', 'not_found', 'There is no template'),
                                    ('house-plans/with-inputs', 'validation_error', 'arguments'),
                                    ('house-plans/too-long', 'validation_error', 'at most')):
            result = self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker, 'template': ref})
            self.refused_as(result, code, fragment)
        self.assertEqual(list(self.read(self.lead, task)['plan']), [])

    def test_a_written_plan_replaces_the_template(self):
        task = self.task(self.lead, 'Publish the spring post')
        self.delegate(task, template=self.REF)
        self.delegate(task, plan=PLAN)
        shown = self.read(self.worker, task)
        self.assertEqual(list(shown['plan']), PLAN)
        self.assertIsNone(shown['plan_template'])

