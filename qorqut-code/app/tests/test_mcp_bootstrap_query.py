import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qorqut.agents.resolver import AgentConfigResolver
from qorqut_operations.contracts import DomainError, Principal

AGENT = '11111111-1111-4111-8111-111111111111'
OWNER = '33333333-3333-4333-8333-333333333333'
BIZ = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
OTHER_BIZ = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
ROLE = {'id': 'role-a', 'version': '1', 'digest': 'sha256:role'}

SCHEMA = '''
CREATE TABLE businesses(id TEXT PRIMARY KEY, name TEXT, archived_at TEXT);
CREATE TABLE machine_registrations(principal_id TEXT, workspace TEXT);
CREATE TABLE business_memberships(business_id TEXT, principal_id TEXT, is_owner INTEGER, active INTEGER);
CREATE TABLE principals(id TEXT PRIMARY KEY, display_name TEXT, active INTEGER);
CREATE TABLE installation(id TEXT, owner_principal_id TEXT);
CREATE TABLE documents(id TEXT PRIMARY KEY, business_id TEXT, namespace TEXT, path TEXT, content_hash TEXT,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)), availability TEXT NOT NULL DEFAULT 'ready',
    revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE artifacts(kind TEXT, artifact_id TEXT, version TEXT, digest TEXT, stored_path TEXT);
'''


class Connection(sqlite3.Connection):
    """Remembers which document rows the snapshot query returned."""

    document_ids = None

    def execute(self, sql, *args):
        cursor = super().execute(sql, *args)
        if 'FROM documents' in sql:
            rows = cursor.fetchall()
            self.document_ids = [row['id'] for row in rows]
            return rows
        return cursor


class BootstrapQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = sqlite3.connect(':memory:', factory=Connection)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.executemany('INSERT INTO businesses VALUES (?,?,NULL)', [(BIZ, 'Selected'), (OTHER_BIZ, 'Other')])
        self.db.execute("INSERT INTO principals VALUES (?, 'Owner', 1)", (OWNER,))
        self.db.execute("INSERT INTO installation VALUES ('service', ?)", (OWNER,))
        self.db.execute("INSERT INTO artifacts VALUES ('role', ?, ?, ?, ?)", (ROLE['id'], ROLE['version'], ROLE['digest'], str(self.root)))
        self.db.commit()
        profile = SimpleNamespace(
            data={'role': ROLE, 'plugins': [], 'tool_policy': {'overrides': {}}},
            scope=SimpleNamespace(kind='ecosystem', business_id=None, businesses=frozenset({BIZ}),
                                  global_capabilities=('read_global_context',)))
        self.proposed = (profile, {'workspace': str(self.root / 'workspace')})
        resolver = AgentConfigResolver.__new__(AgentConfigResolver)
        resolver.root = self.root
        resolver.registry = SimpleNamespace(installed=lambda: [])
        resolver.agentic_view = SimpleNamespace(catalog_revision='rev')

        @contextmanager
        def read():
            yield SimpleNamespace(connection=self.db)
        resolver.database = SimpleNamespace(read=read)
        self.resolver = resolver
        patcher = patch('qorqut.agents.resolver.with_effective_scope', lambda profile, connection: profile)
        patcher.start()
        self.addCleanup(patcher.stop)

    def document(self, ident, metadata, business=None, path=None):
        self.db.execute('INSERT INTO documents (id, business_id, namespace, path, content_hash, metadata_json) VALUES (?,?,?,?,?,?)',
                        (ident, business, 'global' if business is None else 'business:' + business,
                         path or ident + '.md', 'sha256:' + ident, json.dumps(metadata)))
        self.db.commit()

    def snapshot(self, business_id=None):
        return self.resolver._snapshot(Principal(AGENT, 'agent'), business_id, proposed=self.proposed)

    def seed_sources(self):
        for ident, key in (('d1', 'foundation'), ('d3', 'ecosystem'), ('d2', 'ecosystem-agent')):
            self.document(ident, {'bootstrap_key': key})

    def test_only_needed_bootstrap_rows_are_read_and_registered(self):
        self.seed_sources()
        self.document('d4', {})
        self.document('d5', {'bootstrap_key': 'business-agent'})
        self.document('d6', {'bootstrap_key': None})
        self.document('d7', {'kind': 'note'}, business=BIZ)
        self.document('d8', {'bootstrap_key': 'foundation'}, business=OTHER_BIZ)  # not visible from either scope
        for target in (None, BIZ):
            snapshot = self.snapshot(target)
            self.assertEqual(self.db.document_ids, ['d1', 'd2', 'd3'])
            self.assertEqual({key: doc['id'] for key, doc in snapshot['documents'].items()},
                             {'foundation': 'd1', 'ecosystem-agent': 'd2', 'ecosystem': 'd3'})

    def test_matching_row_in_selected_project_still_reaches_wrong_scope_rejection(self):
        self.seed_sources()
        self.document('d4', {'bootstrap_key': 'ecosystem'}, business=BIZ)
        self.document('d5', {}, business=BIZ)
        with self.assertRaises(DomainError) as raised:
            self.snapshot(BIZ)
        self.assertEqual(raised.exception.code, 'context_scope_conflict')
        self.assertIn('wrong registered scope', str(raised.exception))
        self.assertEqual(self.db.document_ids, ['d1', 'd2', 'd3', 'd4'])

    def test_duplicate_global_sources_remain_visible_to_validation(self):
        self.seed_sources()
        self.document('d4', {'bootstrap_key': 'foundation'})
        with self.assertRaises(DomainError) as raised:
            self.snapshot()
        self.assertEqual(raised.exception.code, 'context_scope_conflict')
        self.assertIn('Multiple mandatory sources', str(raised.exception))
        self.assertEqual(self.db.document_ids, ['d1', 'd2', 'd3', 'd4'])

    def test_source_file_escape_is_still_rejected(self):
        self.seed_sources()
        (self.root / 'outside.md').write_text('x')
        (self.root / 'd1.md').symlink_to(self.root / 'outside.md')
        with self.assertRaises(DomainError) as raised:
            self.snapshot()
        self.assertEqual(raised.exception.code, 'context_scope_conflict')
        self.assertIn('escaped its registered scope', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
