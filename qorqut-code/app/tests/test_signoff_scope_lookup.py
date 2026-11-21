"""A Signoff tool called over MCP with only a signoff_id finds the Signoff's review scope itself, and says so when it cannot."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.signoffs.ports import HostExecution, HostInputSources, HostPeers, HostReferences, HostTasks
from qorqut.transport import mcp
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.roles import GLOBAL_WORKER_ROLE
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.lookup import LOCATED, SignoffLookup
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths
from qorqut_operations.tasks import TaskOperations

from test_task_change_records import ports as task_ports
from test_task_project_lookup import Delegation

STAMP = '2026-11-10T12:00:00Z'
ELSEWHERE = 'Proposal does not exist in any review scope you can reach'
HERE = 'Proposal does not exist in this review scope'
FORM = {'title': 'Choose a date', 'explanation': 'Which date works?', 'kind': 'question', 'selection_mode': 'none',
        'notes_required': True}


class Policy:
    """Allow everything, except what a test lists: ``{(principal, project_id, operation): mode}``."""

    def __init__(self):
        self.modes, self.contexts = {}, {}

    def resolve(self, context, descriptor, targets, uow):
        mode = self.modes.get((context.principal_id, context.project_id, descriptor.name), 'allow')
        return PolicyDecision(mode, 'test', {'global_capability': descriptor.global_capability}
                              if descriptor.global_capability else {})

    def context_for(self, principal_id, project_id=None, review_project_id=None, **_):
        return replace(self.contexts[principal_id], project_id=project_id, review_project_id=review_project_id)


class Harness(unittest.TestCase):
    """The real Signoff service with an ecosystem agent over MCP: Alpha and Beta in its reach, Gamma outside."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.db = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.agent = str(uuid4()), str(uuid4())
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
        self.owner_ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard',
            allowed_project_ids={self.alpha, self.beta, self.gamma}, global_capabilities={'read_global_context'})
        # A registered global agent over MCP: no review scope selected unless a call names one.
        self.agent_ctx = InvocationContext(Principal(self.agent, 'agent'), allowed_project_ids={self.alpha, self.beta},
                                           global_capabilities={'read_global_context'}, profile_revision=1, source='mcp')
        self.policy = Policy()
        self.policy.contexts = {self.owner: self.owner_ctx, self.agent: self.agent_ctx}
        signoff_ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(),
            references=HostReferences(), platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT,
            sources=HostInputSources())
        self.signoffs = SignoffService(self.db, signoff_ports, locks=LockPaths.under(root / 'locks'))
        self.registry = OperationRegistry()
        self.app = Application(self.db, self.registry, policy=self.policy, signoffs=self.signoffs)
        self.signoffs.bind(self.app)
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=task_ports())
        for contribution in (self.signoffs.contribution(), self.tasks.contribution()):
            for entry in contribution.entries:
                self.registry.declare(entry)
            for descriptor in contribution.descriptors:
                self.registry.register(descriptor)
        self.registry.freeze()

    # -- helpers ---------------------------------------------------------------------------------------------------

    def call(self, operation, arguments, ctx=None, **options):
        if self.registry.get(operation).effect_kind != 'read':
            options.setdefault('idempotency_key', str(uuid4()))
        return self.app.invoke(ctx or self.agent_ctx, operation, arguments, options)

    def ask(self, project, key='question', ctx=None, **form):
        """A pending input request reviewed in this project (``None`` is Global), asked by the agent unless ``ctx``."""
        scope = {'project_id': project} if project else {}
        arguments = {'request_key': key, 'form': {**FORM, **form}, **({} if project else {'scope': 'global'})}
        created = self.call('signoffs_input_create', arguments, ctx, **scope)
        self.assertEqual(created.status, 'ok', created.to_dict())
        return plain(created.data['signoff'])

    def notes(self, signoff_id):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute(
                'SELECT body FROM signoff_notes WHERE signoff_id=? ORDER BY sequence', (signoff_id,))]

    def receipts(self, key):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute(
                'SELECT target_namespace FROM idempotency_receipts WHERE idempotency_key=?', (key,))]

    def refused(self, result, message, code='not_found'):
        self.assertEqual(result.status, 'error', result.to_dict())
        self.assertEqual((result.error['code'], result.error['message']), (code, message))
        self.assertNotIn('details', result.error)


class SignoffScopeLookupTests(Harness):
    # -- nothing selected --------------------------------------------------------------------------------------------

    def test_a_project_signoff_reads_and_writes_in_its_own_review_scope(self):
        signoff = self.ask(self.alpha)
        read = self.call('signoffs_get', {'signoff_id': signoff['id']})
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertEqual(read.data['signoff']['review_project_id'], self.alpha)
        key = str(uuid4())
        comment = self.call('signoffs_comment', {'signoff_id': signoff['id'], 'text': 'Any day but Friday.'},
                            idempotency_key=key)
        self.assertEqual(comment.status, 'ok', comment.to_dict())
        self.assertEqual(self.receipts(key), ['project:' + self.alpha])
        listed = self.call('signoffs_notes_list', {'signoff_id': signoff['id']})
        self.assertEqual([note['body'] for note in listed.data['notes']], ['Any day but Friday.'])
        urgent = self.call('signoffs_attention_set', {'signoff_id': signoff['id'], 'attention': {
            'urgent': True, 'reason': 'The venue holds the date until tonight'}})
        self.assertEqual(urgent.status, 'ok', urgent.to_dict())
        withdrawn = self.call('signoffs_input_cancel', {'signoff_id': signoff['id']})
        self.assertEqual(withdrawn.status, 'ok', withdrawn.to_dict())
        self.assertEqual(self.call('signoffs_get', {'signoff_id': signoff['id']}).data['signoff']['state'], 'cancelled')

    def test_a_global_signoff_runs_as_scope_global(self):
        signoff = self.ask(None)
        read = self.call('signoffs_get', {'signoff_id': signoff['id']})
        self.assertEqual(read.status, 'ok', read.to_dict())
        self.assertIsNone(read.data['signoff']['review_project_id'])
        self.assertEqual(plain(read.data), plain(self.call('signoffs_get', {'signoff_id': signoff['id'], 'scope': 'global'}).data))
        key = str(uuid4())
        comment = self.call('signoffs_comment', {'signoff_id': signoff['id'], 'text': 'Asked in Global.'}, idempotency_key=key)
        self.assertEqual(comment.status, 'ok', comment.to_dict())
        self.assertEqual(self.receipts(key), ['global'])
        self.assertEqual(self.call('signoffs_notes_list', {'signoff_id': signoff['id']}).data['notes'][0]['body'],
                         'Asked in Global.')
        withdrawn = self.call('signoffs_input_cancel', {'signoff_id': signoff['id']})
        self.assertEqual(withdrawn.status, 'ok', withdrawn.to_dict())

    def test_a_located_write_replays_exactly_like_the_explicit_one(self):
        for project, namespace, explicit in ((self.beta, 'project:' + self.beta, ({}, {'project_id': self.beta})),
                                              (None, 'global', ({'scope': 'global'}, {}))):
            signoff = self.ask(project)
            key, arguments = str(uuid4()), {'signoff_id': signoff['id'], 'text': 'Sent.'}
            first = self.call('signoffs_comment', arguments, idempotency_key=key)
            again = self.call('signoffs_comment', arguments, idempotency_key=key)
            named = self.call('signoffs_comment', {**arguments, **explicit[0]}, idempotency_key=key, **explicit[1])
            self.assertEqual(first.status, 'ok', first.to_dict())
            # The same receipt answers all three: one note, one receipt, the same stored result.
            self.assertEqual(plain(again.data), plain(first.data))
            self.assertEqual(plain(named.data), plain(first.data))
            self.assertEqual(self.notes(signoff['id']), ['Sent.'])
            self.assertEqual(self.receipts(key), [namespace])
            # Different input under the same key is still a conflict.
            changed = self.call('signoffs_comment', {**arguments, 'text': 'Other.'}, idempotency_key=key)
            self.assertEqual(changed.error['code'], 'idempotency_conflict')
            # The explicit call first, the located call after: the same receipt again.
            key = str(uuid4())
            named = self.call('signoffs_comment', {**arguments, **explicit[0]}, idempotency_key=key, **explicit[1])
            located = self.call('signoffs_comment', arguments, idempotency_key=key)
            self.assertEqual(plain(located.data), plain(named.data))
            self.assertEqual(self.notes(signoff['id']), ['Sent.', 'Sent.'])
            self.assertEqual(self.receipts(key), [namespace])

    def test_a_signoff_the_caller_cannot_read_does_not_exist_anywhere_it_can_reach(self):
        hidden = self.ask(self.gamma, ctx=self.owner_ctx, title='Confidential plan')
        for signoff_id in (hidden['id'], str(uuid4())):
            for operation, arguments in (('signoffs_get', {}), ('signoffs_notes_list', {}),
                                         ('signoffs_comment', {'text': 'Hello'}), ('signoffs_input_cancel', {})):
                refused = self.call(operation, {'signoff_id': signoff_id, **arguments})
                self.refused(refused, ELSEWHERE)
                self.assertNotIn('Gamma', str(refused.to_dict()))
        self.assertEqual(self.notes(hidden['id']), [])
        # A project the agent may reach but may not read Signoffs in is not disclosed either.
        denied = self.ask(self.beta)
        self.policy.modes[(self.agent, self.beta, 'signoffs_get')] = 'deny'
        self.refused(self.call('signoffs_notes_list', {'signoff_id': denied['id']}), ELSEWHERE)
        # An archived project is outside every agent's reach: nothing says the Signoff is there.
        archived = self.ask(self.alpha, key='archived')
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE projects SET archived_at=? WHERE id=?', (STAMP, self.alpha))
        narrowed = replace(self.agent_ctx, allowed_project_ids={self.beta})
        self.refused(self.call('signoffs_get', {'signoff_id': archived['id']}, ctx=narrowed), ELSEWHERE)

    # -- a selection stands ------------------------------------------------------------------------------------------

    def test_a_wrong_project_says_where_the_signoff_is(self):
        signoff, general = self.ask(self.alpha, title='Book the venue'), self.ask(None, title='Review the week')
        for operation, arguments in (('signoffs_get', {}), ('signoffs_comment', {'text': 'Booked.'})):
            refused = self.call(operation, {'signoff_id': signoff['id'], **arguments}, project_id=self.beta)
            self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
            self.assertEqual(refused.error['message'], 'Proposal is not in the selected review scope; it is in Alpha. '
                             'Call again with signoff_id alone, without project_id or scope')
            self.assertEqual(plain(refused.error['details']), {'signoff': {
                'id': signoff['id'], 'review_project_id': self.alpha, 'project_name': 'Alpha', 'title': 'Book the venue'}})
            refused = self.call(operation, {'signoff_id': general['id'], **arguments}, project_id=self.alpha)
            self.assertEqual(refused.error['code'], 'not_found', refused.to_dict())
            self.assertEqual(plain(refused.error['details']), {'signoff': {
                'id': general['id'], 'review_project_id': None, 'project_name': 'Global', 'title': 'Review the week'}})
        self.assertEqual(self.notes(signoff['id']) + self.notes(general['id']), [])
        # The right project still works, with the id alone or with the project named.
        self.assertEqual(self.call('signoffs_get', {'signoff_id': signoff['id']}, project_id=self.alpha).status, 'ok')

    def test_a_selected_project_discloses_nothing_the_caller_cannot_read(self):
        hidden = self.ask(self.gamma, ctx=self.owner_ctx)
        for signoff_id in (hidden['id'], str(uuid4())):
            self.refused(self.call('signoffs_get', {'signoff_id': signoff_id}, project_id=self.alpha), HERE)
        denied = self.ask(self.beta)
        self.policy.modes[(self.agent, self.beta, 'signoffs_get')] = 'deny'
        self.refused(self.call('signoffs_get', {'signoff_id': denied['id']}, project_id=self.alpha), HERE)

    def test_an_explicit_scope_or_review_selector_is_left_as_it_was(self):
        signoff, general = self.ask(self.alpha), self.ask(None)
        with patch.object(SignoffLookup, 'place', side_effect=AssertionError('looked up')):
            # scope=global is the caller's own selection of Global, answered as before: a Global read still shows a
            # project Signoff in the caller's reach, and its conversation still asks for its own review scope.
            for chosen in (signoff, general):
                self.assertEqual(self.call('signoffs_get', {'signoff_id': chosen['id'], 'scope': 'global'}).status, 'ok')
            self.refused(self.call('signoffs_notes_list', {'signoff_id': signoff['id'], 'scope': 'global'}),
                         'Select this signoff\u2019s owning review scope')
            # scope=project without a project keeps the operation's own refusal.
            refused = self.call('signoffs_get', {'signoff_id': signoff['id'], 'scope': 'project'})
            self.assertEqual(refused.error['code'], 'scope_required', refused.to_dict())
            # A review selector, as an argument or as the transport's option, is the caller's own selection too.
            for arguments, options in (({'review_project_id': self.alpha}, {}), ({}, {'review_project_id': self.alpha})):
                refused = self.call('signoffs_get', {'signoff_id': signoff['id'], **arguments}, **options)
                self.assertEqual(refused.status, 'error', refused.to_dict())
                self.assertNotIn('details', refused.error)

    # -- other callers ---------------------------------------------------------------------------------------------

    def test_the_dashboard_and_workers_still_select_their_scope_themselves(self):
        signoff = self.ask(self.alpha)
        with patch.object(SignoffLookup, '__call__', side_effect=AssertionError('lookup outside agent MCP')):
            refused = self.call('signoffs_get', {'signoff_id': signoff['id']}, ctx=self.owner_ctx)
            self.assertEqual(refused.status, 'error', refused.to_dict())
            self.assertNotEqual(refused.error['message'], ELSEWHERE)
            self.refused(self.call('signoffs_get', {'signoff_id': signoff['id']}, ctx=self.owner_ctx, project_id=self.beta), HERE)
            read = self.call('signoffs_get', {'signoff_id': signoff['id']}, ctx=self.owner_ctx, project_id=self.alpha)
            self.assertEqual(read.status, 'ok', read.to_dict())
            # The service's own readers are not operations and never consult it.
            direct = self.signoffs.get(replace(self.owner_ctx, project_id=self.alpha, review_project_id=self.alpha),
                                       signoff['id'])
            self.assertEqual(direct.status, 'ok', direct.to_dict())

    def test_an_owner_session_stays_in_its_bound_project(self):
        mine, elsewhere = self.ask(self.alpha, ctx=self.owner_ctx), self.ask(self.beta, ctx=self.owner_ctx)
        session = replace(self.owner_ctx, source='mcp', project_id=self.alpha, review_project_id=self.alpha,
                          allowed_project_ids=frozenset({self.alpha}), owner_session_project_id=self.alpha)
        with patch.object(SignoffLookup, 'place', side_effect=AssertionError('looked up')):
            self.assertEqual(self.call('signoffs_get', {'signoff_id': mine['id']}, ctx=session).status, 'ok')
            self.refused(self.call('signoffs_get', {'signoff_id': elsewhere['id']}, ctx=session), HERE)

    def test_a_delegated_worker_is_looked_up_only_in_the_scope_its_assignment_binds(self):
        mine, elsewhere, general = self.ask(self.alpha), self.ask(self.beta), self.ask(None)
        worker_of = lambda project: self.worker(Delegation(self.agent, project, ('signoffs_get',), ()))
        worker = worker_of(self.alpha)
        with self.db.read() as uow:
            for signoff, namespace in ((mine, 'project:' + self.alpha), (elsewhere, None), (general, None)):
                arguments = {'signoff_id': signoff['id']}
                self.assertEqual(self.signoffs.lookup(worker, arguments, uow), (arguments, namespace))
            # A selection of its own is never looked up, and never told where the Signoff is.
            selected = replace(worker, project_id=self.beta, review_project_id=self.beta)
            self.assertEqual(self.signoffs.lookup(selected, {'signoff_id': mine['id']}, uow), ({'signoff_id': mine['id']}, None))
        # Through the engine: the worker reads its assignment's Signoff by id alone, and one reviewed elsewhere keeps the
        # call's own answer, which says nothing about where that Signoff is.
        read = self.call('signoffs_get', {'signoff_id': mine['id']}, ctx=worker)
        self.assertEqual((read.status, read.data['signoff']['review_project_id']), ('ok', self.alpha), read.to_dict())
        refused = self.call('signoffs_get', {'signoff_id': elsewhere['id']}, ctx=worker)
        self.assertEqual(refused.error['code'], 'scope_required', refused.to_dict())
        self.assertNotIn('details', refused.error)
        # A Global assignment finds a Global Signoff, stated as scope=global, and nothing in a project.
        worker = worker_of(None)
        with self.db.read() as uow:
            self.assertEqual(self.signoffs.lookup(worker, {'signoff_id': general['id']}, uow),
                             ({'signoff_id': general['id'], 'scope': 'global'}, 'global'))
            self.assertEqual(self.signoffs.lookup(worker, {'signoff_id': mine['id']}, uow), ({'signoff_id': mine['id']}, None))

    def worker(self, delegation):
        """The context of a delegated worker of the agent, whose assignment the application now knows."""
        self.app.delegation = delegation
        with self.db.transaction() as uow:
            # The parent agent holds the capability to delegate; its worker acts under the parent's own profile.
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, "
                                   "'$.scope.global_capabilities', json('[\"read_global_context\",\"manage_delegated_assignments\"]')) "
                                   "WHERE agent_id=?", (self.agent,))
        delegation.selector_kinds = lambda operation: {'signoff_id': 'signoffs'}
        scope = delegation.assignment_row['scope_json']
        scope['record_ids']['signoffs'] = AnyRecord()
        scope['kind'] = 'project' if delegation.assignment_row['project_id'] else 'global'
        return delegation.context(self.agent_ctx)

    def test_outcomes_are_observed_in_the_scope_the_signoff_was_found_in(self):
        signoff, general = self.ask(self.alpha), self.ask(None)
        seen = []
        self.app.incident_reporter = NS(observe_result=lambda ctx, operation, result: seen.append(
            (operation, ctx.project_id, result.status)))
        self.call('signoffs_get', {'signoff_id': signoff['id']})
        self.call('signoffs_comment', {'signoff_id': general['id'], 'text': 'Noted.'})
        self.call('signoffs_get', {'signoff_id': str(uuid4())})
        self.assertEqual(seen[:2], [('signoffs_get', self.alpha, 'ok'), ('signoffs_comment', None, 'ok')])

    # -- which operations --------------------------------------------------------------------------------------------

    def test_every_agent_operation_naming_one_signoff_locates_it_and_says_so(self):
        located = {descriptor.name: descriptor for descriptor in self.registry.installed()
                   if isinstance(descriptor.locate_scope, SignoffLookup)}
        self.assertEqual(set(located), {'signoffs_get', 'signoffs_notes_list', 'signoffs_comment', 'signoffs_attention_set',
                                        'signoffs_input_cancel', 'signoffs_source_link', 'signoffs_source_unlink',
                                        'signoffs_reject'})
        for name, descriptor in located.items():
            self.assertIs(descriptor.locate_scope, self.signoffs.lookup, name)
            self.assertTrue(descriptor.description.endswith(LOCATED), name)
            self.assertIn('signoff_id', descriptor.input_schema['required'], name)
        self.assertIn('Pass signoff_id alone', LOCATED)
        for name in ('signoffs_list', 'signoffs_input_create'):
            self.assertIsNone(self.registry.get(name).locate_scope, name)

    def test_the_hook_leaves_calls_it_does_not_decide_untouched(self):
        signoff = self.ask(self.alpha)
        with self.db.read() as uow:
            for arguments in ({}, {'signoff_id': 7}, {'signoff_id': signoff['id'], 'scope': 'global'},
                              {'signoff_id': signoff['id'], 'scope': 'project'},
                              {'signoff_id': signoff['id'], 'review_project_id': self.alpha}):
                self.assertEqual(self.signoffs.lookup(self.agent_ctx, arguments, uow), (arguments, None), arguments)
            arguments = {'signoff_id': signoff['id']}
            reviewing = replace(self.agent_ctx, review_project_id=self.alpha)
            self.assertEqual(self.signoffs.lookup(reviewing, arguments, uow), (arguments, None))
            # A project Signoff keeps the caller's arguments; only a Global one states its scope.
            self.assertEqual(self.signoffs.lookup(self.agent_ctx, arguments, uow), (arguments, 'project:' + self.alpha))
            self.assertIs(self.signoffs.lookup(self.agent_ctx, arguments, uow)[0], arguments)


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
        return asyncio.run(self.transport._single_result(name, arguments, self.actor))

    def test_the_mcp_route_finds_the_review_scope_from_the_id(self):
        signoff, general = self.ask(self.alpha), self.ask(None)
        for chosen, project in ((signoff, self.alpha), (general, None)):
            read = self.send('signoffs_get', {'signoff_id': chosen['id']})
            self.assertEqual(read['status'], 'ok', read)
            self.assertEqual(read['data']['signoff']['id'], chosen['id'])
            written = self.send('signoffs_comment', {'signoff_id': chosen['id'], 'text': 'Over MCP.',
                                                     'idempotency_key': str(uuid4())})
            self.assertEqual(written['status'], 'ok', written)
            self.assertEqual(self.notes(chosen['id']), ['Over MCP.'])
            with self.db.read() as uow:
                stored = uow.connection.execute('SELECT review_project_id FROM signoffs WHERE id=?', (chosen['id'],)).fetchone()
            self.assertEqual(stored[0], project)
        wrong = self.send('signoffs_get', {'signoff_id': signoff['id'], 'project_id': self.beta})
        self.assertEqual(wrong['error']['details']['signoff']['review_project_id'], self.alpha)
        missing = self.send('signoffs_get', {'signoff_id': str(uuid4())})
        self.assertEqual((missing['error']['code'], missing['error']['message']), ('not_found', ELSEWHERE))


class AnyRecord:
    """A record boundary that admits every id."""

    def __contains__(self, identity):
        return True


if __name__ == '__main__':
    unittest.main()
