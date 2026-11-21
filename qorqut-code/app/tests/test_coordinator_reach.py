"""The configured coordinator reaches the operational records of every agent it manages; a peer still reaches only its own."""
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from qorqut_operations.actions.operations import ActionOperations
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from qorqut_operations.storage.database import Database

from qorqut.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from qorqut.background.integration import reads_whole_job
from qorqut.composition.assembly import host_assembly
from qorqut.domains.agent_context import AgentContext
from qorqut.inbox.service import HostInbox
from qorqut.management.service import ManagementService
from qorqut.service.changes import ChangeFeed

STAMP = '2026-11-20T00:00:00Z'
ROLES = {'coordinator': 'ordubashy', 'former': 'ordubashy', 'worker': 'global-worker', 'peer': 'global-worker',
         'applier': 'learning-applier'}


class CoordinatorReach(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.database = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.database.close)
        self.owner = new_id()
        self.ids = {name: new_id() for name in ROLES}
        with self.database.transaction() as uow:
            uow.repository('principals').insert({'id': self.owner, 'kind': 'human', 'display_name': 'Owner'})
            for name, role in ROLES.items():
                identity = self.ids[name]
                # The former coordinator is an earlier Ordubashy registration that no longer acts.
                status = 'inactive' if name == 'former' else 'active'
                uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': name})
                uow.repository('agent_profiles', project_id=None).insert({
                    'agent_id': identity, 'principal_id': identity, 'namespace': 'ecosystem', 'name': name,
                    'workspace': str(root / name), 'status': status,
                    'profile_json': {'name': name, 'status': status, 'runtime': 'claude-code', 'role': {'id': role},
                        'scope': {'kind': 'ecosystem', 'allowed_project_ids': [],
                                  'global_capabilities': ['read_global_context', 'manage_delegated_assignments']}}})
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(),
                'owner_principal_id': self.owner, 'management_coordinator_id': self.ids['coordinator']})

    def context(self, name, **changes):
        return InvocationContext(Principal(self.ids[name], 'agent', name), profile_revision=1,
            global_capabilities=frozenset({'read_global_context', 'manage_delegated_assignments'}),
            role_artifact_ref=ArtifactRef(ROLES[name], '1.83.0', 'sha256:' + 'a' * 64), **changes)

    def delegated(self, name):
        """The same principal acting inside one of its delegated assignments: never the coordinator in person."""
        return self.context(name, delegation_assignment_id=new_id(), delegation_generation=1,
                            delegation_binding_id=new_id())

    def refused(self, call):
        with self.assertRaises(DomainError) as refused:
            call()
        return refused.exception.code

    # -- management intake -------------------------------------------------------------------------------------------

    def management(self):
        return ManagementService(NS(current_context=lambda ctx, uow: ctx))

    def message(self, uow):
        """A direct message the worker sent the peer: the worker is its requester."""
        identity = new_id()
        event = uow.connection.execute(
            "INSERT INTO inbox_events(project_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(NULL,'agent_messages',?,?,'direct_message','Stuck',?)", (identity, self.ids['worker'], STAMP)).lastrowid
        uow.connection.execute(
            "INSERT INTO agent_messages(id,event_id,project_id,sender_id,recipient_id,thread_id,body,created_at,"
            "current_sender_id,current_recipient_id) VALUES(?,?,NULL,?,?,?,?,?,?,?)",
            (identity, event, self.ids['worker'], self.ids['peer'], identity, 'I am stuck', STAMP,
             self.ids['worker'], self.ids['peer']))
        return identity

    def routed(self, uow, coordinator):
        identity = new_id()
        uow.connection.execute(
            "INSERT INTO management_routing(id,source_type,source_id,project_id,coordinator_id,attention_destination,"
            "created_by,created_at,updated_at) VALUES(?,'agent_messages',?,NULL,?,'management',?,?,?)",
            (identity, self.message(uow), self.ids[coordinator], self.ids['worker'], STAMP, STAMP))
        return identity

    def test_the_coordinator_routes_a_source_that_is_not_in_intake_yet(self):
        service = self.management()
        with self.database.transaction() as uow:
            arguments = {'scope': 'global', 'source_type': 'agent_messages', 'source_id': self.message(uow),
                         'attention_destination': 'management'}
            plan = service.prepare_route(self.context('coordinator'), dict(arguments), None, uow)
            self.assertEqual(plan.operation_name, 'management_route')
            self.assertEqual(self.refused(lambda: service.prepare_route(self.context('peer'), dict(arguments), None, uow)),
                             'forbidden')

    def test_intake_routed_under_a_former_coordinator_stays_with_the_configured_one(self):
        service = self.management()
        with self.database.transaction() as uow:
            routing = self.routed(uow, 'former')
            coordinator, peer = self.context('coordinator'), self.context('peer')
            listed = service.list(coordinator, {'scope': 'global'}, None, uow)['items']
            self.assertEqual([item['id'] for item in listed], [routing])
            self.assertEqual(service.get(coordinator, {'routing_id': routing}, None, uow)['routing']['id'], routing)
            arguments = {'scope': 'global', 'routing_id': routing, 'disposition': 'resolved'}
            plan = service.prepare_resolve(coordinator, dict(arguments), NS(expected_revision=1), uow)
            self.assertEqual(plan.operation_name, 'management_resolve')
            # A peer that neither sent nor received authority over the item still sees none of it.
            self.assertEqual(service.list(peer, {'scope': 'global'}, None, uow)['items'], [])
            self.assertEqual(self.refused(lambda: service.get(peer, {'routing_id': routing}, None, uow)), 'not_found')

    # -- another agent's context, jobs and assignment results -------------------------------------------------------------

    def agent_context(self):
        def profile(principal, connection=None):
            return NS(data={'status': 'active'}, scope=NS(kind='ecosystem', projects=frozenset())), None
        return AgentContext(NS(), NS(profile=profile))

    def test_the_coordinator_inspects_a_worker_and_nobody_inspects_the_learning_applier(self):
        reader = self.agent_context()
        with self.database.read() as uow:
            coordinator = self.context('coordinator')
            self.assertEqual(reader.access(coordinator, {'agent_id': self.ids['worker']}, uow), self.ids['worker'])
            self.assertEqual(reader.access(coordinator, {}, uow), self.ids['coordinator'])
            for caller, target in (('coordinator', 'applier'), ('worker', 'peer'), ('peer', 'coordinator')):
                self.assertEqual(self.refused(lambda: reader.access(self.context(caller), {'agent_id': self.ids[target]}, uow)),
                                 'forbidden', (caller, target))
            delegated = self.delegated('coordinator')
            self.assertEqual(self.refused(lambda: reader.access(delegated, {'agent_id': self.ids['worker']}, uow)), 'forbidden')

    def test_the_coordinator_reads_a_job_another_agent_started_in_full(self):
        job = {'initiated_by_principal_id': self.ids['worker']}
        owner = InvocationContext(Principal(self.owner, 'human'), source='dashboard')
        with self.database.read() as uow:
            self.assertTrue(reads_whole_job(self.context('coordinator'), job, uow))
            self.assertTrue(reads_whole_job(self.context('worker'), job, uow))
            self.assertTrue(reads_whole_job(owner, job, uow))
            self.assertFalse(reads_whole_job(self.context('peer'), job, uow))
            self.assertFalse(reads_whole_job(self.delegated('coordinator'), job, uow))

    def test_the_coordinator_records_the_result_of_an_assignment_another_parent_created(self):
        assignment = {'id': new_id(), 'parent_principal_id': self.ids['former'], 'project_id': None, 'state': 'completed',
                      'result_json': {'summary': 'Done'}, 'scope_json': {'kind': 'global', 'allowed_project_ids': []}}
        actions = NS(application=NS(delegation=NS(assignment=lambda uow, identity: assignment)))
        arguments = {'delegation_assignment_id': assignment['id']}
        with self.database.read() as uow:
            found = ActionOperations.delegation(actions, self.context('coordinator'), uow, arguments, 'global-operational', [])
            self.assertEqual(found['id'], assignment['id'])
            self.assertEqual(self.refused(lambda: ActionOperations.delegation(
                actions, self.context('peer'), uow, arguments, 'global-operational', [])), 'not_found')

    # -- a blocked assignment of another parent --------------------------------------------------------------------------

    def test_the_coordinator_is_told_when_another_parents_assignment_blocks_and_can_read_the_notice(self):
        store = OperationalDelegation(clock=lambda: STAMP)
        operations = OperationalDelegationOperations(store)
        with self.database.transaction() as uow:
            assignment = store.create(uow, parent_principal_id=self.ids['worker'], project_id=None,
                scope={'kind': 'global', 'allowed_project_ids': [], 'record_ids': {}, 'source_ids': {}},
                objective='Stale work', plan={'steps': ['wait']}, allowed_operations=[])
            operations._notify_parent(uow, assignment, 'blocked', 'waiting for a file')
            told = {row[0] for row in uow.connection.execute(
                "SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id WHERE e.source_id=?",
                (assignment['id'],))}
            self.assertEqual(told, {self.ids['worker'], self.ids['coordinator']})
            event = dict(uow.connection.execute("SELECT * FROM inbox_events WHERE source_id=?", (assignment['id'],)).fetchone())
            inbox = HostInbox(NS(app=NS(authorize=lambda *a, **k: None, registry=NS(get=lambda name: name))))
            self.assertTrue(inbox.event_visible(self.context('coordinator'), event, uow))
            self.assertTrue(inbox.event_visible(self.context('worker'), event, uow))
            self.assertFalse(inbox.event_visible(self.context('peer'), event, uow))
            item = {}
            inbox.decorate_after_extensions(self.context('coordinator'), event, item, uow)
            self.assertEqual(item['assignment']['id'], assignment['id'])
            # A decision is the parent's own news: the coordinator that made it is not told about itself.
            operations._notify_parent(uow, assignment, 'cancelled', 'decided by the coordinator')
            decided = [row[0] for row in uow.connection.execute(
                "SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
                "WHERE e.source_id=? AND e.event_type='cancelled'", (assignment['id'],))]
            self.assertEqual(decided, [self.ids['worker']])


if __name__ == '__main__':
    unittest.main()
