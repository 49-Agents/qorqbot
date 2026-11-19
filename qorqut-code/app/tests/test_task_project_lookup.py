"""A Task tool called over MCP with only a task_id finds the Task's project itself, and says so when it cannot."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.transport import mcp
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, OperationResult, PolicyDecision, Principal, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.roles import GLOBAL_WORKER_ROLE
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks.lookup import TaskLookup
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations

from test_task_change_records import ports

STAMP = '2026-11-10T12:00:00Z'


class Policy:
    """Allow everything, except what a test lists: ``{(principal, business_id, operation): mode}``."""

    def __init__(self):
        self.modes = {}

    def resolve(self, context, descriptor, targets, uow):
        mode = self.modes.get((context.principal_id, context.business_id, descriptor.name), 'allow')
        return PolicyDecision(mode, 'test')


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
            uow.repository('agent_profiles', business_id=None).insert(dict(agent_id=self.agent, principal_id=self.agent,
                home_business_id=None, namespace='ecosystem', name='ordubashy', workspace='/tmp/ordubashy',
                status='active', profile_json={'status': 'active', 'role': {'id': GLOBAL_WORKER_ROLE}, 'scope': {
                    'kind': 'ecosystem', 'business_id': None, 'global_capabilities': ['read_global_context']}},
                created_at=STAMP, updated_at=STAMP))
            for business, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta'), (self.gamma, 'Gamma')):
                uow.repository('businesses').insert({'id': business, 'slug': name.lower(), 'name': name,
                                                     'workspace': name.lower(), 'hq_path': name.lower() + '/hq'})
                uow.repository('business_memberships', business_id=business).insert(
                    {'principal_id': self.owner, 'active': 1, 'is_owner': 1})
        everything = frozenset({self.alpha, self.beta, self.gamma})
        self.owner_ctx = InvocationContext(Principal(self.owner, 'human'), allowed_business_ids=everything,
                                           global_capabilities={'read_global_context'}, source='dashboard')
        # A registered global agent over MCP: no project selected unless a call names one.
        self.agent_ctx = InvocationContext(Principal(self.agent, 'agent'), allowed_business_ids={self.alpha, self.beta},
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

    def create(self, business, title='Prepare the founding email', **extra):
        options = {'idempotency_key': str(uuid4()), **({'business_id': business} if business else {})}
        arguments = {'title': title, **({} if business else {'scope': 'global'}), **extra}
        created = self.app.invoke(self.owner_ctx, 'tasks_create', arguments, options)
        self.assertEqual(created.status, 'ok', created.to_dict())
        return created.data['task']

    def legacy(self, business, title='Prepare the founding email', **extra):
        """A Task with a UUID id, as Tasks were made before short ids: the shortened-id suggestion is for these."""
        return self.create(business, title, id=str(uuid4()), **extra)

    def call(self, operation, arguments, ctx=None, **options):
        if self.registry.get(operation).effect_kind != 'read':
            options.setdefault('idempotency_key', str(uuid4()))
        return self.app.invoke(ctx or self.agent_ctx, operation, arguments, options)

    def comments(self, task_id):
        with self.db.read() as uow:
            return [(row['business_id'], row['body']) for row in uow.connection.execute(
                "SELECT business_id, body FROM thread_messages WHERE item_id=? AND kind='comment' ORDER BY created_at, id",
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
        self.assertEqual(read.data['task']['business_id'], self.alpha)
        for operation in ('tasks_thread_get', 'tasks_notes_list'):
            self.assertEqual(self.call(operation, {'task_id': task['id']}).status, 'ok', operation)
        key = str(uuid4())
        comment = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Drafted.'}, idempotency_key=key)
        self.assertEqual(comment.status, 'ok', comment.to_dict())
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'Drafted.')])
        self.assertEqual(self.receipts(key), ['business:' + self.alpha])
        updated = self.call('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}},
                            expected_revision=comment.data['task']['revision'])
        self.assertEqual(updated.status, 'ok', updated.to_dict())
        self.assertEqual(updated.data['task']['due'], '2026-12-01')

    def test_a_global_task_still_reads_without_a_project(self):
        task = self.create(None)
        read = self.call('tasks_get', {'task_id': task['id']})
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertIsNone(read.data['task']['business_id'])

    def test_a_resolved_write_replays_exactly_like_the_explicit_one(self):
        task = self.create(self.beta)
        key = str(uuid4())
        first = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key)
        again = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key)
        explicit = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Sent.'}, idempotency_key=key,
                             business_id=self.beta)
        self.assertEqual(first.status, 'ok', first.to_dict())
        # The same receipt answers all three: one comment, one receipt, the same stored result.
        self.assertEqual(plain(again.data), plain(first.data))
        self.assertEqual(plain(explicit.data), plain(first.data))
        self.assertEqual(self.comments(task['id']), [(self.beta, 'Sent.')])
        self.assertEqual(self.receipts(key), ['business:' + self.beta])
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
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}).data['task']['business_id'], self.beta)
        replay = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Booked.'}, idempotency_key=key)
        self.assertEqual(plain(replay.data), plain(first.data))
        self.assertEqual(len(self.comments(task['id'])), 1)
        self.assertEqual(self.receipts(key), ['business:' + self.alpha])

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
        self.assertEqual((context.business_id, context.review_business_id), (self.alpha, self.alpha))
        self.assertEqual((plan.target_namespace, plan.review_business_id), ('business:' + self.alpha, self.alpha))
        self.assertNotIn('business_id', plan.arguments)
        # Approval rebuilds the proposer from the plan's namespace; the lookup is never consulted again.
        proposer = replace(self.agent_ctx, business_id=self.alpha, review_business_id=self.alpha)
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

    def test_a_reused_id_asks_for_the_project_listing_only_reachable_candidates(self):
        slug = 'founding-email-preparation'
        # New ids are unique across projects; an id held in several projects predates that rule.
        with patch('qorqut_operations.tasks.operations.taken', return_value=False):
            for business in (self.alpha, self.beta, self.gamma):
                self.create(business, id=slug)
        refused = self.call('tasks_get', {'task_id': slug})
        self.assertEqual(refused.error['code'], 'validation_error', refused.to_dict())
        self.assertIn('business_id', refused.error['field_errors'])
        self.assertEqual(plain(refused.error['details']), {'task_id': slug, 'candidates': [
            {'business_id': self.alpha, 'business_name': 'Alpha', 'title': 'Prepare the founding email'},
            {'business_id': self.beta, 'business_name': 'Beta', 'title': 'Prepare the founding email'}]})
        self.assertEqual(self.call('tasks_comment', {'task_id': slug, 'text': 'x'}).error['code'], 'validation_error')
        chosen = self.call('tasks_get', {'task_id': slug}, business_id=self.beta)
        self.assertEqual(chosen.data['task']['business_id'], self.beta)

    def test_a_wrong_project_says_where_the_task_is(self):
        task = self.create(self.alpha, title='Book the venue')
        for operation, arguments in (('tasks_get', {}), ('tasks_comment', {'text': 'Booked.'})):
            refused = self.call(operation, {'task_id': task['id'], **arguments}, business_id=self.beta)
            self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
            self.assertEqual(plain(refused.error['details']), {'task': {
                'id': task['id'], 'business_id': self.alpha, 'business_name': 'Alpha', 'title': 'Book the venue'}})
        self.assertEqual(self.comments(task['id']), [])
        # Explicit Global is respected the same way.
        refused = self.call('tasks_get', {'task_id': task['id'], 'scope': 'global'})
        self.assertEqual(refused.error['details']['task']['business_id'], self.alpha)
        # The right project still works.
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}, business_id=self.alpha).status, 'ok')

    def test_a_task_moved_from_the_named_project_keeps_record_moved(self):
        task = self.create(self.alpha)
        with self.db.transaction() as uow:
            inspection = self.tasks.relocation.inspect(self.owner_ctx, uow, self.alpha, self.beta, [task['id']])
            self.tasks.relocation.apply(self.owner_ctx, uow, self.alpha, self.beta, [task['id']],
                                        inspection.fingerprint(self.owner), move_id=str(uuid4()), at=STAMP)
        refused = self.call('tasks_get', {'task_id': task['id']}, business_id=self.alpha)
        self.assertEqual(refused.error['code'], 'record_moved', refused.to_dict())

    # -- shortened and upper-case ids ------------------------------------------------------------------------------

    def test_a_shortened_id_names_the_task_it_begins_without_running(self):
        task = self.legacy(self.beta, title='Follow up with Jason')
        for operation, arguments in (('tasks_get', {}), ('tasks_comment', {'text': 'Followed up.'})):
            for options in ({}, {'business_id': self.alpha}):
                refused = self.call(operation, {'task_id': task['id'][:8], **arguments}, **options)
                self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
                self.assertEqual(plain(refused.error['details']), {'did_you_mean': {
                    'id': task['id'], 'business_id': self.beta, 'business_name': 'Beta', 'title': 'Follow up with Jason'}})
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
        for options in ({}, {'business_id': self.alpha}):
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
        refused = self.call('tasks_get', {'task_id': task['id']}, ctx=self.owner_ctx, business_id=self.beta)
        self.assertEqual(refused.error['code'], 'not_found')
        self.assertNotIn('details', refused.error)
        self.assertEqual(self.call('tasks_get', {'task_id': task['id']}, ctx=self.owner_ctx,
                                   business_id=self.alpha).status, 'ok')

    def test_an_owner_session_stays_in_its_bound_project(self):
        mine, elsewhere = self.legacy(self.alpha), self.legacy(self.beta)
        session = replace(self.owner_ctx, source='mcp', business_id=self.alpha, review_business_id=self.alpha,
                          allowed_business_ids=frozenset({self.alpha}), owner_session_business_id=self.alpha)
        self.assertEqual(self.call('tasks_get', {'task_id': mine['id'].upper()}, ctx=session).status, 'ok')
        short = self.call('tasks_get', {'task_id': mine['id'][:8]}, ctx=session)
        self.assertEqual(short.error['details']['did_you_mean']['id'], mine['id'])
        for task_id in (elsewhere['id'], elsewhere['id'][:8]):
            refused = self.call('tasks_get', {'task_id': task_id}, ctx=session)
            self.assertEqual(refused.error['code'], 'not_found')
            self.assertNotIn('details', refused.error)

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

        def agent_context(principal, *, diagnostic=False, business_id=None, session_registration_id=None,
                          review_business_id=None):
            return replace(base, business_id=business_id, review_business_id=review_business_id)
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
        self.assertEqual(read['data']['task']['business_id'], self.alpha)
        written = self.send('tasks_comment', {'task_id': task['id'], 'text': 'Over MCP.', 'idempotency_key': str(uuid4())})
        self.assertEqual(written['status'], 'ok', written)
        self.assertEqual(self.comments(task['id']), [(self.alpha, 'Over MCP.')])
        short = self.send('tasks_get', {'task_id': task['id'][:8]})
        self.assertEqual(short['error']['details']['did_you_mean']['id'], task['id'])
        wrong = self.send('tasks_get', {'task_id': task['id'], 'business_id': self.beta})
        self.assertEqual(wrong['error']['details']['task']['business_id'], self.alpha)

    def test_a_generated_short_id_is_found_by_its_exact_match(self):
        # tasks_create gives a short id (five characters, too short for the shortened-UUID suggestion): passed alone over
        # MCP it is found exactly, in its project, for reads and writes alike.
        task = self.create(self.beta)
        self.assertEqual(len(task['id']), 5)
        for given in (task['id'], task['id'].upper()):
            read = self.send('tasks_get', {'task_id': given})
            self.assertEqual(read['status'], 'ok', read)
            self.assertEqual((read['data']['task']['id'], read['data']['task']['business_id']), (task['id'], self.beta))
        written = self.send('tasks_comment', {'task_id': task['id'], 'text': 'Short.', 'idempotency_key': str(uuid4())})
        self.assertEqual(written['status'], 'ok', written)
        self.assertEqual(self.comments(task['id']), [(self.beta, 'Short.')])
        # Its start is no id and is not guessed at.
        refused = self.send('tasks_get', {'task_id': task['id'][:4]})
        self.assertEqual(refused['error']['code'], 'not_found', refused)
        self.assertNotIn('did_you_mean', refused['error'].get('details') or {})


if __name__ == '__main__':
    unittest.main()
