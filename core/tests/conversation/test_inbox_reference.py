"""D53 read views over temporary archives and explicitly synthetic receipts."""

from contextlib import contextmanager
from dataclasses import replace
import json
from unittest.mock import patch
from uuid import UUID, uuid4

from adapters.test_native_records import claude, codex, raw
from conversation import test_native_projection as native
from messaging.support import ChatCase
from messaging.test_dispatch import SyntheticInput
from qorqut_core.contracts import canonical, canonical_hash
from qorqut_core.conversation.ledger import transcript_line
from qorqut_core.conversation.models import ConversationItem, ConversationList, Entry
from qorqut_core.messaging import attempts
from qorqut_core.messaging.notifications import notification_text
from qorqut_core.transcripts.archive_models import ArchiveMetadata


class InboxReferences(ChatCase):
    archive = native.NativeProjection.archive
    consume = native.NativeProjection.consume

    def bind(self, provider="codex"):
        native.NativeProjection.bind(self, provider)
        self.runtime = SyntheticInput(self)
        self.set_target()
        self.app.runtime_input = self.runtime

    def set_target(self):
        old = self.runtime.target.binding
        process = self.candidate.process
        binding = old.model_copy(update={
            "run_id": UUID(self.binding.run_id), "run_generation": self.binding.run_generation,
            "boot_id": process.boot_id, "native": process, "recorder": process,
            "tmux": old.tmux.model_copy(update={"server": process, "pane": process})})
        self.runtime.target = self.runtime.target.model_copy(update={"binding": binding,
            "terminal_id": self.candidate.terminal_id, "native_session_id": self.candidate.native_session_id,
            "provider": self.provider, "cli_version": self.version,
            "idle_event_seq": self.runtime.target.idle_event_seq + 1})

    def project(self, *records):
        self.archive(b"".join(raw(record) for record in records))
        self.consume()

    def page(self, tx=None, **arguments):
        if tx is None:
            return self.conversation(self.onbash, **arguments)
        return self.app.conversations.list(tx, ConversationList(agent_id=self.onbash["id"], **arguments),
            self.context, None).data.model_dump(mode="json")

    @contextmanager
    def damaged(self):
        # Only this disposable DB, rolled back with its guards. Exercise reads
        # of bad saved evidence without weakening production write invariants.
        with self.app.db.transaction() as tx:
            tx.connection.execute("SAVEPOINT damaged_fixture")
            for row in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                tx.connection.execute(f'DROP TRIGGER "{row[0]}"')
            tx.connection.execute("DROP INDEX delivery_attempts_idle")
            tx.connection.execute("PRAGMA ignore_check_constraints=ON")
            tx.connection.execute("PRAGMA defer_foreign_keys=ON")
            try:
                yield tx
            finally:
                tx.connection.execute("ROLLBACK TO damaged_fixture")
                tx.connection.execute("RELEASE damaged_fixture")
                tx.connection.execute("PRAGMA ignore_check_constraints=OFF")
                tx.connection.execute("PRAGMA defer_foreign_keys=OFF")

    def test_matching_failures_and_resumed_source_lineage(self):
        self.bind()
        message = self.send(recipient=self.onbash).data["message"]["message_id"]
        pointer = notification_text(UUID(message))
        self.pump()
        plain = self.send("plain-pointer", pointer, recipient=self.onbash).data["entries"][0]
        self.project(codex(pointer), codex(pointer), codex(pointer, role="AgentMessage"),
            codex("prefix " + pointer), codex(pointer + "\n"), codex(pointer.upper()),
            codex(notification_text(uuid4())),
            codex("x" * 65536 + pointer))
        page = self.page()
        positives = [e for e in page["items"] if e["inbox_reference"] is not None]
        self.assertEqual(len(positives), 2)  # Identical manual input is indistinguishable.
        original = next(e for e in page["items"] if e["source"].get("message_id") == message)
        expected = {"message_id": message, "message_entry_id": original["entry_id"]}
        self.assertTrue(all(e["inbox_reference"] == expected for e in positives))
        self.assertNotEqual(positives[0]["entry_id"], positives[1]["entry_id"])
        self.assertTrue(all(e["delivery"] is None and e["handling"] is None for e in positives))
        self.assertIsNone(next(e for e in page["items"] if e["entry_id"] == plain["entry_id"])["inbox_reference"])
        first, second = positives
        one = dict(before_seq=first["conversation_seq"] + 1, recovery_epoch=page["recovery_epoch"], limit=1)
        self.assertEqual(self.page(**one)["items"][0]["inbox_reference"], expected)
        self.assertTrue(self.page(**one)["has_older"] and self.page(**one)["has_newer"])

        def absent(tx):
            item = self.page(tx, **one)["items"][0]
            self.assertIsNone(item["inbox_reference"])
            self.assertEqual(item["text"], pointer)
            self.assertEqual(item["entry_id"], first["entry_id"])

        attempt = self.rows("delivery_attempts")[0]
        corruptions = [
            ("delivery_attempts", "state", "uncertain"),
            ("delivery_attempts", "state", "deferred"),
            ("delivery_attempts", "state", "prepared"),
            ("delivery_attempts", "target_json", "{}"),
            ("delivery_attempts", "target_json", "not JSON"),
            ("delivery_attempts", "target_sha256", "0" * 64),
            ("delivery_attempts", "payload_sha256", "0" * 64),
            ("delivery_attempts", "payload_bytes", 50),
            ("delivery_attempts", "runner_receipt_json", None),
            ("delivery_attempts", "runner_receipt_json", "not JSON"),
            ("delivery_attempts", "runner_receipt_json", "{}"),
            ("archive_epochs", "conflicted", 1),
            ("archive_epochs", "registered_recovery_epoch", str(uuid4())),
            ("archive_sources", "native_session_id", str(uuid4())),
            ("archive_runs", "run_generation", 99),
            ("archive_runs", "environment_generation", 99),
            ("archive_runs", "boot_id", str(uuid4())),
            ("archive_runs", "recovery_epoch", str(uuid4())),
            ("session_owners", "terminal_id", str(uuid4())),
            ("session_owners", "process_json", "{}"),
            ("messages", "recipient_agent_id", self.a["id"]),
            ("conversation_entries", "conversation_id", self.a["conversation_id"]),
        ]
        for table, column, value in corruptions:
            with self.subTest(table=table, column=column, value=value), self.damaged() as tx:
                where = " WHERE entry_id=?" if table == "conversation_entries" else ""
                tx.connection.execute(f"UPDATE {table} SET {column}=?{where}",
                    (value, original["entry_id"]) if where else (value,))
                absent(tx)
        for field, value in (("attempt_id", str(uuid4())), ("control_epoch", 99),
                ("payload_sha256", "0" * 64), ("payload_bytes", 52),
                ("state", "uncertain"), ("phase", "paste")):
            with self.subTest(receipt=field), self.damaged() as tx:
                receipt = json.loads(attempt["runner_receipt_json"])
                receipt[field] = value
                tx.connection.execute("UPDATE delivery_attempts SET runner_receipt_json=?", (canonical(receipt),))
                absent(tx)
        for field, value in (("provider", "claude"), ("native_session_id", str(uuid4())),
                ("conversation_id", self.a["conversation_id"]), ("terminal_id", str(uuid4())),
                ("installation_id", str(uuid4()))):
            with self.subTest(target=field), self.damaged() as tx:
                target = json.loads(attempt["target_json"])
                target[field] = value
                # Rehash so the mismatched lineage must be checked independently.
                tx.connection.execute("UPDATE delivery_attempts SET target_json=?,target_sha256=?,installation_id=?",
                    (canonical(target), canonical_hash(target), target["installation_id"]))
                absent(tx)
        with self.damaged() as tx:
            tx.connection.execute("DELETE FROM conversation_entries WHERE entry_id=?", (original["entry_id"],))
            absent(tx)
        with self.damaged() as tx:
            # The other part is outside the requested page, but still disqualifies it.
            tx.connection.execute("UPDATE conversation_native SET native_id=?,part_index=1 WHERE entry_id=?",
                (first["source"]["native_id"], second["entry_id"]))
            absent(tx)
        with self.damaged() as tx:
            # Multiple valid submitted records establish the same reference;
            # neither one is exposed as the causal attempt.
            duplicate = {**attempt, "attempt_id": str(uuid4()), "attempt_seq": 2,
                "previous_attempt_id": attempt["attempt_id"]}
            receipt = json.loads(duplicate["runner_receipt_json"])
            receipt["attempt_id"] = duplicate["attempt_id"]
            duplicate["runner_receipt_json"] = canonical(receipt)
            tx.connection.execute(f"INSERT INTO delivery_attempts ({','.join(duplicate)}) VALUES ({','.join('?' for _ in duplicate)})",
                tuple(duplicate.values()))
            self.assertEqual(self.page(tx, **one)["items"][0]["inbox_reference"], expected)
            tx.connection.execute("UPDATE delivery_attempts SET target_sha256=? WHERE attempt_id=?",
                ("0" * 64, attempt["attempt_id"]))
            self.assertEqual(self.page(tx, **one)["items"][0]["inbox_reference"], expected)

        self.set_target()
        self.pump()
        with patch.object(attempts, "target", wraps=attempts.target) as checked:
            self.assertEqual(self.page(**one)["items"][0]["inbox_reference"], expected)
            self.assertEqual({call.args[0]["message_id"] for call in checked.call_args_list}, {message})
        # Already projected under run 1; run 2 alone submits the notification.
        resumed = self.send("resume", recipient=self.onbash).data["message"]["message_id"]
        self.project(codex(notification_text(UUID(resumed))))
        before = self.page()["items"][-1]
        self.assertIsNone(before["inbox_reference"])
        self.observation = "exited"
        self.assertTrue(self.owners.release(UUID(self.binding.run_id), self.candidate.process))
        self.candidate = self.candidate.model_copy(update={"process": self.candidate.process.model_copy(update={"pid": 124})})
        self.observation = "live"
        owner = self.owners.connect(self.candidate)
        self.binding = replace(self.binding, run_id=str(owner.run_id), run_generation=owner.run_generation)
        self.set_target()
        self.pump()
        self.assertTrue(any(r["message_id"] == resumed and r["state"] == "submitted" for r in self.rows("delivery_attempts")))
        self.assertIsNone(self.page()["items"][-1]["inbox_reference"])
        self.app.archive.register(self.context, self.binding, self.source, self.epoch,
            ArchiveMetadata(capability_evidence="Synthetic pinned-format fixture; no process or provider execution"))
        after = self.page()["items"][-1]
        self.assertNotEqual(after["source"]["run_id"], str(owner.run_id))
        self.assertEqual(after["inbox_reference"]["message_id"], resumed)
        self.assertEqual({k: v for k, v in before.items() if k != "inbox_reference"},
            {k: v for k, v in after.items() if k != "inbox_reference"})

    def test_annotations_preserve_entries_raw_daily_bytes_and_reopen(self):
        self.bind("claude")
        self.runtime.refusal = "runtime_unavailable"
        message = self.send(recipient=self.onbash).data["message"]["message_id"]
        pointer = notification_text(UUID(message))
        self.project(claude(pointer), claude("ordinary terminal input"))
        self.pump()
        before = self.page()
        self.assertTrue(all(e["inbox_reference"] is None for e in before["items"]))
        tables = ("conversation_entries", "conversation_native", "conversation_cursors", "conversation_publications",
            "archive_runs", "archive_sources", "archive_epochs", "archive_run_sources", "archive_chunks",
            "native_projections", "native_parse_checkpoints", "native_parse_gaps")
        saved = {table: self.rows(table) for table in tables}
        raw_files = {row["object_path"]: (self.settings.data_root / row["object_path"]).read_bytes()
            for row in saved["archive_chunks"]}
        daily = self.path(self.onbash).read_bytes()
        self.runtime.refusal = None
        self.clock.advance(61)
        self.pump()
        after = self.page()
        self.assertEqual(after["items"][1]["inbox_reference"],
            {"message_id": message, "message_entry_id": before["items"][0]["entry_id"]})
        self.assertIsNone(after["items"][2]["inbox_reference"])
        for old, new in zip(before["items"], after["items"]):
            self.assertEqual({k: old[k] for k in Entry.model_fields}, {k: new[k] for k in Entry.model_fields})
        self.assertEqual({k: v for k, v in before.items() if k != "items"},
            {k: v for k, v in after.items() if k != "items"})
        self.assertEqual(b"".join(transcript_line(ConversationItem.model_validate(e)) for e in after["items"]), daily)
        self.assertNotIn(b"inbox_reference", daily)
        self.observation = "exited"
        self.assertTrue(self.owners.release(UUID(self.binding.run_id), self.candidate.process))
        self.restart()
        counts = self.counts()
        with patch.object(self.app.archive, "read_chunk", side_effect=AssertionError("read must be SQL-only")), \
                patch.object(self.app.runtime_input, "prepare", side_effect=AssertionError("no live lookup")):
            self.assertEqual(self.page(), after)
            self.assertEqual(self.page(), after)
        self.assertEqual(self.counts(), counts)
        self.assertEqual({table: self.rows(table) for table in tables}, saved)
        self.assertEqual(self.path(self.onbash).read_bytes(), daily)
        self.assertEqual({path: (self.settings.data_root / path).read_bytes() for path in raw_files}, raw_files)
