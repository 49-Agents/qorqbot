"""A worker's done closure is recorded as an Action by the service, once, and the coordinator is told whether a done
Task has its Action. The real Task engine, the closure hook and the MCP transport's single-call path."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

import qorqut_agentic
from qorqut.composition.assembly import host_assembly
from qorqut.domains.tasks import closure_actions
from qorqut.domains.tasks.closure import TaskClosure
from qorqut.domains.tasks.closure_actions import ClosureActions, closure_identity
from qorqut.service.changes import ChangeFeed
from qorqut.transport import mcp
from qorqut_agentic.behavior.instructions import from_catalog
from qorqut_context.integration.queue.queue import admit_action_revision
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.roles import GLOBAL_WORKER_ROLE, LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations

from test_task_change_records import ports

STAMP = '2026-11-10T12:00:00Z'
CATALOG = Path(qorqut_agentic.__file__).parent / 'resources/defaults/operations/runtime-instructions'
RESULT = 'Sent the founding email to all twelve investors.'
LONG = ' '.join(f'word{n}' for n in range(1, 31))


class Harness(unittest.TestCase):
    """An owner, the configured coordinator, a worker and the learning applier over two projects. Every Task event is
    addressed to the coordinator, as the host routes a worker's closure."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.lead, self.worker, self.applier = (str(uuid4()) for _ in range(4))
        self.alpha, self.beta = sorted(str(uuid4()) for _ in range(2))
        agents = ((self.lead, 'Ordubashy', ORDUBASHY_ROLE), (self.worker, 'Scout', GLOBAL_WORKER_ROLE),
                  (self.applier, 'Applier', LEARNING_APPLIER_ROLE))
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner',
                active=1, created_at=STAMP, updated_at=STAMP))
            for identity, name, role in agents:
                uow.repository('principals').insert(dict(id=identity, kind='agent', display_name=name,
                    active=1, created_at=STAMP, updated_at=STAMP))
                uow.repository('agent_profiles', project_id=None).insert(dict(agent_id=identity, principal_id=identity,
                    name=name.lower(), workspace='/tmp/' + name.lower(),
                    status='active', profile_json={'status': 'active', 'role': {'id': role}, 'scope': {
                        'kind': 'ecosystem', 'project_id': None, 'global_capabilities': ['read_global_context']}},
                    created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                owner_principal_id=self.owner, management_coordinator_id=self.lead, created_at=STAMP, updated_at=STAMP))
            for project, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta')):
                uow.repository('projects').insert({'id': project, 'slug': name.lower(), 'name': name,
                                                     'workspace': name.lower(), 'hq_path': name.lower() + '/hq'})
                uow.repository('project_memberships', project_id=project).insert(
                    {'principal_id': self.owner, 'active': 1, 'is_owner': 1})
        projects = frozenset({self.alpha, self.beta})
        self.contexts = {self.owner: InvocationContext(Principal(self.owner, 'human'), allowed_project_ids=projects,
                                                       global_capabilities={'read_global_context'}, source='dashboard')}
        for identity, _, _ in agents:
            self.contexts[identity] = InvocationContext(Principal(identity, 'agent'), allowed_project_ids=projects,
                global_capabilities={'read_global_context'}, profile_revision=1, source='mcp')
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        routing = NS(routes=lambda *a: ((self.lead,), ()), snooze_routes=lambda *a: ())
        self.tasks = TaskOperations(self.app, items, ports=ports(routing=routing))
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)
        # The closure hook with stand-ins for the boards, Focus and reminders; the recorder and the Context queue's
        # admission of its revision are the real ones.
        self.recorder = ClosureActions(admit_action_revision)
        self.tasks.on_change = TaskClosure(NS(follow_task=noop, clock=lambda: datetime.now(timezone.utc)),
                                           NS(release_task=noop), NS(release_task=noop), self.recorder)

    # -- calls ----------------------------------------------------------------------------------------------------

    def invoke(self, who, operation, arguments, project=None):
        options = {'idempotency_key': str(uuid4()), **({'project_id': project} if project else {})}
        return self.app.invoke(self.contexts[who], operation, {**arguments, **({} if project else {'scope': 'global'})},
                               options)

    def ok(self, result):
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data['task']

    def task(self, project, title='Email the founding investors', who=None, **extra):
        return self.ok(self.invoke(who or self.owner, 'tasks_create', {'title': title, **extra}, project))

    def close(self, who, task, **extra):
        return self.invoke(who, 'tasks_complete', {'task_id': task['id'], **extra}, task['project_id'])

    def update(self, who, task, changes):
        return self.invoke(who, 'tasks_update', {'task_id': task['id'], 'changes': changes}, task['project_id'])

    # -- reads ----------------------------------------------------------------------------------------------------

    def rows(self, sql, *parameters):
        with self.db.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def actions(self):
        return self.rows('SELECT * FROM action_logs ORDER BY created_at, id')

    def plant(self, task, *, performer, project=None, category=None, withdrawn=0, text='Sent the investor email'):
        """An Action somebody recorded for the Task with ``actions_record``, as its stored rows."""
        identity = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('action_logs', project_id=None).insert(dict(
                id=identity, category=category or ('project-operational' if project else 'global-operational'),
                action_text=text, performed_by=performer, recorded_by=self.lead, origin='agent', occurred_at=STAMP,
                refs_json={'task': task['id']}, created_at=STAMP, updated_at=STAMP))
            if project:
                uow.connection.execute('INSERT INTO action_projects(action_id,project_id,created_at) VALUES(?,?,?)',
                                       (identity, project, STAMP))
            if withdrawn:
                uow.connection.execute('UPDATE action_logs SET withdrawn=1 WHERE id=?', (identity,))
        return identity


class ClosureActionTests(Harness):
    def test_a_worker_closure_of_a_project_task_records_one_automatic_action(self):
        task = self.task(self.alpha)
        closed = self.ok(self.close(self.worker, task, result=RESULT))
        self.assertEqual((closed['status'], closed['status_line']), ('done', RESULT))
        [action] = self.actions()
        self.assertEqual(action['id'], closure_identity(self.alpha, task['id']))
        # Stored in Global and linked to the Task's project; the worker is both performer and recorder.
        self.assertIsNone(action['project_id'])
        self.assertEqual((action['origin'], action['category'], action['action_text'], action['withdrawn']),
                         ('automatic', 'project-operational', RESULT, 0))
        self.assertEqual((action['performed_by'], action['recorded_by']), (self.worker, self.worker))
        self.assertEqual(json.loads(action['refs_json']), {'task': task['id']})
        self.assertEqual(action['time_precision'], 'instant')
        self.assertEqual(self.rows('SELECT project_id FROM action_projects WHERE action_id=?', action['id']),
                         [{'project_id': self.alpha}])
        # Its one revision is stored and admitted to the Context queue in the same write.
        [revision] = self.rows('SELECT * FROM action_revisions WHERE action_id=?', action['id'])
        self.assertEqual((revision['revision'], revision['operation'], revision['recorded_by'], revision['performed_by']),
                         (1, 'add', self.worker, self.worker))
        self.assertEqual(json.loads(revision['provenance_json']), {'via': 'task_closure'})
        [queued] = self.rows('SELECT item_id, revision, operation, submitted_by FROM context_inputs')
        self.assertEqual(queued, {'item_id': action['id'], 'revision': 1, 'operation': 'add',
                                  'submitted_by': self.worker})

    def test_a_global_task_records_a_global_operational_action(self):
        task = self.task(None)
        self.ok(self.close(self.worker, task, result=RESULT))
        [action] = self.actions()
        self.assertEqual((action['project_id'], action['category'], action['id']),
                         (None, 'global-operational', closure_identity(None, task['id'])))
        self.assertEqual(json.loads(action['refs_json']), {'task': task['id']})
        self.assertEqual(self.rows('SELECT * FROM action_projects'), [])

    def test_the_text_is_the_result_of_this_write_cut_to_twenty_words_or_the_title(self):
        long = self.task(self.alpha)
        self.ok(self.close(self.worker, long, result=LONG))
        # The full result stays on the Task; the Action keeps the 20 words of generated text.
        self.assertEqual(self.rows('SELECT title FROM tasks WHERE id=?', long['id'])[0]['title'], long['title'])
        self.assertEqual(self.ok(self.invoke(self.owner, 'tasks_get', {'task_id': long['id']}, self.alpha))['status_line'], LONG)
        untold = self.task(self.alpha, title='Renew the domain')
        self.ok(self.close(self.worker, untold))
        # A status line written earlier is not this closure's result: the title names what was done.
        earlier = self.task(self.beta, title='Publish the price list')
        self.ok(self.update(self.worker, earlier, {'status_line': 'Waiting for the last two prices.'}))
        self.ok(self.update(self.worker, earlier, {'status': 'done'}))
        # tasks_update that sets the state and the result in one write is a closure with a result.
        told = self.task(self.beta, title='Book the venue')
        self.ok(self.update(self.worker, told, {'status': 'done', 'status_line': 'Booked the hall for 12 March.'}))
        texts = {json.loads(action['refs_json'])['task']: action['action_text'] for action in self.actions()}
        self.assertEqual(texts, {long['id']: ' '.join(LONG.split()[:19]) + ' …', untold['id']: 'Renew the domain',
                                 earlier['id']: 'Publish the price list', told['id']: 'Booked the hall for 12 March.'})

    def test_a_cancelled_task_records_nothing(self):
        task = self.task(self.alpha)
        self.ok(self.close(self.worker, task, status='cancelled', result='Dropped: merged into the launch Task.'))
        self.assertEqual(self.actions(), [])
        # Nor does moving the already closed Task to done: that is an edit of a closed Task, not a closure.
        self.ok(self.update(self.worker, task, {'status': 'done'}))
        self.assertEqual(self.actions(), [])

    def test_closures_by_the_coordinator_the_applier_and_the_owner_record_nothing(self):
        for who in (self.lead, self.applier, self.owner):
            for project in (self.alpha, None):
                # The owner's words go through the private conversation, so the owner closes without a result.
                self.ok(self.close(who, self.task(project), **({} if who == self.owner else {'result': RESULT})))
        self.assertEqual([row['n'] for row in self.rows("SELECT COUNT(*) AS n FROM tasks WHERE status='done'")], [6])
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.rows('SELECT * FROM context_inputs'), [])

    def test_children_closed_along_with_their_parent_are_not_recorded(self):
        parent = self.task(self.alpha, title='Launch the newsletter')
        children = [self.task(self.alpha, title=title, parent_id=parent['id']) for title in ('Write issue one', 'Import the list')]
        self.ok(self.close(self.worker, parent, result='Launched the newsletter to 400 readers.', children='close'))
        states = {row['id']: row['status'] for row in self.rows('SELECT id, status FROM tasks')}
        self.assertEqual(states, {parent['id']: 'done', **{child['id']: 'done' for child in children}})
        [action] = self.actions()
        self.assertEqual((json.loads(action['refs_json']), action['action_text']),
                         ({'task': parent['id']}, 'Launched the newsletter to 400 readers.'))

    def test_a_task_reopened_and_closed_again_keeps_one_action(self):
        task = self.task(self.alpha)
        self.ok(self.close(self.worker, task, result=RESULT))
        self.ok(self.update(self.worker, task, {'status': 'working'}))
        self.ok(self.close(self.worker, task, result='Sent it again with the corrected attachment.'))
        [action] = self.actions()
        self.assertEqual(action['action_text'], RESULT)
        # A withdrawn closure Action is still that Task's one closure Action.
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE action_logs SET withdrawn=1 WHERE id=?', (action['id'],))
        self.ok(self.update(self.worker, task, {'status': 'working'}))
        self.ok(self.close(self.worker, task, result='Third time.'))
        self.assertEqual(len(self.actions()), 1)
        self.assertEqual(len(self.rows('SELECT * FROM context_inputs')), 1)

    def test_nothing_is_recorded_while_the_closer_has_a_current_action_for_the_task(self):
        mine, theirs, withdrawn = (self.task(self.alpha, title=title) for title in ('One', 'Two', 'Three'))
        self.plant(mine, performer=self.worker, project=self.alpha)
        self.plant(theirs, performer=self.applier, project=self.alpha)
        self.plant(withdrawn, performer=self.worker, project=self.alpha, withdrawn=1)
        for task in (mine, theirs, withdrawn):
            self.ok(self.close(self.worker, task, result=RESULT))
        recorded = [json.loads(action['refs_json'])['task'] for action in self.actions() if action['origin'] == 'automatic']
        self.assertCountEqual(recorded, [theirs['id'], withdrawn['id']])

    def test_a_failing_recorder_does_not_fail_the_closure(self):
        task = self.task(self.alpha)

        def refuse(ctx, revision, uow):
            raise RuntimeError('the Context queue is unavailable')
        self.recorder.on_revision = refuse
        with self.assertLogs(closure_actions.logger, 'ERROR') as logged:
            closed = self.ok(self.close(self.worker, task, result=RESULT))
        self.assertEqual((closed['status'], closed['status_line']), ('done', RESULT))
        self.assertIn(task['id'], logged.output[0])
        self.assertIn('the Context queue is unavailable', logged.output[0])
        # Only the Action was rolled back: no row of it stays, and the Task write with its thread entry committed.
        for table in ('action_logs', 'action_revisions', 'action_projects', 'context_inputs'):
            self.assertEqual(self.rows(f'SELECT * FROM {table}'), [], table)
        self.assertEqual(self.rows('SELECT status FROM tasks WHERE id=?', task['id']), [{'status': 'done'}])
        self.assertTrue(self.rows("SELECT 1 FROM thread_messages WHERE item_id=? AND kind='comment'", task['id']))
        # A storage refusal is isolated the same way.
        self.recorder.on_revision = admit_action_revision
        second = self.task(self.alpha, title='Second')
        with self.db.transaction() as uow:
            uow.connection.execute("CREATE TRIGGER refuse_actions BEFORE INSERT ON action_logs "
                                   "BEGIN SELECT RAISE(ABORT,'no actions today'); END")
        with self.assertLogs(closure_actions.logger, 'ERROR'):
            self.ok(self.close(self.worker, second, result=RESULT))
        self.assertEqual(self.actions(), [])
        self.assertEqual(self.rows('SELECT status FROM tasks WHERE id=?', second['id']), [{'status': 'done'}])

    def test_the_action_adds_no_inbox_delivery(self):
        task = self.task(self.alpha)
        before = self.rows('SELECT COUNT(*) AS n FROM inbox_deliveries WHERE agent_id=?', self.lead)[0]['n']
        self.ok(self.close(self.worker, task, result=RESULT))
        self.assertEqual(len(self.actions()), 1)
        # The coordinator is told of the closure once, by the Task's own event; the Action's announcement reaches nobody.
        deliveries = self.rows('SELECT e.source_type, e.source_id, e.event_type FROM inbox_deliveries d JOIN inbox_events e '
                               'ON e.id=d.event_id WHERE d.agent_id=? ORDER BY e.id', self.lead)[before:]
        self.assertEqual([(row['source_type'], row['source_id']) for row in deliveries], [('tasks', task['id'])])
        self.assertEqual(self.rows("SELECT * FROM inbox_events WHERE source_type='actions'"), [])


class Caller:
    """The transport's caller for whichever agent a test acts as."""
    scope = 'agent'

    def __init__(self, contexts):
        self.contexts = contexts

    def exposes(self, descriptor):
        return True

    def check_operation(self, canonical_name):
        pass

    def validate_options(self, actor, canonical_name, options):
        pass

    def validate_arguments(self, actor, canonical_name, args):
        pass

    def identity(self, actor):
        return (actor.principal.id,)

    def invocation_context(self, actor, *, canonical_name, options, native_binding=None):
        return replace(self.contexts[actor.principal.id], project_id=options.project_id,
                       review_project_id=options.review_project_id)


class CoordinatorHintTests(Harness):
    """What a done Task's result tells its caller over MCP, with the instruction texts of the packaged catalog."""

    def setUp(self):
        super().setUp()
        self.instructions = from_catalog(lambda name: (CATALOG / name).read_text())
        self.missing = self.instructions.render('task-action-missing')
        self.runtime = NS(registry=self.registry, application=self.app, resolver=NS(
            require_agentic_view=lambda: NS(runtime_instructions=lambda: self.instructions)))
        self.transport = mcp.MCPTransport(self.runtime, None, None, caller=Caller(self.contexts))

    def send(self, who, name, arguments):
        if name != 'tasks_get':
            arguments = {**arguments, 'idempotency_key': str(uuid4())}
        actor = NS(principal=self.contexts[who].principal)
        return asyncio.run(self.transport._single_result(name, arguments, actor))

    def read(self, who, task, **extra):
        result = self.send(who, 'tasks_get', {'task_id': task['id'], **extra})
        self.assertEqual(result['status'], 'ok', result)
        return result

    def recorded(self, key, text, performer):
        return self.instructions.render('task-action-recorded', ACTION_ID=key, TEXT=text, PERFORMER=performer)

    def test_the_coordinator_is_told_of_the_action_a_worker_closure_recorded(self):
        for project in (self.alpha, None):
            task = self.task(project)
            closed = self.send(self.worker, 'tasks_complete', {'task_id': task['id'], 'result': RESULT})
            self.assertEqual(closed['status'], 'ok', closed)
            # The worker is told nothing new, on its own closure or on a read.
            self.assertNotIn('agent_hint', closed)
            self.assertNotIn('agent_hint', self.read(self.worker, task))
            expected = self.recorded(closure_identity(project, task['id']), RESULT, 'Scout')
            self.assertIn('Do not record it again.', expected)
            # The compact presentation of the read keeps the hint, and so does the full one.
            self.assertEqual(self.read(self.lead, task).get('agent_hint'), expected)
            self.assertEqual(self.read(self.lead, task, response_detail='full').get('agent_hint'), expected)
            # A write of his own that leaves the Task done says the same.
            noted = self.send(self.lead, 'tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}})
            self.assertEqual((noted['status'], noted.get('agent_hint')), ('ok', expected), noted)

    def test_the_coordinator_is_told_when_a_recent_closure_has_no_action(self):
        task = self.task(self.alpha)
        closed = self.send(self.lead, 'tasks_complete', {'task_id': task['id'], 'result': RESULT})
        self.assertEqual((closed['status'], closed.get('agent_hint')), ('ok', self.missing), closed)
        self.assertIn('actions_record', self.missing)
        self.assertEqual(self.read(self.lead, task).get('agent_hint'), self.missing)
        self.assertEqual(self.actions(), [])
        # An Action of another scope, or a withdrawn one, is not this Task's Action.
        self.plant(task, performer=self.lead, project=self.beta)
        self.plant(task, performer=self.lead, category='global-operational')
        self.plant(task, performer=self.lead, project=self.alpha, withdrawn=1)
        self.assertEqual(self.read(self.lead, task).get('agent_hint'), self.missing)
        # Once he records it, the read names it, with the performer the Action credits.
        key = self.plant(task, performer=self.owner, project=self.alpha, text='Owner sent the investor email')
        self.assertEqual(self.read(self.lead, task).get('agent_hint'),
                         self.recorded(key, 'Owner sent the investor email', 'Owner'))
        # A Global Task counts a global-operational Action, never a project's.
        general = self.task(None)
        self.ok(self.close(self.owner, general))
        self.plant(general, performer=self.lead, project=self.alpha)
        self.assertEqual(self.read(self.lead, general).get('agent_hint'), self.missing)
        key = self.plant(general, performer=self.lead, category='global-operational', text='Filed the yearly report')
        self.assertEqual(self.read(self.lead, general).get('agent_hint'),
                         self.recorded(key, 'Filed the yearly report', 'Ordubashy'))

    def test_the_fact_follows_a_hint_the_write_already_carries(self):
        task = self.task(self.alpha)
        self.app.ordubashy_attention = NS(task_update_hint=lambda ctx, args, operation: 'Clear your todo_focus.')
        closed = self.send(self.lead, 'tasks_complete', {'task_id': task['id'], 'result': RESULT})
        self.assertEqual(closed.get('agent_hint'), 'Clear your todo_focus. ' + self.missing, closed)
        # The worker keeps exactly the hint it had.
        other = self.task(self.alpha, title='Other')
        closed = self.send(self.worker, 'tasks_complete', {'task_id': other['id'], 'result': RESULT})
        self.assertEqual(closed.get('agent_hint'), 'Clear your todo_focus.', closed)

    def test_an_older_closure_an_open_task_and_a_cancelled_one_say_nothing(self):
        old, open_, cancelled = (self.task(self.alpha, title=title) for title in ('Old', 'Open', 'Cancelled'))
        self.ok(self.close(self.owner, old))
        self.ok(self.close(self.owner, cancelled, status='cancelled'))
        self.assertEqual(self.read(self.lead, old).get('agent_hint'), self.missing)
        stale = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat().replace('+00:00', 'Z')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE tasks SET latest_completed_at=? WHERE id=?', (stale, old['id']))
        for task in (old, open_, cancelled):
            self.assertNotIn('agent_hint', self.read(self.lead, task), task['title'])
        # His own closure as cancelled, and a write that leaves a Task open, say nothing either.
        dropped = self.send(self.lead, 'tasks_complete', {'task_id': open_['id'], 'status': 'cancelled'})
        self.assertEqual(dropped['status'], 'ok', dropped)
        self.assertNotIn('agent_hint', dropped)
        # An old closure that has its Action is still named: the window only limits the reminder that one is missing.
        key = self.plant(old, performer=self.worker, project=self.alpha)
        self.assertEqual(self.read(self.lead, old).get('agent_hint'), self.recorded(key, 'Sent the investor email', 'Scout'))

    def test_only_the_configured_coordinator_is_told(self):
        task = self.task(self.alpha)
        self.ok(self.close(self.owner, task))
        for who in (self.worker, self.applier):
            self.assertNotIn('agent_hint', self.read(who, task), who)
        # An agent that holds the coordinator's role without being the configured coordinator is told nothing.
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
        self.assertNotIn('agent_hint', self.read(self.lead, task))

    def test_a_failing_lookup_leaves_the_result_as_it_was(self):
        task = self.task(self.alpha)
        self.ok(self.close(self.owner, task))
        self.runtime.resolver = NS(require_agentic_view=lambda: (_ for _ in ()).throw(RuntimeError('no catalog')))
        with self.assertLogs(mcp.logger, 'ERROR'):
            result = self.read(self.lead, task)
        self.assertEqual(result['data']['task']['id'], task['id'])
        self.assertNotIn('agent_hint', result)


if __name__ == '__main__':
    unittest.main()
