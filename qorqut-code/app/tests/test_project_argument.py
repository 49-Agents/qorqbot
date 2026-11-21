"""``project_id`` as an operation's own argument, and as the scope option of every other operation.

The six Project record operations and ``agentic_access`` declare ``project_id`` themselves. Over MCP and over the
dashboard's routes the value reaches their handlers: the record operations run in the Project it names, and
``agentic_access`` checks it against the caller's native access where the caller is. For any other operation it stays
the option that selects the scope and is removed from the arguments."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut.agentic import access as native_access
from qorqut.agents.roles import ORDUBASHY_ROLE
from qorqut.composition.assembly import host_assembly
from qorqut.composition.catalog import builtin_registry
from qorqut.domains.projects import ProjectRecords, access
from qorqut.domains.projects.compose import OPERATIONS
from qorqut.domains.projects.lookup import ProjectLookup
from qorqut.service.changes import ChangeFeed
from qorqut.transport import mcp
from qorqut_operations.application.engine import Application
from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.storage.database import Database

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:
    SERVICE = False

STAMP = '2026-11-21T12:00:00Z'
WORKSPACE = '/tmp/ordubashy'
READS = {'read_global_context'}
OUT_OF_SCOPE = "Selected project is outside the agent's current scope"


class Policy:
    """Allows everything and remembers the scope each operation was authorized in. For the dashboard it also answers
    who the owner is in the scope a route selected."""

    def __init__(self, projects):
        self.projects, self.scopes = projects, []

    def resolve(self, context, descriptor, targets, uow):
        self.scopes.append((descriptor.name, context.project_id))
        return PolicyDecision('allow', 'test')

    def context_for(self, principal_id, *, project_id=None, review_project_id=None, uow=None):
        return InvocationContext(Principal(principal_id, 'human'), allowed_project_ids=self.projects,
                                 global_capabilities=READS, source='dashboard', project_id=project_id,
                                 review_project_id=review_project_id)


def echo(ctx, args, options, uow):
    return {'arguments': dict(args), 'selected_project_id': ctx.project_id}


def sample(name, properties, required=(), scopes=('project', 'global')):
    return OperationDescriptor(name, schema(properties, required), effect_kind='read', supports_signoff=False,
                               scope_kinds=frozenset(scopes), global_capability='read_global_context', read=echo)


class Harness(unittest.TestCase):
    """The real Project record operations and ``agentic_access`` on one application, with the owner, the configured
    Ordubashy and three Projects: the agent reaches Alpha and Beta, and its native access covers Alpha only."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.agent, self.registration = str(uuid4()), str(uuid4()), str(uuid4())
        self.alpha, self.beta, self.gamma = sorted(str(uuid4()) for _ in range(3))
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('principals').insert(dict(id=self.agent, kind='agent', display_name='Ordubashy', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('agent_profiles', project_id=None).insert(dict(
                agent_id=self.agent, principal_id=self.agent,
                name='ordubashy', workspace=WORKSPACE, status='active', created_at=STAMP, updated_at=STAMP,
                profile_json={'name': 'ordubashy', 'status': 'active', 'runtime': 'claude-code',
                              'role': {'id': ORDUBASHY_ROLE},
                              'scope': {'kind': 'ecosystem', 'global_capabilities': sorted(READS)}}))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                owner_principal_id=self.owner, management_coordinator_id=self.agent, created_at=STAMP, updated_at=STAMP))
            for project, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta'), (self.gamma, 'Gamma')):
                uow.repository('projects').insert({'id': project, 'slug': name.lower(), 'name': name,
                                                   'workspace': name.lower(), 'hq_path': name.lower() + '/hq'})
                uow.repository('project_memberships', project_id=project).insert(
                    {'principal_id': self.owner, 'active': 1, 'is_owner': 1})
            uow.connection.execute(
                "INSERT INTO machine_registrations(id,principal_id,workspace,service_url,profile_revision,created_at,"
                "updated_at) VALUES(?,?,?,?,1,?,?)",
                (self.registration, self.agent, WORKSPACE, 'http://127.0.0.1:1/mcp', STAMP, STAMP))
        self.policy = Policy(frozenset({self.alpha, self.beta, self.gamma}))
        self.registry = builtin_registry()
        self.app = Application(self.db, self.registry, policy=self.policy)
        items = ItemResolver(self.app, [ItemContribution('projects', ('project',), ProjectLookup(self.app))],
                             candidates=NS(candidates=lambda *a, **k: ()))
        ProjectRecords(self.app, items).register(self.registry)
        self.selections = []
        profile = NS(agent_id=self.agent, revision=1,
                     scope=NS(projects=frozenset({self.alpha}), global_capabilities=frozenset()))

        def selection(profile, **context):
            self.selections.append(context)
            return {'project_id': context.get('project_id'), 'work_type': context.get('work_type')}
        native_access.AgenticAccess(
            NS(profile=lambda principal, connection=None: (profile, {'workspace': WORKSPACE})),
            NS(selection=selection, layer=NS(root='/layer', state='/state', instance_id='instance'))).register(self.registry)
        # Three shapes of operation that do not own the field: one that never names it, one that declares the optional
        # selector, and a Global-only one whose declared inputs are all its own.
        self.registry.register(sample('sample_scoped_read', {'label': {'type': 'string'}}))
        self.registry.register(sample('sample_selector_read', {'project_id': UUID, 'label': {'type': 'string'}}))
        self.registry.register(sample('sample_global_read', {'project_id': UUID}, ('project_id',), scopes=('global',)))

    def records(self, call, reach):
        """Each of the six operations once: read Alpha, list it, find Beta, read Beta as its item, link and unlink it."""
        item = {'type': 'project', 'id': self.beta, 'project_id': self.beta}
        read = call('project_record_get', {'project_id': self.alpha})
        self.assertEqual(read['status'], 'ok', read)
        self.assertEqual(read['data']['project']['id'], self.alpha)
        listed = call('project_items_list', {'project_id': self.alpha})
        self.assertEqual(listed['status'], 'ok', listed)
        self.assertEqual(listed['data']['items'], [])
        found = call('project_items_candidates', {'project_id': self.alpha, 'source_project_id': None, 'type': 'project'})
        self.assertEqual(found['status'], 'ok', found)
        # Every other Project the caller reaches, never the Project itself.
        self.assertEqual({entry['id'] for entry in found['data']['items']}, reach - {self.alpha})
        shown = call('project_item_get', {'project_id': self.alpha, 'item': item})
        self.assertEqual(shown['status'], 'ok', shown)
        self.assertEqual(shown['data']['record']['project']['id'], self.beta)
        linked = call('project_items_link', {'project_id': self.alpha, 'item': item, 'idempotency_key': str(uuid4())})
        self.assertEqual(linked['status'], 'ok', linked)
        link = linked['data']['link']
        listed = call('project_items_list', {'project_id': self.alpha})
        self.assertEqual([entry['id'] for entry in listed['data']['items']], [self.beta], listed)
        # expected_revision is the unlink's own input as well as an option name; either way it reaches the handler.
        stale = call('project_items_unlink', {'project_id': self.alpha, 'link_id': link['id'],
                                              'expected_revision': link['revision'] + 1, 'idempotency_key': str(uuid4())})
        self.assertEqual(stale['error']['code'], 'revision_conflict', stale)
        removed = call('project_items_unlink', {'project_id': self.alpha, 'link_id': link['id'],
                                                'expected_revision': link['revision'], 'idempotency_key': str(uuid4())})
        self.assertEqual(removed['status'], 'ok', removed)
        self.assertEqual(removed['data'], {'removed': True})
        self.assertEqual(call('project_items_list', {'project_id': self.alpha})['data']['items'], [])


class OwnProjectRuleTests(Harness):
    def test_only_the_record_operations_and_agentic_access_own_the_field(self):
        own = {name for name in (*OPERATIONS, 'agentic_access', 'sample_scoped_read', 'sample_selector_read',
                                 'sample_global_read') if mcp.own_project(self.registry.get(name))}
        self.assertEqual(own, {*OPERATIONS, 'agentic_access'})

    def test_the_advertised_schema_names_project_id_once_with_the_operations_own_definition(self):
        for name in OPERATIONS:
            with self.subTest(operation=name):
                declared = self.registry.get(name).input_schema
                advertised = mcp.tool_schema(self.registry, name)
                self.assertEqual(advertised['properties']['project_id'], mcp.plain(declared['properties']['project_id']))
                self.assertIn('description', advertised['properties']['project_id'])
                self.assertIn('project_id', advertised['required'])
        self.assertNotIn('project_id', mcp.tool_schema(self.registry, 'agentic_access')['required'])
        ordinary = mcp.tool_schema(self.registry, 'sample_scoped_read')
        self.assertEqual(ordinary['properties']['project_id'], mcp.OPTION_SCHEMAS['project_id'])
        self.assertNotIn('project_id', ordinary['required'])

    def test_the_record_gate_compares_the_selected_project_with_the_named_one(self):
        owner = self.policy.context_for(self.owner)
        chosen = access.selected_scope(owner, {'scope': 'global'}, self.alpha, None)
        self.assertEqual((chosen.project_id, chosen.review_project_id), (self.alpha, self.alpha))
        same = access.selected_scope(replace(owner, project_id=self.alpha), {}, self.alpha, None)
        self.assertEqual(same.project_id, self.alpha)
        with self.assertRaises(DomainError) as refused:
            access.selected_scope(replace(owner, project_id=self.beta), {}, self.alpha, None)
        self.assertEqual((refused.exception.code, refused.exception.message),
                         ('validation_error', 'The selected project must match project_id'))


class MCPTests(Harness):
    """The registered-agent MCP route. The agent's context is resolved from the scope option alone, and resolving it
    for a Project outside the agent's scope is refused there, before any handler."""

    def setUp(self):
        super().setUp()
        self.options = []
        base = InvocationContext(Principal(self.agent, 'agent'), allowed_project_ids={self.alpha, self.beta},
                                 global_capabilities=READS, profile_revision=1, source='mcp')

        def agent_context(principal, *, diagnostic=False, project_id=None, session_registration_id=None,
                          review_project_id=None):
            self.options.append(project_id)
            if project_id is not None and project_id not in base.allowed_project_ids:
                raise DomainError('forbidden', OUT_OF_SCOPE)
            return replace(base, project_id=project_id, review_project_id=review_project_id,
                           session_registration_id=session_registration_id)
        runtime = NS(registry=self.registry, application=self.app, agent_context=agent_context)
        self.transport = mcp.MCPTransport(runtime, None, None)
        self.actor = NS(principal=base.principal, registration_id=self.registration, credential_id=str(uuid4()),
                        delegation_binding_id=None, delegation_assignment_id=None, delegation_generation=None)

    def send(self, name, arguments):
        return asyncio.run(self.transport._single_result(name, arguments, self.actor))

    def test_each_record_operation_runs_in_the_project_it_names(self):
        seen = []

        def call(operation, arguments):
            del self.policy.scopes[:], self.options[:]
            result = self.send(operation, arguments)
            seen.append((operation, list(self.options), self.policy.scopes[:1]))
            return result
        self.records(call, {self.alpha, self.beta})
        # The argument is not the option: the caller's context is resolved with no Project selected, and the engine
        # selects the named one for the invocation.
        for operation, options, scopes in seen:
            self.assertEqual((options, scopes), ([None], [(operation, self.alpha)]), operation)

    def test_a_link_is_written_in_the_named_project(self):
        key = str(uuid4())
        linked = self.send('project_items_link', {'project_id': self.alpha, 'idempotency_key': key,
                                                  'item': {'type': 'project', 'id': self.beta, 'project_id': self.beta}})
        self.assertEqual(linked['status'], 'ok', linked)
        with self.db.read() as uow:
            receipts = [row[0] for row in uow.connection.execute(
                'SELECT target_namespace FROM idempotency_receipts WHERE idempotency_key=?', (key,))]
            links = [tuple(row) for row in uow.connection.execute(
                'SELECT project_id, target_type, target_id FROM project_item_links')]
        self.assertEqual(receipts, ['project:' + self.alpha])
        self.assertEqual(links, [(self.alpha, 'project', self.beta)])

    def test_a_record_operation_without_or_beyond_its_project_is_refused(self):
        missing = self.send('project_record_get', {})
        self.assertEqual(missing['error']['code'], 'validation_error', missing)
        self.assertIn('project_id', str(missing['error']['field_errors']))
        malformed = self.send('project_record_get', {'project_id': 'alpha'})
        self.assertEqual(list(malformed['error']['field_errors']), ['project_id'], malformed)
        # Gamma is outside the agent's scope: the engine has no Project to select, and no handler runs.
        beyond = self.send('project_record_get', {'project_id': self.gamma})
        self.assertEqual((beyond['error']['code'], beyond['error']['message']),
                         ('scope_required', 'Operation requires a selected project'), beyond)
        self.assertEqual(self.options, [None, None, None])

    def test_project_id_stays_the_scope_option_of_every_other_operation(self):
        for name in ('sample_scoped_read', 'sample_selector_read'):
            with self.subTest(operation=name):
                del self.options[:]
                result = self.send(name, {'project_id': self.alpha, 'label': 'x'})
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['data'], {'arguments': {'label': 'x'}, 'selected_project_id': self.alpha})
                self.assertEqual(self.options, [self.alpha])
                beyond = self.send(name, {'project_id': self.gamma})
                self.assertEqual(beyond['error']['message'], OUT_OF_SCOPE, beyond)
        del self.options[:]
        kept = self.send('sample_global_read', {'project_id': self.gamma})
        self.assertEqual(kept['data'], {'arguments': {'project_id': self.gamma}, 'selected_project_id': None}, kept)
        self.assertEqual(self.options, [None])

    def access(self, **context):
        with patch.object(native_access, 'current_agent_credential', lambda *a, **k: None):
            return self.send('agentic_access', {'operation': 'catalog.list', **context})

    def test_agentic_access_without_a_project(self):
        result = self.access(work_type='research')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['selection'], {'project_id': None, 'work_type': 'research'})
        self.assertEqual(self.selections, [{'work_type': 'research'}])
        self.assertEqual(self.options, [None])

    def test_agentic_access_with_a_project_inside_native_access(self):
        result = self.access(project_id=self.alpha, work_type='research')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['data']['selection'], {'project_id': self.alpha, 'work_type': 'research'})
        self.assertEqual(self.selections, [{'project_id': self.alpha, 'work_type': 'research'}])
        # It reads where the caller is: the Project is routing context, not the selected scope.
        self.assertEqual(self.options, [None])
        self.assertEqual(self.policy.scopes, [('agentic_access', None)])

    def test_agentic_access_refuses_a_project_outside_native_access_itself(self):
        for project in (self.beta, self.gamma):
            with self.subTest(project=project):
                result = self.access(project_id=project)
                self.assertEqual((result['error']['code'], result['error']['message']),
                                 ('forbidden', 'Project is outside current native access'), result)
        self.assertEqual(self.selections, [])
        malformed = self.access(project_id='ALPHA')
        self.assertEqual(malformed['status'], 'error', malformed)
        self.assertEqual(self.options, [None, None, None])


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class DashboardRouteTests(Harness):
    """The owner's root and project operation routes through the real ``HTTPAPI`` dispatch."""

    def setUp(self):
        super().setUp()
        from qorqut.transport.http import HTTPAPI
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = NS(database=self.db, require_authority=lambda: None, application=self.app, registry=self.registry,
                         policy=self.policy)
        api.compatibility, api.guards = None, ()
        api.authentication = NS(installation_owner=lambda: (NS(id=self.owner, display_name='Owner'),))
        api.origins = NS(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = NS(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args, **options: None
        api._report_transport_failure = lambda *args, **options: None

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route('/api/{path:path}', handler, methods=['GET', 'POST'])]))
        self.addCleanup(self.client.close)

    def post(self, path, body):
        return self.client.post('/api/' + path, content=json.dumps(body), headers={'Content-Type': 'application/json'}).json()

    def test_the_root_route_takes_each_record_operation_with_its_project(self):
        seen = []

        def call(operation, arguments):
            del self.policy.scopes[:]
            result = self.post('root/operations/' + operation, {'scope': 'global', **arguments})
            seen.append((operation, self.policy.scopes[:1]))
            return result
        self.records(call, {self.alpha, self.beta, self.gamma})
        for operation, scopes in seen:
            self.assertEqual(scopes, [(operation, self.alpha)])

    def test_the_root_route_still_refuses_a_scope_option(self):
        # tasks_list is not installed here; like any operation that does not own the field, its project_id is the option.
        for operation, body in (('tasks_list', {'project_id': self.alpha}),
                                ('tasks_list', {'review_project_id': self.alpha}),
                                ('project_record_get', {'scope': 'global', 'project_id': self.alpha,
                                                        'review_project_id': self.alpha})):
            with self.subTest(operation=operation, body=sorted(body)):
                refused = self.post('root/operations/' + operation, body)
                self.assertEqual((refused['error']['code'], refused['error']['message']),
                                 ('forbidden', 'This endpoint requires Global scope'), refused)

    def test_the_project_route_takes_each_record_operation_naming_its_own_project(self):
        self.records(lambda operation, arguments: self.post('project/alpha/operations/' + operation, arguments),
                     {self.alpha, self.beta, self.gamma})
        scoped = self.post('project/alpha/operations/project_record_get', {'scope': 'project', 'project_id': self.alpha})
        self.assertEqual(scoped['data']['project']['id'], self.alpha, scoped)

    def test_the_project_route_refuses_a_record_operation_naming_another_project(self):
        item = {'item': {'type': 'project', 'id': self.gamma, 'project_id': self.gamma}}
        bodies = {'project_record_get': {}, 'project_items_list': {}, 'project_items_candidates': {},
                  'project_item_get': item, 'project_items_link': {**item, 'idempotency_key': str(uuid4())},
                  'project_items_unlink': {'link_id': str(uuid4()), 'expected_revision': 1, 'idempotency_key': str(uuid4())}}
        self.assertEqual(set(bodies), set(OPERATIONS))
        for operation, body in bodies.items():
            with self.subTest(operation=operation):
                del self.policy.scopes[:]
                refused = self.post('project/alpha/operations/' + operation, {'project_id': self.beta, **body})
                self.assertEqual((refused['error']['code'], refused['error']['message']),
                                 ('forbidden', 'Operation target differs from the selected project'), refused)
                self.assertEqual(self.policy.scopes, [])

    def test_the_project_route_keeps_the_option_rule_for_every_other_operation(self):
        for name in ('sample_scoped_read', 'sample_selector_read'):
            with self.subTest(operation=name):
                same = self.post('project/alpha/operations/' + name, {'project_id': self.alpha, 'label': 'x'})
                self.assertEqual(same['status'], 'ok', same)
                self.assertEqual(same['data'], {'arguments': {'label': 'x'}, 'selected_project_id': self.alpha})
                other = self.post('project/alpha/operations/' + name, {'project_id': self.beta})
                self.assertEqual((other['error']['code'], other['error']['message']),
                                 ('forbidden', "The body cannot change the URL's selected project"), other)


if __name__ == '__main__':
    unittest.main()
