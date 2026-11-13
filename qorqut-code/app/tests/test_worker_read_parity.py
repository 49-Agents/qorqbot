"""A global worker reads what the configured Ordubashy's role reads; its writes do not change.

Stubs over SQLite and a small registry of real descriptors, evaluated by the host's real policy rules."""
import json
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut_operations.application import Application, OperationDescriptor
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal

from qorqut.agents import read_parity
from qorqut.agents.operations import AgentOperations
from qorqut.agents.resolver import AgentConfigResolver, effective_tool_policy
from qorqut.composition.policy import resolve_operation_policy

OWNER, ORDUBASHY, WORKER, APPLIER = (f'00000000-0000-4000-8000-00000000000{n}' for n in range(1, 5))
ORDUBASHY_REF = {'id': 'ordubashy', 'version': '1.0.0', 'digest': 'sha256:' + 'a' * 64}
WORKER_REF = {'id': 'global-worker', 'version': '1.0.0', 'digest': 'sha256:' + 'b' * 64}
APPLIER_REF = {'id': 'learning-applier', 'version': '1.0.0', 'digest': 'sha256:' + 'c' * 64}
SCOPES = ('business', 'global')

ORDUBASHY_DEFAULTS = {
    'tasks_get': 'allow', 'tasks_update': 'allow', 'actions_get': 'allow', 'actions_record': 'allow',
    'notes_list': 'allow', 'tables_get': 'allow', 'tables_rows_write': 'allow', 'environment_get': 'allow',
    'learnings_list': 'deny', 'calendar_events_list': 'allow', 'calendar_event_create': 'allow',
}
WORKER_DEFAULTS = {'tasks_get': 'allow', 'tasks_update': 'allow'}
READS = {'tasks_get', 'actions_get', 'notes_list', 'tables_get', 'environment_get', 'learnings_list',
         'calendar_events_list', 'fastmail_thread_get'}


def build_registry():
    registry = OperationRegistry()
    registry.extension_global_capabilities = frozenset({'manage_fastmail'})

    def read(name, **options):
        registry.register(OperationDescriptor(name, {'type': 'object'}, effect_kind='read', supports_signoff=False,
                                              read=lambda *a: {}, **options))

    def change(name, **options):
        registry.register(OperationDescriptor(name, {'type': 'object'}, effect_kind='records',
                                              prepare=lambda *a: None, apply=lambda *a: None, **options))

    for name in ('tasks_get', 'actions_get', 'notes_list', 'environment_get', 'learnings_list'):
        read(name, scope_kinds={'business', 'global'}, global_capability='read_global_context')
    read('tables_get', scope_kinds={'business', 'global'}, global_capability='read_global_context')
    private = dict(scope_kinds={'global'}, global_read_capability_alternatives=frozenset({'read_global_context'}))
    read('calendar_events_list', global_capability='manage_calendar', **private)
    read('fastmail_thread_get', global_capability='manage_fastmail', default_mode='allow', **private)
    for name in ('tasks_update', 'actions_record', 'actions_follow', 'tables_follow', 'tables_rows_write'):
        change(name, scope_kinds={'business', 'global'}, global_capability='read_global_context')
    change('calendar_event_create', scope_kinds={'global'}, global_capability='manage_calendar')
    change('fastmail_message_update', scope_kinds={'global'}, global_capability='manage_fastmail', default_mode='allow')
    return registry


def modes(registry, defaults, overrides, name, role='global-worker'):
    return tuple(resolve_operation_policy(name, defaults, overrides, registry, role_id=role, scope=scope).mode
                 for scope in SCOPES)


class ParityHelperTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_registry()

    def parity(self, overrides=None, role='global-worker', ordubashy=ORDUBASHY_DEFAULTS):
        return read_parity.worker_parity_defaults(role, WORKER_DEFAULTS, overrides or {}, ordubashy, self.registry)

    def test_only_ordubashy_allowed_plain_reads_are_added(self):
        result = self.parity()
        self.assertEqual(result.added, {'actions_get', 'notes_list', 'tables_get', 'calendar_events_list'})
        # A write Ordubashy's role allows, a read it denies and the secret-bearing environment read stay out.
        for name in ('actions_record', 'tables_rows_write', 'calendar_event_create', 'learnings_list', 'environment_get'):
            self.assertNotIn(name, result.added)
        for name in result.added:
            self.assertEqual(modes(self.registry, result.defaults, {}, name), ('allow', 'allow'), name)

    def test_owner_only_operations_are_never_added(self):
        registry = OperationRegistry()
        registry.register(OperationDescriptor('owner_secret_get', {'type': 'object'}, effect_kind='read', supports_signoff=False,
                                              owner_only=True, exposure={'dashboard'}, read=lambda *a: {}))
        self.assertEqual(read_parity.parity_reads({}, {'owner_secret_get': 'allow'}, registry), frozenset())

    def test_no_non_read_operation_changes_mode(self):
        result = self.parity()
        for entry in self.registry.entries():
            if entry.effect_kind != 'read':
                self.assertEqual(modes(self.registry, result.defaults, {}, entry.name),
                                 modes(self.registry, WORKER_DEFAULTS, {}, entry.name), entry.name)
        # Following an Action or a Table is decided from its read grant; it is pinned to what it was.
        self.assertEqual(result.pinned, {'actions_follow', 'tables_follow'})
        self.assertEqual(modes(self.registry, result.defaults, {}, 'actions_follow'), ('deny', 'deny'))

    def test_an_explicit_deny_override_wins(self):
        overrides = {'actions_get': 'deny'}
        result = self.parity(overrides)
        self.assertEqual(modes(self.registry, result.defaults, overrides, 'actions_get'), ('deny', 'deny'))

    def test_an_override_that_a_read_would_widen_keeps_that_read_out(self):
        # tables_follow's own grant is capped by tables_get. Adding tables_get would lift this explicit allow, so
        # tables_get is left out rather than admit the worker to a write it could not do before.
        overrides = {'tables_follow': 'allow'}
        result = self.parity(overrides)
        self.assertNotIn('tables_get', result.added)
        self.assertEqual(modes(self.registry, result.defaults, overrides, 'tables_follow'),
                         modes(self.registry, WORKER_DEFAULTS, overrides, 'tables_follow'))
        self.assertIn('actions_get', result.added)

    def test_without_a_configured_ordubashy_nothing_is_added(self):
        result = self.parity(ordubashy=None)
        self.assertEqual((dict(result.defaults), result.added), (WORKER_DEFAULTS, frozenset()))

    def test_other_roles_are_unchanged(self):
        for role in ('learning-applier', 'ordubashy'):
            result = self.parity(role=role)
            self.assertEqual((dict(result.defaults), result.added), (WORKER_DEFAULTS, frozenset()), role)

    def test_global_table_reads_follow_the_business_decision_for_a_worker_only(self):
        result = self.parity()
        self.assertEqual(modes(self.registry, result.defaults, {}, 'tables_get'), ('allow', 'allow'))
        self.assertEqual(modes(self.registry, result.defaults, {}, 'tables_rows_write'), ('deny', 'deny'))
        # Another role's business default still never carries into the Global table namespace.
        self.assertEqual(modes(self.registry, {'tables_get': 'allow'}, {}, 'tables_get', role='learning-applier'),
                         ('allow', 'deny'))


def profile_stub(agent_id, ref, capabilities=('read_global_context',), overrides=None, plugins=()):
    data = {'agent_id': agent_id, 'status': 'active', 'role': dict(ref), 'plugins': list(plugins),
            'tool_policy': {'overrides': dict(overrides or {})},
            'scope': {'kind': 'ecosystem', 'business_id': None, 'allowed_business_ids': [],
                      'global_capabilities': list(capabilities)}}
    return NS(data=data, revision=1, agent_id=agent_id,
              scope=NS(kind='ecosystem', business_id=None, global_capabilities=frozenset(capabilities)))


class ResolverParityTests(unittest.TestCase):
    """Discovery, orientation and call-time enforcement read the same parity defaults."""

    def setUp(self):
        self.registry = build_registry()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE installation(id TEXT PRIMARY KEY, owner_principal_id TEXT, management_coordinator_id TEXT);"
            "CREATE TABLE principals(id TEXT PRIMARY KEY, kind TEXT, active INTEGER);"
            "CREATE TABLE agent_profiles(agent_id TEXT PRIMARY KEY, home_business_id TEXT, status TEXT, profile_json TEXT, revision INTEGER);")
        self.conn.execute("INSERT INTO installation VALUES('service',?,?)", (OWNER, ORDUBASHY))
        for agent, ref in ((ORDUBASHY, ORDUBASHY_REF), (WORKER, WORKER_REF), (APPLIER, APPLIER_REF)):
            self.conn.execute("INSERT INTO principals VALUES(?, 'agent', 1)", (agent,))
            self.conn.execute("INSERT INTO agent_profiles VALUES(?, NULL, 'active', ?, 1)",
                              (agent, json.dumps(profile_stub(agent, ref).data)))
        roles = {'ordubashy': ORDUBASHY_DEFAULTS, 'global-worker': WORKER_DEFAULTS, 'learning-applier': WORKER_DEFAULTS}
        self.catalog = NS(get=lambda kind, ref, connection=None: NS(
            id=ref['id'], ref=dict(ref), manifest={'default_tool_policy': roles[ref['id']]}))
        self.resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        self.resolver.registry, self.resolver.catalog = self.registry, self.catalog
        self.uow = NS(connection=self.conn)

    def basis(self, profile):
        with patch.object(AgentConfigResolver, 'profile', lambda self, principal, connection=None: (profile, {})):
            return self.resolver.policy_basis(InvocationContext(Principal(profile.agent_id, 'agent')), self.uow)

    def rows(self, profile):
        role = self.catalog.get('role', profile.data['role'])
        parity = self.resolver.parity_defaults(profile, role, self.conn)
        return {row['canonical_operation']: row for row in effective_tool_policy(profile, role, self.registry, parity=parity)}

    def test_discovery_and_enforcement_agree(self):
        worker = profile_stub(WORKER, WORKER_REF)
        basis = self.basis(worker)
        rows = self.rows(worker)
        for name in ('actions_get', 'notes_list', 'tables_get', 'calendar_events_list', 'fastmail_thread_get',
                     'tasks_update', 'actions_record', 'actions_follow', 'tables_follow', 'tables_rows_write'):
            # The mode discovery reports is the mode the engine resolves from the policy basis.
            self.assertEqual(rows[name]['effective_mode'],
                             resolve_operation_policy(name, basis.defaults, {}, self.registry, role_id='global-worker').mode, name)
        for name in ('actions_get', 'notes_list', 'tables_get', 'calendar_events_list', 'fastmail_thread_get'):
            self.assertTrue(rows[name]['available'], name)
        self.assertEqual(rows['actions_get']['source'], {'kind': 'worker_read_parity'})
        self.assertEqual(rows['tasks_get']['source']['kind'], 'role_default')
        self.assertEqual(rows['tables_get']['scope_modes'], {'business': 'allow', 'global': 'allow'})
        for name in ('actions_record', 'actions_follow', 'tables_rows_write'):
            self.assertFalse(rows[name]['available'], name)
        # Mail and calendar changes still need the write grant, and say so.
        for name, capability in (('calendar_event_create', 'manage_calendar'), ('fastmail_message_update', 'manage_fastmail')):
            self.assertEqual((rows[name]['denial_reason'], rows[name]['missing_capability']), ('capability_not_granted', capability))

    def test_a_changed_or_missing_coordinator_changes_what_is_mirrored(self):
        worker = profile_stub(WORKER, WORKER_REF)
        self.assertEqual(self.basis(worker).defaults['notes_list'], 'allow')
        self.conn.execute("UPDATE installation SET management_coordinator_id=?", (APPLIER,))  # not an Ordubashy
        self.assertNotIn('notes_list', self.basis(worker).defaults)
        self.assertEqual(self.rows(worker)['notes_list']['denial_reason'], 'policy_denied')

    def test_a_private_account_read_is_offered_only_to_workers_and_ordubashy(self):
        rows = self.rows(profile_stub(APPLIER, APPLIER_REF))
        self.assertEqual(rows['fastmail_thread_get']['missing_capability'], 'manage_fastmail')
        self.assertFalse(rows['fastmail_thread_get']['available'])

    def test_the_incident_names_the_missing_capability(self):
        # Sentinel: a worker with allow overrides on mail but without manage_fastmail.
        overrides = {'fastmail_thread_get': 'allow', 'fastmail_message_update': 'allow'}
        worker = profile_stub(WORKER, WORKER_REF, overrides=overrides)
        rows = self.rows(worker)
        self.assertTrue(rows['fastmail_thread_get']['available'])
        self.assertEqual(rows['fastmail_message_update']['missing_capability'], 'manage_fastmail')
        context = InvocationContext(Principal(WORKER, 'agent'), global_capabilities=frozenset({'read_global_context'}))
        self.assertEqual(Application.namespace(None, context, self.registry.get('fastmail_thread_get'), {}), 'global')
        with self.assertRaises(DomainError) as refused:
            Application.namespace(None, context, self.registry.get('fastmail_message_update'), {})
        self.assertEqual(refused.exception.code, 'forbidden')
        self.assertIn('manage_fastmail', refused.exception.message)
        self.assertEqual(refused.exception.details['missing_capability'], 'manage_fastmail')
        # agents_update accepts the profile and reports the override that cannot take effect.
        operations = AgentOperations.__new__(AgentOperations)
        operations.resolver = NS(registry=self.registry)
        notices = operations._inert_overrides(worker, NS(to_dict=lambda: {'tools': list(rows.values())}))
        self.assertEqual([(item['operation'], item['missing_capability']) for item in notices],
                         [('fastmail_message_update', 'manage_fastmail')])
        self.assertIn('manage_fastmail', notices[0]['message'])


class PrivateAccountAccessTests(unittest.TestCase):
    """Mail and calendar handlers: reads with read_global_context, changes only with the manage grant."""

    def setUp(self):
        profiles = {WORKER: profile_stub(WORKER, WORKER_REF), APPLIER: profile_stub(APPLIER, APPLIER_REF),
                    ORDUBASHY: profile_stub(ORDUBASHY, ORDUBASHY_REF)}
        self.resolver = NS(profile=lambda principal, connection=None: (profiles[principal.id], {}))
        installation = {'owner_principal_id': OWNER}
        self.uow = NS(connection=None, repository=lambda name: NS(get=lambda key: installation))
        for target, value in (('qorqut.management.ManagementService._coordinator', staticmethod(lambda uow: ORDUBASHY)),
                              ('qorqut.agents.worker_access.is_worker', lambda connection, agent: agent == WORKER),
                              ('qorqut.calendar.operations.is_worker', lambda connection, agent: agent == WORKER)):
            patch(target, value).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def ctx(agent, *capabilities, kind='agent'):
        return InvocationContext(Principal(agent, kind), global_capabilities=frozenset(capabilities))

    def checks(self):
        from qorqut_fastmail.operations import account_authority
        from qorqut.calendar.operations import CalendarOperations
        calendar = CalendarOperations(NS(resolver=self.resolver))
        return (('fastmail', lambda ctx, read: account_authority(ctx, self.uow, self.resolver, read=read), 'manage_fastmail'),
                ('calendar', lambda ctx, read: calendar.access(ctx, self.uow, read=read), 'manage_calendar'))

    def test_a_worker_reads_without_the_manage_grant_and_cannot_change(self):
        for label, check, capability in self.checks():
            check(self.ctx(WORKER, 'read_global_context'), True)
            with self.assertRaises(DomainError) as refused:
                check(self.ctx(WORKER, 'read_global_context'), False)
            self.assertIn(capability, refused.exception.message, label)
            check(self.ctx(WORKER, 'read_global_context', capability), False)  # the explicit grant still works

    def test_another_role_holding_read_global_context_is_still_refused(self):
        for label, check, _ in self.checks():
            with self.assertRaises(DomainError, msg=label):
                check(self.ctx(APPLIER, 'read_global_context'), True)
            with self.assertRaises(DomainError, msg=label):
                check(self.ctx('00000000-0000-4000-8000-000000000009', 'read_global_context', kind='human'), True)
            check(self.ctx(ORDUBASHY, 'read_global_context'), True)
            check(self.ctx(OWNER, 'read_global_context', kind='human'), True)

    def test_only_the_reads_declare_the_read_alternative(self):
        from qorqut.calendar.operations import CalendarOperations, READS as CALENDAR_READS
        from qorqut_fastmail.operations import FastmailOperations, READS as MAIL_READS
        registry = OperationRegistry()
        CalendarOperations(NS(registry=registry)).register()
        FastmailOperations(NS(programs=None), NS()).register(registry)
        for descriptor in registry.installed():
            reading = descriptor.name in (*CALENDAR_READS, *MAIL_READS)
            self.assertEqual(descriptor.global_read_capability_alternatives,
                             frozenset({'read_global_context'}) if reading else frozenset(), descriptor.name)
            self.assertEqual(descriptor.global_capability_selected({'read_global_context'}) is not None, reading, descriptor.name)


if __name__ == '__main__':
    unittest.main()
