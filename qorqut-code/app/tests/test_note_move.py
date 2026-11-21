"""Project notes move between projects with their file and earlier versions; an upload moves with the table made from it.

A note is a ``documents`` row plus a file in its project's folder, and its versions live under its project's history
namespace. A move changes the row's project, copies the file to the same relative path in the destination folder (the
source copy is retired once the move is committed) and copies the versions, keeping the originals where they were."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.domains.tables.relocation import TableHostRelocation
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.documents.relocation import DocumentRelocation
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.record_moves import moved_location
from qorqut_operations.tables.relocation import TableRelocation

STAMP, LATER = '2026-11-10T12:00:00Z', '2026-11-12T12:00:00Z'


class NoteMoveFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.db = Database(self.folder / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner = str(uuid4())
        self.source, self.destination = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                                                       owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            for project, slug in ((self.source, 'launch'), (self.destination, 'product')):
                uow.repository('projects').insert({'id': project, 'name': slug.title(), 'slug': slug,
                                                     'workspace': slug, 'hq_path': str(self.folder / slug / 'hq')})
        self.ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard', project_id=self.source,
                                     allowed_project_ids=frozenset({self.source, self.destination}))
        self.engine = DocumentRelocation(lambda project, path: Path(project['hq_path']))

    def hq(self, project):
        return self.folder / ('launch' if project == self.source else 'product') / 'hq'

    def note(self, body=b'# Launch brief\n', *, kind='project_owned_note', path=None, versions=2):
        identity, path = str(uuid4()), path or f'notes/{uuid4().hex[:8]}.md'
        target = self.hq(self.source) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        with self.db.transaction() as uow:
            uow.connection.execute('INSERT INTO documents(id,project_id,namespace,kind,path,content_hash,metadata_json,created_at,updated_at) '
                                   'VALUES(?,?,?,?,?,?,?,?,?)', (identity, self.source, self.source, kind, path,
                                                                 'sha256:' + hashlib.sha256(body).hexdigest(), json.dumps({'title': 'Brief'}),
                                                                 STAMP, STAMP))
            for version in range(1, versions + 1):
                uow.connection.execute('INSERT INTO qdv_document_versions(namespace,document_id,version,content,content_sha256,author_id,'
                                       'created_at,reason) VALUES(?,?,?,?,?,?,?,?)',
                                       ('project:' + self.source, identity, version, f'v{version}', 'x', self.owner, STAMP, 'edit'))
        return identity, path

    def preview(self, *ids):
        with self.db.read() as uow:
            return self.engine.inspect(self.ctx, uow, self.source, self.destination, list(ids))

    def move(self, *ids):
        inspection = self.preview(*ids)
        self.assertEqual(inspection.blockers, [])
        with self.db.transaction() as uow:
            return self.engine.relocate(self.ctx, uow, self.source, self.destination, list(ids), inspection.fingerprint(self.owner),
                                        move_id=str(uuid4()), at=LATER)


class NoteMoveTests(NoteMoveFixture):
    def test_a_note_moves_with_its_file_and_earlier_versions(self):
        identity, path = self.note()
        self.assertEqual(self.move(identity)['counts']['versions'], 2)
        self.assertEqual((self.hq(self.destination) / path).read_bytes(), b'# Launch brief\n')
        with self.db.read() as uow:
            row = uow.connection.execute('SELECT project_id, namespace, path FROM documents WHERE id=?', (identity,)).fetchone()
            self.assertEqual(tuple(row), (self.destination, self.destination, path))
            spaces = [item[0] for item in uow.connection.execute(
                'SELECT namespace FROM qdv_document_versions WHERE document_id=? ORDER BY namespace, version', (identity,))]
            self.assertEqual(sorted(set(spaces)), sorted({'project:' + self.source, 'project:' + self.destination}))
            self.assertEqual(len(spaces), 4)
            self.assertEqual(moved_location(uow, 'document', self.source, identity), self.destination)
        # The source copy stays until the move is committed and then is retired by the sweep.
        self.assertTrue((self.hq(self.source) / path).exists())
        with self.db.read() as uow:
            self.assertEqual(self.engine.sweep(uow), 1)
        self.assertFalse((self.hq(self.source) / path).exists())

    def test_a_failed_transaction_leaves_the_source_untouched_and_the_retry_reuses_the_copy(self):
        identity, path = self.note()
        inspection = self.preview(identity)
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as uow:
                self.engine.relocate(self.ctx, uow, self.source, self.destination, [identity], inspection.fingerprint(self.owner),
                                     move_id=str(uuid4()), at=LATER)
                raise RuntimeError('crash after the copy')
        self.assertTrue((self.hq(self.source) / path).exists())
        with self.db.read() as uow:
            self.assertEqual(self.engine.sweep(uow), 0)
            self.assertEqual(uow.connection.execute('SELECT project_id FROM documents WHERE id=?', (identity,)).fetchone()[0], self.source)
        self.move(identity)

    def test_an_edited_file_a_taken_path_and_other_kinds_block(self):
        edited, path = self.note()
        (self.hq(self.source) / path).write_bytes(b'changed outside the service')
        taken, taken_path = self.note()
        (self.hq(self.destination) / taken_path).parent.mkdir(parents=True, exist_ok=True)
        (self.hq(self.destination) / taken_path).write_bytes(b'someone else')
        journal, _ = self.note(kind='project_note')
        codes = {item['code'] for item in self.preview(edited, taken, journal).blockers}
        self.assertEqual(codes, {'document_changed', 'path_exists', 'unsupported_document'})

    def test_a_link_to_a_task_left_behind_stays_as_history(self):
        identity, _ = self.note()
        edge = str(uuid4())
        with self.db.transaction() as uow:
            uow.connection.execute("INSERT INTO relationships(project_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at,"
                                   "created_by) VALUES(?,?,'material',?,'task','T-1','concerns','',?,?,?)",
                                   (self.source, edge, identity, STAMP, STAMP, self.owner))
        self.assertEqual(self.move(identity)['counts']['historical_relationships'], 1)
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT project_id FROM relationships WHERE id=?', (edge,)).fetchone()[0], self.source)

    def test_a_table_moves_together_with_the_upload_its_rows_came_from(self):
        upload, path = self.note(b'name\nAda\n', kind='data_upload', path='uploads/people.csv', versions=0)
        tables = TableRelocation((TableHostRelocation(),), documents=self.engine)
        table_ids = []
        with self.db.transaction() as uow:
            for title in ('People', 'Staying'):
                table_id = str(uuid4())
                table_ids.append(table_id)
                uow.connection.execute('INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at) '
                                       'VALUES(?,?,?,?,?,?,?)', (self.source, table_id, title, '[]', self.owner, STAMP, STAMP))
                uow.connection.execute('INSERT INTO data_rows(project_id,id,table_id,position,cells_json,source_upload_id,created_by,'
                                       'updated_by,created_at,updated_at) VALUES(?,?,?,0,?,?,?,?,?,?)',
                                       (self.source, str(uuid4()), table_id, '{}', upload, self.owner, self.owner, STAMP, STAMP))
        with self.db.read() as uow:
            alone = tables.inspect(self.ctx, uow, self.source, self.destination, [table_ids[0]])
            both = tables.inspect(self.ctx, uow, self.source, self.destination, table_ids)
        self.assertEqual([item['code'] for item in alone.blockers], ['upload_in_use'])
        self.assertEqual(both.blockers, [])
        with self.db.transaction() as uow:
            result = tables.apply(self.ctx, uow, self.source, self.destination, table_ids, both.fingerprint(self.owner),
                                  move_id=str(uuid4()), at=LATER)
        self.assertEqual(result['counts']['uploads'], 1)
        self.assertTrue((self.hq(self.destination) / path).exists())
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT project_id FROM documents WHERE id=?', (upload,)).fetchone()[0], self.destination)
            self.assertIsNone(uow.connection.execute('PRAGMA foreign_key_check(data_rows)').fetchone())

    def test_global_or_same_project_is_refused(self):
        identity, _ = self.note()
        with self.db.read() as uow:
            for source, destination in ((None, self.destination), (self.source, self.source)):
                with self.assertRaises(DomainError):
                    self.engine.inspect(self.ctx, uow, source, destination, [identity])



class MoveOperationTests(NoteMoveFixture):
    """The host operations: the owner previews and moves through the ordinary application path; a denied tool stops both."""

    def setUp(self):
        super().setUp()
        from qorqut.domains.documents.moves import NoteMoves
        from qorqut.domains.tables.moves import TableMoves
        from qorqut_operations.application.engine import Application
        from qorqut_operations.application.registry import OperationDescriptor, OperationRegistry
        from qorqut_operations.contracts import PolicyDecision
        self.denied = set()
        self.registry = OperationRegistry(())
        for name in ('notes_get', 'notes_update', 'notes_create', 'tables_get', 'tables_update', 'tables_create'):
            self.registry.register(OperationDescriptor(name, {'type': 'object'}, effect_kind='read', supports_signoff=False,
                                                       read=lambda *a: {}))
        policy = type('Policy', (), {'resolve': lambda _, ctx, descriptor, *a, **k: PolicyDecision(
            'deny' if descriptor.name in self.denied else 'allow', 'test')})()
        self.app = Application(self.db, self.registry, policy=policy)
        NoteMoves(self.app, self.engine).register(self.registry)
        TableMoves(self.app, documents=self.engine).register(self.registry)
        self.owner_ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard', project_id=self.source,
                                           allowed_project_ids=frozenset({self.source, self.destination}))

    def call(self, operation, args):
        from qorqut_operations.contracts import InvocationOptions, new_id, plain
        result = self.app.invoke(self.owner_ctx, operation, {'source_project_id': self.source, 'destination_project_id': self.destination,
                                                             **args}, InvocationOptions(idempotency_key=new_id()))
        return result.status, plain(result.data if result.status == 'ok' else result.error)

    def test_the_owner_previews_and_moves_a_note_through_the_operations(self):
        identity, path = self.note()
        status, data = self.call('notes_move_to_project_preview', {'document_ids': [identity]})
        self.assertEqual(status, 'ok', data)
        preview = data['preview']
        self.assertTrue(preview['can_move'])
        self.assertEqual(preview['notes'][0]['id'], identity)
        status, data = self.call('notes_move_to_project', {'document_ids': [identity], 'preview_token': preview['token']})
        self.assertEqual(status, 'ok', data)
        self.assertEqual(data['moved'], [{'id': identity, 'project_id': self.destination}])
        self.assertTrue((self.hq(self.destination) / path).exists())
        # The next preview retires the committed move's source file.
        other, _ = self.note()
        self.call('notes_move_to_project_preview', {'document_ids': [other]})
        self.assertFalse((self.hq(self.source) / path).exists())

    def test_a_denied_destination_permission_stops_the_preview(self):
        identity, _ = self.note()
        self.denied.add('notes_create')
        status, error = self.call('notes_move_to_project_preview', {'document_ids': [identity]})
        self.assertNotEqual(status, 'ok')
        self.assertEqual(error['code'], 'policy_denied')


if __name__ == '__main__':
    unittest.main()
