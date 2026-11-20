"""The configured coordinator reads and controls every parent's delegated assignments; any other parent only its own."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from qorqut_operations.storage.database import Database

from qorqut.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

STAMP = '2026-11-20T00:00:00Z'


class CoordinatorControlsAssignments(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.database = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.database.close)
        self.store = OperationalDelegation(clock=lambda: STAMP)
        self.operations = OperationalDelegationOperations(self.store)
        self.owner, self.coordinator, self.parent, self.other = [new_id() for _ in range(4)]
        with self.database.transaction() as uow:
            uow.repository('principals').insert({'id': self.owner, 'kind': 'human', 'display_name': 'Owner'})
            for identity in (self.coordinator, self.parent, self.other):
                uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': 'Agent'})
                uow.repository('agent_profiles', business_id=None).insert({
                    'agent_id': identity, 'principal_id': identity, 'namespace': 'ecosystem', 'name': identity,
                    'workspace': str(root / identity), 'status': 'active',
                    'profile_json': {'name': identity, 'status': 'active', 'runtime': 'claude-code',
                        'role': {'id': 'ordubashy' if identity == self.coordinator else 'global-worker'},
                        'scope': {'kind': 'ecosystem', 'allowed_business_ids': [],
                                  'global_capabilities': ['manage_delegated_assignments']}}})
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(),
                'owner_principal_id': self.owner, 'management_coordinator_id': self.coordinator})
            self.assignment = self.store.create(uow, parent_principal_id=self.parent, business_id=None,
                scope={'kind': 'global', 'allowed_business_ids': [], 'record_ids': {}, 'source_ids': {}},
                objective='Stale work', plan={'steps': ['wait']}, allowed_operations=[])

    def context(self, identity):
        return InvocationContext(Principal(identity, 'agent', 'Agent'), profile_revision=1,
            global_capabilities=frozenset({'manage_delegated_assignments'}),
            role_artifact_ref=ArtifactRef('ordubashy', '1.34.0', 'sha256:' + 'a' * 64))

    def control(self, identity, action, revision):
        context, arguments = self.context(identity), {'assignment_id': self.assignment['id'], 'action': action}
        with self.database.transaction() as uow:
            plan = self.operations.prepare_control(context, arguments, SimpleNamespace(expected_revision=revision), uow)
            return self.operations.apply(context, plan, uow)['assignment']

    def test_the_coordinator_cancels_another_parents_assignment_and_the_parent_is_told(self):
        cancelled = self.control(self.coordinator, 'cancel', self.assignment['revision'])
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(cancelled['parent_principal_id'], self.parent)
        with self.database.transaction() as uow:
            actor = uow.connection.execute("SELECT actor_principal_id FROM operational_assignment_events "
                "WHERE assignment_id=? AND event_type='cancelled'", (self.assignment['id'],)).fetchone()[0]
            told = uow.connection.execute("SELECT e.summary FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND e.source_id=?", (self.parent, self.assignment['id'])).fetchone()
        self.assertEqual(actor, self.coordinator)
        self.assertIn('decided by the coordinator', told[0])

    def test_the_coordinator_reads_and_lists_every_parents_assignments(self):
        context = self.context(self.coordinator)
        with self.database.transaction() as uow:
            read = self.operations.read('delegated_assignment_get', context, {'assignment_id': self.assignment['id']}, uow)
            own = self.operations.read('delegated_assignments_list', context, {}, uow)
            everyone = self.operations.read('delegated_assignments_list', context, {'all_parents': True}, uow)
        self.assertEqual(read['id'], self.assignment['id'])
        self.assertEqual(own['assignments'], [])
        self.assertEqual([row['id'] for row in everyone['assignments']], [self.assignment['id']])

    def test_another_agent_neither_sees_nor_controls_it_and_the_parent_still_does(self):
        context = self.context(self.other)
        with self.database.transaction() as uow:
            with self.assertRaises(DomainError) as refused:
                self.operations.read('delegated_assignment_get', context, {'assignment_id': self.assignment['id']}, uow)
            self.assertEqual(refused.exception.code, 'not_found')
            with self.assertRaises(DomainError) as refused:
                self.operations.read('delegated_assignments_list', context, {'all_parents': True}, uow)
            self.assertEqual(refused.exception.code, 'forbidden')
        with self.assertRaises(DomainError):
            self.control(self.other, 'cancel', self.assignment['revision'])
        paused = self.control(self.parent, 'pause', self.assignment['revision'])
        self.assertEqual(paused['state'], 'paused')
        with self.database.transaction() as uow:
            self.assertIsNone(uow.connection.execute("SELECT 1 FROM inbox_deliveries WHERE agent_id=?",
                                                     (self.parent,)).fetchone())


if __name__ == '__main__':
    unittest.main()
