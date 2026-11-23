"""A delegated Task's plan and the worker's queue: hand-over, step tools, who may do what, and the Todo order."""
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
from uuid import uuid4

from qorqut.agents.task_plans import TaskPlans, plan_key
from qorqut.instance_paths import InstancePaths
from qorqut.signoffs.ports import HostExecution, HostInputSources, HostPeers, HostReferences, HostTasks
from qorqut_agentic.layer import AgenticLayer

import plan_template_package
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.locks import LockPaths
from qorqut_operations.tasks.types import attributes
from test_single_source import STAMP, Fixture, profile

PLAN = ['Find the three suppliers', 'Ask each for a quote', 'Put the quotes in the table']


class Policy:
    """Allows every operation unless a test names another mode for it. The binding carries the capability a Global
    Signoff is shown by, as the installed policy's does."""
    def __init__(self, context_for):
        self.modes, self.context_for = {}, context_for

    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision(self.modes.get(descriptor.name, 'allow'), 'test',
                              {'global_capability': descriptor.global_capability} if descriptor.global_capability else {})


class PlanFixture(Fixture):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name).resolve()
        self.layer = AgenticLayer.initialize(base / 'tree', base / 'state')
        self.files = base / 'operations'
        self.files.mkdir()
        self.plans = TaskPlans(NS(application=self.app, agentic_layer=self.layer, domains=NS(tasks=self.tasks),
                                  paths=InstancePaths(base, {}))).register()
        # The real Signoff service, with its own visibility rules, in place of the base fixture's stand-in.
        self.policy = self.app.policy = Policy(self.app.policy.context_for)
        ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
                             platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT, sources=HostInputSources())
        self.signoffs = self.app.signoffs = SignoffService(self.db, ports, locks=LockPaths.under(base / 'locks'))
        self.signoffs.bind(self.app)
        contribution = self.signoffs.contribution()
        for entry in contribution.entries:
            self.registry.declare(entry)
        for descriptor in contribution.descriptors:
            self.registry.register(descriptor)
        self.other = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.other, kind='agent', display_name='Other', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.connection.execute('INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,profile_json,'
                                   "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
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

    def stored(self, task):
        with self.db.read() as uow:
            return uow.repository('tasks', project_id=None).get(task['id'])

    def told(self, task):
        """The plan step events of a Task, oldest first: their type, summary and who they were delivered to."""
        events = self.rows("SELECT id,event_type,summary FROM inbox_events WHERE source_type='tasks' AND source_id=? "
                           "AND event_type LIKE 'plan_step%' ORDER BY id", task['id'])
        return [(kind, summary, sorted(row[0] for row in self.rows('SELECT agent_id FROM inbox_deliveries WHERE event_id=?', event)))
                for event, kind, summary in events]

    def interrupt(self, operation, between):
        """Run ``between`` once, after the engine change of the next ``operation`` call and before its write transaction."""
        descriptor = self.app.registry.get(operation)
        original, fired = descriptor.external, []

        def external(ctx, plan):
            original(ctx, plan)
            if not fired:
                fired.append(True)
                between()
        object.__setattr__(descriptor, 'external', external)
        self.addCleanup(object.__setattr__, descriptor, 'external', original)

    def waiting_for(self, agent, task):
        """The agent's unacknowledged plan step events about the Task, oldest first."""
        return [row[0] for row in self.rows(
            "SELECT e.event_type FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? "
            "AND d.acknowledged_at IS NULL AND e.source_type='tasks' AND e.source_id=? AND e.event_type LIKE 'plan_step%' "
            "ORDER BY e.id", agent, task['id'])]

    # -- real Signoffs -----------------------------------------------------------------------------------------------

    def deciding_owner(self):
        return replace(self.ctx(self.owner), source='dashboard')

    def question(self, who=None, key=None, **scope):
        """A pending input request, posted through ``signoffs_input_create``."""
        result = self.app.invoke(scope.get('ctx') or self.ctx(who or self.worker), 'signoffs_input_create',
                                 {**({} if 'ctx' in scope else {'scope': 'global'}), 'request_key': key or str(uuid4()),
                                  'form': {'title': 'Choose a date', 'explanation': 'Which date works?', 'kind': 'question',
                                           'selection_mode': 'none', 'notes_required': True}},
                                 {'idempotency_key': str(uuid4())})
        return plain(self.ok(result))['signoff']

    def answer(self, signoff, owner=None):
        result = self.signoffs.respond(owner or self.deciding_owner(), signoff['id'], proposal_revision=signoff['proposal_revision'],
                                       payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'],
                                       idempotency_key=str(uuid4()), notes='Next Tuesday')
        self.assertEqual(result.status, 'ok', result.error)

    def proposal(self, supersedes=None):
        """A pending proposal: a Task update the policy sends to the owner, staged by the worker. ``supersedes`` names
        the worker's earlier proposal this one replaces."""
        task = self.task(self.lead, f'Proposed change {uuid4()}')
        options = {'idempotency_key': str(uuid4()),
                   'expected_revision': self.rows('SELECT revision FROM tasks WHERE id=?', task['id'])[0][0]}
        if supersedes:
            options.update(supersedes_signoff_id=supersedes['id'], expected_superseded_status_revision=self.rows(
                'SELECT status_revision FROM signoffs WHERE id=?', supersedes['id'])[0][0])
        self.policy.modes['tasks_update'] = 'signoff'
        staged = self.app.invoke(self.ctx(self.worker), 'tasks_update', {'scope': 'global', 'task_id': task['id'],
                                                                         'changes': {'next_step': 'Proposed'}}, options)
        self.policy.modes.pop('tasks_update')
        self.assertEqual(staged.status, 'signoff_required', staged.to_dict())
        return plain(staged.signoff)

    def decide(self, signoff, decision):
        self.policy.modes['tasks_update'] = 'signoff'
        result = self.signoffs.review(self.deciding_owner(), signoff['id'], decision=decision,
                                      proposal_revision=signoff['proposal_revision'], payload_hash=signoff['payload_hash'],
                                      expected_status_revision=signoff['status_revision'], idempotency_key=str(uuid4()),
                                      feedback='Not this way.' if decision == 'reject' else None)
        self.policy.modes.pop('tasks_update')
        self.assertIn(result.status, ('ok', 'accepted'), result.error)

    def signoff_state(self, signoff):
        return self.rows('SELECT state, superseded_by_id IS NOT NULL, category FROM signoffs WHERE id=?', signoff['id'])[0]

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
        service = NS(select=lambda ctx, project, uow: ctx, app=self.app)
        with self.db.read() as uow:
            return task_context(service, self.ctx(self.lead), {'type': 'task', 'id': task['id'], 'project_id': None}, uow)

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



    def test_a_template_does_not_replace_a_plan_that_still_has_a_checked_step(self):
        task = self.planned('Offer', plan=REVIEWED)
        refused = self.invoke(self.worker, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker, 'template': self.REF})
        self.refused_as(refused, 'forbidden', 'only the coordinator or the owner')
        self.assertEqual(list(self.read(self.worker, task)['plan']), REVIEWED)
        self.delegate(task, template=self.REF)
        self.assertEqual(self.read(self.worker, task)['plan_template'], self.REF)


class GrantTests(PlanFixture):
    def test_every_plan_operation_is_registered_and_follows_the_task_update_grant(self):
        from qorqut.agents.task_plans import PLAN_OPERATIONS
        from qorqut.composition.policy import _fallback_defaults
        self.assertIn('tasks_step_confirm', PLAN_OPERATIONS)
        for name in PLAN_OPERATIONS:
            self.assertIsNone(self.registry.get(name).default_mode)
            decided = _fallback_defaults(name, {}, {}, self.registry, role_id='global-worker', scope=None,
                                         resolve=lambda operation: ('as', operation))
            self.assertEqual(decided, ('as', 'tasks_update'), name)


REVIEWED = ['Draft the offer', {'text': 'Agree the price', 'check': {'kind': 'review'}}, 'Send the offer']


class StepShapeTests(PlanFixture):
    def test_a_plain_plan_is_stored_and_keyed_as_before(self):
        import hashlib, json
        task = self.planned()
        row = self.stored(task)
        self.assertEqual(attributes(row)['plan'], PLAN)
        material = json.dumps([row.get('project_id'), row['id'], row['created_at'], PLAN], ensure_ascii=False, separators=(',', ':'))
        self.assertEqual(plan_key(row), 'plan-' + hashlib.sha256(material.encode()).hexdigest()[:48])
        self.assertEqual(self.read(self.worker, task)['plan_steps'][0], {'id': 's1', 'text': PLAN[0], 'state': 'pending', 'current': True})

    def test_a_step_with_only_text_is_stored_as_the_text(self):
        task = self.planned(plan=[{'text': '  Find the three suppliers '}, PLAN[1], {'text': PLAN[2]}])
        self.assertEqual(attributes(self.stored(task))['plan'], PLAN)
        self.assertEqual(plan_key(self.stored(task)), plan_key({**self.stored(self.planned('Other')), 'id': task['id'],
                                                               'created_at': self.stored(task)['created_at']}))

    def test_tagged_steps_are_stored_normalized_and_shown(self):
        task = self.planned(plan=['Draft', {'text': ' Agree the price ', 'notify': 'both', 'check': {'kind': 'review'}},
                                  {'check': {'signoff_id': ' abc ', 'kind': 'signoff'}, 'text': 'Get the approval'},
                                  {'text': 'Write it up', 'check': {'kind': 'file'}}])
        self.assertEqual(attributes(self.stored(task))['plan'], [
            'Draft', {'text': 'Agree the price', 'notify': 'both', 'check': {'kind': 'review'}},
            {'text': 'Get the approval', 'check': {'kind': 'signoff', 'signoff_id': 'abc'}},
            {'text': 'Write it up', 'check': {'kind': 'file'}}])
        before = plain(self.read(self.worker, task)['plan_steps'])
        self.assertEqual(before, [
            {'id': 's1', 'text': 'Draft', 'state': 'pending', 'current': True},
            {'id': 's2', 'text': 'Agree the price', 'state': 'pending', 'notify': 'both', 'check': 'review'},
            {'id': 's3', 'text': 'Get the approval', 'state': 'pending', 'check': 'signoff'},
            {'id': 's4', 'text': 'Write it up', 'state': 'pending', 'check': 'file'}])
        # The same fields once the run exists.
        after = plain(self.ok(self.step(self.worker, 'done', task, step_id='s1'))['plan_steps'])
        self.assertEqual(after[1], {**before[1], 'current': True})
        self.assertEqual(after[2:], before[2:])

    def test_bad_step_shapes_are_refused(self):
        task = self.task(self.lead, 'Badly planned')
        for step in ({'text': 'x', 'notify': 'later'}, {'text': 'x', 'check': {'kind': 'vote'}}, {'text': 'x', 'check': {}},
                     {'text': 'x', 'check': {'kind': 'review', 'task_id': 't'}}, {'text': 'x', 'check': {'kind': 'task', 'path': 'a'}},
                     {'text': 'x', 'why': 'because'}, {'notify': 'done'}, {'text': ''}, 7):
            result = self.invoke(self.lead, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker, 'plan': [step]})
            self.assertEqual((result.status, (result.error or {}).get('code')), ('error', 'validation_error'), (step, result.to_dict()))
        self.assertEqual(list(self.read(self.lead, task)['plan']), [])

    def test_the_stored_shape_says_what_is_wrong(self):
        from qorqut_operations.tasks.types import normalize_step
        for step, fragment in (({'text': 'x', 'check': {'kind': 'vote'}}, 'review, signoff, task, file'),
                               ({'text': 'x', 'check': {'kind': 'review', 'task_id': 't'}}, 'remove task_id'),
                               ({'text': 'x', 'check': {'kind': 'task', 'path': 'a'}}, 'takes only task_id'),
                               ({'text': 'x', 'notify': 'later'}, 'start, done or both'),
                               ({'text': 'x', 'why': 'y'}, 'remove why')):
            with self.assertRaises(DomainError) as raised:
                normalize_step(step)
            self.assertIn(fragment, raised.exception.message)

    def test_a_step_is_added_with_its_tags(self):
        task = self.planned()
        added = self.ok(self.step(self.worker, 'add', task, text='Have the totals confirmed', after='s1', notify='done',
                                  check={'kind': 'review'}))
        self.assertEqual(added['plan_steps'][1], {'id': 's4', 'text': 'Have the totals confirmed', 'state': 'pending', 'added': True,
                                                  'notify': 'done', 'check': 'review'})
        self.refused(self.step(self.worker, 'add', task, text='Bad', check={'kind': 'task', 'path': 'x'}), 'takes only task_id')
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        handed = self.ok(self.step(self.worker, 'done', task, step_id='s4', note='Totals attached'))
        self.assertTrue(handed['plan_steps'][1]['awaiting_review'])


class NotifyTests(PlanFixture):
    TAGGED = [{'text': 'Find the three suppliers', 'notify': 'both'}, {'text': 'Ask each for a quote', 'notify': 'done'},
              {'text': 'Put the quotes in the table', 'notify': 'start'}, {'text': 'Tell the owner', 'notify': 'both'}]

    def test_the_coordinator_hears_of_tagged_steps_and_the_task_is_not_written(self):
        task = self.planned(plan=self.TAGGED)
        before = self.rows('SELECT revision,updated_at FROM tasks WHERE id=?', task['id'])
        entries = self.rows('SELECT count(*) FROM thread_messages WHERE item_id=?', task['id'])
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Found Acme, Bolt and Cord.'))
        # The first step's start is the hand-over itself; its end is told, and the next step is tagged for its end only.
        self.assertEqual(self.told(task), [('plan_step_done', 'Step s1 of "Research suppliers" is done: Find the three suppliers. '
                                                              'Note: Found Acme, Bolt and Cord.', [self.lead])])
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        kind, summary, recipients = self.told(task)[1]
        self.assertEqual((kind, recipients), ('plan_step_done,plan_step_started', [self.lead]))
        self.assertEqual(summary, 'Step s2 of "Research suppliers" is done: Ask each for a quote. '
                                  'Step s3 is now in hand: Put the quotes in the table.')
        self.ok(self.step(self.worker, 'done', task, step_id='s3'))
        self.assertEqual(self.told(task)[2], ('plan_step_started', 'Step s4 of "Research suppliers" is now in hand: Tell the owner.',
                                              [self.lead]))
        self.ok(self.step(self.worker, 'done', task, step_id='s4'))
        self.assertEqual([kind for kind, _, _ in self.told(task)],
                         ['plan_step_done', 'plan_step_done,plan_step_started', 'plan_step_started', 'plan_step_done'])
        self.assertEqual(self.rows('SELECT revision,updated_at FROM tasks WHERE id=?', task['id']), before)
        self.assertEqual(self.rows('SELECT count(*) FROM thread_messages WHERE item_id=?', task['id']), entries)

    def test_untagged_steps_tell_nobody(self):
        task = self.planned()
        for step in ('s1', 's2', 's3'):
            self.ok(self.step(self.worker, 'done', task, step_id=step))
        self.assertEqual(self.told(task), [])

    def test_the_actor_is_never_told(self):
        task = self.planned(plan=self.TAGGED)
        self.ok(self.step(self.lead, 'done', task, step_id='s1'))
        self.ok(self.step(self.lead, 'done', task, step_id='s2'))
        self.assertEqual(self.told(task), [])
        # The owner's action is told to the coordinator.
        self.ok(self.step(self.owner, 'done', task, step_id='s3'))
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)], [('plan_step_started', [self.lead])])

    def test_a_skipped_step_starts_the_next_one(self):
        task = self.planned(plan=self.TAGGED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.ok(self.step(self.owner, 'skip', task, step_id='s2', reason='Quotes are in already'))
        # A skipped step is not done, so only the step that became current is told.
        self.assertEqual(self.told(task)[1][0], 'plan_step_started')

    def test_saying_the_same_thing_twice_tells_once(self):
        task = self.planned(plan=self.TAGGED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Three found.'))
        # A new request with the same words reaches the engine again and changes nothing.
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Three found.'))
        self.assertEqual(len(self.told(task)), 1)

    def test_skipping_a_later_step_does_not_announce_the_step_in_hand_again(self):
        task = self.planned('Ordered', plan=['One', {'text': 'Two', 'notify': 'start'}, 'Three', {'text': 'Four', 'notify': 'start'}])
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.assertEqual([kind for kind, _, _ in self.told(task)], ['plan_step_started'])
        self.ok(self.step(self.owner, 'skip', task, step_id='s3', reason='No need'))
        self.ok(self.step(self.owner, 'skip', task, step_id='s4', reason='No need'))
        self.assertEqual([kind for kind, _, _ in self.told(task)], ['plan_step_started'])
        # Skipping the step in hand moves it on: nothing is left here, and in a longer plan the next one is announced.
        other = self.planned('Longer', plan=['One', 'Two', {'text': 'Three', 'notify': 'start'}])
        self.ok(self.step(self.owner, 'skip', other, step_id='s2', reason='No need'))
        self.assertEqual(self.told(other), [])
        self.ok(self.step(self.owner, 'skip', other, step_id='s1', reason='No need'))
        self.assertEqual([(kind, summary) for kind, summary, _ in self.told(other)],
                         [('plan_step_started', 'Step s3 of "Longer" is now in hand: Three.')])

    def test_an_action_whose_write_failed_is_told_when_it_is_repeated_and_only_once(self):
        task = self.planned('Make the offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        # A write of the Task lands between the engine change and the transaction that would have told of it.
        self.interrupt('tasks_step_done', lambda: self.ok(self.update(self.lead, task, {'next_step': 'Look at the price'})))
        failed = self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.')
        self.assertEqual(failed.status, 'error', failed.to_dict())
        self.assertTrue(self.read(self.lead, task)['plan_steps'][1]['awaiting_review'])     # the engine change stands
        self.assertEqual(self.told(task), [])
        again = self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertIn('the coordinator was told', again['hint'])
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)], [('plan_step_submitted', [self.lead])])
        # Said once more, it is not told again.
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertEqual(len(self.told(task)), 1)
        # The decision is recovered the same way, for the worker.
        self.interrupt('tasks_step_confirm', lambda: self.ok(self.update(self.worker, task, {'next_step': 'Waiting'})))
        self.assertEqual(self.step(self.lead, 'confirm', task, step_id='s2', approved=True).status, 'error')
        self.assertEqual(len(self.told(task)), 1)
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=True))
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=True))
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)],
                         [('plan_step_submitted', [self.lead]), ('plan_step_approved', [self.worker])])

    def test_a_step_finished_by_the_owner_whose_write_failed_is_told_when_repeated(self):
        task = self.planned('Make the offer', plan=[{'text': 'Agree the price', 'check': {'kind': 'review'}, 'notify': 'done'},
                                                    {'text': 'Send it', 'notify': 'start'}])
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='They accept 400.'))
        self.interrupt('tasks_step_done', lambda: self.ok(self.update(self.lead, task, {'next_step': 'Look at the price'})))
        self.assertEqual(self.step(self.owner, 'done', task, step_id='s1').status, 'error')
        self.ok(self.step(self.owner, 'done', task, step_id='s1'))
        self.ok(self.step(self.owner, 'done', task, step_id='s1'))
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)],
                         [('plan_step_submitted', [self.lead]), ('plan_step_done,plan_step_started', [self.lead]),
                          ('plan_step_approved', [self.worker])])

    def test_two_requests_saying_the_same_thing_at_once_both_succeed_and_tell_once(self):
        task = self.planned('Make the offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        twin = []

        def same_request():
            runner = threading.Thread(target=lambda: twin.append(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.')))
            runner.start()
            runner.join(30)
        # The twin is carried out whole between the first request's engine change and its write transaction.
        self.interrupt('tasks_step_done', same_request)
        first = self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertEqual(len(twin), 1)
        second = self.ok(twin[0])
        self.assertEqual(first, second)
        self.assertIn('the coordinator was told', first['hint'])
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)], [('plan_step_submitted', [self.lead])])

    def test_the_hint_says_the_coordinator_was_told_only_when_he_was(self):
        task = self.planned('Make the offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE installation SET management_coordinator_id=NULL')
        handed = self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.assertEqual(self.told(task), [])
        self.assertNotIn('was told', handed['hint'])
        self.assertIn('no coordinator could be told', handed['hint'])


class ReviewTests(PlanFixture):
    def reviewed(self, title='Make the offer'):
        task = self.planned(title, plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        return task

    def test_a_worker_hands_a_review_step_in(self):
        task = self.reviewed()
        before = self.rows('SELECT revision FROM tasks WHERE id=?', task['id'])
        handed = self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertEqual(handed['plan_steps'][1], {'id': 's2', 'text': 'Agree the price', 'state': 'active', 'check': 'review',
                                                   'awaiting_review': True, 'current': True})
        self.assertIsNone(handed['next_step'])
        self.assertFalse(handed['plan_finished'])
        self.assertIn('next Task meanwhile', handed['hint'])
        self.assertEqual(self.told(task), [('plan_step_submitted', 'Step s2 of "Make the offer" waits for your confirmation: '
                                            "Agree the price. Worker's note: They accept 400. Confirm it or send it back with "
                                            'tasks_step_confirm.', [self.lead])])
        self.assertEqual(self.read(self.lead, task)['plan_steps'][1]['awaiting_review'], True)
        self.assertEqual(self.rows('SELECT revision FROM tasks WHERE id=?', task['id']), before)
        # Nothing past it can be worked, and handing it in again changes nothing.
        self.refused(self.step(self.worker, 'done', task, step_id='s3'), 'comes after step s2')
        self.refused(self.step(self.worker, 'done', task, step_id='s2', note='Again'), 'already waits')
        self.assertEqual(len(self.told(task)), 1)

    def test_a_worker_cannot_confirm(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.refused_as(self.step(self.worker, 'confirm', task, step_id='s2', approved=True), 'forbidden', "coordinator's decision")
        self.refused_as(self.step(self.other, 'confirm', task, step_id='s2', approved=True), 'forbidden', 'recorded worker')
        self.assertTrue(self.read(self.worker, task)['plan_steps'][1]['awaiting_review'])

    def test_the_coordinator_approves(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        approved = self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=True))
        self.assertEqual(approved['plan_steps'][1], {'id': 's2', 'text': 'Agree the price', 'state': 'completed', 'check': 'review',
                                                     'note': 'They accept 400.'})
        self.assertEqual(approved['next_step'], {'id': 's3', 'text': 'Send the offer'})
        self.assertEqual(self.told(task)[1], ('plan_step_approved', 'Step s2 of "Make the offer" was confirmed: Agree the price. '
                                                                    'Go on with the plan.', [self.worker]))
        # Deciding again in the same words changes nothing and tells nobody; another decision is refused.
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=True))
        self.assertEqual(len(self.told(task)), 2)
        self.refused(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='No'), 'already completed')
        self.ok(self.step(self.worker, 'done', task, step_id='s3'))

    def test_sent_back_handed_in_again_and_approved(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.refused(self.step(self.lead, 'confirm', task, step_id='s2', approved=False), 'with feedback')
        back = self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='Get it in writing.'))
        self.assertEqual(back['plan_steps'][1], {'id': 's2', 'text': 'Agree the price', 'state': 'active', 'check': 'review',
                                                 'feedback': 'Get it in writing.', 'current': True})
        self.assertEqual(back['next_step']['id'], 's2')
        self.assertEqual(self.told(task)[1], ('plan_step_returned', 'Step s2 of "Make the offer" was sent back: Agree the price. '
                                              'Feedback: Get it in writing. Fix it and mark the step done again.', [self.worker]))
        self.assertEqual(self.read(self.worker, task)['plan_steps'][1]['feedback'], 'Get it in writing.')
        self.refused(self.step(self.lead, 'confirm', task, step_id='s2', approved=True), 'does not wait for confirmation')
        # The worker hands it in again in the same words; it waits again and does not finish by itself.
        again = self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertTrue(again['plan_steps'][1]['awaiting_review'])
        self.assertNotIn('feedback', again['plan_steps'][1])
        self.assertEqual([kind for kind, _, _ in self.told(task)], ['plan_step_submitted', 'plan_step_returned', 'plan_step_submitted'])
        # Sent back and handed in a second time, then approved, each in the words used before.
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='Get it in writing.'))
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        done = self.ok(self.step(self.owner, 'confirm', task, step_id='s2', approved=True))
        self.assertEqual(self.states(done['plan_steps'])[1], ('s2', 'completed'))
        self.assertEqual([kind for kind, _, _ in self.told(task)][3:], ['plan_step_returned', 'plan_step_submitted', 'plan_step_approved'])
        self.assertEqual(self.told(task)[-1][2], [self.worker])

    def test_the_coordinator_or_the_owner_finishes_a_review_step_directly(self):
        task = self.reviewed()
        done = self.ok(self.step(self.lead, 'done', task, step_id='s2', note='Agreed on the phone.'))
        self.assertEqual((done['plan_steps'][1]['state'], done['plan_steps'][1]['note']), ('completed', 'Agreed on the phone.'))
        self.assertEqual(self.told(task), [])
        # Marking a step that waits is its approval.
        other = self.planned('Second offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', other, step_id='s1'))
        self.ok(self.step(self.worker, 'done', other, step_id='s2', note='They accept 300.'))
        approved = self.ok(self.step(self.owner, 'done', other, step_id='s2'))
        self.assertEqual((approved['plan_steps'][1]['state'], approved['plan_steps'][1]['note']), ('completed', 'They accept 300.'))
        self.assertEqual([(kind, who) for kind, _, who in self.told(other)],
                         [('plan_step_submitted', [self.lead]), ('plan_step_approved', [self.worker])])

    def test_a_review_step_tagged_done_tells_the_coordinator_when_the_owner_approves(self):
        task = self.planned('Make the offer', plan=[{'text': 'Agree the price', 'check': {'kind': 'review'}, 'notify': 'done'}, 'Send it'])
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.ok(self.step(self.owner, 'confirm', task, step_id='s1', approved=True))
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)],
                         [('plan_step_submitted', [self.lead]), ('plan_step_done', [self.lead]), ('plan_step_approved', [self.worker])])

    def test_a_step_without_a_review_is_not_confirmed(self):
        task = self.planned()
        self.refused(self.step(self.lead, 'confirm', task, step_id='s1', approved=True), 'does not wait for confirmation')

    def test_a_waiting_step_can_be_skipped(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        skipped = self.ok(self.step(self.lead, 'skip', task, step_id='s2', reason='The owner set the price'))
        self.assertEqual(skipped['plan_steps'][1], {'id': 's2', 'text': 'Agree the price', 'state': 'skipped', 'check': 'review',
                                                    'note': 'The owner set the price'})
        self.assertEqual(skipped['next_step']['id'], 's3')
        # The worker was told it would hear of the decision, so it hears of this one.
        self.assertEqual(self.told(task)[1], ('plan_step_skipped', 'Step s2 of "Make the offer" was skipped and no longer waits '
                                              'for confirmation: Agree the price. Reason: The owner set the price. '
                                              'Go on with the plan.', [self.worker]))
        self.ok(self.step(self.lead, 'skip', task, step_id='s2', reason='The owner set the price'))
        self.assertEqual(len(self.told(task)), 2)

    def test_skipping_a_step_that_did_not_wait_tells_no_worker(self):
        task = self.reviewed()
        self.ok(self.step(self.lead, 'skip', task, step_id='s2', reason='The owner set the price'))
        self.ok(self.step(self.lead, 'skip', task, step_id='s3', reason='Sent already'))
        self.assertEqual(self.told(task), [])

    def test_what_told_the_coordinator_is_settled_when_the_step_is_decided_or_skipped(self):
        for decide in (lambda task: self.step(self.lead, 'confirm', task, step_id='s2', approved=True),
                       lambda task: self.step(self.owner, 'confirm', task, step_id='s2', approved=False, feedback='In writing.'),
                       lambda task: self.step(self.owner, 'done', task, step_id='s2'),
                       lambda task: self.step(self.owner, 'skip', task, step_id='s2', reason='The owner set the price')):
            task, other = self.reviewed(f'Offer {uuid4()}'), self.reviewed(f'Another offer {uuid4()}')
            self.ok(self.step(self.worker, 'done', task, step_id='s2'))
            self.ok(self.step(self.worker, 'done', other, step_id='s2'))
            self.assertEqual(self.waiting_for(self.lead, task), ['plan_step_submitted'])
            self.ok(decide(task))
            self.assertNotIn('plan_step_submitted', self.waiting_for(self.lead, task))
            # Only that step's hand-in is settled: another Task's still waits for him, and so does the worker's news.
            self.assertEqual(self.waiting_for(self.lead, other), ['plan_step_submitted'])
            self.assertEqual(len(self.waiting_for(self.worker, task)), 1)
        self.assertEqual(self.rows("SELECT DISTINCT d.outcome FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                                   "WHERE e.source_id=? AND e.event_type='plan_step_submitted'", task['id']),
                         [('Settled: the step no longer waits for confirmation',)])

    def test_a_step_handed_in_again_waits_for_him_again(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='In writing.'))
        self.assertEqual(self.waiting_for(self.lead, task), [])
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.assertEqual(self.waiting_for(self.lead, task), ['plan_step_submitted'])

    def test_what_told_the_coordinator_is_settled_when_the_task_closes_or_its_plan_is_replaced(self):
        for settle in (lambda task: self.update(self.owner, task, {'status': 'cancelled'}),
                       lambda task: self.complete(self.lead, task, result='Agreed elsewhere.'),
                       lambda task: self.invoke(self.owner, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                                               'plan': ['Start over']}),
                       lambda task: self.invoke(self.owner, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker,
                                                                               'plan': []})):
            task, other = self.reviewed(f'Offer {uuid4()}'), self.reviewed(f'Another offer {uuid4()}')
            self.ok(self.step(self.worker, 'done', task, step_id='s2'))
            self.ok(self.step(self.worker, 'done', other, step_id='s2'))
            # An ordinary write of the Task leaves it waiting.
            self.ok(self.update(self.owner, task, {'due': '2026-12-02'}))
            self.assertEqual(self.waiting_for(self.lead, task), ['plan_step_submitted'])
            self.ok(settle(task))
            self.assertEqual(self.waiting_for(self.lead, task), [])
            self.assertEqual(self.waiting_for(self.lead, other), ['plan_step_submitted'])


class CheckTests(PlanFixture):
    def checked(self, check):
        return self.planned(f'Checked work {uuid4()}', plan=[{'text': 'Finish the checked thing', 'check': check}, 'Report'])

    def output(self, task):
        return self.plans.run(self.stored(task))['steps'][0]['output']

    def first(self, task):
        return self.states(self.read(self.worker, task)['plan_steps'])[0]

    def test_a_signoff_named_by_the_plan(self):
        asked = self.question()
        task = self.checked({'kind': 'signoff', 'signoff_id': asked['id']})
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'it is pending')
        # The coordinator is held to the check as well, and nothing was recorded.
        self.refused(self.step(self.lead, 'done', task, step_id='s1'), 'it is pending')
        self.assertEqual(self.first(task), ('s1', 'pending'))
        self.answer(asked)
        done = self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Answered this morning'))
        self.assertEqual(self.states(done['plan_steps'])[0], ('s1', 'completed'))
        self.assertEqual(self.output(task), {'check': 'signoff', 'signoff_id': asked['id']})

    def test_a_signoff_named_as_evidence(self):
        answered, approved = self.question(), self.proposal()
        self.answer(answered)
        self.decide(approved, 'approve')
        self.assertIn(self.signoff_state(approved)[0], ('approved', 'applied'))
        task = self.checked({'kind': 'signoff'})
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'evidence.signoff_id')
        self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'task_id': 'x'}), 'evidence.signoff_id')
        self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': 'nope'}), 'no such Signoff')
        self.ok(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': answered['id']}))
        self.assertEqual(self.output(task), {'check': 'signoff', 'signoff_id': answered['id']})
        other = self.checked({'kind': 'signoff'})
        self.ok(self.step(self.lead, 'done', other, step_id='s1', evidence={'signoff_id': approved['id']}))

    def test_a_signoff_the_owner_turned_down_or_that_was_withdrawn_never_passes(self):
        rejected, withdrawn = self.proposal(), self.question()
        self.decide(rejected, 'reject')
        self.ok(self.invoke(self.worker, 'signoffs_input_cancel', {'signoff_id': withdrawn['id']}))
        self.assertEqual((self.signoff_state(rejected)[0], self.signoff_state(withdrawn)[0]), ('rejected', 'cancelled'))
        for signoff, state in ((rejected, 'rejected'), (withdrawn, 'cancelled')):
            named = self.checked({'kind': 'signoff', 'signoff_id': signoff['id']})
            error = self.refused(self.step(self.worker, 'done', named, step_id='s1'), f'it is {state}, which never passes')
            self.assertIn('Only the coordinator releases the step: by skipping it', error['message'])
            self.assertEqual(self.first(named), ('s1', 'pending'))
            loose = self.checked({'kind': 'signoff'})
            error = self.refused(self.step(self.worker, 'done', loose, step_id='s1', evidence={'signoff_id': signoff['id']}),
                                 f'it is {state}, which never passes')
            self.assertIn('Name another Signoff as evidence, or ask the coordinator to skip the step', error['message'])
        self.ok(self.step(self.lead, 'skip', named, step_id='s1', reason='The owner said no'))

    def test_a_replaced_signoff_does_not_pass_and_its_replacement_does(self):
        first = self.proposal()
        second = self.proposal(supersedes=first)
        self.assertTrue(self.signoff_state(first)[1])
        task = self.checked({'kind': 'signoff'})
        self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': first['id']}),
                     'was replaced by a newer one')
        self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': second['id']}), 'it is pending')
        self.decide(second, 'approve')
        # Replaced after the owner had responded, the older one still does not count.
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET state='approved', error_json=NULL WHERE id=?", (first['id'],))
        self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': first['id']}),
                     'was replaced by a newer one')
        self.ok(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': second['id']}))

    def test_a_signoff_the_caller_may_not_read_is_refused_like_one_that_does_not_exist(self):
        answered = self.question()
        self.answer(answered)
        task = self.checked({'kind': 'signoff'})
        unknown = self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': 'nope'}),
                               'you can read no such Signoff in this scope')['message']
        # The policy that governs reading Signoffs governs the check: denied, the answered Signoff says nothing.
        self.policy.modes['signoffs_get'] = 'deny'
        denied = self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': answered['id']}),
                              'you can read no such Signoff in this scope')['message']
        self.assertEqual(denied, unknown.replace('nope', answered['id']))
        self.assertEqual(self.first(task), ('s1', 'pending'))
        self.policy.modes.clear()
        self.ok(self.step(self.worker, 'done', task, step_id='s1', evidence={'signoff_id': answered['id']}))

    def projects(self):
        """Two projects the worker and the coordinator belong to, and a call made in one of them."""
        alpha, beta = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            for identity, slug in ((alpha, 'alpha'), (beta, 'beta')):
                uow.repository('projects').insert({'id': identity, 'name': slug, 'slug': slug, 'workspace': slug})

        def call(who, project, operation, arguments, task=None):
            ctx = replace(self.ctx(who), project_id=project, review_project_id=project,
                          allowed_project_ids=frozenset({alpha, beta}))
            options = {'idempotency_key': str(uuid4())}
            if task:
                options['expected_revision'] = self.rows('SELECT revision FROM tasks WHERE id=?', task['id'])[0][0]
                arguments = {'task_id': task['id'], **arguments}
            return self.app.invoke(ctx, operation, arguments, options)
        return alpha, beta, call

    def test_a_signoff_of_another_project_is_refused_without_saying_how_it_stands(self):
        alpha, beta, call = self.projects()
        task = plain(self.ok(call(self.lead, alpha, 'tasks_create', {'title': 'In alpha'})))['task']
        self.ok(call(self.lead, alpha, 'tasks_delegate', {'worker_id': self.worker, 'plan': [
            {'text': 'Wait for the owner', 'check': {'kind': 'signoff'}}, 'Report']}, task))
        ctx = lambda project: replace(self.ctx(self.worker), project_id=project, review_project_id=project,
                                       allowed_project_ids=frozenset({alpha, beta}))
        there, here = self.question(ctx=ctx(beta)), self.question(ctx=ctx(alpha))
        owner = lambda project: replace(self.deciding_owner(), project_id=project, review_project_id=project,
                                         allowed_project_ids=frozenset({alpha, beta}))
        self.answer(there, owner(beta))
        self.assertEqual(self.signoff_state(there)[0], 'answered')
        refused = call(self.worker, alpha, 'tasks_step_done', {'step_id': 's1', 'evidence': {'signoff_id': there['id']}}, task)
        message = self.refused(refused, 'you can read no such Signoff in this scope')['message']
        self.assertNotIn('answered', message)
        unknown = self.refused(call(self.worker, alpha, 'tasks_step_done', {'step_id': 's1', 'evidence': {'signoff_id': 'nope'}}, task),
                               'you can read no such Signoff in this scope')['message']
        self.assertEqual(message, unknown.replace('nope', there['id']))
        # The project's own Signoff is read, and counts once the owner has answered it.
        self.refused(call(self.worker, alpha, 'tasks_step_done', {'step_id': 's1', 'evidence': {'signoff_id': here['id']}}, task),
                     'it is pending')
        self.answer(here, owner(alpha))
        self.ok(call(self.worker, alpha, 'tasks_step_done', {'step_id': 's1', 'evidence': {'signoff_id': here['id']}}, task))

    def test_a_task_that_must_be_done(self):
        needed = self.task(self.lead, 'Needed first')
        task = self.checked({'kind': 'task', 'task_id': needed['id']})
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'is done; it is')
        self.ok(self.complete(self.lead, needed, result='Finished.'))
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.assertEqual(self.output(task), {'check': 'task', 'task_id': needed['id']})
        # Named as evidence; a cancelled or unknown Task does not count.
        loose = self.checked({'kind': 'task'})
        self.refused(self.step(self.worker, 'done', loose, step_id='s1'), 'evidence.task_id')
        self.refused(self.step(self.worker, 'done', loose, step_id='s1', evidence={'task_id': 'missing'}), 'no such Task')
        dropped = self.task(self.lead, 'Dropped')
        self.ok(self.update(self.lead, dropped, {'status': 'cancelled'}))
        self.refused(self.step(self.worker, 'done', loose, step_id='s1', evidence={'task_id': dropped['id']}), 'it is cancelled')
        self.ok(self.step(self.worker, 'done', loose, step_id='s1', evidence={'task_id': needed['id']}))

    def test_a_task_check_cannot_name_its_own_task(self):
        loose = self.checked({'kind': 'task'})
        self.refused(self.step(self.worker, 'done', loose, step_id='s1', evidence={'task_id': loose['id']}), 'cannot name its own Task')
        self.refused(self.step(self.lead, 'done', loose, step_id='s1', evidence={'task_id': loose['id']}), 'cannot name its own Task')
        # Named by a step added to the plan, it is refused the same way.
        self.ok(self.step(self.lead, 'skip', loose, step_id='s1', reason='Not needed'))
        self.ok(self.step(self.worker, 'add', loose, text='Wait for this Task', after='s1',
                          check={'kind': 'task', 'task_id': loose['id']}))
        self.refused(self.step(self.worker, 'done', loose, step_id='s3'), 'cannot name its own Task')
        self.assertEqual(self.states(self.read(self.worker, loose)['plan_steps'])[1], ('s3', 'pending'))

    def folder(self, agent):
        folder = self.files / 'agents' / agent
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def test_a_file_that_must_exist(self):
        folder, path = self.folder(self.worker), f'agents/{self.worker}/report.md'
        task = self.checked({'kind': 'file', 'path': path})
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'No file exists')
        (folder / 'report.md').write_text('')
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'which is empty')
        (folder / 'report.md').write_text('Three quotes.')
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.assertEqual(self.output(task), {'check': 'file', 'path': path})

    def test_a_file_is_named_in_the_callers_own_folder(self):
        (self.folder(self.worker) / 'notes.txt').write_text('x')
        (self.folder(self.other) / 'theirs.txt').write_text('x')
        (self.files / 'notes.txt').write_text('x')
        outside = self.files.parent / 'secret.txt'
        outside.write_text('x')
        (self.folder(self.worker) / 'link.txt').symlink_to(outside)
        task = self.checked({'kind': 'file'})
        self.refused(self.step(self.worker, 'done', task, step_id='s1'), 'evidence.path')
        own = f'agents/{self.worker}'
        for path, fragment in ((str(outside), 'outside the operations root'), ('../secret.txt', "'..'"),
                               (own + '/link.txt', 'symbolic link'), (own + '/reports', 'No file exists'), ('.', 'is required')):
            self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'path': path}), fragment)
        # Elsewhere under the operations root the answer is the same whether or not a file is there.
        answers = set()
        for path in (f'agents/{self.other}/theirs.txt', f'agents/{self.other}/missing.txt', 'notes.txt', 'missing.txt',
                     'projects/alpha/plan.md', own):
            message = self.refused(self.step(self.worker, 'done', task, step_id='s1', evidence={'path': path}),
                                   'is not in either')['message']
            answers.add(message.replace(path, '<path>'))
        self.assertEqual(len(answers), 1)
        # The coordinator is held to his own folder too; the owner reads the whole operations root, as in the file viewer.
        self.refused(self.step(self.lead, 'done', task, step_id='s1', evidence={'path': own + '/notes.txt'}), 'is not in either')
        self.assertEqual(self.first(task), ('s1', 'pending'))
        self.ok(self.step(self.worker, 'done', task, step_id='s1', evidence={'path': str(self.files / own / 'notes.txt')}))
        self.assertEqual(self.output(task), {'check': 'file', 'path': own + '/notes.txt'})
        other = self.checked({'kind': 'file'})
        self.ok(self.step(self.owner, 'done', other, step_id='s1', evidence={'path': 'notes.txt'}))

    def test_a_file_of_the_tasks_project_counts(self):
        alpha, beta, call = self.projects()
        for slug in ('alpha', 'beta'):
            (self.files / 'projects' / slug).mkdir(parents=True)
            (self.files / 'projects' / slug / 'offer.md').write_text('The offer.')
        task = plain(self.ok(call(self.lead, alpha, 'tasks_create', {'title': 'In alpha'})))['task']
        self.ok(call(self.lead, alpha, 'tasks_delegate', {'worker_id': self.worker, 'plan': [
            {'text': 'Write the offer', 'check': {'kind': 'file'}}, 'Report']}, task))
        done = lambda path: call(self.worker, alpha, 'tasks_step_done', {'step_id': 's1', 'evidence': {'path': path}}, task)
        self.refused(done('projects/beta/offer.md'), 'is not in either')
        self.ok(done('projects/alpha/offer.md'))

    def test_a_checked_step_keeps_the_order_and_can_be_skipped(self):
        asked = self.question()
        task = self.planned('Ordered', plan=['First', {'text': 'Second', 'check': {'kind': 'signoff', 'signoff_id': asked['id']}}])
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'comes after step s1')
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'it is pending')
        self.ok(self.step(self.lead, 'skip', task, step_id='s2', reason='Not needed after all'))

    def test_a_checked_step_whose_write_failed_is_told_when_it_is_repeated(self):
        asked = self.question()
        self.answer(asked)
        task = self.planned('Paid', plan=[{'text': 'Pay', 'notify': 'done', 'check': {'kind': 'signoff', 'signoff_id': asked['id']}},
                                          'Report'])
        self.interrupt('tasks_step_done', lambda: self.ok(self.update(self.lead, task, {'next_step': 'Look at it'})))
        self.assertEqual(self.step(self.worker, 'done', task, step_id='s1', note='Paid.').status, 'error')
        self.assertEqual((self.first(task), self.told(task)), (('s1', 'completed'), []))
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Paid.'))
        self.ok(self.step(self.worker, 'done', task, step_id='s1', note='Paid.'))
        self.assertEqual([(kind, who) for kind, _, who in self.told(task)], [('plan_step_done', [self.lead])])
        self.refused(self.step(self.worker, 'done', task, step_id='s1', note='Other words'), 'already completed')

    def test_a_check_is_looked_up_even_when_the_step_before_finishes_meanwhile(self):
        asked = self.question()
        task = self.planned('Ordered', plan=['First', {'text': 'Pay', 'check': {'kind': 'signoff', 'signoff_id': asked['id']}}, 'Third'])
        workflows = self.layer.workflows
        read, calls = workflows.plan_run, []

        def plan_run(key):
            found = read(key)
            if not calls:
                # The parallel tasks_step_done of the step before lands after this call has read the plan.
                calls.append(key)
                self.ok(self.step(self.worker, 'done', task, step_id='s1'))
            return found
        workflows.plan_run = plan_run
        self.addCleanup(setattr, workflows, 'plan_run', read)
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'comes after step s1')
        workflows.plan_run = read
        self.assertEqual(self.states(self.read(self.worker, task)['plan_steps']),
                         [('s1', 'completed'), ('s2', 'pending'), ('s3', 'pending')])
        self.refused(self.step(self.worker, 'done', task, step_id='s2'), 'it is pending')
        # A step whose check holds is still refused out of order, before the engine is asked.
        self.answer(asked)
        late = self.planned('Late', plan=['First', {'text': 'Pay', 'check': {'kind': 'signoff', 'signoff_id': asked['id']}}])
        self.refused(self.step(self.worker, 'done', late, step_id='s2'), 'comes after step s1')
        self.assertEqual(self.states(self.read(self.worker, late)['plan_steps']), [('s1', 'pending'), ('s2', 'pending')])


class GuardTests(PlanFixture):
    """While a plan holds an unfinished step with a check, only the coordinator or the owner replaces it."""
    PLAIN = ['Prepare', 'Agree the price', 'Send the offer']

    def hand(self, who, task, **arguments):
        return self.invoke(who, 'tasks_delegate', {'task_id': task['id'], 'worker_id': self.worker, **arguments})

    def test_a_worker_cannot_replace_a_plan_that_still_has_a_checked_step(self):
        for started in (False, True):
            task = self.planned(f'Offer {started}', plan=REVIEWED)
            if started:
                self.ok(self.step(self.worker, 'done', task, step_id='s1'))
                self.ok(self.step(self.worker, 'done', task, step_id='s2'))
            before = self.rows('SELECT revision, fields_json FROM tasks WHERE id=?', task['id'])
            for arguments in ({'plan': self.PLAIN}, {'plan': []}, {'plan': REVIEWED[:1]}):
                self.refused_as(self.hand(self.worker, task, **arguments), 'forbidden', 'only the coordinator or the owner')
            self.refused_as(self.hand(self.other, task, plan=self.PLAIN), 'forbidden', 'Ask the coordinator')
            self.assertEqual(self.rows('SELECT revision, fields_json FROM tasks WHERE id=?', task['id']), before)
            # The same plan handed over again changes nothing and is not a replacement.
            self.ok(self.hand(self.worker, task, plan=REVIEWED))
            self.assertEqual(len(self.read(self.worker, task)['plan_steps']), 3)

    def test_the_coordinator_or_the_owner_replaces_it(self):
        task = self.planned('Offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.ok(self.hand(self.lead, task, plan=self.PLAIN))
        self.assertEqual(self.states(self.read(self.worker, task)['plan_steps']),
                         [('s1', 'pending'), ('s2', 'pending'), ('s3', 'pending')])
        self.ok(self.hand(self.lead, task, plan=REVIEWED))
        self.ok(self.hand(self.owner, task, plan=[]))
        self.assertEqual(list(self.read(self.worker, task)['plan']), [])

    def test_a_worker_still_replaces_a_plan_without_an_unfinished_checked_step(self):
        plain_task = self.planned()
        self.ok(self.step(self.worker, 'done', plain_task, step_id='s1'))
        self.ok(self.hand(self.worker, plain_task, plan=['Start over']))
        self.assertEqual(self.states(self.read(self.worker, plain_task)['plan_steps']), [('s1', 'pending')])
        # Notify tags are not checks.
        tagged = self.planned('Tagged', plan=[{'text': 'One', 'notify': 'both'}, 'Two'])
        self.ok(self.hand(self.worker, tagged, plan=['One', 'Two']))
        # Once every checked step is finished the plan is the worker's to replace again.
        task = self.planned('Offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        self.refused_as(self.hand(self.worker, task, plan=self.PLAIN), 'forbidden', 'only the coordinator or the owner')
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=True))
        self.ok(self.hand(self.worker, task, plan=self.PLAIN))

    def test_a_checked_step_added_to_a_plain_plan_is_guarded_too(self):
        task = self.planned()
        self.ok(self.step(self.worker, 'add', task, text='Have it confirmed', check={'kind': 'review'}))
        self.refused_as(self.hand(self.worker, task, plan=['Start over']), 'forbidden', 'only the coordinator or the owner')
        self.ok(self.step(self.lead, 'skip', task, step_id='s4', reason='Not needed'))
        self.ok(self.hand(self.worker, task, plan=['Start over']))

    def test_no_other_task_write_carries_a_plan(self):
        task = self.planned('Offer', plan=REVIEWED)
        for changes in ({'plan': self.PLAIN}, {'fields': {'$task': {'plan': self.PLAIN}}}, {'plan_template': None}):
            self.assertEqual(self.update(self.worker, task, changes).status, 'error')
        self.assertEqual(self.invoke(self.worker, 'tasks_create', {'title': 'New', 'plan': self.PLAIN}).status, 'error')
        self.assertEqual(list(self.read(self.worker, task)['plan']), REVIEWED)
