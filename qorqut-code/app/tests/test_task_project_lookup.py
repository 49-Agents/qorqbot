"""A Task tool called over MCP with only a task_id finds the Task's project itself, and says so when it cannot."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.agents.attention import Attention
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.transport import mcp
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import ArtifactRef, InvocationContext, OperationResult, PolicyDecision, Principal, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.roles import GLOBAL_WORKER_ROLE
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks.lookup import TaskLookup
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations

from test_task_change_records import ports

STAMP = '2026-11-10T12:00:00Z'


class Policy:
    """Allow everything, except what a test lists: ``{(principal, project_id, operation): mode}``."""

    def __init__(self):
        self.modes = {}

    def resolve(self, context, descriptor, targets, uow):
        mode = self.modes.get((context.principal_id, context.project_id, descriptor.name), 'allow')
        return PolicyDecision(mode, 'test')


class AnyRecord:
    """A record boundary that admits every id: the new thread entry a comment writes cannot be listed in advance."""

    def __contains__(self, identity):
        return True


class Delegation:
    """One live project assignment, as the host's delegation provider answers for it. Its record boundary names the
    Tasks given and admits the comment's own writes; the assignment's project scope is what the tests exercise."""

    worker_operations = frozenset()
    context_scoped_reads = frozenset()

    def __init__(self, principal, project_id, operations, task_ids):
        self.binding_row = {'id': str(uuid4()), 'assignment_id': str(uuid4()), 'parent_principal_id': principal,
                            'execution_generation': 1, 'revoked_at': None, 'credential_hash': 'h' * 64}
        self.assignment_row = {'id': self.binding_row['assignment_id'], 'project_id': project_id,
                               'allowed_operations_json': list(operations), 'links_json': {}, 'lease_id': str(uuid4()),
                               'plan_revision': 1, 'scope_json': {'kind': 'project', 'record_ids': {
                                   'tasks': list(task_ids), 'project_configs': ['tasks'], 'thread_messages': AnyRecord()}}}
        self.actions = []

    def binding(self, uow, binding_id):
        return self.binding_row if binding_id == self.binding_row['id'] else None

    def authenticate_binding(self, uow, credential_hash):
        return self.binding_row

    def assignment(self, uow, assignment_id):
        return self.assignment_row if assignment_id == self.assignment_row['id'] else None

    def supports_role(self, role):
        return True

    def required_capabilities(self, role):
        return frozenset({'manage_delegated_assignments'})

    def selector_kinds(self, operation_name):
        return {'task_id': 'tasks'}

    def thread_has_assignment_message(self, uow, thread_id, assignment_id):
        return False

    def record_committed_action(self, uow, assignment_id, **action):
        self.actions.append(action)

    def context(self, base):
        return replace(base, delegation_binding_id=self.binding_row['id'],
                       delegation_assignment_id=self.assignment_row['id'], delegation_generation=1,
                       role_artifact_ref=ArtifactRef('ordubashy', '1.34.0', 'a' * 64),
                       global_capabilities=base.global_capabilities | {'manage_delegated_assignments'})


class Harness(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.agent = str(uuid4()), str(uuid4())
        # Alpha and Beta are the agent's projects; Gamma exists but is outside the agent's reach.
        self.alpha, self.beta, self.gamma = sorted(str(uuid4()) for _ in range(3))
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner',
                active=1, created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            uow.repository('principals').insert(dict(id=self.agent, kind='agent', display_name='Ordubashy',
                active=1, created_at=STAMP, updated_at=STAMP))
            uow.repository('agent_profiles', project_id=None).insert(dict(agent_id=self.agent, principal_id=self.agent,
                name='ordubashy', workspace='/tmp/ordubashy',
                status='active', profile_json={'status': 'active', 'role': {'id': GLOBAL_WORKER_ROLE}, 'scope': {
                    'kind': 'ecosystem', 'project_id': None, 'global_capabilities': ['read_global_context']}},
                created_at=STAMP, updated_at=STAMP))
            for project, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta'), (self.gamma, 'Gamma')):
                uow.repository('projects').insert({'id': project, 'slug': name.lower(), 'name': name,
                                                     'workspace': name.lower(), 'hq_path': name.lower() + '/hq'})
        everything = frozenset({self.alpha, self.beta, self.gamma})
        self.owner_ctx = InvocationContext(Principal(self.owner, 'human'), allowed_project_ids=everything,
                                           global_capabilities={'read_global_context'}, source='dashboard')
        # A registered global agent over MCP: no project selected unless a call names one.
        self.agent_ctx = InvocationContext(Principal(self.agent, 'agent'), allowed_project_ids={self.alpha, self.beta},
                                           global_capabilities={'read_global_context'}, profile_revision=1, source='mcp')
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.policy = Policy()
        self.app = Application(self.db, self.registry, policy=self.policy)
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=ports())
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)

    # -- helpers ---------------------------------------------------------------------------------------------------

    def create(self, project, title='Prepare the founding email', **extra):
        options = {'idempotency_key': str(uuid4()), **({'project_id': project} if project else {})}
        arguments = {'title': title, **({} if project else {'scope': 'global'}), **extra}
        created = self.app.invoke(self.owner_ctx, 'tasks_create', arguments, options)
        self.assertEqual(created.status, 'ok', created.to_dict())
        return created.data['task']

    def legacy(self, project, title='Prepare the founding email', **extra):
        """A Task with a UUID id, as Tasks were made before short ids: the shortened-id suggestion is for these."""
        return self.create(project, title, id=str(uuid4()), **extra)

    def call(self, operation, arguments, ctx=None, **options):
        if self.registry.get(operation).effect_kind != 'read':
            options.setdefault('idempotency_key', str(uuid4()))
        return self.app.invoke(ctx or self.agent_ctx, operation, arguments, options)

    def comments(self, task_id):
        with self.db.read() as uow:
            return [(row['project_id'], row['body']) for row in uow.connection.execute(
                "SELECT project_id, body FROM thread_messages WHERE item_id=? AND kind='comment' ORDER BY created_at, id",
                (task_id,))]

    def receipts(self, key):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute(
                'SELECT target_namespace FROM idempotency_receipts WHERE idempotency_key=?', (key,))]


class TaskProjectLookupTests(Harness):
    # -- one match -------------------------------------------------------------------------------------------------

    def test_a_unique_id_reads_and_writes_in_its_own_project(self):
        task = self.create(self.alpha)
        read = self.call('tasks_get', {'task_id': task['id']})
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertEqual(read.data['task']['project_id'], self.alpha)
        for operation in ('tasks_thread_get', 'tasks_notes_list'):
            self.assertEqual(self.call(operation, {'task_id': task['id']}).status, 'ok', operation)
        key = str(uuid4())
        comment = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Drafted.'}, idempotency_key=key)
        self.assertEqual(comment.status, 'ok', comment.to_dict())
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'Drafted.')])
        self.assertEqual(self.receipts(key), ['project:' + self.alpha])
        updated = self.call('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}},
                            expected_revision=comment.data['task']['revision'])
        self.assertEqual(updated.status, 'ok', updated.to_dict())
        self.assertEqual(updated.data['task']['due'], '2026-12-01')

    def test_a_global_task_still_reads_without_a_project(self):
        task = self.create(None)
        read = self.call('tasks_get', {'task_id': task['id']})
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertIsNone(read.data['task']['project_id'])

    def test_a_resolved_write_replays_exactly_like_the_explicit_one(self):
        task = self.create(self.beta)
        key = str(uuid4())
        first = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key)
        again = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key)
        explicit = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key,
                             project_id=self.beta)
        self.assertEqual(first.status, 'ok', first.to_dict())
        # The same receipt answers all three: one comment, one receipt, the same stored result.
        self.assertEqual(plain(again.data), plain(first.data))
        self.assertEqual(plain(explicit.data), plain(first.data))
        self.assertEqual(self.comments(task['id']), [(self.beta, 'Sent.')])
        self.assertEqual(self.receipts(key), ['project:' + self.beta])
        # Different input under the same key is still a conflict.
        changed = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Other.'}, idempotency_key=key)
        self.assertEqual(changed.error['code'], 'idempotency_conflict')

    def test_a_replay_after_the_task_moved_returns_the_original_receipt(self):
        task = self.create(self.alpha)
        key = str(uuid4())
        first = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Booked.'}, idempotency_key=key)
        self.assertEqual(first.status, 'ok', first.to_dict())
        with self.db.transaction() as uow:
            relocation = self.tasks.relocation
            inspection = relocation.inspect(self.owner_ctx, uow, self.alpha, self.beta, [task['id']])
            relocation.apply(self.owner_ctx, uow, self.alpha, self.beta, [task['id']],
                             inspection.fingerprint(self.owner), move_id=str(uuid4()), at=STAMP)
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}).data['task']['project_id'], self.beta)
        replay = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Booked.'}, idempotency_key=key)
        self.assertEqual(plain(replay.data), plain(first.data))
        self.assertEqual(len(self.comments(task['id'])), 1)
        self.assertEqual(self.receipts(key), ['project:' + self.alpha])

    def test_a_staged_write_pins_the_located_project_for_approval(self):
        task = self.create(self.alpha)
        self.policy.modes[(self.agent, self.alpha, 'tasks_update')] = 'signoff'
        staged = []

        def stage(context, descriptor, plan, options, uow):
            staged.append((context, plan))
            return OperationResult(context.request_id, 'signoff_required', signoff={'id': str(uuid4())})
        self.app.signoffs = NS(stage=stage)
        result = self.call('tasks_update', {'task_id': task['id'], 'changes': {'title': 'Send the founding email'}},
                           expected_revision=task['revision'])
        self.assertEqual(result.status, 'signoff_required', result.to_dict())
        context, plan = staged[0]
        self.assertEqual((context.project_id, context.review_project_id), (self.alpha, self.alpha))
        self.assertEqual((plan.target_namespace, plan.review_project_id), ('project:' + self.alpha, self.alpha))
        self.assertNotIn('project_id', plan.arguments)
        # Approval rebuilds the proposer from the plan's namespace; the lookup is never consulted again.
        proposer = replace(self.agent_ctx, project_id=self.alpha, review_project_id=self.alpha)
        with patch.object(TaskLookup, '__call__', side_effect=AssertionError('lookup during approval')):
            with self.db.read() as uow:
                self.app.validate_prepared(proposer, plan, uow, require_approval=False)

    # -- no reach, several matches, the wrong project ------------------------------------------------------------

    def test_a_task_outside_the_callers_reach_is_an_ordinary_not_found(self):
        hidden = self.legacy(self.gamma)
        for task_id in (hidden['id'], hidden['id'][:8], hidden['id'].upper()):
            for operation, arguments in (('tasks_get', {}), ('tasks_comment', {'text': 'Hello'})):
                refused = self.call(operation, {'task_id': task_id, **arguments})
                self.assertEqual(refused.error['code'], 'not_found', (operation, task_id))
                self.assertNotIn('details', refused.error)
        self.assertEqual(self.comments(hidden['id']), [])
        # A project the agent may reach but may not read Tasks in is not searched either.
        denied = self.legacy(self.beta)
        self.policy.modes[(self.agent, self.beta, 'tasks_get')] = 'deny'
        refused = self.call('tasks_get', {'task_id': denied['id'][:8]})
        self.assertEqual(refused.error['code'], 'not_found')
        self.assertNotIn('details', refused.error)

    def test_a_wrong_project_says_where_the_task_is(self):
        task = self.create(self.alpha, title='Book the venue')
        for operation, arguments in (('tasks_get', {}), ('tasks_comment', {'text': 'Booked.'})):
            refused = self.call(operation, {'task_id': task['id'], **arguments}, project_id=self.beta)
            self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
            self.assertEqual(plain(refused.error['details']), {'task': {
                'id': task['id'], 'project_id': self.alpha, 'project_name': 'Alpha', 'title': 'Book the venue'}})
        self.assertEqual(self.comments(task['id']), [])
        # Explicit Global is respected the same way.
        refused = self.call('tasks_get', {'task_id': task['id'], 'scope': 'global'})
        self.assertEqual(refused.error['details']['task']['project_id'], self.alpha)
        # The right project still works.
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}, project_id=self.alpha).status, 'ok')

    def test_a_task_moved_from_the_named_project_keeps_record_moved(self):
        task = self.create(self.alpha)
        with self.db.transaction() as uow:
            inspection = self.tasks.relocation.inspect(self.owner_ctx, uow, self.alpha, self.beta, [task['id']])
            self.tasks.relocation.apply(self.owner_ctx, uow, self.alpha, self.beta, [task['id']],
                                        inspection.fingerprint(self.owner), move_id=str(uuid4()), at=STAMP)
        refused = self.call('tasks_get', {'task_id': task['id']}, project_id=self.alpha)
        self.assertEqual(refused.error['code'], 'record_moved', refused.to_dict())

    # -- shortened and upper-case ids ------------------------------------------------------------------------------

    def test_a_shortened_id_names_the_task_it_begins_without_running(self):
        task = self.legacy(self.beta, title='Follow up with Jason')
        for operation, arguments in (('tasks_get', {}), ('tasks_comment', {'text': 'Followed up.'})):
            for options in ({}, {'project_id': self.alpha}):
                refused = self.call(operation, {'task_id': task['id'][:8], **arguments}, **options)
                self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
                self.assertEqual(plain(refused.error['details']), {'did_you_mean': {
                    'id': task['id'], 'project_id': self.beta, 'project_name': 'Beta', 'title': 'Follow up with Jason'}})
        self.assertEqual(self.comments(task['id']), [])
        # Too short, not hexadecimal, or the start of two ids: no suggestion.
        twin = 'abcdef12-0000-4000-8000-000000000001'
        self.create(self.alpha, id=twin)
        self.create(self.beta, id='abcdef12-0000-4000-8000-000000000002')
        for given in (task['id'][:5], 'zz' + task['id'][:8], 'abcdef12'):
            refused = self.call('tasks_get', {'task_id': given})
            self.assertEqual(refused.error['code'], 'not_found', given)
            self.assertNotIn('details', refused.error, given)

    def test_an_upper_case_id_is_looked_up_in_lower_case(self):
        task = self.create(self.alpha)
        for options in ({}, {'project_id': self.alpha}):
            read = self.call('tasks_get', {'task_id': task['id'].upper()}, **options)
            self.assertEqual(read.status, 'ok', read.to_dict())
            self.assertEqual(read.data['task']['id'], task['id'])
        key = str(uuid4())
        first = self.call('tasks_comment', {'task_id': task['id'].upper(), 'text': 'Done.'}, idempotency_key=key)
        again = self.call('tasks_comment', {'task_id': task['id'].upper(), 'text': 'Done.'}, idempotency_key=key)
        self.assertEqual(plain(again.data), plain(first.data))
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'Done.')])

    # -- other callers ---------------------------------------------------------------------------------------------

    def test_the_dashboard_still_selects_its_scope_itself(self):
        task = self.legacy(self.alpha)
        for task_id in (task['id'], task['id'][:8]):
            refused = self.call('tasks_get', {'task_id': task_id}, ctx=self.owner_ctx)
            self.assertEqual(refused.error['code'], 'not_found')
            self.assertNotIn('details', refused.error)
        refused = self.call('tasks_get', {'task_id': task['id']}, ctx=self.owner_ctx, project_id=self.beta)
        self.assertEqual(refused.error['code'], 'not_found')
        self.assertNotIn('details', refused.error)
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}, ctx=self.owner_ctx,
                                   project_id=self.alpha).status, 'ok')

    def test_an_owner_session_stays_in_its_bound_project(self):
        mine, elsewhere = self.legacy(self.alpha), self.legacy(self.beta)
        session = replace(self.owner_ctx, source='mcp', project_id=self.alpha, review_project_id=self.alpha,
                          allowed_project_ids=frozenset({self.alpha}), owner_session_project_id=self.alpha)
        self.assertEqual(self.call('tasks_get', {'task_id': mine['id'].upper()}, ctx=session).status, 'ok')
        short = self.call('tasks_get', {'task_id': mine['id'][:8]}, ctx=session)
        self.assertEqual(short.error['details']['did_you_mean']['id'], mine['id'])
        for task_id in (elsewhere['id'], elsewhere['id'][:8]):
            refused = self.call('tasks_get', {'task_id': task_id}, ctx=session)
            self.assertEqual(refused.error['code'], 'not_found')
            self.assertNotIn('details', refused.error)

    def test_a_delegated_worker_finds_its_task_in_its_assignment_project_only(self):
        task, elsewhere = self.create(self.alpha), self.create(self.beta)
        delegation = Delegation(self.agent, self.alpha, ('tasks_comment', 'tasks_get'), (task['id'], elsewhere['id']))
        self.app.delegation = delegation
        with self.db.transaction() as uow:
            # The parent agent holds the capability to delegate; its worker acts under the parent's own profile.
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, "
                                   "'$.scope.global_capabilities', json('[\"read_global_context\",\"manage_delegated_assignments\"]')) "
                                   "WHERE agent_id=?", (self.agent,))
        worker = delegation.context(self.agent_ctx)
        read = self.call('tasks_get', {'task_id': task['id'].upper()}, ctx=worker)
        self.assertEqual((read.status, read.data['task']['project_id']), ('ok', self.alpha), read.to_dict())
        written = self.call('tasks_comment', {'task_id': task['id'], 'text': 'From the worker.'}, ctx=worker)
        self.assertEqual(written.status, 'ok', written.to_dict())
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'From the worker.')])
        self.assertEqual([action['target_namespace'] for action in delegation.actions], ['project:' + self.alpha])
        # A Task in another project is not looked for: the call keeps its own scope, which the assignment refuses, and
        # the answer says nothing about where that Task is.
        refused = self.call('tasks_comment', {'task_id': elsewhere['id'], 'text': 'Not mine.'}, ctx=worker)
        self.assertEqual(refused.error['code'], 'forbidden', refused.to_dict())
        self.assertNotIn('details', refused.error)
        self.assertEqual(self.comments(elsewhere['id']), [])

    def test_outcomes_are_observed_in_the_project_the_task_was_found_in(self):
        task = self.create(self.alpha)
        seen = []
        self.app.incident_reporter = NS(observe_result=lambda ctx, operation, result: seen.append(
            (operation, ctx.project_id, result.status)))
        self.call('tasks_get', {'task_id': task['id']})
        self.call('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}},
                  expected_revision=task['revision'] + 5)
        self.assertEqual([(operation, project) for operation, project, _ in seen],
                         [('tasks_get', self.alpha), ('tasks_update', self.alpha)])
        self.assertEqual([status == 'ok' for _, _, status in seen], [True, False])

    # -- tasks_get include ----------------------------------------------------------------------------------------

    def test_include_reports_every_refusal_and_keeps_the_task(self):
        task = self.create(self.alpha)
        for mode, code in (('deny', 'policy_denied'), ('signoff', 'invalid_policy')):
            self.policy.modes[(self.agent, self.alpha, 'tasks_associated_records')] = mode
            read = self.call('tasks_get', {'task_id': task['id'], 'include': ['actions', 'signoffs']})
            self.assertEqual(read.status, 'ok', read.to_dict())
            self.assertEqual(plain(read.data['task']['associated']),
                             {'actions': {'unavailable': code}, 'signoffs': {'unavailable': code}})
            self.assertEqual(read.data['task']['id'], task['id'])

    def test_an_owner_session_includes_nothing_outside_its_catalog(self):
        task = self.create(self.alpha)
        session = replace(self.owner_ctx, source='mcp', project_id=self.alpha, review_project_id=self.alpha,
                          allowed_project_ids=frozenset({self.alpha}), owner_session_project_id=self.alpha)
        records = NS(kinds=('task', 'action', 'signoff'))
        with patch.object(type(self.tasks.associated), 'items', new=records), \
                patch.object(self.tasks.associated, 'read', side_effect=AssertionError('not reached')):
            read = self.call('tasks_get', {'task_id': task['id'], 'include': ['actions', 'signoffs']}, ctx=session)
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertEqual(plain(read.data['task']['associated']),
                         {'actions': {'unavailable': 'forbidden'}, 'signoffs': {'unavailable': 'forbidden'}})

    def test_every_operation_naming_one_task_locates_it(self):
        located = {descriptor.name for descriptor in self.registry.installed() if descriptor.locate_scope is not None}
        named = {descriptor.name for descriptor in self.registry.installed()
                 if 'task_id' in descriptor.input_schema.get('properties', {})}
        self.assertEqual(located, named)
        self.assertEqual(named, {'tasks_get', 'tasks_thread_get', 'tasks_notes_list', 'tasks_associated_records',
                                 'tasks_update', 'tasks_assign', 'tasks_comment', 'tasks_complete', 'tasks_delegate',
                                 'tasks_follow'})


class TransportLookupTests(Harness):
    """The registered-agent MCP route: the agent's context carries only what the call's options select."""

    def setUp(self):
        super().setUp()
        base = self.agent_ctx

        def agent_context(principal, *, diagnostic=False, project_id=None, session_registration_id=None,
                          review_project_id=None):
            return replace(base, project_id=project_id, review_project_id=review_project_id)
        runtime = NS(registry=self.registry, application=self.app, agent_context=agent_context)
        self.transport = mcp.MCPTransport(runtime, None, None)
        self.actor = NS(principal=base.principal, registration_id=None, credential_id=str(uuid4()),
                        delegation_binding_id=None, delegation_assignment_id=None, delegation_generation=None)

    def send(self, name, arguments):
        with patch.object(mcp.logger, 'exception'):  # No live request: the tasks_get timeline delivery is skipped.
            return asyncio.run(self.transport._single_result(name, {'response_detail': 'full', **arguments}
                                                             if name == 'tasks_get' else arguments, self.actor))

    def test_the_mcp_route_finds_the_project_from_the_id(self):
        task = self.legacy(self.alpha)
        read = self.send('tasks_get', {'task_id': task['id']})
        self.assertEqual(read['status'], 'ok', read)
        self.assertEqual(read['data']['task']['project_id'], self.alpha)
        written = self.send('tasks_comment', {'task_id': task['id'], 'text': 'Over MCP.', 'idempotency_key': str(uuid4())})
        self.assertEqual(written['status'], 'ok', written)
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'Over MCP.')])
        short = self.send('tasks_get', {'task_id': task['id'][:8]})
        self.assertEqual(short['error']['details']['did_you_mean']['id'], task['id'])
        wrong = self.send('tasks_get', {'task_id': task['id'], 'project_id': self.beta})
        self.assertEqual(wrong['error']['details']['task']['project_id'], self.alpha)

    def test_a_located_completion_gets_the_focus_transition_hint(self):
        task = self.create(self.alpha)
        instructions = NS(render=lambda key, **values: key)
        self.app.ordubashy_attention = Attention(NS(application=self.app, focus=NS(inbox=NS()), database=self.db,
            resolver=NS(require_agentic_view=lambda: NS(runtime_instructions=lambda: instructions))))
        with self.db.transaction() as uow:
            uow.connection.execute('INSERT INTO agent_attention(agent_id, focus_json, updated_at) VALUES (?,?,?)',
                (self.agent, json.dumps({'type': 'task', 'id': task['id'], 'project_id': self.alpha}), STAMP))
        with patch('qorqut.agents.attention.enrolled_board', return_value=True):
            # The id given alone and in upper case: the hint names the Task as found and stored.
            done = self.send('tasks_complete', {'task_id': task['id'].upper(), 'idempotency_key': str(uuid4())})
        self.assertEqual(done['status'], 'ok', done)
        self.assertEqual(done.get('agent_hint'), 'attention-focus-transition', done)

    def test_include_follow_ups_continue_the_page(self):
        # 25 associated Actions, read 20 at a time: the advertised follow-up, the same call with the scope arguments the
        # tasks_get used, and task_id alone located over MCP all continue the same page.
        for project, scope, given in ((self.alpha, {}, {'project_id': self.alpha}),
                                       (None, {'scope': 'global'}, {'scope': 'global'})):
            task = self.create(project)
            items = [{'type': 'action', 'id': f'global/a{n:03}', 'project_id': None, 'label': '', 'state': None,
                      'revision': 1, 'task_ids': [task['id']]} for n in range(25)]
            associated = self.tasks.associated
            with patch.object(type(associated), 'items', new=NS(kinds=('task', 'action'))), \
                    patch.object(associated, 'sources', return_value=set()), \
                    patch.object(associated, 'records', return_value=items):
                first = self.call('tasks_get', {'task_id': task['id'], 'include': ['actions'], **scope},
                                  **({'project_id': project} if project else {}))
                self.assertEqual(first.status, 'ok', first.to_dict())
                page = first.data['task']['associated']['actions']
                self.assertEqual([item['id'] for item in page['items']], [item['id'] for item in items[:20]])
                arguments = ', '.join(f'{key}="{value}"' for key, value in given.items())
                self.assertEqual(page['more'], f'tasks_associated_records(task_id="{task["id"]}", record_type="action", '
                                               f'{arguments}, limit=20, cursor=next_cursor)')
                base = {'task_id': task['id'], 'record_type': 'action', 'limit': 20, 'cursor': page['next_cursor']}
                options = {'project_id': project} if project else {}
                follow_ups = {
                    'advertised': self.call('tasks_associated_records', {**base, **scope}, **options),
                    'with scope arguments': self.call('tasks_associated_records',
                        {**base, 'scope': 'project' if project else 'global', 'include_descendants': True}, **options),
                    'located': self.call('tasks_associated_records', base)}
                for name, result in follow_ups.items():
                    self.assertEqual(result.status, 'ok', (project, name, result.to_dict()))
                    self.assertEqual([item['id'] for item in result.data['items']], [item['id'] for item in items[20:]],
                                     (project, name))
                    self.assertIsNone(result.data['next_cursor'])
                over_mcp = self.send('tasks_associated_records', base)
                self.assertEqual(over_mcp['status'], 'ok', over_mcp)
                self.assertEqual([item['id'] for item in over_mcp['data']['items']], [item['id'] for item in items[20:]])

    def test_a_generated_short_id_is_found_by_its_exact_match(self):
        # tasks_create gives a short id (five characters, too short for the shortened-UUID suggestion): passed alone over
        # MCP it is found exactly, in its project, for reads and writes alike.
        task = self.create(self.beta)
        self.assertEqual(len(task['id']), 5)
        for given in (task['id'], task['id'].upper()):
            read = self.send('tasks_get', {'task_id': given})
            self.assertEqual(read['status'], 'ok', read)
            self.assertEqual((read['data']['task']['id'], read['data']['task']['project_id']), (task['id'], self.beta))
        written = self.send('tasks_comment', {'task_id': task['id'], 'text': 'Short.', 'idempotency_key': str(uuid4())})
        self.assertEqual(written['status'], 'ok', written)
        self.assertEqual(self.comments(task['id']), [(self.beta, 'Short.')])
        # Its start is no id and is not guessed at.
        refused = self.send('tasks_get', {'task_id': task['id'][:4]})
        self.assertEqual(refused['error']['code'], 'not_found', refused)
        self.assertNotIn('did_you_mean', refused['error'].get('details') or {})


if __name__ == '__main__':
    unittest.main()
