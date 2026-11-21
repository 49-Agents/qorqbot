"""Who may act in a project: the installation owner and system workers in every active project, nobody in an archived one."""
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from qorqut.agents.global_scope import active_project_participant
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.background import BackgroundAuthority, Program, ProgramRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.project_lifecycle import owner_may_act, owner_project_ids
from qorqut_operations.storage.database import Database

STAMP = '2026-11-10T12:00:00.000000Z'


class ProjectReachRuleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Database(Path(folder.name).resolve() / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.guest, self.active, self.archived = (str(uuid4()) for _ in range(4))
        programs = ProgramRegistry()
        programs.register(Program('probe.project', 1, {'type': 'object'}, lambda *a: None))
        self.authority = BackgroundAuthority(self.db, programs)
        with self.db.transaction() as uow:
            for identity, name in ((self.owner, 'Owner'), (self.guest, 'Guest')):
                uow.repository('principals').insert(dict(id=identity, kind='human', display_name=name, active=1,
                                                         created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()), owner_principal_id=self.owner,
                                                       created_at=STAMP, updated_at=STAMP))
            for identity, slug in ((self.active, 'active'), (self.archived, 'closed')):
                uow.repository('projects').insert({'id': identity, 'slug': slug, 'name': slug, 'workspace': '/' + slug})
            uow.connection.execute('UPDATE projects SET archived_at=? WHERE id=?', (STAMP, self.archived))
            self.worker = self.authority.register_workers(uow)[0]
        self.policy = PolicyResolver(OperationRegistry(()), rules=PolicyRules(), worker_profiles=self.authority.worker_profiles)

    def context(self, identity, kind, project=None, **extra):
        return InvocationContext(Principal(identity, kind), project_id=project, **extra)

    def refreshed(self, identity, kind, project=None, **extra):
        with self.db.read() as uow:
            return self.policy.refresh_context(self.context(identity, kind, project, **extra), uow)

    def test_the_owner_reaches_an_active_project(self):
        with self.db.read() as uow:
            self.assertTrue(owner_may_act(uow.connection, self.owner, self.active))
            self.assertEqual(owner_project_ids(uow.connection, self.owner), frozenset({self.active}))
            self.assertTrue(active_project_participant(uow.connection, self.active, self.owner))
        context = self.refreshed(self.owner, 'human', self.active, allowed_project_ids=frozenset({self.active, self.archived}),
                                 source='dashboard')
        self.assertEqual(context.allowed_project_ids, frozenset({self.active}))

    def test_the_owner_does_not_reach_an_archived_project(self):
        with self.db.read() as uow:
            self.assertFalse(owner_may_act(uow.connection, self.owner, self.archived))
            self.assertFalse(active_project_participant(uow.connection, self.archived, self.owner))
        with self.assertRaises(DomainError) as refused:
            self.refreshed(self.owner, 'human', self.archived, allowed_project_ids=frozenset({self.archived}), source='dashboard')
        self.assertEqual(refused.exception.code, 'project_archived')

    def test_a_person_who_is_not_the_owner_reaches_no_project(self):
        with self.db.read() as uow:
            self.assertFalse(owner_may_act(uow.connection, self.guest, self.active))
            self.assertEqual(owner_project_ids(uow.connection, self.guest), frozenset())
            self.assertFalse(active_project_participant(uow.connection, self.active, self.guest))
        context = self.refreshed(self.guest, 'human', None, allowed_project_ids=frozenset({self.active}), source='dashboard')
        self.assertEqual(context.allowed_project_ids, frozenset())

    def test_a_project_scoped_worker_reaches_an_active_project(self):
        with self.db.read() as uow:
            self.assertEqual(self.authority.worker_profiles(self.worker, uow).project_ids, frozenset({self.active}))
            self.authority.require_worker(self.worker, self.active, uow)
            self.assertTrue(active_project_participant(uow.connection, self.active, self.worker))
        context = self.refreshed(self.worker, 'system', self.active, source='worker')
        self.assertEqual(context.allowed_project_ids, frozenset({self.active}))

    def test_a_project_scoped_worker_does_not_reach_an_archived_project(self):
        with self.db.read() as uow:
            with self.assertRaises(DomainError) as refused:
                self.authority.require_worker(self.worker, self.archived, uow)
            self.assertEqual(refused.exception.code, 'forbidden')
            self.assertFalse(active_project_participant(uow.connection, self.archived, self.worker))
        with self.assertRaises(DomainError):
            self.refreshed(self.worker, 'system', self.archived, source='worker')

    def test_the_policy_allows_the_owner_in_an_active_project_and_refuses_a_person_who_is_not_the_owner(self):
        descriptor = type('Descriptor', (), {'owner_only': False, 'global_capability': None, 'effect_kind': 'compound',
                                              'name': 'probe', 'global_capability_evidence': lambda self, caps: {}})()
        with self.db.read() as uow:
            owner = self.policy.resolve(self.refreshed(self.owner, 'human', self.active, source='dashboard',
                                                       allowed_project_ids=frozenset({self.active})), descriptor, (), uow)
            with self.assertRaises(DomainError) as refused:
                self.policy.resolve(self.context(self.guest, 'human', self.active, source='dashboard',
                                                 allowed_project_ids=frozenset({self.active})), descriptor, (), uow)
        self.assertEqual((owner.mode, owner.provenance), ('allow', 'owner'))
        self.assertEqual(refused.exception.code, 'forbidden')

    def test_the_owner_keeps_per_project_person_details(self):
        from qorqut_operations.documents.people import details
        with self.db.transaction() as uow:
            row = uow.repository('principals').get(self.owner)
            self.assertEqual(details(row, uow, project_id=self.active)['affiliation'], 'internal')
            uow.repository('project_person_details', project_id=self.active).insert(
                {'principal_id': self.owner, 'details_json': {'summary': 'Founder', 'contact': {'email': 'o@example.com'}}})
        with self.db.read() as uow:
            row = uow.repository('principals').get(self.owner)
            shown = details(row, uow, project_id=self.active)
            self.assertEqual((shown['summary'], shown['contact']['email']), ('Founder', 'o@example.com'))
            self.assertEqual(details(row, uow, project_id=self.archived)['summary'], '')
            triggers = uow.connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'inbox_capture_project_person_details_%'")
            self.assertEqual(len(triggers.fetchall()), 3)


if __name__ == '__main__':
    unittest.main()
