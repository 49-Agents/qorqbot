from copy import copy
from dataclasses import replace
import json
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from qorqut_core.contracts import CoreError, Invocation, canonical_hash
from qorqut_core.storage.database import Database, HostLock, assert_external_io_allowed
from qorqut_core.storage.migrations import MIGRATIONS
from qorqut_core.transcripts.archive import ArchiveMetadata
from qorqut_core.transcripts.archive_models import ArchiveState
from qorqut_core.transcripts.spool import CHUNK_BYTES
from test_foundation import InjectedCrash
from .support import ArchiveCase


class InventoryTests(ArchiveCase):
    def test_registration_replay_links_metadata_attribution_and_atomic_rollback(self):
        # Source locators are evidence and cannot be opened by registration.
        def hash_outside_sql(value):
            assert_external_io_allowed()
            return canonical_hash(value)
        with patch("qorqut_core.transcripts.archive.directory", side_effect=AssertionError("registration file I/O")), \
                patch("qorqut_core.transcripts.archive.canonical_hash", side_effect=hash_outside_sql):
            state = self.register()
        self.assertEqual(state.host_archived_offset, 0)
        self.assertFalse(self.object_path().parent.exists())
        before = self.inventory()
        self.clock.advance(10)
        agent_context = Invocation(actor_id=self.agent["actor_id"])
        self.assertEqual(self.register(context=agent_context), state)
        self.assertEqual(self.inventory(), before)
        metadata = self.rows("archive_epochs")[0]
        for key, value in self.metadata.model_dump().items():
            self.assertEqual(metadata[key], value)
        events = [r for r in self.rows("events") if r["kind"].startswith("archive.")]
        self.assertEqual([r["kind"] for r in events], ["archive.source_registered", "archive.epoch_registered", "archive.run_linked"])
        self.assertEqual({r["actor_id"] for r in events}, {str(self.owner.actor_id)})
        self.assertEqual(self.rows("archive_runs")[0]["recovery_epoch"], self.binding.recovery_epoch)
        new_run = replace(self.binding, run_id=str(uuid4()), run_generation=2, recovery_epoch=str(uuid4()))
        self.register(binding=new_run, context=agent_context)
        self.assertEqual(len(self.rows("archive_run_sources")), 2)
        self.assertEqual(self.rows("archive_run_sources")[-1]["registered_by"], self.agent["actor_id"])
        for changes in ({"run_generation": 3}, {"boot_id": str(uuid4())}):
            with self.assertRaises(CoreError) as conflict:
                self.register(binding=replace(new_run, **changes))
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
        for source, metadata in ((replace(self.source, native_session_id="other"), self.metadata),
                                 (self.source, self.metadata.model_copy(update={"locator": "changed"}))):
            with self.assertRaises(CoreError) as conflict:
                self.register(source=source, metadata=metadata)
            self.assertEqual(conflict.exception.code, "idempotency_conflict")
        before = self.inventory()
        self.fault.point = "archive_register_before_commit"
        with self.assertRaises(InjectedCrash):
            self.register(source=replace(self.source, source_id=str(uuid4())), binding=replace(new_run, run_id=str(uuid4())))
        self.assertEqual(self.inventory(), before)

    def test_parent_epochs_and_distinct_source_identity(self):
        self.register()
        child = replace(self.source, source_id=str(uuid4()), parent_source_id=self.source.source_id)
        self.register(source=child)
        # Same native name/locator/bytes never deduplicates distinct UUIDs.
        self.assertEqual(len(self.rows("archive_sources")), 2)
        another = self.create(start=False)
        other_id = another.data["agent"]["id"]
        other_binding = replace(self.binding, agent_id=other_id, run_id=str(uuid4()))
        other_source = replace(child, source_id=str(uuid4()), agent_id=other_id)
        before = self.inventory()
        with self.assertRaises(CoreError) as wrong:
            self.register(source=other_source, binding=other_binding)
        self.assertEqual(wrong.exception.code, "target_changed")
        self.assertEqual(self.inventory(), before)
        for code in ("not_found", "invalid_argument"):
            source_id = str(uuid4())
            candidate = replace(child, source_id=source_id,
                                parent_source_id=str(uuid4()) if code == "not_found" else source_id)
            with self.assertRaises(CoreError) as error:
                self.register(source=candidate)
            self.assertEqual(error.exception.code, code)
        successor = str(uuid4())
        with self.assertRaises(CoreError) as missing:
            self.register(epoch=successor)
        self.assertEqual(missing.exception.code, "invalid_argument")
        evidence = self.metadata.model_copy(update={"previous_epoch": self.epoch, "divergence_reason": "Synthetic rewrite"})
        self.register(epoch=successor, metadata=evidence)
        self.receive()
        self.assertEqual(self.read().payload, self.chunk().payload)
        self.assertEqual(self.state(epoch=successor).host_archived_offset, 0)
        self.assertEqual(self.get(self.agent["id"])["lifecycle"], "provisioning")

    def test_validation_rechecks_dataclasses_and_models_before_any_effect(self):
        self.register()
        before = self.inventory()
        base = self.chunk()
        bad_chunks = [replace(base, **change) for change in (
            {"offset": True}, {"offset": -1}, {"offset": 2**63 - 1}, {"offset": None},
            {"length": True}, {"length": 0}, {"length": CHUNK_BYTES + 1}, {"length": "3"},
            {"payload": bytearray(base.payload)}, {"payload": b"different"},
            {"sha256": base.sha256.upper()}, {"sha256": "0" * 64},
            {"source_id": "00000000-0000-4000-8000-00000000000A"}, {"source_epoch": "../outside"})]
        with patch("qorqut_core.transcripts.archive.directory", side_effect=AssertionError("invalid input I/O")):
            for bad in bad_chunks:
                with self.subTest(chunk=bad), self.assertRaises(CoreError) as error:
                    self.receive(bad)
                self.assertEqual(error.exception.code, "invalid_argument")
                self.assertNotIn(str(bad.payload), str(error.exception))
            for name, value in (("run_generation", True), ("environment_generation", 2**63), ("run_id", "bad")):
                forged = copy(self.binding)
                object.__setattr__(forged, name, value)
                with self.assertRaises(CoreError) as invalid:
                    self.receive(binding=forged)
                self.assertEqual(invalid.exception.code, "invalid_argument")
            for field, value in (("capability_evidence", "\ud800"), ("locator", "x\0x"),
                                 ("file_identity", ""), ("divergence_reason", "unpaired"),
                                 ("native_created_at", "2026-10-07T12:00:00+01:00"),
                                 ("native_created_at", "2026-10-07T12:00:00." + "0" * 4096 + "Z")):
                invalid = self.metadata.model_copy(update={field: value})
                with self.assertRaises(CoreError) as error:
                    self.register(metadata=invalid)
                self.assertEqual(error.exception.code, "invalid_argument")
            for value in (True, -1, "0", None, 2**63):
                with self.assertRaises(CoreError) as error:
                    self.read(value)
                self.assertEqual(error.exception.code, "invalid_argument")
        self.assertEqual(self.inventory(), before)
        self.assertFalse(self.object_path().parent.exists())
        with self.assertRaises(ValidationError):
            ArchiveState(**{**self.state().model_dump(), "recovery_held": 1})

    def test_unknown_lineage_gap_and_uninitialized_workspace_have_no_effect(self):
        self.register()
        before = self.inventory()
        cases = ((replace(self.binding, run_id=str(uuid4())), self.chunk(), "not_found"),
                 (replace(self.binding, boot_id=str(uuid4())), self.chunk(), "idempotency_conflict"),
                 (replace(self.binding, installation_id=str(uuid4())), self.chunk(), "target_changed"),
                 (self.binding, replace(self.chunk(), source_id=str(uuid4())), "not_found"),
                 (self.binding, replace(self.chunk(), source_epoch=str(uuid4())), "not_found"),
                 (self.binding, self.chunk(offset=1), "revision_conflict"))
        for binding, chunk, code in cases:
            with self.subTest(code=code), self.assertRaises(CoreError) as error:
                self.receive(chunk, binding)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(CoreError) as unknown:
            self.app.archive.state(Invocation(actor_id=uuid4()), self.source.source_id, self.epoch)
        self.assertEqual(unknown.exception.code, "not_found")
        self.assertEqual(self.inventory(), before)
        result = self.create(start=False)
        binding = replace(self.binding, agent_id=result.data["agent"]["id"], run_id=str(uuid4()))
        source = replace(self.source, source_id=str(uuid4()), agent_id=binding.agent_id)
        self.register(source=source, binding=binding)
        with self.assertRaises(CoreError) as not_ready:
            self.receive(self.chunk(source=source), binding)
        self.assertEqual(not_ready.exception.code, "not_ready")
        self.assertFalse((self.settings.data_root / "agents" / binding.agent_id).exists())

    def test_maximum_unicode_metadata_stays_bounded_and_permanent_guards(self):
        full = "\U0001f340" * 4096
        metadata = ArchiveMetadata(capability_evidence=full, locator=full, file_identity=full)
        self.register(metadata=metadata)
        successor = str(uuid4())
        self.register(epoch=successor, metadata=metadata.model_copy(update={"previous_epoch": self.epoch, "divergence_reason": full}))
        self.receive()
        with self.assertRaises(CoreError):
            self.receive(self.chunk(b"different"))
        before = self.inventory()
        for table in ("archive_runs", "archive_sources", "archive_epochs", "archive_run_sources", "archive_chunks", "archive_conflicts"):
            row = before[table][0]
            column = next(iter(row))
            for sql in (f"UPDATE {table} SET {column}='changed'", f"DELETE FROM {table}", f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with self.subTest(table=table, sql=sql), self.assertRaises(CoreError) as error:
                    with self.app.db.transaction() as tx:
                        tx.connection.execute(sql)
                self.assertEqual(error.exception.code, "storage_blocked")
        with self.assertRaises(CoreError):
            with self.app.db.transaction() as tx:
                tx.connection.execute("UPDATE archive_epochs SET conflicted=0")
        with self.assertRaises(CoreError):
            with self.app.db.transaction() as tx:
                tx.connection.execute("UPDATE archive_epochs SET host_archived_offset=0")
        self.assertEqual(self.inventory(), before)
        for event in self.rows("events"):
            self.assertLessEqual(len(event["payload_json"].encode()), 65536)
            self.assertNotIn("payload", json.loads(event["payload_json"]))

    def test_legacy_v3_migration_preserves_data_and_prior_checksums(self):
        root = self.root / "legacy"
        root.mkdir()
        lock = HostLock(root / "locks" / "host.lock").acquire()
        self.addCleanup(lock.close)
        db = Database(root / "state.sqlite3", lock)
        db.migrate(migrations=MIGRATIONS[:3])
        with sqlite3.connect(db.path) as conn:
            conn.execute("INSERT INTO installation(singleton,installation_id,recovery_epoch,created_at) VALUES(1,?,?,?)",
                         (str(uuid4()), str(uuid4()), self.clock()))
            conn.execute("INSERT INTO actors VALUES(?,?,?,?)", (str(self.owner.actor_id), "owner", None, self.clock()))
            before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in ("installation", "actors", "schema_migrations")}
        with self.assertRaises(CoreError) as unsupported:
            db.validate()
        self.assertEqual(unsupported.exception.code, "unsupported_schema")
        db.migrate()
        with db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA user_version").fetchone()[0], 7)
            for table, rows in before.items():
                condition = " WHERE version<=3" if table == "schema_migrations" else ""
                self.assertEqual([tuple(r) for r in tx.connection.execute(f"SELECT * FROM {table}{condition}")], rows)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(tx.connection.execute("SELECT count(*) FROM archive_chunks").fetchone()[0], 0)
