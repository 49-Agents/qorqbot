"""Subprojects: real Projects nested inside Projects, to any depth.

Creation under a parent (its folder inside the parent's folder), re-parenting with the folder moving on disk, the refusals
(a cycle, an archived parent, an archived subproject's parent), the archive cascade and the restore of exactly what went
with it, the Project read's path and children, and the file Explorer's badges."""
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_operations.application import Application
from qorqut_operations.contracts import (DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal,
                                         new_id, plain)
from qorqut_operations.project_lifecycle import active_project_ids
from qorqut_operations.projects import hierarchy
from qorqut_operations.projects.archive import ProjectArchive
from qorqut_operations.projects.creation import ProjectCreationOperations
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.executor import PreparedFileExecutor
from qorqut_operations.storage.files import FileApplier
from qorqut_operations.storage.locks import LockPaths

from qorqut.composition.assembly import host_assembly
from qorqut.composition.catalog import builtin_registry
from qorqut.composition.files import FileTargetResolver
from qorqut.composition.locations import project_root, relative_to_operations
from qorqut.domains.projects import access
from qorqut.domains.projects.move import ProjectMove
from qorqut.file_viewer import explorer
from qorqut.instance_paths import InstancePaths, PROJECTS_DIRECTORY
from qorqut.operations_history.composition import PROJECT_EXCLUDED, registered_areas
from qorqut.service.changes import ChangeFeed


class Policy:
    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision('allow', 'test', {'global_capability': descriptor.global_capability})


def refresh(ctx, uow):
    return replace(ctx, allowed_project_ids=active_project_ids(uow.connection))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='project hierarchy ')).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.paths = InstancePaths(self.root, {})
        (self.root / 'operations' / PROJECTS_DIRECTORY).mkdir(parents=True)
        self.db = Database(self.root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        registry = builtin_registry()
        self.app = Application(self.db, registry, policy=Policy(), context_resolver=refresh)
        self.owner_id, self.coordinator_id = new_id(), new_id()
        self.creation = ProjectCreationOperations(
            self.app, operations_root=self.paths.operations, staging_root=self.root / 'runtime' / 'staging' / PROJECTS_DIRECTORY,
            project_root=lambda row: project_root(self.paths, row), relative_path=lambda path: relative_to_operations(self.paths, path),
            projects_directory=PROJECTS_DIRECTORY,
            creator_actor=lambda uow, identity: 'ordubashy' if identity == self.coordinator_id else None)
        self.creation.register(registry)
        targets = FileTargetResolver(lambda resource: self.creation.creation.directory_target(resource))
        self.app.file_executor = PreparedFileExecutor(self.db, FileApplier(self.db, targets.seal(), locks=LockPaths.under(self.root / 'locks')))
        self.released = []
        self.archive = ProjectArchive(self.app, release_todos=lambda uow, project_id, actor: self.released.append(project_id))
        self.archive.register(registry)
        ProjectMove(self.app, self.paths).register(registry)
        with self.db.transaction() as uow:
            for identity, kind in ((self.owner_id, 'human'), (self.coordinator_id, 'agent')):
                uow.repository('principals').insert({'id': identity, 'kind': kind, 'display_name': kind})
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(),
                'owner_principal_id': self.owner_id, 'management_coordinator_id': self.coordinator_id})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': self.coordinator_id, 'principal_id': self.coordinator_id,
                'name': 'Ordubashy', 'workspace': str(self.root / 'ordubashy'), 'status': 'active',
                'profile_json': {'name': 'Ordubashy', 'status': 'active', 'role': {'id': 'ordubashy'}, 'runtime': 'claude-code',
                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context', 'coordinate_projects']}}})
        self.owner = InvocationContext(Principal(self.owner_id, 'human'), source='dashboard', global_capabilities={'read_global_context'})
        self.coordinator = InvocationContext(Principal(self.coordinator_id, 'agent'), source='mcp', profile_revision=1,
                                             global_capabilities={'read_global_context', 'coordinate_projects'})

    def invoke(self, operation, args, ctx=None):
        result = self.app.invoke(ctx or self.coordinator, operation, args, InvocationOptions(idempotency_key=new_id()))
        value = plain(result)
        if value['status'] != 'ok':
            raise DomainError(value['error']['code'], value['error']['message'])
        return value['data']

    def create(self, name, parent=None):
        return self.invoke('project_create', {'name': name, **({'parent_project_id': parent} if parent else {})})['project']

    def row(self, project_id):
        with self.db.read() as uow:
            return uow.repository('projects').get(project_id)

    def folder(self, project_id):
        return project_root(self.paths, self.row(project_id))

    def archive_now(self, project_id):
        with self.db.read() as uow:
            revision = uow.repository('projects').get(project_id)['revision']
        result = self.app.invoke(self.owner, 'project_archive', {'project_id': project_id, 'rationale': 'Done'},
                                 InvocationOptions(idempotency_key=new_id(), expected_revision=revision))
        self.assertEqual(plain(result)['status'], 'ok', plain(result))

    def restore(self, project_id):
        with self.db.read() as uow:
            revision = uow.repository('projects').get(project_id)['revision']
        return plain(self.app.invoke(self.owner, 'project_restore', {'project_id': project_id},
                                     InvocationOptions(idempotency_key=new_id(), expected_revision=revision)))

    def archived(self):
        with self.db.read() as uow:
            return {row['name'] for row in uow.connection.execute('SELECT name FROM projects WHERE archived_at IS NOT NULL')}


class CreationAndMoves(Fixture):
    def test_a_subproject_is_a_project_whose_folder_sits_inside_its_parent_s_at_any_depth(self):
        top = self.create('Forty-nine')
        z = self.create('Z', top['id'])
        y = self.create('Y', z['id'])
        self.assertEqual((z['parent_id'], y['parent_id'], top['parent_id']), (top['id'], z['id'], None))
        operations = self.paths.operations
        self.assertEqual(self.folder(y['id']), operations / 'projects/forty-nine/subprojects/z/subprojects/y')
        self.assertTrue((self.folder(y['id']) / 'code').is_dir())
        self.assertEqual(self.row(y['id'])['workspace'], str(operations / 'projects/forty-nine/subprojects/z/subprojects/y'))
        self.assertEqual(y['workspace_path'], self.row(y['id'])['workspace'])
        with self.db.read() as uow:
            self.assertEqual([item['name'] for item in hierarchy.path_view(uow.connection, y['id'])], ['Forty-nine', 'Z', 'Y'])
            self.assertEqual([row['name'] for row in hierarchy.descendants(uow.connection, top['id'])], ['Z', 'Y'])

    def test_a_subproject_under_an_archived_or_missing_parent_is_refused(self):
        top = self.create('Alpha')
        with self.assertRaises(DomainError) as refusal:
            self.create('Beta', new_id())
        self.assertEqual(refusal.exception.code, 'not_found')
        self.archive_now(top['id'])
        with self.assertRaises(DomainError) as refusal:
            self.create('Beta', top['id'])
        self.assertEqual(refusal.exception.code, 'project_archived')

    def test_moving_a_project_moves_its_folder_and_its_subprojects_folders(self):
        a, b = self.create('A'), self.create('B')
        child = self.create('Child', a['id'])
        (self.folder(child['id']) / 'notes.md').write_text('kept')
        moved = self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': b['id']}, self.owner)
        self.assertEqual([item['name'] for item in moved['path']], ['B', 'A'])
        operations = self.paths.operations
        self.assertFalse((operations / 'projects/a').exists())
        self.assertEqual(self.folder(a['id']), operations / 'projects/b/subprojects/a')
        self.assertEqual((self.folder(child['id']) / 'notes.md').read_text(), 'kept')
        self.assertEqual(self.row(child['id'])['workspace'], str(operations / 'projects/b/subprojects/a/subprojects/child'))
        self.assertEqual(self.row(child['id'])['parent_id'], a['id'])
        # Back to the top level, by Ordubashy.
        self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': None})
        self.assertEqual((self.row(a['id'])['parent_id'], self.row(a['id'])['folder']), (None, None))
        self.assertEqual((self.folder(child['id']) / 'notes.md').read_text(), 'kept')
        self.assertEqual(self.folder(child['id']), operations / 'projects/a/subprojects/child')

    def test_a_move_under_itself_a_descendant_or_an_archived_project_is_refused(self):
        a = self.create('A')
        child = self.create('Child', a['id'])
        grandchild = self.create('Grandchild', child['id'])
        for parent in (a['id'], child['id'], grandchild['id']):
            with self.assertRaises(DomainError) as refusal:
                self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': parent}, self.owner)
            self.assertEqual(refusal.exception.code, 'validation_error')
        other = self.create('Other')
        self.archive_now(other['id'])
        with self.assertRaises(DomainError) as refusal:
            self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': other['id']})
        self.assertIn(refusal.exception.code, {'not_found', 'project_archived'})
        self.assertIsNone(self.row(a['id'])['parent_id'])

    def test_a_failed_folder_move_changes_nothing_in_the_store(self):
        a, b = self.create('A'), self.create('B')
        before = self.row(a['id'])
        with mock.patch('qorqut.domains.projects.move.move_directory', side_effect=DomainError('revision_conflict', 'occupied')):
            with self.assertRaises(DomainError):
                self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': b['id']}, self.owner)
        self.assertEqual(self.row(a['id']), before)
        self.assertTrue(self.folder(a['id']).is_dir())

    def test_only_the_owner_and_ordubashy_move_projects(self):
        a, b = self.create('A'), self.create('B')
        worker = new_id()
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': worker, 'kind': 'agent', 'display_name': 'worker'})
            uow.repository('agent_profiles', project_id=None).insert({'agent_id': worker, 'principal_id': worker,
                'name': 'Worker', 'workspace': str(self.root / 'worker'), 'status': 'active',
                'profile_json': {'name': 'Worker', 'status': 'active', 'role': {'id': 'global-worker'}, 'runtime': 'claude-code',
                    'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
        ctx = InvocationContext(Principal(worker, 'agent'), source='mcp', profile_revision=1, global_capabilities={'read_global_context'})
        with self.assertRaises(DomainError) as refusal:
            self.invoke('project_move', {'project_id': a['id'], 'parent_project_id': b['id']}, ctx)
        self.assertEqual(refusal.exception.code, 'forbidden')


class ArchiveCascade(Fixture):
    def test_archiving_a_parent_archives_its_subtree_and_restoring_it_returns_only_what_went_with_it(self):
        top = self.create('Top')
        own = self.create('Archived alone', top['id'])
        under_own = self.create('Under alone', own['id'])
        cascade = self.create('Goes along', top['id'])
        deep = self.create('Deep', cascade['id'])
        self.archive_now(own['id'])
        self.assertEqual(self.archived(), {'Archived alone', 'Under alone'})
        self.archive_now(top['id'])
        self.assertEqual(self.archived(), {'Top', 'Archived alone', 'Under alone', 'Goes along', 'Deep'})
        self.assertEqual((self.row(cascade['id'])['archived_with'], self.row(deep['id'])['archived_with'],
                          self.row(own['id'])['archived_with'], self.row(under_own['id'])['archived_with']),
                         (top['id'], top['id'], None, own['id']))
        self.assertEqual(set(self.released) >= {top['id'], cascade['id'], deep['id']}, True)
        # A subproject is not restored while its parent is archived.
        refused = self.restore(deep['id'])
        self.assertEqual((refused['status'], refused['error']['code']), ('error', 'project_archived'))
        self.assertEqual(self.restore(top['id'])['status'], 'ok')
        self.assertEqual(self.archived(), {'Archived alone', 'Under alone'})
        self.assertIsNone(self.row(deep['id'])['archived_with'])
        # The one archived on its own restores with what went with it.
        self.assertEqual(self.restore(own['id'])['status'], 'ok')
        self.assertEqual(self.archived(), set())


class Reads(Fixture):
    def test_the_project_read_names_its_path_and_its_active_subprojects(self):
        top = self.create('49')
        z = self.create('Z', top['id'])
        gone = self.create('Old', top['id'])
        self.archive_now(gone['id'])
        with self.db.read() as uow:
            view = access.project_view(uow.repository('projects').get(z['id']), uow)
            self.assertEqual((view['parent_id'], [item['name'] for item in view['path']]), (top['id'], ['49', 'Z']))
            parent = access.project_view(uow.repository('projects').get(top['id']), uow)
            self.assertEqual([item['name'] for item in parent['subprojects']], ['Z'])
            self.assertEqual(access.project_view(uow.repository('projects').get(top['id']))['parent_id'], None)

    def test_history_captures_a_subproject_inside_its_top_project_with_its_system_folders_left_out(self):
        top = self.create('Top')
        self.create('Child', top['id'])
        with self.db.read() as uow:
            areas = [area for area in registered_areas(uow) if area.kind == 'project_folder']
        self.assertEqual([area.project for area in areas], ['top'])
        self.assertIn('subprojects/child/code', areas[0].excluded)
        self.assertEqual(len(areas[0].excluded), 2 * len(PROJECT_EXCLUDED))

    def test_the_explorer_badges_subproject_folders_and_counts_subprojects(self):
        top = self.create('Top')
        child = self.create('Child', top['id'])
        signoffs = NS(_context=lambda ctx, uow: refresh(self.owner, uow), _visibility=lambda ctx, uow: ('1=1', []),
                      ports=NS(management=explorer.ABSENT))
        runtime = NS(database=self.db, signoffs=signoffs)
        found = explorer.project_metadata(runtime, lambda project: None, {'top', 'top/subprojects/child'})
        self.assertEqual((found['top']['subprojects'], found['top/subprojects/child']['subprojects']), (1, 0))
        self.assertEqual(found['top/subprojects/child']['id'], child['id'])
        self.assertEqual(explorer.project_folder_of('projects/top/subprojects/child/notes'), 'top/subprojects/child')
        self.assertEqual(explorer.project_folder_of('projects/top/subprojects'), 'top')
        self.assertEqual(explorer.project_folder_of('projects/top'), 'top')
        self.assertIsNone(explorer.project_folder_of('notes'))


if __name__ == '__main__':
    unittest.main()
