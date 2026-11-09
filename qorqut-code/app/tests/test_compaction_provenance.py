"""Compaction cannot become owner speech, a delivery, or a unit of owner work."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut.domains.learnings import analysis
from qorqut.native_chat import projection, schema, store
from qorqut.native_chat.reader import ConversationReader
from qorqut.native_chat.captures import Indexer, StepResult


SUMMARY = "This session is being continued from a previous conversation. Keep this context."


def record(value, offset=0):
    return SimpleNamespace(kind="json", value=value, offset=offset, length=10, end=offset + 10)


def claude(kind="user", text=SUMMARY, **metadata):
    return {"type": kind, "sessionId": "session", "uuid": "input", **metadata,
            "message": {"role": kind, "content": text}}


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


class CompactionProvenanceTests(unittest.TestCase):
    def projector(self, provider):
        projector = projection.Projector(provider, "session")
        if provider == "codex":
            projector.feed(record({"type": "session_meta", "payload": {"id": "session"}}))
        return projector

    def summaries(self, provider):
        if provider == "claude":
            return [claude(isCompactSummary=True), claude(isCompactSummary=True, isMeta=True),
                    {"type": "summary", "summary": SUMMARY, "sessionId": "session"},
                    {"type": "system", "subtype": "compact_boundary", "sessionId": "session"}]
        return [{"type": "compacted", "payload": {"message": SUMMARY}},
                event("context_compacted"),
                event("item_completed", item={"type": "ContextCompaction", "id": "compact"}),
                event("user_message", message=SUMMARY, isCompactSummary=True),
                event("item_completed", item={"type": "UserMessage", "id": "compact",
                      "isCompactSummary": True, "content": [{"type": "text", "text": SUMMARY}]})]

    def final(self, provider):
        if provider == "claude":
            value = claude("assistant", "Continuing.")
            value["message"]["stop_reason"] = "end_turn"
            return value
        return {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                "id": "reply", "channel": "final", "content": [{"type": "output_text", "text": "Continuing."}]}}

    def user(self, provider, text="Please continue."):
        return claude(text=text) if provider == "claude" else event("user_message", message=text)

    def test_summary_never_becomes_a_chat_or_delivery_or_evidence_entry(self):
        # All three consumers use Projector.entries; no text matching is involved.
        for provider in ("claude", "codex"):
            for summary in self.summaries(provider):
                with self.subTest(provider=provider, summary=summary):
                    projector = self.projector(provider)
                    before = projector.state.dump()
                    outcome = projector.feed(record(summary))
                    self.assertEqual(outcome.entries, ())
                    self.assertTrue(outcome.hidden)
                    self.assertIsNone(outcome.completion)
                    self.assertEqual(projector.state.dump(), before)
                    completion = projector.feed(record(self.final(provider), 10)).completion
                    self.assertFalse(completion.user_work)
                    self.assertNotEqual(completion.origin, projection.ORIGIN_USER)

    def test_compaction_preserves_real_work_and_system_origin_across_saved_state(self):
        for provider in ("claude", "codex"):
            for real_input in (False, True):
                projector = self.projector(provider)
                projector.state.origin = projection.ORIGIN_NUDGE
                if real_input:
                    projector.feed(record(self.user(provider)))
                for summary in self.summaries(provider):
                    projector.feed(record(summary, 10))
                projector = projection.Projector(provider, "session", projection.ProjectorState.load(projector.state.dump()))
                completion = projector.feed(record(self.final(provider), 20)).completion
                self.assertEqual(completion.origin, projection.ORIGIN_NUDGE)
                self.assertEqual(completion.user_work, real_input)

    def test_pasted_summary_and_compaction_request_are_still_owner_input(self):
        for provider in ("claude", "codex"):
            for text in (SUMMARY, "Please compact this conversation.", '{"isCompactSummary":true}'):
                projector = self.projector(provider)
                entry = projector.feed(record(self.user(provider, text))).entry
                self.assertEqual(entry.speaker, projection.SPEAKER_USER)
                self.assertEqual(entry.text, text)
                self.assertTrue(projector.feed(record(self.final(provider), 10)).completion.user_work)

    def test_old_archive_window_starting_at_summary_does_not_show_it_as_owner(self):
        records = [record(claude(isCompactSummary=True)), record(claude(text="Actual owner input"), 10)]
        class Archive:
            @contextmanager
            def iter_records(self, receipt, offset=0):
                yield iter(row for row in records if row.offset >= offset)
        entries = ConversationReader._project(Archive(), None, "claude", "session", 0, 20)
        self.assertEqual([entry.text for entry in entries], ["Actual owner input"])

    def test_reply_counter_does_not_advance_owner_work_for_summary_only_turn(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE principals(id TEXT PRIMARY KEY)")
        for statement in schema.SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO principals VALUES('agent')")
        for index, real_input in enumerate((False, True)):
            projector = self.projector("claude")
            if real_input:
                projector.feed(record(self.user("claude")))
            projector.feed(record(claude(isCompactSummary=True), 10))
            completion = projector.feed(record(self.final("claude"), 20)).completion
            store.insert_reply(conn, lineage=projection.Lineage("agent", "claude", "session", "archive"),
                kind=completion.identity_kind, key=f"reply-{index}", session_id=None, receipt_id="receipt",
                offset=20, length=10, completed_at="2026-11-09T00:00:00Z", preview=completion.preview,
                origin=completion.origin, user_work=completion.user_work)
            self.assertEqual(conn.execute("SELECT work_sequence FROM native_agent_counters").fetchone()[0], index)

    def core(self, records):
        return analysis.build_core({"source_id": "registered/session.jsonl", "content": "\n".join(map(json.dumps, records))})

    def test_learning_counts_and_owner_evidence_exclude_every_compaction_form(self):
        for provider in ("claude", "codex"):
            for summary in self.summaries(provider):
                with self.subTest(provider=provider, summary=summary):
                    records = [self.user(provider, "Real instruction."), summary, self.final(provider)]
                    if provider == "codex":
                        records.insert(0, {"type": "session_meta", "payload": {"id": "session"}})
                    core = self.core(records)
                    self.assertEqual(core["stats"]["owner_messages"], 1)
                    self.assertEqual(analysis.owner_blocks(core["markdown"]), ["Real instruction."])
                    self.assertIn("(no owner message)", core["markdown"])

    def test_compaction_only_learning_has_no_owner_quotes(self):
        core = self.core([claude(isCompactSummary=True), self.final("claude")])
        self.assertEqual(core["stats"]["owner_messages"], 0)
        self.assertEqual(analysis.owner_blocks(core["markdown"]), [])
        self.assertIn("COMPACTION SUMMARY (harness)", core["markdown"])

    def test_old_inflight_cursor_is_repaired_in_batches_before_counting_new_replies(self):
        for real_input in (False, True):
            with self.subTest(real_input=real_input):
                conn = sqlite3.connect(":memory:")
                self.addCleanup(conn.close)
                conn.row_factory = sqlite3.Row
                conn.execute("CREATE TABLE principals(id TEXT PRIMARY KEY)")
                for statement in schema.SCHEMA:
                    conn.execute(statement)
                conn.execute("INSERT INTO principals VALUES('agent')")
                session = {"agent_id": "agent", "provider": "claude", "conversation_id": "session",
                           "id": "binding", "transcript_path": "/registered/session.jsonl"}
                capture = store.ensure_capture(conn, session)
                stale = json.dumps({"origin": "user", "work": True})
                store.update_capture(conn, capture, indexed_offset=20, captured_length=30, index_state_json=stale)
                records = [record(self.user("claude") if real_input else self.final("claude")),
                           record(claude(isCompactSummary=True), 10), record(self.final("claude"), 20)]
                class Archive:
                    @contextmanager
                    def iter_records(self, receipt, offset=0):
                        yield iter(row for row in records if row.offset >= offset)
                class Database:
                    @contextmanager
                    def transaction(self, **kwargs):
                        with conn:
                            yield SimpleNamespace(connection=conn)
                runtime = SimpleNamespace(database=Database(), application=SimpleNamespace(ensure_writes_open=lambda uow: None))
                receipt = SimpleNamespace(record_boundary=30, length=30, sequence=1, receipt_id="receipt",
                                          archive_id="archive", complete=True)
                # Restart the indexer after every bounded batch, including halfway
                # through repair. Replayed historical completions have no effects.
                with patch("qorqut.native_chat.captures.BATCH_RECORDS", 1):
                    for batch in range(3):
                        capture = store.get_capture(conn, "agent", "claude", "session")
                        result = StepResult()
                        Indexer(runtime)._index(Archive(), capture, session, receipt, False, result)
                        self.assertEqual(result.completions, 1 if batch == 2 else 0)
                        if batch < 2:
                            self.assertTrue(result.more)
                            self.assertEqual(conn.execute("SELECT count(*) FROM native_replies").fetchone()[0], 0)
                current = store.get_capture(conn, "agent", "claude", "session")
                self.assertEqual(current["indexed_offset"], 30)
                self.assertEqual(json.loads(current["index_state_json"])["provenance_version"], projection.PROVENANCE_VERSION)
                self.assertEqual(conn.execute("SELECT user_work FROM native_replies").fetchone()[0], int(real_input))
                self.assertEqual(store.counters(conn, "agent")["work_sequence"], int(real_input))


if __name__ == "__main__":
    unittest.main()
