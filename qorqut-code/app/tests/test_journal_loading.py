"""Journal reads keep their access/filing semantics while avoiding per-note scans."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.transport.compatibility import DashboardCompatibility
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from qorqut_operations.documents.library import SourceLibrary
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.serialization import document_bytes, split_document
from qorqut_operations.documents.store import DocumentStore
from qorqut_operations.storage.database import Database


class JournalLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.database = Database(cls.root / 'operations.sqlite', assembly=host_assembly(ChangeFeed()))
        cls.database.initialize_target()
        cls.owner_id, cls.business_id = new_id(), new_id()
        cls.owner = InvocationContext(Principal(cls.owner_id, 'human'), source='dashboard',
            global_capabilities={'read_global_context'})
        cls.store = DocumentStore(cls.database, operations_root=cls.root,
            document_root=lambda business, path: cls.root)
        cls.sources = DocumentSources(cls.store, SourceLibrary(cls.store),
            agent_workspace=lambda *a: cls.root, scope_root=lambda *a: cls.root)
        cls.filing_calls = []

        def filing(uow, context, upload):
            cls.filing_calls.append((context, upload['id']))
            return {'status': 'processed', 'document_revision': upload['revision']}

        cls.compat = DashboardCompatibility(SimpleNamespace(database=cls.database,
            domains=SimpleNamespace(documents=SimpleNamespace(source_filing=filing))), local_files=object())
        cls.note_ids = []
        with cls.database.transaction() as uow:
            uow.repository('principals').insert({'id': cls.owner_id, 'kind': 'human', 'display_name': 'Owner'})
            uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': cls.owner_id})
            uow.repository('businesses').insert({'id': cls.business_id, 'slug': 'project', 'name': 'Project',
                'workspace': str(cls.root / 'project'), 'hq_path': str(cls.root / 'project/hq')})
            for index in range(84):
                identity = cls.add_document(uow, 'global_note', {'title': f'Note {index}'})
                cls.note_ids.append(identity)
                if index < 3:
                    for revision in (1, 2):
                        cls.add_document(uow, 'context_upload', {'journal_source_id': identity,
                            'journal_source_revision': revision, 'journal_source_hash': 'saved-hash'})
            cls.archived = cls.add_document(uow, 'global_note', {}, archived=True)
            cls.project_note = cls.add_document(uow, 'business_owned_note', {}, business_id=cls.business_id)
            cls.legacy_note = cls.add_document(uow, 'business_note', {}, business_id=cls.business_id)
            cls.add_document(uow, 'context_upload', {'journal_source_id': cls.archived})
            cls.add_document(uow, 'agent_note', {})

    @classmethod
    def add_document(cls, uow, kind, metadata, *, business_id=None, archived=False):
        identity = new_id()
        path = f'{identity}.md'
        (cls.root / path).write_bytes(document_bytes(metadata, 'A thought to keep.'))
        uow.repository('documents', business_id=business_id).insert({'id': identity, 'kind': kind, 'path': path,
            'namespace': business_id or 'global',
            'metadata_json': metadata, 'content_hash': 'saved-hash', 'archived': int(archived)})
        return identity

    @classmethod
    def tearDownClass(cls):
        cls.database.close()
        cls.temp.cleanup()

    def test_batch_preserves_payload_order_and_filing_visibility_with_one_scan(self):
        with self.database.read() as uow:
            rows = self.sources.read('notes_list', self.owner, {'limit': 200}, uow)['items']
            calls = []
            uow.connection.set_trace_callback(calls.append)
            expected = [self.compat.page_view(row, uow, self.owner) for row in rows]
            individual = [q for q in calls if 'journal_source_id' in q]
            calls.clear()
            grouped = self.compat.journal_uploads(rows, uow)
            actual = [self.compat.page_view(row, uow, self.owner, journal_uploads=grouped.get(row['id'], ())) for row in rows]
            batched = [q for q in calls if 'journal_source_id' in q]
            uow.connection.set_trace_callback(None)
        self.assertEqual(actual, expected)
        self.assertEqual(len(individual), 85)
        self.assertEqual(len(batched), 1)
        self.assertNotIn(self.archived, grouped)
        self.assertTrue(all(context is self.owner for context, _ in self.filing_calls))
        self.assertEqual(sum(len(note['filerSubmissions']) for note in actual), 6)

    def test_journal_access_and_project_target_are_preserved(self):
        with self.database.read() as uow:
            queries = []
            uow.connection.set_trace_callback(queries.append)
            rows = self.sources.read('notes_list', self.owner, {'limit': 200}, uow)['items']
            uow.connection.set_trace_callback(None)
            self.assertEqual({row['id'] for row in rows}, {*self.note_ids, self.legacy_note})
            self.assertFalse(any('SELECT * FROM "documents" WHERE "business_id" IS NULL' in q for q in queries))
            stranger = InvocationContext(Principal(new_id(), 'human'), source='dashboard')
            with self.assertRaises(DomainError):
                self.sources.read('notes_list', stranger, {}, uow)
            project = InvocationContext(Principal(self.owner_id, 'human'), source='dashboard',
                business_id=self.business_id, allowed_business_ids={self.business_id})
            rows = self.sources.read('notes_list', project, {'target_business_id': self.business_id}, uow)['items']
            self.assertEqual([row['id'] for row in rows], [self.project_note])
            for args in ({}, {'target_business_id': new_id()}):
                with self.assertRaises(DomainError):
                    self.sources.read('notes_list', project, args, uow)

    def test_submission_batch_is_bounded_and_empty_or_project_lists_do_no_scan(self):
        with self.database.read() as uow:
            queries = []
            uow.connection.set_trace_callback(queries.append)
            self.assertEqual(self.compat.journal_uploads([], uow), {})
            self.assertEqual(self.compat.journal_uploads([{'id': self.project_note, 'kind': 'business_owned_note'}], uow), {})
            self.assertEqual(queries, [])
            selected = [{'id': identity, 'kind': 'global_note'} for identity in self.note_ids]
            selected += [{'id': new_id(), 'kind': 'global_note'} for _ in range(120)]
            grouped = self.compat.journal_uploads(selected, uow)
            uow.connection.set_trace_callback(None)
        self.assertEqual(len(queries), 2)
        self.assertEqual(len(grouped), 204)
        self.assertEqual(sum(map(len, grouped.values())), 6)

    def test_cached_metadata_keeps_content_fresh_and_returns_independent_values(self):
        data = document_bytes({'title': 'Original', 'statuses': {'Task': 'todo'}, 'tags': ['one']}, '\nFirst body')
        metadata, body = split_document(data)
        self.assertEqual(body, '\nFirst body')
        metadata['statuses']['Task'] = 'done'
        metadata['tags'].append('changed')
        self.assertEqual(split_document(data)[0], {'title': 'Original', 'statuses': {'Task': 'todo'}, 'tags': ['one']})
        self.assertEqual(split_document(data.replace(b'First body', b'Edited body'))[1], '\nEdited body')
        self.assertEqual(split_document(data.replace(b'Original', b'Edited'))[0]['title'], 'Edited')
        for bad in (b'---\ntitle: first\ntitle: second\n---\nText', b'---\n- a list\n---\nText'):
            with self.assertRaises(DomainError):
                split_document(bad)


if __name__ == '__main__':
    unittest.main()
