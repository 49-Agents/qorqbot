"""Schema nine to ten preserves immutable entry facts and existing dated bytes."""

from unittest.mock import patch

from messaging.support import ChatCase
from qorqut_core.app import Application
from qorqut_core.storage.database import Database
from qorqut_core.storage.migrations import MIGRATIONS
from test_foundation import InjectedCrash


class Migration(ChatCase):
    def test_populated_nine_upgrade_and_rollback(self):
        self.app.close()
        self.settings = self.settings.model_copy(update={'data_root': self.root / 'schema-nine'})
        migrate = Database.migrate
        with patch('qorqut_core.storage.database.SCHEMA_VERSION', 9), \
                patch.object(Database, 'migrate', lambda db: migrate(db, migrations=MIGRATIONS[:9])):
            self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
            self.onbash = self.rows('agents')[0]
            self.pump()
            self.send('old-message', 'Immutable text', recipient=self.onbash)
            path = self.settings.data_root / 'agents' / self.onbash['id'] / 'workspace/files/saved.txt'
            path.write_text('synthetic artifact')
            reference = self.app.invoke('files_reference', self.context, {'idempotency_key': 'old-file',
                'agent_id': self.onbash['id'], 'path': 'files/saved.txt'})
            self.assertTrue(reference.ok, reference)
            self.pump()
            before_file = self.path(self.onbash).read_bytes()
            with self.app.db.read() as tx:
                tables = [r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                before = {table: [dict(r) for r in tx.connection.execute(f'SELECT * FROM {table}')] for table in tables}
                triggers = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.app.close()
        self.app.db.lock.acquire()
        try:
            self.fault.point = 'migration_before_version'
            with self.assertRaises(InjectedCrash):
                self.app.db.migrate()
            with patch('qorqut_core.storage.database.SCHEMA_VERSION', 9), self.app.db.read() as tx:
                self.assertEqual(tx.connection.execute('PRAGMA user_version').fetchone()[0], 9)
                self.assertIsNone(tx.connection.execute("SELECT name FROM sqlite_master WHERE name='browser_surfaces'").fetchone())
            self.app.db.migrate()
        finally:
            self.app.db.lock.close()
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        with self.app.db.read() as tx:
            for table, expected in before.items():
                clause = ' WHERE version<=9' if table == 'schema_migrations' else ''
                actual = [dict(r) for r in tx.connection.execute(f'SELECT * FROM {table}{clause}')]
                if table == 'conversation_entries':
                    self.assertTrue(all(r.pop('browser_request_id') is None for r in actual))
                self.assertEqual(actual, expected, table)
            self.assertTrue(triggers <= {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")})
            self.assertEqual(tx.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(tx.connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(self.app.db.validate(), 11)
        self.assertEqual(self.path(self.onbash).read_bytes(), before_file)
        self.assertEqual(len(self.conversation(self.onbash)['items']), 2)
