"""Every native user turn is indexed once as a pointer into the archive, never as text."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest

from qorqut.native_chat import projection, schema, store
from qorqut.native_chat.captures import Indexer, StepResult
from qorqut.native_chat.contracts import (InputRequest, STATE_SUBMITTED, STATE_SUBMITTING, STATE_WAITING, TYPE_BROADCAST, TYPE_CHAT,
                                          TYPE_NUDGE, TYPE_TELEGRAM)

ASK = "Please send the Linden contract today."
STAMP = "2026-11-12T09:00:00.000000Z"
SESSION = {"agent_id": "agent", "provider": "claude", "conversation_id": "session", "id": "binding",
           "transcript_path": "/registered/session.jsonl"}


def record(value, offset):
    return SimpleNamespace(kind="json", value=value, offset=offset, length=10, end=offset + 10)


def claude(text, uuid, kind="user", **metadata):
    return {"type": kind, "sessionId": "session", "uuid": uuid, **metadata, "message": {"role": kind, "content": text}}


def reply(uuid="reply"):
    value = claude("Done.", uuid, "assistant")
    value["message"]["stop_reason"] = "end_turn"
    return value


class OwnerTurnIndexTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE principals(id TEXT PRIMARY KEY)")
        for statement in schema.SCHEMA:
            self.conn.execute(statement)
        self.conn.executemany("INSERT INTO principals VALUES(?)", [("agent",), ("owner",)])
        self.bind("claude")

    def bind(self, provider, generation=1):
        self.session = {**SESSION, "provider": provider, "id": f"binding-{provider}"}
        self.conn.execute(
            "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
            "observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.session["id"], "agent", provider, "session", generation, "terminal", 1, "started", STAMP, STAMP))

    def index(self, values, *, batch=None):
        records = [record(value, position * 10) for position, value in enumerate(values)]
        conn = self.conn

        class Archive:
            @contextmanager
            def iter_records(self, receipt, offset=0):
                yield iter(row for row in records if row.offset >= offset)

        class Database:
            @contextmanager
            def transaction(self, **kwargs):
                with conn:
                    yield SimpleNamespace(connection=conn)

            @contextmanager
            def read(self):
                yield SimpleNamespace(connection=conn)

        runtime = SimpleNamespace(database=Database(), application=SimpleNamespace(ensure_writes_open=lambda uow: None))
        receipt = SimpleNamespace(record_boundary=len(records) * 10, length=len(records) * 10, sequence=1, receipt_id="receipt",
                                  archive_id="archive", complete=True)
        if store.get_capture(conn, "agent", self.session["provider"], "session") is None:
            store.ensure_capture(conn, self.session)
        for _ in range(len(records) + 1):
            capture = store.get_capture(conn, "agent", self.session["provider"], "session")
            result = StepResult()
            Indexer(runtime)._index(Archive(), capture, self.session, receipt, False, result)
            if not result.more:
                break
        return self.turns()

    def turns(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM native_owner_turns ORDER BY raw_offset")]

    def admit(self, text, input_type=TYPE_CHAT, *, state=STATE_SUBMITTED, sensitive=False, key=None):
        request = InputRequest("agent", input_type, key or f"{input_type}:{text}", text,
                               principal_id="owner" if input_type == TYPE_CHAT else None, sensitive=sensitive)
        row = store.insert_input(self.conn, self.session, request, store.request_hash(request), state=STATE_SUBMITTING)
        if state == STATE_SUBMITTED:
            store.settle_input(self.conn, row["id"], STATE_SUBMITTED, None, {}, (STATE_SUBMITTING,))
        return store.get_input(self.conn, row["id"])

    def test_only_what_a_person_typed_is_indexed_and_no_text_is_stored(self):
        nudge = projection.marker_text("marker-12345678", TYPE_NUDGE, "You have 2 unread inbox items.")
        tool_result = {"type": "user", "sessionId": "session", "uuid": "tool", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call", "content": "ok"}]}}
        turns = self.index([
            claude(ASK, "ask", timestamp="2026-11-12T11:00:00.5+02:00"),
            claude(nudge, "nudge"),
            claude("<command-name>/compact</command-name>\n<command-args>keep decisions</command-args>", "command"),
            tool_result,
            claude("Summary of the earlier conversation.", "summary", isCompactSummary=True),
            claude("<system-reminder>internal</system-reminder>", "reminder"),
            reply(),
        ])
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertEqual((turn["identity_kind"], turn["identity_key"], turn["raw_offset"], turn["raw_length"]),
                         ("native_id", "ask", 0, 10))
        # The record's own time, in the stored UTC form.
        self.assertEqual(turn["occurred_at"], "2026-11-12T09:00:00.500000Z")
        self.assertEqual((turn["via"], turn["input_id"], turn["redacted"], turn["chars"]), ("typed", None, 0, len(ASK)))
        self.assertEqual((turn["session_id"], turn["receipt_id"], turn["archive_id"]), ("binding-claude", "receipt", "archive"))
        self.assertNotIn(ASK, [value for value in turn.values() if isinstance(value, str)])
        # The reply index is untouched by the new rows.
        self.assertEqual(self.conn.execute("SELECT count(*) FROM native_replies").fetchone()[0], 1)

    def test_a_codex_user_message_is_indexed(self):
        self.bind("codex", generation=2)
        turns = self.index([
            {"type": "session_meta", "payload": {"id": "session"}},
            {"type": "event_msg", "timestamp": "2026-11-12T09:30:00Z", "payload": {"type": "user_message", "message": ASK}},
        ])
        self.assertEqual([(turn["provider"], turn["identity_kind"], turn["raw_offset"], turn["occurred_at"]) for turn in turns],
                         [("codex", "raw_range", 10, "2026-11-12T09:30:00.000000Z")])

    def test_bounded_batches_and_a_repeated_insert_index_each_turn_once(self):
        from unittest.mock import patch
        with patch("qorqut.native_chat.captures.BATCH_RECORDS", 1):
            turns = self.index([claude(ASK, "one"), reply("first"), claude("And the invoice.", "two"), reply("second")])
        self.assertEqual([turn["identity_key"] for turn in turns], ["one", "two"])
        lineage = projection.Lineage("agent", "claude", "session", "archive")
        again = store.insert_owner_turn(self.conn, lineage=lineage, kind="native_id", key="one", session_id="binding-claude",
                                        receipt_id="receipt", offset=0, length=10, occurred_at=STAMP,
                                        digest=store.text_digest(ASK), chars=len(ASK))
        self.assertIsNone(again)
        self.assertEqual(len(self.turns()), 2)

    def test_a_turn_is_matched_to_the_delivered_input_that_typed_it(self):
        chat = self.admit(ASK)
        broadcast = self.admit("Team: stop all sends.", TYPE_BROADCAST)
        self.admit("Never typed.", state=STATE_WAITING, key="waiting")
        self.conn.execute("UPDATE native_inputs SET state=? WHERE source_key='waiting'", (STATE_WAITING,))
        turns = self.index([claude("  " + ASK + "\n", "ask"), claude(ASK, "again"), claude("Team: stop all sends.", "broadcast"),
                            claude("Never typed.", "terminal")])
        self.assertEqual([(turn["identity_key"], turn["via"], turn["input_id"]) for turn in turns], [
            ("ask", "chat", chat["id"]),            # surrounding whitespace does not matter
            ("again", "typed", None),               # one input types one turn; the repeat was entered in the terminal
            ("broadcast", "broadcast", broadcast["id"]),
            ("terminal", "typed", None)])           # an input that was never delivered matches nothing
        # The delivered input's own text is gone; only its digest let the match be made.
        self.assertIsNone(chat["body"])

    def test_an_input_admitted_after_the_turn_was_written_is_not_its_source(self):
        turns = self.index([claude(ASK, "early", timestamp="2020-01-01T00:00:00Z")])
        self.admit(ASK)
        self.assertEqual((turns[0]["via"], turns[0]["input_id"]), ("typed", None))

    def test_a_turn_that_may_be_a_private_input_is_withheld(self):
        private = self.admit("secret-code-4417", TYPE_TELEGRAM, sensitive=True, state=STATE_SUBMITTING)
        self.assertNotIn("text_sha256", self.conn.execute("SELECT metadata_json FROM native_inputs WHERE id=?",
                                                          (private["id"],)).fetchone()[0])
        turns = self.index([claude("secret-code-4417", "private")])
        self.assertEqual([(turn["via"], turn["input_id"], turn["redacted"], turn["chars"]) for turn in turns],
                         [("typed", None, 1, 0)])

    def test_a_private_input_long_settled_withholds_nothing(self):
        private = self.admit("secret-code-4417", TYPE_TELEGRAM, sensitive=True)
        self.conn.execute("DROP TRIGGER native_inputs_guard")
        self.conn.execute("UPDATE native_inputs SET created_at='2026-01-01T00:00:00.000000Z',updated_at='2026-01-01T00:00:05.000000Z',"
                          "settled_at='2026-01-01T00:00:05.000000Z' WHERE id=?", (private["id"],))
        turns = self.index([claude(ASK, "ask")])
        self.assertEqual((turns[0]["redacted"], turns[0]["chars"]), (0, len(ASK)))

    def test_only_owner_inputs_that_are_not_private_keep_a_text_digest(self):
        chat = self.admit(ASK)
        metadata = self.conn.execute("SELECT metadata_json FROM native_inputs WHERE id=?", (chat["id"],)).fetchone()[0]
        self.assertIn(store.text_digest(ASK), metadata)
        request = InputRequest("agent", TYPE_NUDGE, "nudge:1", "You have unread items.")
        nudge = store.insert_input(self.conn, self.session, request, store.request_hash(request), state=STATE_SUBMITTING,
                                   marker="marker-12345678")
        self.assertEqual(self.conn.execute("SELECT metadata_json FROM native_inputs WHERE id=?", (nudge["id"],)).fetchone()[0], "{}")

    def test_the_index_is_append_only(self):
        self.index([claude(ASK, "ask")])
        for statement in ("UPDATE native_owner_turns SET redacted=1", "DELETE FROM native_owner_turns"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement)


if __name__ == "__main__":
    unittest.main()
