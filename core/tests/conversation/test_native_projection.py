"""Archive-to-ledger checks use only disposable, synthetic native sources."""

from dataclasses import replace
import hashlib
import json
from unittest.mock import patch
from uuid import UUID, uuid4

from adapters.test_native_records import SESSION, claude, codex, raw
from messaging.support import ChatCase
from qorqut_core.adapters.native_records import MAX_RECORD_BYTES, PARSER_VERSION, parse_native_record
from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.conversation.native import NativeProjector, NativeProvenance
from qorqut_core.runner.models import ProcessFingerprint
from qorqut_core.runtime import OwnerCandidate, ProcessObservation, SessionOwners
from qorqut_core.storage.database import Database, assert_external_io_allowed
from qorqut_core.storage.migrations import MIGRATIONS
from qorqut_core.transcripts.archive_models import ArchiveMetadata
from qorqut_core.transcripts.spool import CHUNK_BYTES, Chunk, RunBinding, Source
from test_foundation import InjectedCrash


class NativeProjection(ChatCase):
    def bind(self, provider="codex"):
        self.provider = provider
        self.version = "0.160.0" if provider == "codex" else "2.1.287"
        self.observation = "live"
        self.owners = SessionOwners(self.app, observe_process=lambda _: ProcessObservation(
            state=self.observation, observed_at=self.clock(), reason="synthetic"))
        self.candidate = OwnerCandidate(agent_id=self.onbash["id"], terminal_id=uuid4(),
            recovery_epoch=self.config()["recovery_epoch"], environment_generation=1,
            provider=provider, cli_version=self.version, native_session_id=SESSION,
            process=ProcessFingerprint(boot_id=uuid4(), pid=123, start_ticks=123, uid=1000, pid_namespace=123))
        owner = self.owners.connect(self.candidate)
        self.binding = RunBinding(str(self.settings.installation_id), self.onbash["id"],
            str(self.candidate.recovery_epoch), 1, str(self.candidate.process.boot_id), str(owner.run_id), owner.run_generation)
        self.source = Source(str(uuid4()), self.binding.installation_id, self.binding.agent_id, "native", SESSION)
        self.epoch, self.offset = str(uuid4()), 0
        self.app.archive.register(self.context, self.binding, self.source, self.epoch,
            ArchiveMetadata(capability_evidence="Synthetic pinned-format fixture; no process or provider execution"))

    def archive(self, payload, size=CHUNK_BYTES):
        for start in range(0, len(payload), size):
            part = payload[start:start+size]
            self.app.archive.receive(self.context, self.binding,
                Chunk(self.source.source_id, self.epoch, self.offset, len(part), hashlib.sha256(part).hexdigest(), part))
            self.offset += len(part)

    def consume(self, **kwargs):
        return NativeProjector(self.app).consume(self.context, self.binding, self.source.source_id, self.epoch, **kwargs)

    def provenance(self, payload, offset=0):
        return NativeProvenance(provider=self.provider, run_id=self.binding.run_id, source_id=self.source.source_id,
            source_epoch=self.epoch, offset=offset, length=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
            parser_version=PARSER_VERSION)

    def test_chunk_boundaries_partial_restart_and_oversize_have_durable_coordinates(self):
        self.bind()
        equal = raw(codex("equal")) + raw(codex("equal"))
        long_text = " a文🙂\n" * 32000
        large = raw(codex(long_text, role="AgentMessage"))
        giant = b"x" * (MAX_RECORD_BYTES + 300001) + b"\n"
        tail = raw(codex("after giant", timestamp=None))[:-1]
        data = equal + large + giant + tail
        self.archive(data[:37])
        self.archive(data[37:])
        with patch.object(self.app.archive, "read_chunk", wraps=self.app.archive.read_chunk) as reads:
            first = self.consume(max_bytes=53)
            self.assertEqual((first.through_offset, first.scan_offset), (0, 53))
            self.assertEqual(self.rows("conversation_native"), [])
            self.assertEqual([call.args[3] for call in reads.call_args_list], [0, 37])
        self.restart()
        saw_oversize_partial = False
        for _ in range(40):
            checkpoint = self.consume(max_bytes=200001)
            if len(equal + large) + MAX_RECORD_BYTES < checkpoint.scan_offset < len(equal + large + giant):
                saw_oversize_partial = True
                self.assertEqual(checkpoint.through_offset, len(equal + large))
                self.assertEqual(self.rows("native_parse_gaps"), [])
            if checkpoint.scan_offset == len(data):
                break
        self.assertTrue(saw_oversize_partial)
        self.assertEqual(checkpoint.through_offset, len(data) - len(tail))
        self.assertEqual(checkpoint.scan_offset, len(data))
        gaps = self.rows("native_parse_gaps")
        self.assertEqual([(g["from_offset"], g["through_offset"], g["reason"]) for g in gaps],
            [(len(equal + large), len(equal + large + giant), "record_too_large")])
        entries = self.conversation(self.onbash)["items"]
        self.assertEqual([e["text"] for e in entries[:2]], ["equal", "equal"])
        self.assertNotEqual(entries[0]["source"]["native_id"], entries[1]["source"]["native_id"])
        self.assertEqual("".join(e["text"] for e in entries[2:]), long_text)
        self.clock.advance(100)
        self.restart()
        self.archive(b"\n")
        with patch("qorqut_core.conversation.native.parse_native_record", wraps=parse_native_record) as parser:
            self.assertEqual(self.consume().through_offset, len(data) + 1)
            self.assertEqual(parser.call_count, 1)
        final = self.conversation(self.onbash)["items"][-1]
        self.assertEqual((final["text"], final["occurred_at"], final["source"]["timestamp_origin"]),
            ("after giant", checkpoint.ingested_at, "ingestion"))
        self.assertEqual(len(self.rows("native_parse_gaps")), 1)

    def test_reopen_replay_conflict_ownership_and_atomic_checkpoint(self):
        self.bind("claude")
        record = claude("original", timestamp=None)
        payload = raw(record)
        self.archive(payload)
        self.consume()
        first = self.conversation(self.onbash)["items"][0]
        counts = self.counts()
        self.restart()
        self.clock.advance(500)
        item = parse_native_record(self.provider, self.version, SESSION, payload).items[0]
        item = item.model_copy(update={"occurred_at": self.clock()})
        provenance = self.provenance(payload)
        with self.app.db.transaction() as tx:
            replay = self.app.conversations.append_native(tx, self.onbash["id"], item, provenance)
        self.assertEqual(str(replay.entry_id), first["entry_id"])
        self.assertEqual(replay.occurred_at, first["occurred_at"])
        self.assertEqual(self.counts(), counts)
        imports = [r for r in self.rows("operations") if r["name"] == "native_entry_import"]
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["actor_id"], self.onbash["actor_id"])
        self.assertEqual(json.loads(imports[0]["result_json"])["outcome"], "accepted")
        self.assertEqual(sum(j["kind"] == "conversation_publish" for j in self.rows("jobs")), 1)
        for changed_item, changed_provenance, expected in (
                (item.model_copy(update={"text": "changed"}), provenance, "idempotency_conflict"),
                (item, provenance.model_copy(update={"sha256": "0" * 64}), "idempotency_conflict"),
                (item, provenance.model_copy(update={"length": len(payload) + 1}), "target_changed"),
                (item, provenance.model_copy(update={"provider": "codex"}), "target_changed"),
                (item, provenance.model_copy(update={"parser_version": "native-v2"}), "unsupported_capability")):
            with self.assertRaises(CoreError) as error, self.app.db.transaction() as tx:
                self.app.conversations.append_native(tx, self.onbash["id"], changed_item, changed_provenance)
            self.assertEqual(error.exception.code, expected)
        wrong_parent = next(r["operation_id"] for r in self.rows("operations") if r["actor_id"] == str(self.owner.actor_id))
        with self.assertRaises(CoreError), self.app.db.transaction() as tx:
            self.app.conversations.append_native(tx, self.onbash["id"], item, provenance, operation_id=UUID(wrong_parent))
        with self.assertRaises(CoreError) as wrong_binding:
            NativeProjector(self.app).consume(self.context, replace(self.binding, run_generation=2), self.source.source_id, self.epoch)
        self.assertEqual(wrong_binding.exception.code, "target_changed")

        duplicate = raw({**record, "message": {"role": "user", "content": "conflicting native identity"}})
        self.archive(duplicate)
        self.assertEqual(self.consume().through_offset, self.offset)
        self.assertEqual(self.rows("native_parse_gaps")[0]["reason"], "native_identity_conflict")
        self.assertEqual(len(self.rows("conversation_native")), 1)
        next_payload = raw(claude("atomic", role="assistant"))
        self.archive(next_payload)
        checkpoint, counts = self.rows("native_parse_checkpoints"), self.counts()
        self.fault.point = "native_projection_before_commit"
        with self.assertRaises(InjectedCrash):
            self.consume()
        self.assertEqual(self.rows("native_parse_checkpoints"), checkpoint)
        self.assertEqual(self.counts(), counts)
        self.restart()
        self.assertEqual(self.consume().through_offset, self.offset)
        self.assertEqual(len(self.rows("conversation_native")), 2)
        # The read boundary refuses damaged bytes; the saved checkpoint remains.
        start = self.offset
        self.archive(raw(claude("unread")))
        chunk = next(c for c in self.rows("archive_chunks") if c["offset"] == start)
        path = self.settings.data_root / chunk["object_path"]
        path.write_bytes(b"corrupt")
        with self.assertRaises(CoreError):
            self.consume()
        self.assertEqual(self.rows("native_parse_checkpoints")[0]["scan_offset"], start)

    def test_schema_six_and_automatic_utc_publication_with_closed_owner(self):
        self.app.close()
        self.settings = self.settings.model_copy(update={"data_root": self.root / "schema-six"})
        migrate = Database.migrate
        with patch("qorqut_core.storage.database.SCHEMA_VERSION", 6), \
                patch.object(Database, "migrate", lambda db: migrate(db, migrations=MIGRATIONS[:6])):
            self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock)
            self.onbash = next(r for r in self.rows("agents") if r["kind"] == "host")
            self.pump()
            self.send("legacy", "legacy exact 文\n", recipient=self.onbash)
            self.pump()
            old_file = self.path(self.onbash).read_bytes()
            old_rows = self.rows("conversation_entries")
            old_operations = self.rows("operations")
            self.app.close()
        self.app.db.lock.acquire()
        try:
            self.app.db.migrate()
        finally:
            self.app.db.lock.close()
        self.app = Application.open(self.settings, clock=self.clock)
        self.assertEqual(self.rows("conversation_entries"), old_rows)
        self.assertEqual(self.rows("operations"), old_operations)
        self.assertEqual(self.path(self.onbash).read_bytes(), old_file)
        self.assertEqual((len(self.app.registry), len(self.app.jobs.handlers)), (16, 5))
        self.bind("claude")
        self.observation = "exited"
        self.assertTrue(self.owners.release(UUID(self.binding.run_id), self.candidate.process))
        self.archive(raw(claude("native user", timestamp="2026-10-06T20:00:00-04:00"))
            + raw(claude("native agent", role="assistant", timestamp=None)))
        archive_read = self.app.archive.read_chunk
        def checked_read(*args):
            assert_external_io_allowed()
            return archive_read(*args)
        with patch.object(self.app.archive, "read_chunk", side_effect=checked_read):
            self.consume()
        self.pump()
        published = self.path(self.onbash).read_bytes()
        self.assertTrue(published.startswith(old_file))
        rows = [json.loads(line) for line in published.splitlines()]
        self.assertEqual([row["text"] for row in rows], ["legacy exact 文\n", "native user", "native agent"])
        self.assertEqual([row["format_version"] for row in rows], [1, 1, 1])
        self.assertEqual(rows[1]["sender_actor_id"], None)
        self.assertEqual(rows[2]["sender_actor_id"], self.onbash["actor_id"])
        self.assertEqual(rows[1]["occurred_at"], "2026-10-07T00:00:00.000000Z")
        native = self.conversation(self.onbash)["items"][1:]
        self.assertTrue(all(row["delivery"] is None and row["handling"] is None for row in native))
        self.assertTrue(all("message_id" not in row["source"] for row in native))
        previous_path = self.path(self.onbash)
        self.clock.advance(86400)
        self.archive(raw(claude("late", role="assistant", timestamp="2020-01-01T00:00:00Z")))
        self.restart()
        self.consume()
        self.pump()
        self.assertEqual(previous_path.read_bytes(), published)
        self.assertEqual(json.loads(self.path(self.onbash).read_bytes())["text"], "late")
        self.assertEqual(self.conversation(self.onbash)["publication"]["state"], "current")
        self.assertEqual(self.rows("session_owners")[0]["state"], "closed")
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
