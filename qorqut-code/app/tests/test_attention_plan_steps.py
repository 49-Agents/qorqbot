"""Reminders know a delegated Task's plan: the worker's step in hand, and the steps that wait for the coordinator."""
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import qorqut_agentic
from qorqut.agents import attention as boards
from qorqut.agents.attention_delivery import AttentionRuntime, _backoff
from qorqut_agentic.behavior import attention
from qorqut_agentic.behavior.instructions import from_catalog
from qorqut_agentic.errors import StoreError
from qorqut_operations.discussion import emit_event

from test_task_plans import PLAN, REVIEWED, PlanFixture

CATALOG = Path(qorqut_agentic.__file__).parent / 'resources/defaults/operations/runtime-instructions'
TASK = {'id': 'task-1', 'project_id': None, 'title': 'Make the offer', 'status': 'working', 'workers': [],
        'workers_omitted': 0, 'user_needed': False}
FOCUS = {'type': 'task', 'id': 'task-1', 'project_id': None}
LATER = '2099-01-01T00:00:00.000000Z'


def instructions():
    return from_catalog(lambda name: (CATALOG / name).read_text())


class PlanStepTextTests(unittest.TestCase):
    def setUp(self):
        self.instructions = instructions()

    def text(self, step=None, team=None, **extra):
        snapshot = {'focus': FOCUS, 'focused_for': '10m', 'work': TASK, 'next_step': 'Next', 'intent': '', 'candidates': [],
                    'more': False, 'team': team, **({'plan_step': step} if step is not None else {}), **extra}
        return attention.render('focus', snapshot, instructions=self.instructions)

    def test_a_worker_sees_the_step_in_hand_beside_the_existing_lines(self):
        text = self.text({'position': 'step 2 of 3', 'id': 's2', 'text': 'Agree the price'})
        self.assertIn('Plan step: {"position":"step 2 of 3","id":"s2","text":"Agree the price"}', text)
        self.assertIn('Next step: "Next"', text)
        self.assertLess(text.index('Next step:'), text.index('Plan step:'))
        self.assertIn('Continue the Task your todo_focus points at', text)
        self.assertNotIn('confirmation', text)
        self.assertNotIn('sent back', text)

    def test_a_step_that_waits_for_confirmation_sends_the_worker_to_its_next_task(self):
        text = self.text({'position': 'step 2 of 3', 'id': 's2', 'text': 'Agree the price', 'awaiting_review': True})
        self.assertIn('"waiting_for_confirmation":true', text)
        self.assertIn("waits for the coordinator's confirmation", text)
        self.assertIn('Do not redo the step and do not wait for his answer: take your next Task meanwhile.', text)
        # The instruction is part of the lead, above the quoted read-only snapshot.
        self.assertLess(text.index('take your next Task meanwhile'), text.index('Todo snapshot'))
        self.assertIn('Continue the Task your todo_focus points at', text)

    def test_a_step_that_was_sent_back_shows_the_feedback_to_act_on(self):
        text = self.text({'position': 'step 2 of 3', 'id': 's2', 'text': 'Agree the price', 'feedback': 'Get it in writing. ' * 40})
        self.assertIn('"sent_back":true,"feedback":"Get it in writing.', text)
        self.assertIn('Act on the feedback in the snapshot below, then mark the step done again with tasks_step_done.', text)
        self.assertLess(text.index('Act on the feedback'), text.index('Todo snapshot'))
        self.assertNotIn('take your next Task meanwhile', text)
        line = next(line for line in text.splitlines() if line.startswith('Plan step:'))
        self.assertLess(len(line), 450)                                     # the feedback is clipped
        self.assertTrue(line.endswith('…"}'))

    def test_a_finished_plan_says_so(self):
        self.assertIn('Plan step: {"position":"all 3 steps finished"}', self.text({'position': 'all 3 steps finished'}))

    def test_a_task_without_a_plan_renders_as_before(self):
        plain = self.text()
        self.assertEqual(plain, self.text(plan_step=None))
        self.assertNotIn('Plan step', plain)
        self.assertEqual(plain, '\n'.join([
            self.instructions.render('attention-focus') + self.instructions.render('attention-continue'), '',
            self.instructions.render('attention-data'), 'todo_focus: ' + attention.quote(FOCUS) + '; focused for: 10m',
            'Work task: ' + attention.quote(TASK), 'Next step: "Next"',
            self.instructions.render('attention-candidates', COUNT='5') + self.instructions.render('attention-empty'),
            self.instructions.render('attention-more')]))

    def test_the_coordinator_is_shown_the_steps_that_wait_for_him(self):
        team = {'available': [], 'stalled': [], 'stalled_count': 0, 'blocked': [], 'blocked_count': 0, 'awaiting_count': 12,
                'plans_unread': 2, 'awaiting': [{'id': 'offer', 'project_id': None, 'title': 'Make the offer', 'workers': ['Scout'],
                                                 'step': 's2', 'position': 'step 2 of 3', 'text': 'Agree the price',
                                                 'note': 'They accept 400.'}]}
        text = self.text(team=team)
        self.assertIn('whose current plan step waits for your confirmation', text)
        self.assertIn('tasks_step_confirm', text)
        self.assertIn('- {"id":"offer","project_id":null,"title":"Make the offer","workers":["Scout"],"step":"s2",'
                      '"position":"step 2 of 3","text":"Agree the price","note":"They accept 400."}', text)
        self.assertIn('- more Tasks wait for your confirmation', text)
        self.assertIn('- the plans of 2 more delegated Tasks were not read', text)
        self.assertLess(text.index('waits for your confirmation'), text.index('no update for 10 minutes'))
        # The coordinator's own focused Task shows its step; the worker's instruction is not his.
        own = self.text({'position': 'step 1 of 2', 'id': 's1', 'text': 'Decide', 'awaiting_review': True},
                        team={**team, 'awaiting': [], 'awaiting_count': 0, 'plans_unread': 0})
        self.assertIn('Plan step: {"position":"step 1 of 2"', own)
        self.assertNotIn('take your next Task meanwhile', own)
        self.assertIn('tasks_step_confirm; the worker is on other work meanwhile): none', own)


class PlanSnapshotTests(PlanFixture):
    def setUp(self):
        super().setUp()
        self.reminders = AttentionRuntime(NS(application=self.app, task_plans=self.plans))
        self.reads = 0
        read = self.layer.workflows.plan_run

        def counted(key):
            self.reads += 1
            return read(key)
        patch.object(self.layer.workflows, 'plan_run', counted).start()
        self.addCleanup(patch.stopall)

    def ref(self, task):
        return {'type': 'task', 'id': task['id'], 'project_id': None}

    def snapshot(self, agent, task):
        state = {'work_task_json': self.ref(task), 'focus_started_at': None, 'intent': ''}
        self.reads = 0
        with self.db.read() as uow:
            return self.reminders._snapshot(uow, agent, state, self.ref(task))

    def reviewed(self, title='Make the offer'):
        task = self.planned(title, plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        return task

    def team(self, now=LATER):
        self.reads = 0
        with self.db.read() as uow:
            return self.attention.team(uow, self.lead, now, plans=self.reminders._plan)

    def test_the_workers_reminder_carries_the_step_in_hand(self):
        task = self.planned()
        self.planned('Another planned Task')                                # a candidate: its plan is not read
        self.assertEqual(self.snapshot(self.worker, task)['plan_step'],
                         {'position': 'step 1 of 3', 'id': 's1', 'text': PLAN[0]})          # before any step action
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        snapshot = self.snapshot(self.worker, task)
        self.assertEqual(snapshot['plan_step'], {'position': 'step 2 of 3', 'id': 's2', 'text': PLAN[1]})
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(snapshot['candidates']), 1)
        self.assertIsNone(snapshot['team'])
        text = attention.render('focus', snapshot, instructions=instructions())
        self.assertIn('Plan step: {"position":"step 2 of 3","id":"s2","text":"Ask each for a quote"}', text)
        for step in ('s2', 's3'):
            self.ok(self.step(self.worker, 'done', task, step_id=step))
        self.assertEqual(self.snapshot(self.worker, task)['plan_step'], {'position': 'all 3 steps finished'})

    def test_waiting_for_confirmation_and_sent_back(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertEqual(self.snapshot(self.worker, task)['plan_step'],
                         {'position': 'step 2 of 3', 'id': 's2', 'text': 'Agree the price', 'awaiting_review': True})
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='Get it in writing.'))
        snapshot = self.snapshot(self.worker, task)
        self.assertEqual(snapshot['plan_step'], {'position': 'step 2 of 3', 'id': 's2', 'text': 'Agree the price',
                                                 'feedback': 'Get it in writing.'})
        self.assertIn('"sent_back":true,"feedback":"Get it in writing."',
                      attention.render('focus', snapshot, instructions=instructions()))

    def test_a_task_without_a_plan_reads_nothing(self):
        task = self.task(self.lead, 'Quick one')
        self.delegate(task)
        snapshot = self.snapshot(self.worker, task)
        self.assertIsNone(snapshot['plan_step'])
        self.assertEqual(self.reads, 0)
        self.assertNotIn('Plan step', attention.render('focus', snapshot, instructions=instructions()))

    def test_the_reminder_is_written_without_the_plan_when_the_workflow_store_cannot_be_read(self):
        task = self.reviewed()
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        for failure in (StoreError('cannot open the existing runtime database'), OSError('gone')):
            with patch.object(self.layer.workflows, 'plan_run', side_effect=failure):
                snapshot = self.snapshot(self.worker, task)
                team = self.team()
            self.assertIsNone(snapshot['plan_step'])
            self.assertEqual(snapshot['work']['id'], task['id'])
            self.assertNotIn('Plan step', attention.render('focus', snapshot, instructions=instructions()))
            # The coordinator's pass still lists the Task, by the Task alone.
            self.assertEqual((team['awaiting'], [entry['id'] for entry in team['stalled']]), ([], [task['id']]))
            self.assertNotIn('plan', team['stalled'][0])
        # A runtime without the plan engine reads nothing either.
        bare = AttentionRuntime(NS(application=self.app))
        with self.db.read() as uow:
            self.assertIsNone(bare._snapshot(uow, self.worker, {'work_task_json': self.ref(task)}, self.ref(task))['plan_step'])

    def test_the_coordinator_is_shown_a_step_that_waits_and_the_task_is_not_stalled(self):
        waiting, working = self.reviewed(), self.reviewed('Second offer')
        quick = self.task(self.lead, 'Quick one')
        self.delegate(quick)
        self.ok(self.step(self.worker, 'done', waiting, step_id='s2', note='They accept 400.'))
        team = self.team()
        self.assertEqual(team['awaiting'], [{'id': waiting['id'], 'project_id': None, 'title': 'Make the offer',
                                             'workers': ['Worker'], 'step': 's2', 'position': 'step 2 of 3',
                                             'text': 'Agree the price', 'note': 'They accept 400.'}])
        self.assertEqual((team['awaiting_count'], team['plans_unread']), (1, 0))
        self.assertEqual({entry['id']: entry.get('plan') for entry in team['stalled']},
                         {working['id']: 'step 2 of 3', quick['id']: None})
        self.assertEqual(team['stalled_count'], 2)
        self.assertEqual(self.reads, 2)                                     # one per Task that has a plan
        # Without the plan reading the pass is what it was: every quiet Task is stalled.
        with self.db.read() as uow:
            plain = self.attention.team(uow, self.lead, LATER)
        self.assertEqual((plain['stalled_count'], plain['awaiting']), (3, []))
        # Decided, the Task is ordinary delegated work again.
        self.ok(self.step(self.lead, 'confirm', waiting, step_id='s2', approved=True))
        team = self.team()
        self.assertEqual((team['awaiting'], team['stalled_count']), ([], 3))
        # The coordinator's own reminder carries the section.
        with self.db.read() as uow:
            snapshot = self.reminders._snapshot(uow, self.lead, {}, None)
        self.assertEqual(snapshot['team']['awaiting'], [])
        self.assertIn('waits for your confirmation', attention.render('board_normal', snapshot, instructions=instructions()))

    def test_a_step_that_waits_is_shown_when_its_task_is_snoozed_or_parked(self):
        snoozed, parked, closed, mine = (self.reviewed(title) for title in ('Snoozed', 'Parked', 'Closed', 'Also mine'))
        for task in (snoozed, parked, closed, mine):
            self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        far = '2199-01-01T00:00:00.000000Z'
        self.ok(self.update(self.worker, snoozed, {'snoozed_until': far}))
        self.ok(self.update(self.worker, parked, {'status': 'waiting', 'snoozed_until': far}))
        self.ok(self.update(self.lead, closed, {'status': 'cancelled'}))
        self.ok(self.update(self.lead, mine, {'worker_ids': [self.worker, self.lead]}))
        team = self.team()
        self.assertEqual({entry['id'] for entry in team['awaiting']}, {snoozed['id'], parked['id'], mine['id']})
        self.assertEqual(team['awaiting_count'], 3)
        # The stalled and blocked lists keep their rules: none of these is shown there.
        self.assertEqual((team['stalled'], team['blocked']), ([], []))
        self.assertEqual(self.reads, 3)                                     # the closed Task's plan is not read
        # Without a step that waits, a snoozed or parked Task is in no list.
        self.ok(self.step(self.lead, 'confirm', snoozed, step_id='s2', approved=True))
        self.ok(self.step(self.lead, 'confirm', parked, step_id='s2', approved=True))
        team = self.team()
        self.assertEqual(([entry['id'] for entry in team['awaiting']], team['stalled']), ([mine['id']], []))

    def test_the_plans_read_for_one_reminder_are_bounded(self):
        tasks = [self.reviewed(f'Offer {number}') for number in range(4)]
        for task in tasks:
            self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        with patch.object(boards, 'PLAN_READ_LIMIT', 3):
            team = self.team()
        self.assertEqual(self.reads, 3)
        self.assertEqual((team['awaiting_count'], team['plans_unread'], team['stalled_count']), (3, 1, 1))
        self.assertEqual({entry['id'] for entry in team['awaiting']} | {team['stalled'][0]['id']}, {task['id'] for task in tasks})


class PlanStepDeliveryTests(PlanFixture):
    """A plan step event reaches the agent it is addressed to, whatever that agent is focused on."""

    def focus(self, agent, task):
        ref = {'type': 'task', 'id': task['id'], 'project_id': None}
        with self.db.transaction() as uow:
            self.attention.ensure(uow, agent)
            uow.connection.execute('UPDATE agent_attention SET focus_json=?,work_task_json=? WHERE agent_id=?',
                                   (boards.json.dumps(ref), boards.json.dumps(ref), agent))

    def pending(self, agent, task):
        """The agent's unacknowledged events about the Task: their type, and whether a focused agent is shown them."""
        with self.db.read() as uow:
            events = uow.connection.execute(
                "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? "
                "AND d.acknowledged_at IS NULL AND e.source_type='tasks' AND e.source_id=? ORDER BY e.id", (agent, task['id'])).fetchall()
            return [(event['event_type'], self.attention.related_event(uow, agent, event)) for event in events]

    def changed(self, task, agent):
        """An ordinary event about the Task as it now stands, addressed to the agent."""
        with self.db.transaction() as uow:
            revision = uow.connection.execute('SELECT revision FROM tasks WHERE id=?', (task['id'],)).fetchone()[0]
            emit_event(uow, project_id=None, source_type='tasks', source_id=task['id'], source_revision=revision,
                       actor_id=self.lead, event_type='updated', summary='Due set.', required={agent})

    def acknowledge(self, agent):
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE inbox_deliveries SET acknowledged_at='x' WHERE agent_id=?", (agent,))

    def test_step_events_pass_a_focus_on_other_work_and_survive_a_write_of_the_task(self):
        task, elsewhere, own = self.planned('Make the offer', plan=REVIEWED), self.planned('Other work'), self.task(self.lead, 'Coordinating')
        self.focus(self.worker, elsewhere)
        self.focus(self.lead, own)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        self.acknowledge(self.lead)
        self.acknowledge(self.worker)
        # A field change of a Task the worker is not focused on still waits for its focus to end.
        self.changed(task, self.worker)
        self.assertEqual(self.pending(self.worker, task), [('updated', False)])
        self.acknowledge(self.worker)
        # Handed in: the coordinator is shown it at once, and writing the Task does not acknowledge it for him.
        self.ok(self.step(self.worker, 'done', task, step_id='s2', note='They accept 400.'))
        self.assertEqual(self.pending(self.lead, task), [('plan_step_submitted', True)])
        self.ok(self.update(self.lead, task, {'due': '2026-12-02'}))
        self.assertEqual(self.pending(self.lead, task), [('plan_step_submitted', True)])
        self.acknowledge(self.worker)
        # Sent back: the worker, focused on its next Task, is shown it, and its own write of the Task leaves it pending.
        self.ok(self.step(self.lead, 'confirm', task, step_id='s2', approved=False, feedback='Get it in writing.'))
        self.assertEqual(self.pending(self.worker, task), [('plan_step_returned', True)])
        self.ok(self.update(self.worker, task, {'next_step': 'Ask them to write it down'}))
        self.assertEqual(self.pending(self.worker, task), [('plan_step_returned', True)])
        # An ordinary Task event is still acknowledged by the worker's write of the Task.
        self.acknowledge(self.worker)
        self.changed(task, self.worker)
        self.assertEqual(self.pending(self.worker, task), [('updated', False)])
        self.ok(self.update(self.worker, task, {'next_step': 'Waiting for their letter'}))
        self.assertEqual(self.pending(self.worker, task), [])

    def test_a_step_handed_in_starts_the_coordinators_back_off_over(self):
        task = self.planned('Make the offer', plan=REVIEWED)
        self.ok(self.step(self.worker, 'done', task, step_id='s1'))
        quiet = {'last_owner_message_at': None, 'focus_started_at': None, 'last_agent_activity_at': None}
        with self.db.read() as uow:
            before = _backoff(uow, self.lead, quiet, None, LATER)['changed_at']
        self.ok(self.step(self.worker, 'done', task, step_id='s2'))
        created = self.rows("SELECT created_at FROM inbox_events WHERE event_type='plan_step_submitted'")[0][0]
        with self.db.read() as uow:
            self.assertEqual(_backoff(uow, self.lead, quiet, None, LATER)['changed_at'], created)
            self.assertIsNone(_backoff(uow, self.worker, quiet, None, LATER))
        self.assertNotEqual(before, created)


if __name__ == '__main__':
    unittest.main()
