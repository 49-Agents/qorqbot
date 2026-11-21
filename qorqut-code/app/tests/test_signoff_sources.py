"""Response dependencies through real operations, transactions, scope checks and composed storage."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import json
import unittest

from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, Principal, PolicyDecision, new_id, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths
from qorqut_operations.tasks import TaskOperations, TaskPorts
from qorqut.composition.assembly import host_assembly
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.inbox.thread_views import related
from qorqut.service.changes import ChangeFeed
from qorqut.signoffs.ports import HostExecution, HostPeers, HostTasks, HostReferences, HostInputSources


class Policy:
    def __init__(self):
        self.modes = {}

    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision(self.modes.get(descriptor.name, 'allow'), 'synthetic',
                              {'global_capability': descriptor.global_capability} if descriptor.global_capability else {})


def noop(*args, **kwargs):
    return None


class SignoffSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feed = ChangeFeed()
        self.db = Database(self.root / 'state.sqlite', assembly=host_assembly(self.feed)).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.project = new_id(), new_id()
        self.ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard',
            global_capabilities=frozenset({'read_global_context'}), allowed_project_ids=frozenset({self.project}))
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': self.owner, 'kind': 'human', 'display_name': 'Synthetic owner'})
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': self.owner})
            uow.repository('projects').insert({'id': self.project, 'name': 'Example project', 'slug': 'example', 'workspace': 'example'})
            for project, identity in ((None, 'work'), (self.project, 'project-work')):
                uow.repository('tasks', project_id=project).insert({'id': identity, 'title': 'Example work', 'status': 'working', 'created_by': self.owner})
        ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
                            platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT, sources=HostInputSources())
        self.signoffs = SignoffService(self.db, ports, locks=LockPaths.under(self.root / 'locks'))
        self.registry, self.policy = OperationRegistry(), Policy()
        self.policy.context_for = lambda principal_id, project_id=None, review_project_id=None, **_: replace(
            self.ctx, project_id=project_id, review_project_id=review_project_id)
        self.app = Application(self.db, self.registry, policy=self.policy, signoffs=self.signoffs)
        self.signoffs.bind(self.app)
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *_: []))
        task_ports = TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *_: False, worker_excluded=lambda *_: False),
            incidents=NS(link_task=noop),
            references=NS(types=(), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda *_: ((), ()), snooze_routes=lambda *_: ()))
        self.tasks = TaskOperations(self.app, items, task_ports)
        for contribution in (self.signoffs.contribution(), self.tasks.contribution()):
            for entry in contribution.entries:
                self.registry.declare(entry)
            for descriptor in contribution.descriptors:
                self.registry.register(descriptor)
        self.private = PrivateConversations(NS(app=self.app, domains=NS(tasks=self.tasks)))
        self.app.private_conversations = self.private
        def resolve_source(ctx, source, uow):
            self.private.source_check(ctx, source['type'], source['id'], uow)
            row = self.signoffs._row(ctx, source['id'], uow)
            return {**source, 'label': row['preview_json']['title'], 'state': row['state']}
        self.app.attention = NS(legacy=NS(resolve=resolve_source))
        self.registry.freeze()
        self.source = {'type': 'task', 'id': 'work', 'project_id': None}
        self.form = {'title': 'Choose a date', 'explanation': 'Which date works?', 'kind': 'question', 'selection_mode': 'none', 'notes_required': True}

    def invoke(self, name, args, *, project=None, **options):
        ctx = replace(self.ctx, project_id=project, review_project_id=project)
        result = self.app.invoke(ctx, name, {**({'scope': 'global'} if project is None else {}), **args},
                                 {'idempotency_key': new_id(), **options})
        self.assertEqual(result.status, 'ok', result.error)
        return plain(result.data)

    def create(self, key='question', source=True, **changes):
        args = {'request_key': key, 'form': {**self.form, **changes}}
        if source is not False:
            args['source'] = self.source if source is True else source
        return self.invoke('signoffs_input_create', args)

    def task(self, project=None):
        return self.invoke('tasks_get', {'task_id': 'work' if project is None else 'project-work'}, project=project)['task']

    def answer(self, signoff):
        result = self.signoffs.respond(self.ctx, signoff['id'], proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'],
            idempotency_key=new_id(), notes='Next Tuesday')
        self.assertEqual(result.status, 'ok', result.error)
        return result

    def test_atomic_creation_retries_and_task_thread_agree(self):
        before = self.task()
        result = self.create()
        self.assertEqual(result['signoff']['input']['response_policy'], 'owner_only')
        duplicate = self.create()
        self.assertEqual(result['link'], duplicate['link'])
        after = self.task()
        self.assertEqual(after['status'], 'needs_input')
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['work_status'], 'working')
        self.assertEqual([r['id'] for r in after['input_requests']], [result['signoff']['id']])
        with self.db.read() as uow:
            thread = related(self.private, self.ctx, {'source': self.source}, uow)
            self.assertEqual(thread['input_requests'], after['input_requests'])
            self.assertEqual(thread['context']['input_requests'], after['input_requests'])
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM owner_conversations').fetchone()[0], 0)
        self.answer(result['signoff'])
        after_answer = self.task()
        self.assertEqual(after_answer['status'], 'working')
        self.assertEqual(after_answer['revision'], before['revision'])
        self.assertGreater(after_answer['input_version'], after['input_version'])

    def test_standalone_and_multiple_pending_dependencies(self):
        standalone = self.create('standalone', source=False)
        self.assertNotIn('link', standalone)
        self.assertFalse(self.task()['input_needed'])
        first, second = self.create('one'), self.create('two')
        self.answer(first['signoff'])
        self.assertEqual([r['id'] for r in self.task()['input_requests']], [second['signoff']['id']])
        self.invoke('signoffs_input_cancel', {'signoff_id': second['signoff']['id']})
        self.assertEqual(self.task()['status'], 'working')

    def test_source_authorization_failure_rolls_back_creation(self):
        for mode in ('deny', 'signoff'):
            self.policy.modes['tasks_comment'] = mode
            result = self.app.invoke(self.ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': mode,
                'form': self.form, 'source': self.source}, {'idempotency_key': new_id()})
            self.assertEqual(result.status, 'error')
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM signoffs').fetchone()[0], 0)
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM signoff_source_links').fetchone()[0], 0)

    def test_cross_scope_identity_visibility_and_cache_invalidation(self):
        project_source = {**self.source, 'id': 'project-work', 'project_id': self.project}
        initial = self.feed.route_versions({'tasks'}, self.project)
        created = self.create(source=project_source)
        self.assertNotEqual(self.feed.route_versions({'tasks'}, self.project), initial)
        self.assertFalse(self.task()['input_needed'])
        self.assertTrue(self.task(self.project)['input_needed'])
        self.policy.modes['signoffs_get'] = 'deny'
        request = self.task(self.project)['input_requests'][0]
        self.assertTrue(request['unavailable'])
        self.assertNotIn('title', request)
        self.assertNotIn('id', request)
        self.policy.modes.clear()
        before = self.feed.route_versions({'tasks'}, self.project)
        self.answer(created['signoff'])
        self.assertNotEqual(self.feed.route_versions({'tasks'}, self.project), before)
        self.assertFalse(self.task(self.project)['input_needed'])

    def test_unlink_cas_and_scope(self):
        created = self.create()
        args = {'source': self.source, 'signoff_id': created['signoff']['id'], 'link_id': created['link']['id'], 'expected_link_revision': 9, 'scope': 'global'}
        failed = self.app.invoke(self.ctx, 'signoffs_source_unlink', args, {'idempotency_key': new_id()})
        self.assertEqual(failed.error['code'], 'revision_conflict')
        self.invoke('signoffs_source_unlink', {**args, 'expected_link_revision': 1})
        self.assertFalse(self.task()['input_needed'])
        result = self.invoke('signoffs_source_link', {'source': self.source, 'signoff_id': created['signoff']['id']})
        self.assertNotEqual(result['link']['id'], created['link']['id'])
        stale = self.app.invoke(self.ctx, 'signoffs_source_unlink', {**args, 'expected_link_revision': 1}, {'idempotency_key': new_id()})
        self.assertEqual(stale.error['code'], 'revision_conflict', 'A stale unlink cannot delete a recreated dependency')
        repeated = self.invoke('signoffs_source_link', {'source': self.source, 'signoff_id': created['signoff']['id']})
        self.assertEqual(result, repeated)

    def test_legacy_status_is_diagnostic_and_new_manual_status_is_refused(self):
        with self.db.transaction() as uow:
            uow.repository('tasks', project_id=None).update('work', {'status': 'needs_input'}, expected_revision=1)
        task = self.task()
        self.assertEqual(task['status'], 'open')
        self.assertTrue(task['input_request_missing'])
        result = self.app.invoke(self.ctx, 'tasks_update', {'scope': 'global', 'task_id': 'work', 'changes': {'status': 'needs_input'}},
                                 {'expected_revision': task['revision'], 'idempotency_key': new_id()})
        self.assertEqual(result.error['code'], 'validation_error')
        with self.db.read() as uow:
            self.assertEqual(uow.repository('tasks', project_id=None).get('work')['status'], 'needs_input')

    def test_status_filter_and_work_status_are_independent(self):
        self.create()
        needed = self.invoke('tasks_list', {'statuses': ['needs_input']})['tasks']
        self.assertEqual([t['id'] for t in needed], ['work'])
        self.assertEqual(self.invoke('tasks_list', {'statuses': ['working']})['tasks'], [])
        current = self.task()
        changed = self.invoke('tasks_update', {'task_id': 'work', 'changes': {'status': 'blocked'}}, expected_revision=current['revision'])['task']
        self.assertEqual(changed['status'], 'needs_input')
        self.assertEqual(changed['work_status'], 'blocked')

    def test_management_input_cannot_claim_owner_attention(self):
        result = self.app.invoke(self.ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': 'internal',
            'form': {**self.form, 'response_policy': 'management_allowed'}, 'source': self.source}, {'idempotency_key': new_id()})
        self.assertEqual(result.error['code'], 'validation_error')

    def test_linking_prepared_approval_preserves_its_binding_and_it_can_be_approved(self):
        before = self.task()
        self.policy.modes['tasks_update'] = 'signoff'
        staged = self.app.invoke(self.ctx, 'tasks_update', {'scope': 'global', 'task_id': 'work', 'changes': {'title': 'Approved title'}},
            {'expected_revision': before['revision'], 'idempotency_key': new_id()})
        self.assertEqual(staged.status, 'signoff_required', staged.error)
        signoff = plain(staged.signoff)
        self.invoke('signoffs_source_link', {'signoff_id': signoff['id'], 'source': self.source})
        reread = self.invoke('signoffs_get', {'signoff_id': signoff['id']})['signoff']
        self.assertEqual(reread['payload_hash'], signoff['payload_hash'])
        self.assertEqual(reread['status_revision'], signoff['status_revision'])
        self.assertEqual(self.task()['revision'], before['revision'])
        result = self.signoffs.review(self.ctx, signoff['id'], decision='approve', proposal_revision=signoff['proposal_revision'],
            payload_hash=signoff['payload_hash'], expected_status_revision=signoff['status_revision'], idempotency_key=new_id())
        self.assertNotEqual(result.status, 'error', result.error)
        self.assertEqual(self.task()['status'], 'working')
        self.assertEqual(self.task()['title'], before['title'], 'A response queues the existing approval; linking itself never applies it')
        self.signoffs.run_one(signoff['id'])
        self.assertEqual(self.task()['title'], 'Approved title')

    def test_existing_management_request_cannot_be_linked(self):
        internal = self.create('internal', source=False)['signoff']
        result = self.app.invoke(self.ctx, 'signoffs_source_link', {'scope': 'global', 'signoff_id': internal['id'], 'source': self.source},
                                 {'idempotency_key': new_id()})
        self.assertEqual(result.error['code'], 'validation_error')

    def test_project_review_can_wait_on_global_source(self):
        result = self.invoke('signoffs_input_create', {'request_key': 'project', 'form': self.form, 'source': self.source}, project=self.project)
        self.assertTrue(self.task()['input_needed'])
        self.assertFalse(self.task(self.project)['input_needed'])
        self.assertEqual(self.task()['input_requests'][0]['project_id'], self.project)
        self.invoke('signoffs_input_cancel', {'signoff_id': result['signoff']['id']}, project=self.project)
        self.assertFalse(self.task()['input_needed'])

    def test_source_link_requires_signoff_read_authority(self):
        row = self.create(source=False, response_policy='owner_only')['signoff']
        self.policy.modes['signoffs_get'] = 'deny'
        result = self.app.invoke(self.ctx, 'signoffs_source_link', {'scope': 'global', 'signoff_id': row['id'], 'source': self.source},
                                 {'idempotency_key': new_id()})
        self.assertEqual(result.error['code'], 'policy_denied')

    def test_task_summary_preserves_the_same_input_projection(self):
        from qorqut.agents.attention import Attention
        self.create()
        task = self.task()
        with self.db.read() as uow:
            summary = Attention.task_summary(task, uow)
        self.assertEqual(summary['status'], 'needs_input')
        self.assertEqual(summary['input_requests'], task['input_requests'])
        self.assertEqual(summary['input_version'], task['input_version'])

    def test_board_mode_leaves_out_tasks_in_an_archived_project(self):
        # A project archived while its Tasks were still on a worker's board made every timed pass raise
        # ``project_archived``, which discarded every agent's reminders. Those Tasks are retained, not current work.
        from qorqut.agents.attention import Attention
        from qorqut_operations.project_lifecycle import require_active_project
        worker = new_id()
        profile = {'status': 'active', 'role': {'id': 'global-worker'},
                   'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': worker, 'kind': 'agent', 'display_name': 'Example worker'})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': worker, 'principal_id': worker,
                'name': 'example-worker', 'workspace': 'example-worker', 'profile_json': profile})
            task = uow.repository('tasks', project_id=self.project).get('project-work')
            uow.connection.execute('INSERT INTO attention_memberships(id,agent_id,task_project_id,task_id,task_created_at,'
                'created_at) VALUES(?,?,?,?,?,?)', (new_id(), worker, self.project, 'project-work', task['created_at'], task['created_at']))
            # A worker is reminded only of a Task that names it.
            uow.connection.execute('UPDATE tasks SET worker_ids_json=? WHERE project_id=? AND id=?', (json.dumps([worker]), self.project, 'project-work'))
        ctx = replace(self.ctx, principal=Principal(worker, 'agent'), source='mcp', profile_revision=1)
        self.policy.context_for = lambda principal_id, project_id=None, review_project_id=None, **_: replace(
            ctx if principal_id == worker else self.ctx, project_id=project_id, review_project_id=review_project_id)

        def refresh(context, uow):
            require_active_project(uow.connection, context.project_id)
            return context
        self.app.context_resolver = refresh
        board = Attention(NS(application=self.app, focus=NS(inbox=NS())))
        with self.db.read() as uow:
            self.assertEqual(board.board_mode(uow, worker), 'normal')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE projects SET archived_at=? WHERE id=?', (task['created_at'], self.project))
        with self.db.read() as uow:
            self.assertEqual(board.board_mode(uow, worker), 'empty')

    def test_worker_creation_response_delivery_and_board_mode(self):
        from qorqut.agents.attention import Attention
        worker = new_id()
        profile = {'status': 'active', 'role': {'id': 'global-worker'},
                   'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': worker, 'kind': 'agent', 'display_name': 'Example worker'})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': worker, 'principal_id': worker,
                'name': 'example-worker', 'workspace': 'example-worker', 'profile_json': profile})
            task = uow.repository('tasks', project_id=None).get('work')
            uow.connection.execute('INSERT INTO attention_memberships(id,agent_id,task_id,task_created_at,created_at) VALUES(?,?,?,?,?)',
                                   (new_id(), worker, 'work', task['created_at'], task['created_at']))
            # A worker is reminded only of a Task that names it.
            uow.connection.execute('UPDATE tasks SET worker_ids_json=? WHERE project_id IS NULL AND id=?', (json.dumps([worker]), 'work'))
        ctx = replace(self.ctx, principal=Principal(worker, 'agent'), source='mcp', profile_revision=1)
        self.policy.context_for = lambda principal_id, project_id=None, review_project_id=None, **_: replace(
            ctx if principal_id == worker else self.ctx, project_id=project_id, review_project_id=review_project_id)
        board = Attention(NS(application=self.app, focus=NS(inbox=NS())))
        result = self.app.invoke(ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': 'worker',
            'form': self.form, 'source': self.source}, {'idempotency_key': new_id()})
        self.assertEqual(result.status, 'ok', result.error)
        with self.db.read() as uow:
            self.assertEqual(board.board_mode(uow, worker), 'reassess')
        self.answer(result.data['signoff'])
        with self.db.read() as uow:
            self.assertEqual(board.board_mode(uow, worker), 'normal')
            delivered = uow.connection.execute('SELECT e.event_type FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id '
                'WHERE d.agent_id=? AND e.source_type=\'signoffs\' AND e.source_id=?', (worker, result.data['signoff']['id'])).fetchall()
            self.assertTrue(any('response_submitted' in row[0] for row in delivered))

    def test_the_coordinator_unlinks_and_withdraws_another_agents_request_and_a_peer_cannot(self):
        worker, peer, coordinator = new_id(), new_id(), new_id()
        with self.db.transaction() as uow:
            for identity, role in ((worker, 'global-worker'), (peer, 'global-worker'), (coordinator, 'ordubashy')):
                uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': role})
                uow.repository('agent_profiles', project_id=None).insert({'agent_id': identity, 'principal_id': identity,
                    'name': identity, 'workspace': identity, 'profile_json': {'status': 'active',
                        'role': {'id': role}, 'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
            uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (coordinator,))

        def agent(identity):
            return replace(self.ctx, principal=Principal(identity, 'agent'), source='mcp', profile_revision=1)

        def call(identity, name, args):
            return self.app.invoke(agent(identity), name, {'scope': 'global', **args}, {'idempotency_key': new_id()})

        created = call(worker, 'signoffs_input_create', {'request_key': 'stale', 'form': self.form, 'source': self.source})
        self.assertEqual(created.status, 'ok', created.error)
        signoff, link = created.data['signoff'], created.data['link']
        unlink = {'source': self.source, 'signoff_id': signoff['id'], 'link_id': link['id'], 'expected_link_revision': link['revision']}
        # Another worker removes neither the dependency nor the request: both stay the proposer's, the owner's or the coordinator's.
        self.assertEqual(call(peer, 'signoffs_source_unlink', unlink).error['code'], 'forbidden')
        self.assertEqual(call(peer, 'signoffs_input_cancel', {'signoff_id': signoff['id']}).error['code'], 'forbidden')
        self.assertTrue(self.task()['input_needed'])
        unlinked = call(coordinator, 'signoffs_source_unlink', unlink)
        self.assertEqual(unlinked.status, 'ok', unlinked.error)
        self.assertFalse(self.task()['input_needed'])
        withdrawn = call(coordinator, 'signoffs_input_cancel', {'signoff_id': signoff['id']})
        self.assertEqual(withdrawn.status, 'ok', withdrawn.error)
        self.assertEqual(withdrawn.data['signoff']['state'], 'cancelled')

    def test_source_links_follow_task_relocation_without_changing_proposal(self):
        from qorqut.domains.tasks.relocation import HostRelocation
        from qorqut_operations.tasks.relocation import Relocation
        created = self.create()
        relocation = Relocation(None, self.project, ('work',), ('work',))
        adapter = HostRelocation(self.app)
        with self.db.transaction() as uow:
            # Exercise the real host adapter; the full Task move separately owns its Task/relationship writes.
            before = adapter.inspect(uow, relocation)
            self.assertEqual(before.counts['input_links'], 1)
            adapter.apply(uow, relocation, self.signoffs.clock())
            moved = self.signoffs.sources.find(created['signoff']['id'], {**self.source, 'project_id': self.project}, uow)
            self.assertEqual(moved['revision'], 2)
            self.assertIsNone(self.signoffs.sources.find(created['signoff']['id'], self.source, uow))
            row = uow.repository('signoffs', project_id=None).get(created['signoff']['id'])
            self.assertEqual(row['payload_hash'], created['signoff']['payload_hash'])

    def test_unlink_arguments_survive_http_option_parsing(self):
        from qorqut.transport.http import split_options
        created = self.create()
        args, options = split_options({'scope': 'global', 'source': self.source, 'signoff_id': created['signoff']['id'],
            'link_id': created['link']['id'], 'expected_link_revision': 1, 'idempotency_key': new_id()})
        result = self.app.invoke(self.ctx, 'signoffs_source_unlink', args, options)
        self.assertEqual(result.status, 'ok', result.error)


if __name__ == '__main__':
    unittest.main()
