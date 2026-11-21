"""A delegated Task's plan, worked as a plan run of the workflow engine.

The Task holds the plan as it was handed over: ``plan``, its ordered steps. Where the work stands lives in the workflow
engine. The first step action starts a plan run under a key made from the Task and its plan, so the same Task and plan
always reach the same run and a replaced plan starts a new one. A step action never writes the Task record and moves
nothing on a board.

Who may act is decided by the Task. Its recorded worker marks steps done and adds steps; so may the coordinator and the
installation owner. Only the coordinator or the owner skips a step, which is how a worker's question about a step is
answered, and only they confirm a step that waits for confirmation.

A step may carry a notify tag and a check; both travel with the step as its metadata in the plan run.

* ``notify`` (``start``, ``done`` or ``both``) tells the coordinator when the step becomes the step in hand, when it is
  finished, or both. The first step's start is the hand-over itself, so it is never announced, and neither is a step
  that becomes the step in hand by being added.
* ``check`` says what finishes the step besides the worker's word. ``review``: the worker's ``tasks_step_done`` only
  hands the step in, and it is finished when the coordinator or the owner confirms it (``tasks_step_confirm``, or their
  own ``tasks_step_done``); sent back with feedback, the worker hands it in again. ``signoff``, ``task`` and ``file``
  are looked up when the step is marked done, for every caller and before the plan run is touched: the owner has
  responded to the Signoff (it is approved, applied or answered; what the answer was is not read), another Task is
  done, the file exists in the caller's own agent folder or the Task's project folder and is not empty. The check
  names its target, or the caller names it as ``evidence``. A check that does not hold refuses the call and records
  nothing; skipping stays the coordinator's way out.

While a plan holds an unfinished step with a check, only the coordinator or the owner replaces or removes it
(``TaskPlans.guard``, asked by every Task write that would change the stored plan).

What a step action changed is told through the Task's event writer, inside the write transaction that saves the
receipt: see ``qorqut_operations.tasks.events.STEP_EVENTS``. What to tell is read from the events the engine operation
itself recorded in the plan run, which the engine answers a repeated operation with too, and each event written carries
the operation as its marker (``plan_notice``). So an action whose write transaction failed is still told when it is
repeated, and an action that was told is never told twice. The acting principal is never told.

The engine takes the instruction store's lock for every change. That lock must never be waited for inside the
database's write transaction, so the change is made in ``external``: after authorization, outside any transaction,
before the short write transaction that saves the receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

from qorqut_agentic.errors import AgenticError, StateConflictError
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import IDENTITY, TEXT, PlanBuilder, ref, schema, timestamp
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.tasks.events import announce_step, settle_submissions, step_announced
from qorqut_operations.tasks.lookup import LOCATED
from qorqut_operations.tasks.schema import CLOSED_STATES
from qorqut_operations.tasks.scope import task_context, task_namespace
from qorqut_operations.tasks.types import (CHECK_SCHEMA, CHECK_TARGETS, NOTIFY_SCHEMA, PLAN_STEP_LIMIT, PLAN_TEXT_LIMIT,
                                           TARGET_LIMITS, attributes, normalize_step, step_tags, step_text)

from qorqut.agents.attention import task_workers
from qorqut.composition.locations import agent_workspace_of, project_root, relative_to_operations
from qorqut.file_viewer.files import open_regular
from qorqut.file_viewer.paths import normalize_path
from qorqut.management.identity import configured_ordubashy, is_active_installation_owner

PLAN_OPERATIONS = ('tasks_step_done', 'tasks_step_add', 'tasks_step_skip', 'tasks_step_confirm')
# The coordinator's or the owner's decisions; a worker asks for them and goes on with its next Task meanwhile.
LEAD_OPERATIONS = {
    'tasks_step_skip': "Skipping a step is the coordinator's decision. Ask him with agents_message_send, naming the Task "
                       'and the step, and go on with your next Task meanwhile',
    'tasks_step_confirm': "Confirming a step is the coordinator's decision. He was told when the step was handed in; go on "
                          'with your next Task meanwhile',
}
# The plan run is started by the host on the Task's behalf; each step records the agent or person who acted on it.
HOST_ACTOR = {'actor_id': 'task-plans', 'actor_kind': 'host'}
FINISHED = ('completed', 'skipped')
AWAITING, REJECTED = 'awaiting_review', 'rejected'
# The Signoff states that finish a ``signoff`` step: the owner responded to it. A proposal was approved (``approved``,
# and ``applied`` once it was carried out) or a request was answered (``answered``); what the answer says is not read.
# Pending, rejected, failed, conflicted and cancelled do not, and neither does a replaced Signoff or a side chat.
SIGNOFF_DECIDED = ('approved', 'applied', 'answered')
OUTCOME_LIMIT = 256
TITLE_CLIP, TEXT_CLIP, NOTE_CLIP = 60, 100, 150

SCOPE = {'scope': {'enum': ['project', 'global']}}
STEP = {'type': 'string', 'minLength': 1, 'maxLength': 40,
        'description': "The step's id as the Task's plan_steps lists it, such as s2."}
STEP_TEXT = {'type': 'string', 'minLength': 1, 'maxLength': PLAN_TEXT_LIMIT}
EVIDENCE = {'type': 'object', 'additionalProperties': False,
            'properties': {name: {'type': 'string', 'minLength': 1, 'maxLength': limit} for name, limit in TARGET_LIMITS.items()},
            'description': "What shows the step is finished, when the step has a check that does not name it: signoff_id "
                           "for a signoff check, task_id for a task check, path for a file check."}
DESCRIPTIONS = {
    'tasks_step_done': (
        "Mark one step of a Task's plan as done, with an optional short note of what came of it. Steps are done in "
        "order. The answer lists the plan with every step's state and names the next step. A step whose check is review "
        "is only handed in: the coordinator is told, the step waits for his confirmation, and you go on with your next "
        "Task meanwhile. A step whose check is signoff, task or file is finished only when the owner has responded to "
        "that Signoff (approved or answered it; a rejected or cancelled one never passes), that other Task is done, or "
        "that file exists in your own agent folder or the Task's project folder and is not empty; name it in evidence "
        "when the step does not. "
        "The coordinator is told about a step tagged notify. When the last step is finished, verify the result and "
        "close the Task with tasks_complete."),
    'tasks_step_add': (
        "Add a step to a Task's plan when the work turns out to need one. It goes right after the step named in after, "
        "or at the end of the plan when after is left out. A step cannot be placed before work that has already "
        "started. notify and check mean what they mean in a plan handed over with tasks_delegate."),
    'tasks_step_skip': (
        "Close one unfinished step of a Task's plan without doing it, with the reason. This is the coordinator's or the "
        "owner's decision: a worker asks the coordinator and goes on with its next Task meanwhile. The worker is told "
        "when the step skipped was one it had handed in for confirmation."),
    'tasks_step_confirm': (
        "Decide on a step of a Task's plan that a worker handed in for confirmation (plan_steps shows it with "
        "awaiting_review). approved true finishes the step; approved false sends it back with feedback saying what is "
        "missing, and the worker hands it in again. The worker is told either way. This is the coordinator's or the "
        "owner's decision."),
}
TITLES = {'tasks_step_done': 'Mark plan step done', 'tasks_step_add': 'Add plan step', 'tasks_step_skip': 'Skip plan step',
          'tasks_step_confirm': 'Confirm plan step'}


def plan_key(task):
    """The engine operation ID a Task's plan runs under: one per Task and plan."""
    # Sorted keys keep a tagged step's key independent of how its fields were ordered; a plan of plain texts has none.
    material = json.dumps([task.get('project_id'), task['id'], task['created_at'], attributes(task)['plan']],
                          ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return 'plan-' + hashlib.sha256(material.encode()).hexdigest()[:48]


def plan_items(plan):
    """The Task's plan as the engine takes it: each step's text, with its tags as the step's metadata when it has any."""
    return [{'instructions': step_text(step), 'meta': step_tags(step)} if step_tags(step) else step_text(step) for step in plan]


def tagged(view, tags):
    """Add a step's notify tag and the kind of its check to its view, when it carries them."""
    if tags.get('notify'):
        view['notify'] = tags['notify']
    if tags.get('check'):
        view['check'] = tags['check']['kind']
    return view


def rejection(step):
    """The feedback a step was last sent back with."""
    return next((attempt.get('feedback') or '' for attempt in reversed(step.get('attempts', ()))
                 if attempt['status'] == REJECTED), '')


def step_views(plan, run):
    """One entry per step, in order: its id, text and state, the note it was closed with, and which step is current.

    A step also shows its ``notify`` tag and the kind of its ``check`` when it has them, ``awaiting_review`` while it
    waits for confirmation, and ``feedback`` while it stands sent back."""
    if run is None:
        steps = [tagged({'id': f's{number}', 'text': step_text(step), 'state': 'pending'}, step_tags(step))
                 for number, step in enumerate(plan, 1)]
    else:
        steps = []
        for step in run['steps']:
            view = {'id': step['id'], 'text': step['instructions'], 'state': step['state']}
            if step.get('completion_note'):
                view['note'] = step['completion_note']
            if step.get('added'):
                view['added'] = True
            tagged(view, step.get('meta') or {})
            if step['state'] not in FINISHED and step.get('verification') == AWAITING:
                view['awaiting_review'] = True
            elif step['state'] not in FINISHED and step.get('verification') == REJECTED:
                view['feedback'] = rejection(step)
            steps.append(view)
    current = next((step for step in steps if step['state'] not in FINISHED), None)
    if current is not None:
        current['current'] = True
    return steps


def handed_in(step):
    """The note a step of a plan run was last handed in with."""
    return next((attempt.get('note') or '' for attempt in reversed(step.get('attempts', ())) if attempt['status'] == 'submitted'), '')


def standing(plan, run):
    """Where a plan stands, for a reminder: how many steps it has and which one is in hand.

    ``number`` is the place of the step in hand, counted from one, or ``None`` once every step is finished. With it come
    the step's ``id`` and ``text``; ``awaiting_review`` and the ``note`` it was handed in with while it waits for
    confirmation; ``feedback`` while it stands sent back."""
    steps = step_views(plan, run)
    found = {'total': len(steps), 'number': None}
    for number, step in enumerate(steps, 1):
        if not step.get('current'):
            continue
        found.update(number=number, id=step['id'], text=step['text'])
        if step.get('awaiting_review'):
            found.update(awaiting_review=True, note=handed_in(next(item for item in run['steps'] if item['id'] == step['id'])))
        elif 'feedback' in step:
            found['feedback'] = step['feedback']
    return found


def position(found):
    """``step 3 of 5``, or ``all 5 steps finished``."""
    return f"step {found['number']} of {found['total']}" if found['number'] else f"all {found['total']} steps finished"


def clip(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def sentence(lead, text, *more):
    """``lead: text.`` followed by ``label: value.`` for every value given, and by each label that stands alone."""
    parts = [f"{lead}: {clip(text, TEXT_CLIP).rstrip('.')}."]
    for label, value in more:
        if value is None:
            parts.append(label)
        elif value:
            parts.append(f"{label}: {clip(value, NOTE_CLIP).rstrip('.')}.")
    return ' '.join(parts)


def notices(title, run, operation_id):
    """What the engine operation ``operation_id`` did that somebody is told about: ``{to, kinds, summary, step}`` entries,
    ``to`` the coordinator or the workers.

    Read from the events the operation recorded in the plan run and from ``run``, the run as the operation left it. The
    engine answers a repeated operation with the same run, so a repeated action finds the same notices; whether each was
    already told is the announcer's question."""
    events = {event['type']: event['payload'] for event in run.get('events', ())
              if (event.get('payload') or {}).get('operation_id') == operation_id}
    changed = next((events[kind] for kind in ('step_submitted', 'step_reviewed', 'step_completed', 'step_skipped')
                    if kind in events), None)
    if changed is None:
        return []
    steps = run['steps']
    index = next(number for number, step in enumerate(steps) if step['id'] == changed['step_id'])
    new = steps[index]
    named = lambda step, said: f"Step {step['id']} of \"{clip(title, TITLE_CLIP)}\" {said}"
    lead, workers = [], []
    if 'step_submitted' in events:
        lead.append(('plan_step_submitted', sentence(named(new, 'waits for your confirmation'), new['instructions'],
                                                     ("Worker's note", handed_in(new)),
                                                     ('Confirm it or send it back with tasks_step_confirm.', None))))
    if 'step_reviewed' in events:
        if events['step_reviewed']['passed']:
            workers.append(('plan_step_approved', sentence(named(new, 'was confirmed'), new['instructions'],
                                                           ('Go on with the plan.', None))))
        else:
            workers.append(('plan_step_returned', sentence(named(new, 'was sent back'), new['instructions'],
                                                           ('Feedback', rejection(new)),
                                                           ('Fix it and mark the step done again.', None))))
    if events.get('step_skipped', {}).get('withdrawn'):
        # Only a step that waited for confirmation: its worker was told it would hear of the decision.
        workers.append(('plan_step_skipped', sentence(named(new, 'was skipped and no longer waits for confirmation'),
                                                      new['instructions'], ('Reason', new.get('completion_note') or ''),
                                                      ('Go on with the plan.', None))))
    if 'step_completed' in events and (new.get('meta') or {}).get('notify') in ('done', 'both'):
        lead.append(('plan_step_done', sentence(named(new, 'is done'), new['instructions'],
                                                ('Note', new.get('completion_note') or ''))))
    # The step in hand changed only when the step this operation finished was the one in hand: every step before it is
    # finished. Skipping a later step leaves the step in hand where it was, so it is not announced again.
    if (('step_completed' in events or 'step_skipped' in events)
            and all(step['state'] in FINISHED for step in steps[:index])):
        following = next((step for step in steps if step['state'] not in FINISHED), None)
        if following is not None and (following.get('meta') or {}).get('notify') in ('start', 'both'):
            said = f"Step {following['id']} is now in hand" if lead else named(following, 'is now in hand')
            lead.append(('plan_step_started', sentence(said, following['instructions'])))
    return [{'to': to, 'kinds': [kind for kind, _ in found], 'summary': ' '.join(text for _, text in found), 'step': new['id']}
            for to, found in (('coordinator', lead), ('workers', workers)) if found]


def settled(run, operation_id):
    """The engine operations that handed in a step which ``operation_id`` then decided or skipped: their announcements
    no longer apply."""
    found = []
    for event in run.get('events', ()):
        payload = event.get('payload') or {}
        if payload.get('operation_id') != operation_id:
            continue
        if event['type'] == 'step_reviewed' and payload.get('submission_operation_id'):
            found.append(payload['submission_operation_id'])
        elif event['type'] == 'step_skipped' and payload.get('withdrawn'):
            step = next(step for step in run['steps'] if step['id'] == payload['step_id'])
            found.extend(attempt['operation_id'] for attempt in step.get('attempts', ()) if attempt['status'] == 'withdrawn')
    return found


def checked(plan, run):
    """Whether the plan holds an unfinished step that carries a check: in the plan run when it was started, else as stored."""
    if run is not None:
        return any(step['state'] not in FINISHED and (step.get('meta') or {}).get('check') for step in run['steps'])
    return any(step_tags(step).get('check') for step in plan)


class TaskPlans:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application
        self.tasks = runtime.domains.tasks
        self.outcomes, self.lock = {}, threading.Lock()

    def register(self):
        common = dict(supports_signoff=False, scope_kinds=frozenset({'project', 'global'}),
                      global_capability='read_global_context', resolve_namespace=task_namespace,
                      locate_scope=self.tasks.lookup, external=self.external, apply=self.apply)
        shared = {**SCOPE, 'task_id': IDENTITY, 'rationale': TEXT}
        inputs = {
            'tasks_step_done': ({'step_id': STEP, 'note': {'type': 'string', 'maxLength': 1000,
                                                           'description': 'What came of the step, in a sentence.'},
                                 'evidence': EVIDENCE},
                                ('task_id', 'step_id')),
            'tasks_step_add': ({'text': {**STEP_TEXT, 'description': 'The instruction of the new step.'},
                                'after': {**STEP, 'description': 'The id of the step the new one follows; '
                                                                 'left out, the step goes at the end.'},
                                'notify': NOTIFY_SCHEMA, 'check': CHECK_SCHEMA},
                               ('task_id', 'text')),
            'tasks_step_skip': ({'step_id': STEP, 'reason': {**STEP_TEXT, 'description': 'Why the step is not needed.'}},
                                ('task_id', 'step_id', 'reason')),
            'tasks_step_confirm': ({'step_id': STEP,
                                    'approved': {'type': 'boolean', 'description': 'true finishes the step; false sends it '
                                                                                   'back to the worker.'},
                                    'feedback': {**STEP_TEXT, 'description': 'What is missing, for the worker. Required '
                                                                             'when approved is false.'}},
                                   ('task_id', 'step_id', 'approved')),
        }
        for name, (properties, required) in inputs.items():
            self.app.registry.register(OperationDescriptor(
                name, schema({**shared, **properties}, required), description=DESCRIPTIONS[name] + LOCATED,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, uow), **common))
        # ``tasks_get`` shows where the plan stands through this reading.
        self.tasks.plan_steps = self.steps
        # ``tasks_delegate`` copies a named template's steps into the Task through this one.
        self.tasks.plan_template = self.template
        # Every Task write that would change the stored plan asks this one first.
        self.tasks.plan_guard = self.guard
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

    def standing(self, task):
        """Where the Task's plan stands (see ``standing``), for a reminder; ``None`` for a Task without a plan and when
        the workflow store cannot be read, so a reminder never shows a plan it could not read as not started.

        ``task`` needs only what names the plan run: the Task's id, project, creation time and stored fields. This is
        one read of the engine's database on a read-only connection. It never takes the instruction store's lock."""
        plan = attributes(task)['plan']
        if not plan:
            return None
        try:
            found = self.workflows().plan_run(plan_key(task))
        except (AgenticError, DomainError, OSError, sqlite3.Error):
            return None
        return standing(plan, found['run'] if found else None)

    def template(self, ref):
        """The step instructions of the saved workflow ``ref``, when a Task's plan can carry it.

        A template is a workflow entry of the instruction store, saved through the owner's approval like any other
        instruction. It is only read here: the hand-over copies its steps into the Task, so the Task's plan no longer
        depends on the template and is worked, added to and skipped like a plan written by hand.
        """
        try:
            steps = self.workflows().plan_template(ref)['steps']
        except AgenticError as error:
            if error.code == 'lock_timeout':
                raise DomainError('operation_unavailable', 'The template store is busy; call the tool again') from None
            if error.code == 'not_found':
                raise DomainError('not_found', f'There is no template {ref}. Templates are the saved workflows of the '
                                               'instruction store; catalog.list in your agentic helper shows them') from None
            raise DomainError('validation_error', f'{ref} cannot be handed over as a plan: {error.message}') from None
        if len(steps) > PLAN_STEP_LIMIT or any(len(step) > PLAN_TEXT_LIMIT for step in steps):
            raise DomainError('validation_error', f'{ref} cannot be handed over as a plan: a plan holds at most '
                                                  f'{PLAN_STEP_LIMIT} steps of at most {PLAN_TEXT_LIMIT} characters each')
        return steps

    # -- authority -------------------------------------------------------------------------------------------------

    @staticmethod
    def check(name, ctx, task, uow):
        """Refuse a caller who may not take this action on the Task's plan; returns whether it is the coordinator or the owner."""
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
        if name in LEAD_OPERATIONS and not lead:
            raise DomainError('forbidden', LEAD_OPERATIONS[name])
        return lead

    def guard(self, ctx, task, uow):
        """Refuse a caller who may not replace or remove the plan ``task`` holds: while it has an unfinished step that
        carries a check, that is the coordinator's or the owner's decision. Otherwise a worker could hand itself a plan
        without the check. A plan run that cannot be read is judged by the stored plan."""
        plan = attributes(task)['plan']
        if not plan or not checked(plan, self.run(task)):
            return
        if ctx.principal_kind == 'agent':
            lead = ctx.principal_id == configured_ordubashy(uow)
        else:
            lead = is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind)
        if not lead:
            raise DomainError('forbidden', "This Task's plan has an unfinished step that is finished by a check, so only the "
                                           'coordinator or the owner replaces or removes the plan. Ask the coordinator '
                                           'with agents_message_send, naming the Task and what should change')

    @staticmethod
    def said(name, arguments):
        """Refuse what the input schema cannot: a step sent back without feedback, a check of the wrong shape."""
        if name == 'tasks_step_confirm' and not arguments['approved'] and not (arguments.get('feedback') or '').strip():
            raise DomainError('validation_error', 'Send a step back with feedback: say what is missing so the worker can fix it')
        if name == 'tasks_step_add':
            normalize_step({'text': arguments['text'], **{field: arguments[field] for field in ('notify', 'check')
                                                          if arguments.get(field) is not None}})

    # -- checks ----------------------------------------------------------------------------------------------------

    def verify(self, ctx, arguments, step, task):
        """Look up what a ``signoff``, ``task`` or ``file`` step is finished by; returns the check and its target.

        Refuses, in words the caller can act on, when the target is not named or the check does not hold. A refusal says
        nothing of a target the caller could not read with its ordinary tools."""
        check = step['meta']['check']
        kind, field = check['kind'], CHECK_TARGETS[check['kind']]
        named = check.get(field)
        target = named or (arguments.get('evidence') or {}).get(field)
        this = f"Step {step['id']}"
        if not target:
            needed = {'signoff': 'the Signoff the owner responds to', 'task': 'the Task that must be done first',
                      'file': 'the file it produces'}[kind]
            raise DomainError('validation_error', f"{this} is finished by a check: name {needed} as evidence.{field}")
        with self.app.database.read() as uow:
            scoped = task_context(ctx, arguments, uow)
            if kind == 'file':
                return {'check': kind, field: self.verify_file(this, target, self.file_scopes(scoped, task, uow))}
            if kind == 'signoff':
                self.verify_signoff(scoped, uow, this, target, bool(named))
            elif target == task['id']:
                raise DomainError('validation_error', f"{this} is finished once another Task is done; it cannot name its own "
                                                      'Task. Name the Task that must be done first, or ask the coordinator')
            else:
                try:
                    # Read as ``tasks_get`` would let this caller read it, so the refusal tells nothing more.
                    self.app.authorize(scoped, self.app.registry.get('tasks_get'), (), uow,
                                       namespace=task_namespace(scoped, arguments))
                    other = self.tasks.require(scoped, uow, target)
                except DomainError as error:
                    if error.code not in ('not_found', 'forbidden', 'policy_denied'):
                        raise
                    raise DomainError('validation_error', f"{this} is finished by the Task {target}, and there is no such Task "
                                                          'in this scope. Check the id, or ask the coordinator') from None
                if other['status'] != 'done':
                    raise DomainError('validation_error', f"{this} is finished once the Task {target} is done; it is "
                                                          f"{other['status']}. Go on with your next Task meanwhile")
        return {'check': kind, field: target}

    def verify_signoff(self, scoped, uow, this, target, named):
        """Hold a ``signoff`` step to its Signoff: one the caller may read as ``signoffs_get`` would let it, that decides
        something, was not replaced, and that the owner has responded to. What the owner answered is not read."""
        signoffs = getattr(self.app, 'signoffs', None)
        if not hasattr(signoffs, '_row'):
            raise DomainError('operation_unavailable', 'Signoffs cannot be read here, so this step cannot be checked')
        try:
            reader = signoffs._context(scoped, uow)
            self.app.authorize(reader, self.app.registry.get('signoffs_get'), (), uow,
                               namespace='project:' + reader.project_id if reader.project_id else 'global')
            row = signoffs._row(reader, target, uow)
        except DomainError as error:
            if error.code not in ('not_found', 'forbidden', 'policy_denied'):
                raise
            # One answer for a Signoff that does not exist, one in another scope and one the caller may not read.
            raise DomainError('validation_error', f"{this} is finished by the Signoff {target}, and you can read no such "
                                                  'Signoff in this scope. Check the id, or ask the coordinator') from None
        # A step whose plan names the Signoff is released only by the coordinator; one named as evidence can name another.
        release = ('Only the coordinator releases the step: by skipping it, or with a plan that names another Signoff'
                   if named else 'Name another Signoff as evidence, or ask the coordinator to skip the step')
        if row['category'] == 'side_chat':
            raise DomainError('validation_error', f"{this} is finished once the owner has responded to a Signoff; {target} is "
                                                  f"a side chat, which decides nothing. {release}")
        if row['superseded_by_id']:
            raise DomainError('validation_error', f"{this} is finished once the owner has responded to the Signoff {target}, "
                                                  f"and that one was replaced by a newer one. {release}")
        if row['state'] == 'pending':
            raise DomainError('validation_error', f"{this} is finished once the owner has responded to the Signoff {target}; "
                                                  'it is pending. Go on with your next Task meanwhile')
        if row['state'] not in SIGNOFF_DECIDED:
            raise DomainError('validation_error', f"{this} is finished once the owner has approved or answered the Signoff "
                                                  f"{target}; it is {row['state']}, which never passes. {release}")

    def file_scopes(self, ctx, task, uow):
        """The folders a caller may name a ``file`` check's file in, operations-root-relative; ``None`` for the owner.

        An agent names a file in its own agent folder or, for a Task of a project, in that project's folder. The owner
        is held to the file viewer's rules alone, which already show the owner the whole operations root."""
        if ctx.principal_kind != 'agent':
            return None
        paths = getattr(self.runtime, 'paths', None)
        if paths is None:
            raise DomainError('operation_unavailable', 'Files cannot be read here, so this step cannot be checked')
        scopes = []
        try:
            if uow.connection.execute('SELECT 1 FROM agent_profiles WHERE agent_id=?', (ctx.principal_id,)).fetchone() is not None:
                scopes.append(relative_to_operations(paths, agent_workspace_of(paths, ctx.principal_id)))
            if task.get('project_id'):
                project = uow.connection.execute('SELECT slug FROM projects WHERE id=?', (task['project_id'],)).fetchone()
                if project is not None:
                    scopes.append(relative_to_operations(paths, project_root(paths, project)))
        except DomainError:
            raise DomainError('operation_unavailable', 'Files cannot be read here, so this step cannot be checked') from None
        return scopes

    def verify_file(self, this, target, scopes):
        """The operations-root-relative path of the existing, non-empty regular file ``target`` names.

        The path is held to the owner's file viewer rules: beneath the operations root, no ``..``, no symbolic link.
        For an agent it must also lie in one of ``scopes``, which is decided from the path alone, before the file
        system is looked at: a path elsewhere is refused the same way whether or not a file is there."""
        paths = getattr(self.runtime, 'paths', None)
        if paths is None:
            raise DomainError('operation_unavailable', 'Files cannot be read here, so this step cannot be checked')
        root = paths.operations
        shown = clip(target, TEXT_CLIP)
        try:
            relative = normalize_path(target, tuple(dict.fromkeys((str(root), str(root.resolve())))))
        except DomainError as error:
            raise DomainError('validation_error', f"{this} is finished by the file {shown}: {error.message}. "
                                                  'Name it by its path under the operations root') from None
        if scopes is not None and not any(relative.startswith(scope + '/') for scope in scopes):
            raise DomainError('validation_error', f"{this} is finished by a file in your own agent folder or the Task's "
                                                  f"project folder; {shown} is not in either. Put the file there, or "
                                                  'ask the coordinator')
        try:
            opened = open_regular(root, relative)
        except DomainError as error:
            raise DomainError('validation_error', f"{this} is finished by the file {shown}: {error.message}") from None
        except OSError:
            raise DomainError('validation_error', f"{this} is finished by the file {shown}, which cannot be read") from None
        size = opened.size
        opened.close()
        if not size:
            raise DomainError('validation_error', f"{this} is finished by the file {relative}, which is empty")
        return relative

    # -- the three stages ------------------------------------------------------------------------------------------

    def prepare(self, name, ctx, arguments, uow):
        ctx = task_context(ctx, arguments, uow)
        task = self.tasks.require(ctx, uow, arguments['task_id'])
        self.check(name, ctx, task, uow)
        self.said(name, arguments)
        builder = PlanBuilder(ctx, name, arguments, TITLES[name], clock=self.tasks.clock, namespace=task_namespace(ctx, arguments),
                              affected_scope={'kind': 'project', 'project_id': ctx.project_id} if ctx.project_id else {'kind': 'global'})
        # Pinned so the step is recorded against the Task and plan the caller saw.
        builder.expect(ref('tasks', task['id'], ctx.project_id), task)
        builder.effects.append("Records progress on the Task's plan. The Task record is not changed. The coordinator is told "
                               "when the step is tagged for it or waits for his confirmation, and the worker is told of the "
                               "decision on a step it handed in, also when that step is skipped.")
        return builder.build()

    def external(self, ctx, plan):
        name, arguments = plan.operation_name, plain(plan.arguments)
        with self.app.database.read() as uow:
            scoped = task_context(ctx, arguments, uow)
            task = self.tasks.require(scoped, uow, arguments['task_id'])
            lead = self.check(name, scoped, task, uow)
        self.said(name, arguments)
        workflows, key = self.workflows(), plan_key(task)
        actor = {'actor_id': ctx.principal_id, 'actor_kind': 'agent' if ctx.principal_kind == 'agent' else 'owner'}
        try:
            origin = f"task:{task.get('project_id') or 'global'}:{task['id']}"
            run_id = workflows.start_plan(plan_items(attributes(task)['plan']), HOST_ACTOR, origin, key)['run']['run_id']
            # ``start_plan`` answers a started plan with the run as it began, so where it stands now is read apart.
            before = workflows.plan_run(key)['run']
        except AgenticError as error:
            raise self.refusal(error, name, arguments, self.run(task)) from None
        step = next((item for item in before['steps'] if item['id'] == arguments.get('step_id')), None)
        kind = ((step or {}).get('meta') or {}).get('check', {}).get('kind')
        rejected = sum(attempt['status'] == REJECTED for attempt in (step or {}).get('attempts', ()))
        waiting = step is not None and step['verification'] == AWAITING
        finished = step is not None and step['state'] in FINISHED
        output = None
        if name == 'tasks_step_done' and kind == 'review':
            # A worker only ever hands a review step in, also after it was sent back; the coordinator or the owner finishes
            # it, which for a step that waits is its approval. A step that was finished by its approval is asked of the
            # engine as that approval again, so repeating the action finds the operation that made it.
            approving = waiting or (step['state'] == 'completed' and step['verification'] == 'reviewed')
            action, output = ('approve' if approving else 'complete') if lead else 'submit', {'check': 'review'}
        elif name == 'tasks_step_done':
            action = 'complete'
            if kind is not None and finished:
                # Nothing is finished anew: the engine answers the action that finished the step, and refuses any other.
                output = step.get('output')
            elif kind is not None:
                # The check is looked up before the plan run is touched, whatever the step's state: a step that cannot be
                # marked now is refused here, so no later change of the run lets it through unchecked.
                if not step['ready']:
                    raise self.refusal(StateConflictError(f"step {step['id']!r} is not ready"), name, arguments, before)
                output = self.verify(ctx, arguments, step, task)
        else:
            action = {'tasks_step_add': 'add', 'tasks_step_skip': 'skip', 'tasks_step_confirm': 'review'}[name]
        said = {field: arguments.get(field) for field in ('step_id', 'note', 'text', 'after', 'reason')}
        # What steps could not carry before is named only when given, so an action said in the old words keeps its key.
        said.update({field: arguments[field] for field in ('evidence', 'notify', 'check', 'approved', 'feedback')
                     if arguments.get(field) is not None})
        # A step that was sent back is handed in again, and decided again, in the same words: each round is its own change.
        rounds = rejected if action in ('submit', 'complete') else len(step['attempts']) if step and action in ('approve', 'review') else 0
        if rounds:
            said['round'] = rounds
        # A repeated request reaches this stage again before its saved receipt is found, so the engine key is made from
        # what was said, not from the request: saying the same thing twice changes the plan once.
        operation_id ='step-' + hashlib.sha256(json.dumps([name, key, said], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:48]
        try:
            note, step_id = arguments.get('note') or '', arguments.get('step_id')
            if action == 'complete':
                run = workflows.complete_step(run_id, step_id, actor, operation_id, note, output)['run']
            elif action == 'submit':
                run = workflows.submit_step(run_id, step_id, actor, operation_id, note, output)['run']
            elif action == 'approve':
                run = workflows.review_step(run_id, step_id, actor, operation_id, True, note)['run']
            elif action == 'review':
                run = workflows.review_step(run_id, step_id, actor, operation_id, arguments['approved'],
                                            arguments.get('feedback') or '')['run']
            elif action == 'add':
                tags = {field: arguments[field] for field in ('notify', 'check') if arguments.get(field) is not None}
                meta = step_tags(normalize_step({'text': arguments['text'], **tags})) if tags else None
                run = workflows.add_step(run_id, arguments['text'], actor, operation_id, after=arguments.get('after'),
                                         meta=meta or None)['run']
            else:
                run = workflows.skip_step(run_id, step_id, actor, operation_id, arguments['reason'])['run']
        except AgenticError as error:
            raise self.refusal(error, name, arguments, self.run(task)) from None
        # ``run`` is the run as this operation left it, also when the engine answered a repeated operation.
        told = notices(task['title'], run, operation_id)
        if finished:
            # A hand-in repeated after the step was decided: nothing waits for the coordinator any more.
            told = [notice for notice in told if notice['kinds'] != ['plan_step_submitted']]
        # Two requests that say the same thing share ``plan.payload_hash``; each is handed its own outcome, by the thread
        # that runs its ``external`` and then its ``apply``.
        with self.lock:
            while len(self.outcomes) >= OUTCOME_LIMIT:
                self.outcomes.pop(next(iter(self.outcomes)))
            self.outcomes[threading.get_ident(), plan.payload_hash] = (run, operation_id, told)

    def apply(self, ctx, plan, uow):
        with self.lock:
            run, operation_id, told = self.outcomes.pop((threading.get_ident(), plan.payload_hash), (None, None, ()))
        if run is None:
            raise DomainError('operation_unavailable', 'The plan change was not carried out; call the tool again')
        arguments = plain(plan.arguments)
        scoped = task_context(ctx, arguments, uow)
        task = self.tasks.require(scoped, uow, arguments['task_id'])
        self.announce(ctx, scoped, task, operation_id, told, uow)
        # A step that was decided or skipped no longer waits: what told the coordinator of it is settled.
        settle_submissions(uow, scoped.project_id, task['id'], settled(run, operation_id), at=timestamp(self.tasks.clock))
        steps = step_views((), run)
        following = next((step for step in steps if step.get('current')), None)
        waiting = following is not None and following.get('awaiting_review')
        result = {'task_id': plan.arguments['task_id'], 'plan_steps': steps,
                  'next_step': {'id': following['id'], 'text': following['text']} if following and not waiting else None,
                  'plan_finished': following is None}
        if following is None:
            result['hint'] = 'Every step is finished. Verify the result, then close the Task with tasks_complete.'
        elif waiting:
            # Nothing of this plan can be worked until the step is decided, so no next step is named. That the coordinator
            # was told is said only when the event that told him exists.
            handed = next((attempt['operation_id'] for item in run['steps'] if item['id'] == following['id']
                           for attempt in reversed(item.get('attempts', ())) if attempt['status'] == 'submitted'), None)
            heard = handed is not None and step_announced(uow, scoped.project_id, task['id'], f'{handed}:coordinator')
            result['hint'] = (f"Step {following['id']} waits for the coordinator's confirmation, "
                              + ('and the coordinator was told. ' if heard else
                                 'and no coordinator could be told: tell the coordinator or the owner yourself. ')
                              + 'The worker goes on with its next Task meanwhile and is told when the step is confirmed, '
                                'sent back or skipped.')
        elif following.get('feedback'):
            result['hint'] = (f"Step {following['id']} was sent back: {following['feedback']} "
                              'The worker fixes it and marks the step done again.')
        return result

    def announce(self, ctx, scoped, task, operation_id, told, uow):
        """Tell the coordinator or the Task's workers what the operation changed, once; the acting principal is never told.

        Each event carries ``plan_notice``, the engine operation and the side told. A notice whose event exists was
        already told, by this request or by an earlier one that said the same thing, and is not written again; one
        whose write transaction failed left no event and is written now. The Task is only read: a step action does
        not change its record or its revision."""
        for notice in told:
            marker = f"{operation_id}:{notice['to']}"
            if step_announced(uow, scoped.project_id, task['id'], marker):
                continue
            recipients = [configured_ordubashy(uow)] if notice['to'] == 'coordinator' else sorted(task_workers(task))
            announce_step(uow, scoped.project_id, task, ctx.principal_id, notice['kinds'], notice['summary'],
                          [identity for identity in recipients if identity],
                          {'plan_notice': marker, 'plan_operation': operation_id, 'plan_step': notice['step']})

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
            if name == 'tasks_step_confirm' and step is not None and step['verification'] != AWAITING:
                return DomainError('validation_error', f"Step {step['id']} does not wait for confirmation; a step is "
                                                       'confirmed after its worker handed it in with tasks_step_done')
            if name == 'tasks_step_done' and step is not None and step['verification'] == AWAITING:
                return DomainError('validation_error', f"Step {step['id']} already waits for the coordinator's confirmation. "
                                                       'Go on with your next Task meanwhile')
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


__all__ = ['PLAN_OPERATIONS', 'SIGNOFF_DECIDED', 'TaskPlans', 'plan_key', 'position', 'register_task_plans', 'standing',
           'step_views']
