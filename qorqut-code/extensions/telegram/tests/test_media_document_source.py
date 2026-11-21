"""A document source names a document by its id; the project that holds it is found from the id."""
from types import SimpleNamespace as NS
import sqlite3
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import DomainError
from qorqut_telegram import media_sources
from qorqut_telegram.media_sources import SOURCE_SCHEMA, complete_document_source, parse_source

PROJECT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
ASSET = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
NOTE = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
GLOBAL = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
PLAN = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
UNKNOWN = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'


class Reach:
    """Stands in for the scope reach of one read operation: which projects the caller may enter and read in."""
    operations = []

    def __init__(self, application, ctx, uow, operation):
        Reach.operations.append(operation)
        self.names = {PROJECT: 'Alpha'}

    def can_read(self, project_id, scope_stated=True):
        assert scope_stated is False
        return project_id == PROJECT


class DocumentSourceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE documents(id TEXT PRIMARY KEY, project_id TEXT, kind TEXT)')
        self.db.executemany('INSERT INTO documents VALUES(?,?,?)', [
            (ASSET, PROJECT, 'asset'), (NOTE, OTHER, 'agent_note'), (GLOBAL, None, 'content'), (PLAN, PROJECT, 'plan')])
        self.uow = NS(connection=self.db)
        self.ctx = NS(owner_session_project_id=None, delegation_assignment_id=None)
        Reach.operations = []
        patcher = patch('qorqut_operations.records.references.ScopeReach', Reach)
        patcher.start()
        self.addCleanup(patcher.stop)

    def complete(self, source):
        return complete_document_source(None, self.ctx, {'source': source, 'purpose': 'reply'}, self.uow)['source']

    def test_a_document_id_alone_is_a_valid_source(self):
        self.assertEqual(parse_source({'type': 'document', 'document_id': ASSET}),
                         {'type': 'document', 'document_id': ASSET, 'project_id': None})
        self.assertEqual(parse_source({'type': 'document', 'document_id': ASSET, 'project_id': PROJECT})['project_id'], PROJECT)
        self.assertIsNone(parse_source({'type': 'document', 'document_id': ASSET, 'project_id': None})['project_id'])

    def test_the_source_shape_stays_closed(self):
        for source in ({'type': 'document'}, {'type': 'document', 'document_id': ASSET, 'path': 'exports/a.pdf'},
                       {'type': 'document', 'document_id': 'A', 'project_id': PROJECT},
                       {'type': 'document', 'document_id': ASSET, 'project_id': 'x'}):
            with self.assertRaises(DomainError) as raised:
                parse_source(source)
            self.assertEqual(raised.exception.code, 'validation_error')

    def test_the_schema_requires_only_the_type_and_the_id(self):
        document = [shape for shape in SOURCE_SCHEMA['oneOf'] if 'document_id' in shape['properties']][0]
        self.assertEqual(sorted(document['required']), ['document_id', 'type'])
        self.assertIn('project_id', document['properties'])

    def test_a_project_document_gets_its_project(self):
        self.assertEqual(self.complete({'type': 'document', 'document_id': ASSET}),
                         {'type': 'document', 'document_id': ASSET, 'project_id': PROJECT})
        self.assertEqual(Reach.operations, [media_sources.READERS['asset']])

    def test_the_reader_of_the_documents_kind_decides(self):
        self.complete({'type': 'document', 'document_id': NOTE})
        self.assertEqual(Reach.operations, ['notes_get'])

    def test_a_stated_project_is_left_as_given(self):
        for stated in (PROJECT, OTHER, None):
            source = {'type': 'document', 'document_id': ASSET, 'project_id': stated}
            self.assertEqual(self.complete(source), source)
        self.assertEqual(Reach.operations, [])

    def test_a_document_that_is_global_unknown_unreadable_or_of_another_kind_stays_without_a_project(self):
        for identity in (GLOBAL, UNKNOWN, NOTE, PLAN):
            source = {'type': 'document', 'document_id': identity}
            self.assertEqual(self.complete(source), source, identity)

    def test_a_workspace_source_is_untouched(self):
        source = {'type': 'workspace', 'path': 'exports/report.pdf'}
        self.assertEqual(self.complete(source), source)

    def test_a_delegated_or_bound_session_gets_only_its_own_project(self):
        self.ctx = NS(owner_session_project_id=OTHER, delegation_assignment_id=None)
        source = {'type': 'document', 'document_id': ASSET}
        self.assertEqual(self.complete(source), source)


if __name__ == '__main__': unittest.main()
