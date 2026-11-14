"""A delegated Task's plan, worked as a plan run of the workflow engine.

The Task holds the plan as it was handed over: ``plan``, its ordered step instructions. Where the work stands lives in
the workflow engine. The first step action starts a plan run under a key made from the Task and its plan text, so the
same Task and plan always reach the same run and a replaced plan starts a new one. A step action never writes the Task
record: marking a step tells nobody and moves nothing on a board.

Who may act is decided by the Task. Its recorded worker marks steps done and adds steps; so may the coordinator and the
installation owner. Only the coordinator or the owner skips a step, which is how a worker's question about a step is
answered.

The engine takes the instruction store's lock for every change. That lock must never be waited for inside the
database's write transaction, so the change is made in ``external``: after authorization, outside any transaction,
before the short write transaction that saves the receipt.
"""

from __future__ import annotations

import hashlib
import json
import threading

from qorqut_agentic.errors import AgenticError
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import IDENTITY, TEXT, PlanBuilder, ref, schema
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.scope import task_context, task_namespace
from qorqut_operations.tasks.types import PLAN_TEXT_LIMIT, attributes

from qorqut.agents.attention import task_workers
from qorqut.management.identity import configured_ordubashy, is_active_installation_owner

PLAN_OPERATIONS = ('tasks_step_done', 'tasks_step_add', 'tasks_step_skip')
# The plan run is started by the host on the Task's behalf; each step records the agent or person who acted on it.
HOST_ACTOR = {'actor_id': 'task-plans', 'actor_kind': 'host'}
FINISHED = ('completed', 'skipped')
OUTCOME_LIMIT = 256

SCOPE = {'scope': {'enum': ['business', 'global']}}
STEP = {'type': 'string', 'minLength': 1, 'maxLength': 40,
        'description': "The step's id as the Task's plan_steps lists it, such as s2."}
STEP_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': PLAN_TEXT_LIMIT}
DESCRIPTIONS = {
    'tasks_step_done': (
        "Mark one step of a Task's plan as done, with an optional short note of what came of it. Steps are done in "
        "order. The answer lists the plan with every step's state and names the next step. Nobody is notified; when the "
        "last step is finished, verify the result and close the Task with tasks_complete."),
    'tasks_step_add': (
        "Add a step to a Task's plan when the work turns out to need one. It goes right after the step named in after, "
        "or at the end of the plan when after is left out. A step cannot be placed before work that has already "
        "started."),
    'tasks_step_skip': (
        "Close one unfinished step of a Task's plan without doing it, with the reason. This is the coordinator's or the "
        "owner's decision: a worker asks the coordinator and goes on with its next Task meanwhile."),
}
TITLES = {'tasks_step_done': 'Mark plan step done', 'tasks_step_add': 'Add plan step', 'tasks_step_skip': 'Skip plan step'}


def plan_key(task):
    """The engine operation ID a Task's plan runs under: one per Task and plan text."""
    material = json.dumps([task.get('business_id'), task['id'], task['created_at'], attributes(task)['plan']],
                          ensure_ascii=False, separators=(',', ':'))
    return 'plan-' + hashlib.sha256(material.encode()).hexdigest()[:48]


def step_views(plan, run):
    """One entry per step, in order: its id, text and state, the note it was closed with, and which step is current."""
    if run is None:
        steps = [{'id': f's{number}', 'text': text, 'state': 'pending'} for number, text in enumerate(plan, 1)]
    else:
        steps = []
        for step in run['steps']:
            view = {'id': step['id'], 'text': step['instructions'], 'state': step['state']}
            if step.get('completion_note'):
                view['note'] = step['completion_note']
            if step.get('added'):
                view['added'] = True
            steps.append(view)
    current = next((step for step in steps if step['state'] not in FINISHED), None)
    if current is not None:
        current['current'] = True
    return steps


class TaskPlans:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application
        self.tasks = runtime.domains.tasks
        self.outcomes, self.lock = {}, threading.Lock()

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
                      global_capability='read_global_context', resolve_namespace=task_namespace,
                      external=self.external, apply=self.apply)
        shared = {**SCOPE, 'task_id': IDENTITY, 'rationale': TEXT}
        inputs = {
            'tasks_step_done': ({'step_id': STEP, 'note': {'type': 'string', 'maxLength': 1000,
                                                           'description': 'What came of the step, in a sentence.'}},
                                ('task_id', 'step_id')),
            'tasks_step_add': ({'text': {**STEP_TEXT, 'description': 'The instruction of the new step.'},
                                'after': {**STEP, 'description': 'The id of the step the new one follows; '
                                                                 'left out, the step goes at the end.'}},
                               ('task_id', 'text')),
            'tasks_step_skip': ({'step_id': STEP, 'reason': {**STEP_TEXT, 'description': 'Why the step is not needed.'}},
                                ('task_id', 'step_id', 'reason')),
        }
        for name, (properties, required) in inputs.items():
            self.app.registry.register(OperationDescriptor(
                name, schema({**shared, **properties}, required), description=DESCRIPTIONS[name],
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, uow), **common))
        # ``tasks_get`` shows where the plan stands through this reading.
        self.tasks.plan_steps = self.steps
        return self

    # -- reading ---------------------------------------------------------------------------------------------------

    def workflows(self):
        layer = getattr(self.runtime, 'agentic_layer', None)
        if layer is None:
            raise DomainError('operation_unavailable', 'Plans are unavailable: the workflow store is not open')
        return layer.workflows

    def run(self, task):
        """The Task's plan run as it stands, or ``None`` before its first step action or when the store cannot be read."""
        try:
            found = self.workflows().plan_run(plan_key(task))
        except (AgenticError, DomainError, OSError):
            # A Task stays readable when the workflow store is not; its plan then shows as not started.
            return None
        return found['run'] if found else None

    def steps(self, ctx, task, uow):
        return step_views(attributes(task)['plan'], self.run(task))

    # -- authority -------------------------------------------------------------------------------------------------

    @staticmethod
    def check(name, ctx, task, uow):
        if not attributes(task)['plan']:
            raise DomainError('validation_error', 'This Task has no plan; work from its brief')
        if task['status'] in CLOSED_STATES:
            raise DomainError('validation_error', 'This Task is closed; its plan takes no more changes')
        if ctx.principal_kind == 'agent':
            lead = ctx.principal_id == configured_ordubashy(uow)
            if not lead and ctx.principal_id not in task_workers(task):
                raise DomainError('forbidden', "A plan is worked by the Task's recorded worker or the coordinator")
        else:
            lead = is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind)
            if not lead:
                raise DomainError('forbidden', "A plan is worked by the Task's recorded worker, the coordinator or the owner")
        if name == 'tasks_step_skip' and not lead:
            raise DomainError('forbidden', "Skipping a step is the coordinator's decision. Ask him with agents_message_send, "
                                           'naming the Task and the step, and go on with your next Task meanwhile')

    # -- the three stages ------------------------------------------------------------------------------------------

    def prepare(self, name, ctx, arguments, uow):
        ctx = task_context(ctx, arguments, uow)
        task = self.tasks.require(ctx, uow, arguments['task_id'])
        self.check(name, ctx, task, uow)
        builder = PlanBuilder(ctx, name, arguments, TITLES[name], clock=self.tasks.clock, namespace=task_namespace(ctx, arguments),
                              affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'})
        # Pinned so the step is recorded against the Task and plan the caller saw.
        builder.expect(ref('tasks', task['id'], ctx.business_id), task)
        builder.effects.append("Records progress on the Task's plan. The Task record is not changed and nobody is notified.")
        return builder.build()

    def external(self, ctx, plan):
        name, arguments = plan.operation_name, plain(plan.arguments)
        with self.app.database.read() as uow:
            scoped = task_context(ctx, arguments, uow)
            task = self.tasks.require(scoped, uow, arguments['task_id'])
            self.check(name, scoped, task, uow)
        workflows, key = self.workflows(), plan_key(task)
        actor = {'actor_id': ctx.principal_id, 'actor_kind': 'agent' if ctx.principal_kind == 'agent' else 'owner'}
        said = {field: arguments.get(field) for field in ('step_id', 'note', 'text', 'after', 'reason')}
        # A repeated request reaches this stage again before its saved receipt is found, so the engine key is made from
        # what was said, not from the request: saying the same thing twice changes the plan once.
        operation_id ='step-' + hashlib.sha256(json.dumps([name, key, said], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:48]
        try:
            origin = f"task:{task.get('business_id') or 'global'}:{task['id']}"
            run_id = workflows.start_plan(list(attributes(task)['plan']), HOST_ACTOR, origin, key)['run']['run_id']
            if name == 'tasks_step_done':
                run = workflows.complete_step(run_id, arguments['step_id'], actor, operation_id, arguments.get('note') or '')['run']
            elif name == 'tasks_step_add':
                run = workflows.add_step(run_id, arguments['text'], actor, operation_id, after=arguments.get('after'))['run']
            else:
                run = workflows.skip_step(run_id, arguments['step_id'], actor, operation_id, arguments['reason'])['run']
        except AgenticError as error:
            raise self.refusal(error, name, arguments, self.run(task)) from None
        with self.lock:
            while len(self.outcomes) >= OUTCOME_LIMIT:
                self.outcomes.pop(next(iter(self.outcomes)))
            self.outcomes[plan.payload_hash] = run

    def apply(self, ctx, plan, uow):
        with self.lock:
            run = self.outcomes.pop(plan.payload_hash, None)
        if run is None:
            raise DomainError('operation_unavailable', 'The plan change was not carried out; call the tool again')
        steps = step_views((), run)
        following = next((step for step in steps if step.get('current')), None)
        result = {'task_id': plan.arguments['task_id'], 'plan_steps': steps,
                  'next_step': {'id': following['id'], 'text': following['text']} if following else None,
                  'plan_finished': following is None}
        if following is None:
            result['hint'] = 'Every step is finished. Verify the result, then close the Task with tasks_complete.'
        return result

    @staticmethod
    def refusal(error, name, arguments, run):
        """The engine's refusal in words the caller can act on."""
        if error.code == 'lock_timeout':
            return DomainError('operation_unavailable', 'The plan store is busy; call the tool again')
        states = {step['id']: step for step in (run['steps'] if run else ())}
        step = states.get(arguments.get('step_id'))
        if error.code == 'not_found':
            return DomainError('not_found', "This plan has no such step; tasks_get lists the plan's steps")
        if error.code == 'state_conflict':
            if run and run['state'] == 'completed':
                return DomainError('validation_error', 'Every step of this plan is finished. Close the Task with tasks_complete')
            if run and run['state'] != 'running':
                return DomainError('validation_error', f"This plan is {run['state']} and takes no changes")
            if step is not None and step['state'] in FINISHED:
                return DomainError('validation_error', f"Step {step['id']} is already {step['state']}")
            if name == 'tasks_step_done' and step is not None:
                waiting = next((item for item in step.get('depends_on', ()) if states[item]['state'] not in FINISHED), None)
                if waiting:
                    return DomainError('validation_error', f"Step {step['id']} comes after step {waiting}, which is not finished. "
                                                           'Do the steps in order; only the coordinator can skip one')
        return DomainError('validation_error', 'The plan refused this change: ' + error.message)


def register_task_plans(runtime):
    existing = getattr(runtime, 'task_plans', None)
    if existing is not None:
        if isinstance(existing, TaskPlans) and existing.runtime is runtime:
            return existing
        raise DomainError('invalid_configuration', 'Conflicting task plan attachment')
    runtime.task_plans = TaskPlans(runtime).register()
    return runtime.task_plans


__all__ = ['PLAN_OPERATIONS', 'TaskPlans', 'plan_key', 'register_task_plans', 'step_views']
