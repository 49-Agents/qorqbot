from unittest.mock import patch
from uuid import uuid4

from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.storage.database import Database
from qorqut_core.storage.migrations import MIGRATIONS
from runtime.support import RuntimeCase


class Storage(RuntimeCase):
    def test_schema_five_upgrade_preserves_checksums_messages_receipts_and_events(self):
        self.assertEqual([m.checksum for m in MIGRATIONS[:5]], [
            "64faf705fa367df07470bb80d59c2363d97d11e8b1958661d86bcfd700479663",
            "5e91778f3e01521f5a19d7d272ee5a15d0ef9432125a34abe9aab6811356db4c",
            "18ca408f82d5c231eceb76357c9fd98bd4bc7a3ab5f43b2719550c31209cb0d5",
            "68b55717b82af97d735125b64125b71ee6c6ca94a7d02bf313d0b859c2804dca",
            "6ab896e40cd32f03f1bbd4fac83faa7dffc1c7eb98408c3c0b48a7fb48e37a37",
        ])
        settings = self.settings.model_copy(update={"installation_id": uuid4(), "data_root": self.root / "version-five"})
        migrate = Database.migrate
        # Construct a genuine v5 database using only the preserved migrations.
        # Assembly is passive, so its unused runtime module never touches v6.
        with patch("qorqut_core.storage.database.SCHEMA_VERSION", 5), \
                patch.object(Database, "migrate", lambda db: migrate(db, migrations=MIGRATIONS[:5])):
            old = Application.initialize(settings, self.defaults, self.owner, clock=self.clock)
            try:
                # Settle the sole workspace job before queuing publication;
                # advancing a fixed clock between equal-time jobs can starve it.
                old.jobs.run_next(worker_id=uuid4())
                host = old.invoke("agents_list", self.context).data["onbash_agent_id"]
                receipt = old.invoke("messages_send", self.context, {"recipient_agent_id": host,
                    "body": "Original 文\nmessage", "idempotency_key": "pre-migration"})
                self.assertTrue(receipt.ok)
                for _ in range(10):
                    if old.jobs.run_next(worker_id=uuid4()) is None:
                        break
                with old.db.read() as tx:
                    tables = [r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    before = {table: [tuple(r) for r in tx.connection.execute(f"SELECT * FROM {table}")]
                              for table in tables}
                self.assertTrue(before["file_receipts"])
            finally:
                old.close()
        with self.assertRaises(CoreError) as explicit:
            Application.open(settings)
        self.assertEqual(explicit.exception.code, "unsupported_schema")
        old.db.lock.acquire()
        try:
            old.db.migrate()
        finally:
            old.db.lock.close()
        upgraded = Application.open(settings, clock=self.clock)
        try:
            with upgraded.db.read() as tx:
                for table in tables:
                    clause = " WHERE version<=5" if table == "schema_migrations" else ""
                    after = [tuple(r) for r in tx.connection.execute(f"SELECT * FROM {table}{clause}")]
                    self.assertEqual(after, before[table], table)
                self.assertEqual(tx.connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            replay = upgraded.invoke("messages_send", self.context, {"recipient_agent_id": host,
                "body": "Original 文\nmessage", "idempotency_key": "pre-migration"})
            self.assertEqual(replay, receipt)
            self.assertEqual(upgraded.invoke("runtime_get", self.context, {"agent_id": host}).data["status"], "disconnected")
        finally:
            upgraded.close()

    def test_connection_identity_attribution_and_closed_history_cannot_be_rewritten(self):
        candidate = self.candidate()
        connected = self.owners.connect(candidate)
        row = self.rows("session_owners")[0]
        for field, value in (("owner_id", str(uuid4())), ("terminal_id", str(uuid4())),
                             ("connected_by", str(self.owner.actor_id)), ("process_json", "{}"),
                             ("run_generation", 2), ("native_session_id", str(uuid4()))):
            with self.subTest(field=field), self.assertRaises(CoreError):
                with self.app.db.transaction() as tx:
                    tx.connection.execute(f"UPDATE session_owners SET {field}=?", (value,))
        for sql, args in (("DELETE FROM session_owners", ()),
                          (f"INSERT OR REPLACE INTO session_owners ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))):
            with self.assertRaises(CoreError):
                with self.app.db.transaction() as tx:
                    tx.connection.execute(sql, args)
        self.state(candidate, "exited")
        self.assertTrue(self.owners.release(connected.owner_id, candidate.process))
        with self.assertRaises(CoreError):
            with self.app.db.transaction() as tx:
                tx.connection.execute("UPDATE session_owners SET state='active',observation='live',closed_at=NULL,close_reason=NULL")
        self.assertEqual(self.rows("session_owners")[0]["state"], "closed")
