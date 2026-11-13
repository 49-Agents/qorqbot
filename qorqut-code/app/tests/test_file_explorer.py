"""Filesystem boundaries, owner authority and live project badges for the Files pane."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from starlette.requests import Request

from qorqut.file_viewer import explorer, files
from qorqut.file_viewer.paths import normalize_path
from qorqut.file_viewer.service import FileViewer
from qorqut.transport.http import HTTPAPI
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.signoffs.ports import HostExecution, HostPeers, HostTasks, HostReferences
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from qorqut_operations.signoffs import ABSENT, SignoffPorts, SignoffService
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'operations'
        self.root.mkdir()
        self.roots = (str(self.root),)

    def read(self, **query):
        return explorer.listing(self.root, *explorer.query(list(query.items()), self.roots))

    def test_root_and_trailing_slashes_are_directory_only(self):
        self.assertEqual(explorer.query([('path', str(self.root) + '/')], self.roots)[0], '')
        self.assertEqual(explorer.query([('path', 'projects/./')], self.roots)[0], 'projects')
        for path in ['', 'projects/']:
            with self.assertRaises(DomainError):
                normalize_path(path, self.roots)

    def test_strict_query_rejects_traversal_outside_paths_and_ambiguous_queries(self):
        for path in ['../outside', 'projects/../../outside', '/etc', 'x\x00y', '%2e%2e/secret']:
            with self.subTest(path=path), self.assertRaises(DomainError):
                self.read(path=path)
        for query in [[('path', ''), ('path', 'a')], [('unknown', 'a')], [('hidden', 'yes')],
                      [('limit', '101')], [('limit', '9' * 5000)], [('cursor', 'nope')]]:
            with self.subTest(query=query), self.assertRaises(DomainError):
                explorer.query(query, self.roots)

    def test_pages_are_sorted_complete_and_bound_to_folder_and_hidden_setting(self):
        for name in ['Beta', 'alpha']:
            (self.root / name).mkdir()
        for name in ['z.txt', 'A.md', 'a.md', '.hidden', 'bad\nname']:
            (self.root / name).write_text('note')
        first = self.read(limit='2')
        self.assertEqual([row['name'] for row in first['entries']], ['alpha', 'Beta'])
        self.assertEqual((first['total'], first['skipped']), (5, 1))
        page, names = first, []
        while True:
            names.extend(row['name'] for row in page['entries'])
            if not page['next_cursor']:
                break
            page = self.read(limit='2', cursor=page['next_cursor'])
        self.assertEqual(names, ['alpha', 'Beta', 'A.md', 'a.md', 'z.txt'])
        self.assertEqual(self.read(hidden='1')['total'], 6)
        for options in [{'path':'alpha'}, {'hidden':'1'}]:
            with self.assertRaises(DomainError):
                self.read(cursor=first['next_cursor'], **options)

    def test_links_and_special_files_are_listed_without_being_followed(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'private.txt').write_text('outside root')
        (self.root / 'linked').symlink_to(outside, target_is_directory=True)
        os.mkfifo(self.root / 'pipe')
        self.assertEqual({row['name']:row['kind'] for row in self.read()['entries']}, {'linked':'symlink', 'pipe':'special'})
        for path in ['linked', 'linked/child']:
            with self.assertRaises(DomainError) as error:
                self.read(path=path)
            self.assertEqual(error.exception.code, 'symlink_not_followed')
        with self.assertRaises(DomainError):
            self.read(path='pipe')

    def test_component_replaced_by_a_link_during_open_cannot_escape(self):
        (self.root / 'folder').mkdir()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        original = files._open
        def race(parent, name, flags):
            if name == 'folder':
                (self.root / 'folder').rename(self.root / 'old')
                (self.root / 'folder').symlink_to(outside, target_is_directory=True)
            return original(parent, name, flags)
        with patch.object(files, '_open', side_effect=race), self.assertRaises(DomainError) as error:
            self.read(path='folder')
        self.assertEqual(error.exception.code, 'symlink_not_followed')

    def test_scan_limit_is_an_error_rather_than_a_partial_listing(self):
        for i in range(3):
            (self.root / str(i)).touch()
        with patch.object(explorer, 'MAX_ENTRIES', 2), self.assertRaises(DomainError) as error:
            self.read(limit='1')
        self.assertEqual(error.exception.code, 'directory_too_large')


class ProjectBadgesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'state.sqlite', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.project, self.archived, self.denied = [new_id() for _ in range(4)]
        self.context = InvocationContext(Principal(self.owner, 'human'), source='dashboard',
            allowed_business_ids=frozenset({self.project, self.archived}), global_capabilities=frozenset({'read_global_context'}))
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id':self.owner, 'kind':'human', 'display_name':'Example owner'})
            uow.repository('installation').insert({'id':'service', 'installation_id':new_id(), 'owner_principal_id':self.owner})
            for identity, slug in [(self.project, 'example'), (self.archived, 'archived'), (self.denied, 'denied')]:
                (self.root / 'projects' / slug / 'hq').mkdir(parents=True)
                uow.repository('businesses').insert({'id':identity, 'slug':slug, 'name':slug.title(), 'workspace':slug,
                    'hq_path':slug + '/hq', 'config_json':{'one_liner':'Current project description'},
                    'archived_at':'2026-01-01T00:00:00Z' if identity == self.archived else None})
                uow.repository('business_memberships', business_id=identity).insert({'principal_id':self.owner, 'is_owner':True})
            for i, (status, archived, kind, snoozed) in enumerate([
                ('open', False, 'task', None), ('working', False, 'task', None),
                ('needs_input', False, 'task', '2099-01-01T00:00:00Z'),
                ('done', False, 'task', None), ('cancelled', False, 'task', None),
                ('open', True, 'task', None), ('open', False, 'story', None), ('open', False, 'email', None),
            ]):
                uow.repository('tasks', business_id=self.project).insert({'id':f't{i}', 'title':'Example task',
                    'created_by':self.owner, 'status':status, 'archived':archived, 'snoozed_until':snoozed,
                    'fields_json':{'$task':{'task_type':kind}}})
        ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
            platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
        self.signoffs = SignoffService(self.db, ports, locks=LockPaths.under(self.root / 'locks'))
        self.signoffs.bind(NS(database=self.db, signoffs=self.signoffs, current_context=lambda ctx, uow:ctx))
        self.runtime = NS(root=self.root, database=self.db, signoffs=self.signoffs)
        self.viewer = FileViewer(self.runtime, lambda _:self.context)
        self.sequence = 0

    def signoff(self, **fields):
        self.sequence += 1
        values = dict(id=new_id(), review_business_id=self.project, review_scope_kind='business',
            proposer_principal_id=self.owner, operation_name='example', operation_version=1, proposal_revision=1,
            payload_hash='hash', change_set=b'{}', preview_json='{}', expected_targets_json='[]', policy_binding_json='{}',
            affected_scope_json='{"kind":"business"}', state='pending', updated_sequence=self.sequence,
            created_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T00:00:00Z')
        values.update(fields)
        with self.db.transaction() as uow:
            uow.connection.execute(f"INSERT INTO signoffs ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", list(values.values()))
        return values['id']

    def test_live_counts_use_task_type_lifecycle_scope_and_signoff_visibility(self):
        self.signoff()
        # The fixture helper takes overrides without relying on any production write path.
        for overrides in [dict(state='answered'), dict(category='side_chat'), dict(review_business_id=self.denied),
                          dict(expected_targets_json=json.dumps([{'resource':{'business_id':self.denied}}]))]:
            self.signoff(**overrides)
        result = explorer.directory(self.viewer, [('path', 'projects')])
        projects = {row['name']:row['project'] for row in result['entries']}
        self.assertEqual(projects['example'], {'id':self.project, 'name':'Example', 'description':'Current project description',
            'open_tasks':3, 'signoffs':1, 'subprojects':None})
        self.assertIsNone(projects['archived'])
        self.assertIsNone(projects['denied'])
        nested = explorer.directory(self.viewer, [('path', 'projects/example/hq')])
        self.assertEqual(nested['project'], projects['example'])
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET status='done' WHERE id='t0'")
        self.assertEqual(explorer.project_metadata(self.runtime, lambda _:self.context, {'example'})['example']['open_tasks'], 2)

    def test_pending_badges_exclude_superseded_and_management_routed_inputs(self):
        replacement = self.signoff()
        self.signoff(superseded_by_id=replacement)
        managed = self.signoff(category='input_request')
        self.signoffs.ports = replace(self.signoffs.ports, management=NS(
            coordinator=lambda *_:None, auto_route_input=lambda *_:None, routing_for=lambda *_:None,
            managed_input_sql=f"s.id='{managed}'"))
        self.assertEqual(explorer.project_metadata(self.runtime, lambda _:self.context, {'example'})['example']['signoffs'], 1)

    def test_bearer_and_write_requests_are_refused_before_disk_access(self):
        api = HTTPAPI.__new__(HTTPAPI)
        for method, headers in [('GET', [(b'authorization', b'Bearer owner-token')]), ('POST', [])]:
            request = Request({'type':'http', 'method':method, 'path':'/api/root/files/directory', 'query_string':b'', 'headers':headers})
            with self.assertRaises(DomainError):
                asyncio.run(api.files_route(request, None, ['directory']))

    def test_owner_route_is_read_only_and_uncached(self):
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = self.runtime
        api.context = lambda *_:(self.context, None)
        request = Request({'type':'http', 'method':'GET', 'path':'/api/root/files/directory',
            'query_string':b'path=projects', 'headers':[], 'qorqut.request_id':'example'})
        response = asyncio.run(api.files_route(request, None, ['directory']))
        self.assertEqual(response.headers['cache-control'], 'private, no-store')
        entries = json.loads(response.body)['data']['entries']
        self.assertEqual(next(row for row in entries if row['name'] == 'example')['project']['open_tasks'], 3)


if __name__ == '__main__':
    unittest.main()
