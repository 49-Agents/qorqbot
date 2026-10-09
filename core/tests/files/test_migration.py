"""A populated v7 ledger upgrades without changing existing records or bytes."""

from unittest.mock import patch

from adapters.test_native_records import claude, raw
from conversation import test_native_projection as native_fixture
from messaging.support import ChatCase
from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.storage.database import Database
from qorqut_core.storage.migrations import MIGRATIONS
from test_foundation import InjectedCrash


class Migration(ChatCase):
    bind = native_fixture.NativeProjection.bind
    archive = native_fixture.NativeProjection.archive
    consume = native_fixture.NativeProjection.consume

    def test_populated_v7_atomic_upgrade_preserves_messages_native_cursors_receipts_and_files(self):
        self.app.close()
        self.settings = self.settings.model_copy(update={"data_root": self.root / "schema-seven"})
        migrate = Database.migrate
        with patch("qorqut_core.storage.database.SCHEMA_VERSION", 7), \
                patch.object(Database, "migrate", lambda db: migrate(db, migrations=MIGRATIONS[:7])):
            self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
            self.onbash = next(r for r in self.rows("agents") if r["kind"] == "host")
            self.pump()
            saved = self.send("legacy", "legacy exact 文\n", recipient=self.onbash)
            self.bind("claude")
            self.archive(raw(claude("user exact 文\n")) + raw(claude("agent exact 文\n", role="assistant")))
            self.consume()
            self.pump()
            old_file = self.path(self.onbash).read_bytes()
            old_wire = self.conversation(self.onbash)
            with self.app.db.read() as tx:
                tables = [r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                old = {table: [dict(row) for row in tx.connection.execute(f"SELECT * FROM {table}")] for table in tables}
                triggers = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.app.close()
        with self.assertRaises(CoreError) as explicit:
            Application.open(self.settings)
        self.assertEqual(explicit.exception.code, "unsupported_schema")
        self.app.db.lock.acquire()
        try:
            self.fault.point = "migration_before_version"
            with self.assertRaises(InjectedCrash):
                self.app.db.migrate()
            with patch("qorqut_core.storage.database.SCHEMA_VERSION", 7), self.app.db.read() as tx:
                self.assertIsNone(tx.connection.execute("SELECT name FROM sqlite_master WHERE name='file_references'").fetchone())
                for table, rows in old.items():
                    self.assertEqual([dict(row) for row in tx.connection.execute(f"SELECT * FROM {table}")], rows, table)
            self.app.db.migrate()
        finally:
            self.app.db.lock.close()
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        with self.app.db.read() as tx:
            for table, rows in old.items():
                clause = " WHERE version<=7" if table == "schema_migrations" else ""
                current = [dict(row) for row in tx.connection.execute(f"SELECT * FROM {table}{clause}")]
                if table == "conversation_entries":
                    self.assertTrue(all(row.pop("file_reference_id") is None for row in current))
                    self.assertTrue(all(row.pop("browser_request_id") is None for row in current))
                self.assertEqual(current, rows, table)
            current_triggers = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            self.assertTrue(triggers <= current_triggers)
            self.assertEqual(tx.connection.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(tx.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual((len(self.app.registry), len(self.app.jobs.handlers)), (24, 6))
        self.assertEqual(self.path(self.onbash).read_bytes(), old_file)
        self.assertEqual(self.conversation(self.onbash), old_wire)
        self.assertEqual(self.send("legacy", "legacy exact 文\n", recipient=self.onbash), saved)
        # Restored native/entry immutability triggers still reject direct rewrites.
        for table in ("conversation_native", "conversation_entries"):
            row = self.rows(table)[0]
            for sql, values in ((f"DELETE FROM {table}", ()), (f"UPDATE {table} SET entry_id=entry_id", ()),
                    (f"INSERT OR REPLACE INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))):
                with self.assertRaises(CoreError), self.app.db.transaction() as tx:
                    tx.connection.execute(sql, values)
        # A native tool/path-shaped turn remains native; it cannot create a file record.
        self.archive(raw(claude("files_reference files/report.txt", role="assistant")))
        self.consume()
        self.assertEqual(self.rows("file_references"), [])
        source = self.settings.data_root / "agents" / self.onbash["id"] / "workspace/files/report.txt"
        source.write_text("metadata")
        result = self.app.invoke("files_reference", self.context, {"idempotency_key": "after-upgrade",
            "agent_id": self.onbash["id"], "path": "files/report.txt"})
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["entry"]["conversation_seq"], 5)
        self.pump()
        self.assertTrue(self.path(self.onbash).read_bytes().startswith(old_file))
