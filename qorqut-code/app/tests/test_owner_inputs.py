"""The owner's own messages over a period: every agent's chat, the private threads and the owner's notes."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain

from qorqut.domains.owner_inputs import OwnerInputs, PAGE_CHARACTERS
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.native_chat import projection, store
from qorqut.native_chat.contracts import InputRequest, STATE_SUBMITTED, STATE_SUBMITTING

from test_side_chat_rename import build_runtime, create, invoke, reply, source

NOW = datetime(2026, 11, 12, 18, tzinfo=timezone.utc)
COVERED = "2026-11-10T00:00:00.000000Z"      # when chat indexing began in these tests


def at(hour, minute=0):
    return f"2026-11-12T{hour:02d}:{minute:02d}:00.000000Z"


class Archive:
    """One conversation's records by offset, as the transcript archive serves them."""

    def __init__(self):
        self.records = []

    def add(self, text, uuid):
        offset = len(self.records) * 10
        value = {"type": "user", "sessionId": "session", "uuid": uuid, "message": {"role": "user", "content": text}}
        self.records.append(SimpleNamespace(kind="json", value=value, offset=offset, length=10, end=offset + 10))
        return offset

    def load_receipt(self, receipt_id):
        return SimpleNamespace(receipt_id=receipt_id, record_boundary=len(self.records) * 10)

    @contextmanager
    def iter_records(self, receipt, offset=0):
        yield iter(row for row in self.records if row.offset >= offset)


class OwnerInputsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        self.archives = {}
        runtime.native_chat = SimpleNamespace(indexer=SimpleNamespace(archive=lambda profile: self.archives[profile["agent_id"]]))
        # The label of a thread's source is the Focus board's projection; the read authorization before it is the real one.
        runtime.application.attention = SimpleNamespace(legacy=SimpleNamespace(
            resolve=lambda ctx, found, uow: {**found, "label": "A side chat", "business_slug": None}))
        OwnerInputs(runtime, clock=lambda: NOW).register()
        # These tests place stored private messages at chosen times.
        self.write("DROP TRIGGER owner_conversation_messages_no_update", ())
        self.owner, self.lead, self.worker = (ctx.principal_id for ctx in (runtime.owner, runtime.coordinator, runtime.worker))

    # ----- fixtures --------------------------------------------------------------------------------------------------

    def write(self, sql, values):
        with self.runtime.database.transaction() as uow:
            uow.connection.execute(sql, values)

    def turn(self, agent, text, when, *, via="typed", redacted=False):
        """One indexed turn of ``agent``'s conversation and its record in that agent's archive."""
        archive = self.archives.setdefault(agent, Archive())
        uuid = new_id()
        offset = archive.add(text, uuid)
        lineage = projection.Lineage(agent, "claude", "session", "archive")
        with self.runtime.database.transaction() as uow:
            conn = uow.connection
            session = {"agent_id": agent, "provider": "claude", "conversation_id": "session", "id": "binding-" + agent,
                       "transcript_path": "/registered/session.jsonl"}
            if store.get_capture(conn, agent, "claude", "session") is None:
                conn.execute("INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,"
                             "process_started,observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (session["id"], agent, "claude", "session", 1, "terminal", 1, "started", COVERED, COVERED))
                capture = store.ensure_capture(conn, session)
                store.update_capture(conn, capture, receipt_id="receipt")
            delivered = None
            if via != "typed":      # delivered by the service: the turn points at the input that typed it
                request = InputRequest(agent, via, new_id(), text, principal_id=self.owner if via == "chat" else None)
                delivered = store.insert_input(conn, session, request, store.request_hash(request), state=STATE_SUBMITTING)["id"]
                store.settle_input(conn, delivered, STATE_SUBMITTED, None, {}, (STATE_SUBMITTING,))
            conn.execute(
                "INSERT INTO native_owner_turns(id,agent_id,provider,conversation_id,archive_id,identity_kind,identity_key,receipt_id,"
                "raw_offset,raw_length,occurred_at,indexed_at,chars,input_id,via,redacted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lineage.turn_id("native_id", uuid), agent, "claude", "session", "archive", "native_id", uuid, "receipt", offset,
                 10, when, COVERED, 0 if redacted else len(text), delivered, via, 1 if redacted else 0))
        return "chat:" + lineage.turn_id("native_id", uuid), offset

    def answer(self, agent, offset, when, preview="Done."):
        with self.runtime.database.transaction() as uow:
            store.insert_reply(uow.connection, lineage=projection.Lineage(agent, "claude", "session", "archive"), kind="raw_range",
                               key=f"{offset}#reply", session_id=None, receipt_id="receipt", offset=offset + 5, length=5,
                               completed_at=when, preview=preview, origin="user", user_work=True)

    def read(self, args=None, *, ctx=None, operation="owner_inputs_list"):
        return invoke(self.runtime, operation, args, ctx=ctx or self.runtime.coordinator)

    def refused(self, ctx, operation="owner_inputs_list", args=None):
        result = self.runtime.application.invoke(ctx, operation, {"scope": "global", **(args or {})},
                                                 InvocationOptions(idempotency_key=new_id()))
        return None if result.status == "ok" else plain(result.error)["code"]

    def task(self):
        identity = new_id()
        self.write("INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at) VALUES(NULL,?,?,?,?,?,?)",
                   (identity, "Linden contract", "open", self.lead, COVERED, COVERED))
        return identity

    def stamp_thread(self, text, when):
        self.write("UPDATE owner_conversation_messages SET created_at=? WHERE body=?", (when, text))

    # ----- tests -----------------------------------------------------------------------------------------------------

    def test_the_owners_words_in_every_chat_come_back_exactly_in_time_order(self):
        first, offset = self.turn(self.lead, "Send the Linden contract today.", at(9))
        second, _ = self.turn(self.worker, "Also chase the invoice.\nBefore noon.", at(10), via="chat")
        self.answer(self.lead, offset, at(9, 5), "Sent the contract to Linden.")
        result = self.read()
        self.assertEqual([(item["id"], item["channel"], item["via"], item["text"], item["agent"]["id"]) for item in result["items"]], [
            (first, "chat", "typed", "Send the Linden contract today.", self.lead),
            (second, "chat", "chat", "Also chase the invoice.\nBefore noon.", self.worker)])
        self.assertEqual(result["items"][0]["followed_by"]["reply"], {"at": at(9, 5), "preview": "Sent the contract to Linden."})
        self.assertIsNone(result["items"][1]["followed_by"]["reply"])
        self.assertEqual((result["unanswered"], result["next_cursor"], result["chat_covered_from"]), (1, None, COVERED))
        self.assertEqual((result["since"], result["until"]), ("2026-11-11T18:00:00.000000Z", "2026-11-12T18:00:00.000000Z"))
        self.assertEqual(result["gaps"], [])
        # The owner reads the same list.
        self.assertEqual(self.read(ctx=self.runtime.owner)["items"], result["items"])

    def test_the_period_is_hours_a_since_until_range_or_the_last_n_messages(self):
        ids = [self.turn(self.lead, f"Message {hour}", at(hour))[0] for hour in (3, 9, 12, 15)]
        listed = lambda **args: [item["id"] for item in self.read(args)["items"]]
        self.assertEqual(listed(hours=4), ids[3:])
        self.assertEqual(listed(since="2026-11-12T10:00:00+01:00", until=at(13)), ids[1:3])
        self.assertEqual(listed(last_n=2), ids[2:])
        self.assertEqual(listed(agent_id=self.worker), [])
        early = self.read({"hours": 24 * 5})
        self.assertEqual(early["gaps"], [{"code": "chat_not_indexed_before", "at": COVERED,
                                          "message": "Chat messages written before chat indexing began are not listed."}])
        for bad in ({"hours": 2, "since": at(1)}, {"since": "2026-11-12T10:00:00"}, {"since": at(14), "until": at(13)},
                    {"last_n": 2, "cursor": "abc"}):
            self.assertEqual(self.refused(self.runtime.coordinator, args=bad), "validation_error", bad)

    def test_pages_keep_their_period_and_long_text_is_cut_until_it_is_asked_for(self):
        long_text = "x" * 5000
        ids = [self.turn(self.lead, long_text if hour == 10 else f"Message {hour}", at(hour))[0] for hour in (9, 10, 11)]
        page = self.read({"limit": 2})
        self.assertEqual([item["id"] for item in page["items"]], ids[:2])
        cut = page["items"][1]
        self.assertEqual((len(cut["text"]), cut["chars"], cut["truncated"]), (2000, 5000, True))
        rest = self.read({"limit": 2, "cursor": page["next_cursor"]})
        self.assertEqual(([item["id"] for item in rest["items"]], rest["next_cursor"], rest["since"]), (ids[2:], None, page["since"]))
        self.assertEqual(self.refused(self.runtime.coordinator, args={"limit": 3, "channels": ["chat"], "cursor": page["next_cursor"]}),
                         "validation_error")
        full = self.read({"id": ids[1]}, operation="owner_input_get")["item"]
        self.assertEqual((full["text"], full["truncated"], full["id"]), (long_text, False, ids[1]))
        # A page stops once it carries enough text, and says there is more.
        for minute in range(1, 30):
            self.turn(self.worker, "y" * 2000, at(12, minute))
        page = self.read({"limit": 100})
        self.assertLess(len(page["items"]), 32)
        self.assertGreaterEqual(sum(len(item["text"]) for item in page["items"]), PAGE_CHARACTERS)
        self.assertIsNotNone(page["next_cursor"])

    def test_private_threads_and_the_reply_that_followed(self):
        answered = create(self.runtime, "Did the supplier confirm?")
        reply(self.runtime, answered, "Yes, confirmed this morning.")
        open_chat = create(self.runtime, "And the second supplier?")
        self.stamp_thread("Did the supplier confirm?", at(9))
        self.stamp_thread("Yes, confirmed this morning.", at(9, 30))
        self.stamp_thread("And the second supplier?", at(11))
        result = self.read({"channels": ["thread"]})
        self.assertEqual([(item["channel"], item["text"], item["source"]["type"], item["source"]["id"]) for item in result["items"]], [
            ("thread", "Did the supplier confirm?", "signoff", answered["id"]),
            ("thread", "And the second supplier?", "signoff", open_chat["id"])])
        self.assertEqual(result["items"][0]["followed_by"]["reply"], {"at": at(9, 30), "preview": "Yes, confirmed this morning."})
        self.assertIsNone(result["items"][1]["followed_by"]["reply"])
        self.assertEqual(result["unanswered"], 1)
        self.assertEqual(self.read({"id": result["items"][0]["id"]}, operation="owner_input_get")["item"]["text"],
                         "Did the supplier confirm?")
        # A retired per-worker channel is never read.
        self.write("UPDATE owner_conversations SET peer_agent_id=? WHERE source_id=?", (self.worker, open_chat["id"]))
        self.assertEqual([item["source"]["id"] for item in self.read({"channels": ["thread"]})["items"]], [answered["id"]])

    def test_a_source_the_caller_cannot_read_now_is_left_out(self):
        create(self.runtime, "Did the supplier confirm?")
        self.stamp_thread("Did the supplier confirm?", at(9))
        with patch.object(PrivateConversations, "source_check", side_effect=DomainError("not_found", "Signoff does not exist")):
            self.assertEqual(self.read({"channels": ["thread"]})["items"], [])

    def test_the_owners_own_notes_are_listed_and_relays_are_not(self):
        signoff = create(self.runtime, "Opening message")
        self.stamp_thread("Opening message", at(7))
        task = self.task()
        for identity, author, recorder, body, when in (
                ("m1", self.owner, self.owner, "Use the blue logo.", at(9)),
                ("m2", self.owner, self.lead, "The owner said: use the blue logo.", at(9, 10)),
                ("m3", self.worker, self.worker, "Switched to the blue logo.", at(9, 20))):
            self.write("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,created_at) "
                       "VALUES(NULL,?,?,?,?,?,?,?,?)", (identity, "task", task, "comment", body, author, recorder, when))
        self.write("INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at) VALUES(?,?,?,?,?)",
                   ("n1", signoff["id"], "Approved, but cap it at 500.", self.owner, at(10)))
        with patch.object(PrivateConversations, "source_check", return_value=None):
            result = self.read({"channels": ["note"]})
        self.assertEqual([(item["id"], item["text"], item["source"]["type"], item["source"]["id"]) for item in result["items"]], [
            ("task-note:m1", "Use the blue logo.", "task", task),
            ("signoff-note:n1", "Approved, but cap it at 500.", "signoff", signoff["id"])])
        # The next comment in that discussion by someone else is the reply; a relay of the owner's words is not one.
        self.assertEqual(result["items"][0]["followed_by"]["reply"], {"at": at(9, 20), "preview": "Switched to the blue logo."})
        self.assertIsNone(result["items"][1]["followed_by"]["reply"])

    def test_record_changes_written_before_the_owners_next_message_are_attached(self):
        first, _ = self.turn(self.worker, "Close the Linden task.", at(9))
        second, _ = self.turn(self.worker, "Now open one for the invoice.", at(11))
        task = self.task()
        for identity, recorder, body, when in (("s1", self.worker, "Status: open -> done", at(9, 30)),
                                               ("s2", self.lead, "Workers changed", at(9, 40)),
                                               ("s3", self.worker, "Created", at(11, 30))):
            self.write("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,change_json,created_at) "
                       "VALUES(NULL,?,?,?,?,?,?,?,?)", (identity, "task", task, "system", body, recorder, "{}", when))
        with patch.object(PrivateConversations, "source_check", return_value=None):
            items = self.read()["items"]
        changes = lambda item: [(line["at"], line["change"], line["record"]["id"]) for line in item["followed_by"]["records"]]
        self.assertEqual(changes(items[0]), [(at(9, 30), "Status: open -> done", task)])
        self.assertEqual(changes(items[1]), [(at(11, 30), "Created", task)])
        # Without a readable record there is nothing to attach.
        with patch.object(PrivateConversations, "source_check", side_effect=DomainError("forbidden", "No access")):
            self.assertEqual(self.read()["items"][0]["followed_by"]["records"], [])

    def test_a_possibly_private_turn_has_no_text_and_one_broadcast_is_listed_once(self):
        hidden, _ = self.turn(self.lead, "secret-code-4417", at(9), redacted=True)
        self.turn(self.lead, "Team: stop all sends.", at(10), via="broadcast")
        self.turn(self.worker, "Team: stop all sends.", at(10), via="broadcast")
        result = self.read()
        private, broadcast = result["items"]
        self.assertEqual((private["id"], private["text"], private["chars"], private["redacted"]), (hidden, None, None, True))
        self.assertEqual(result["unanswered"], 1)
        self.assertEqual({broadcast["agent"]["id"], broadcast["also_to"][0]["id"]}, {self.lead, self.worker})
        self.assertNotIn("secret-code-4417", str(self.read({"id": hidden}, operation="owner_input_get")))

    def test_an_archive_that_cannot_be_read_is_named_as_a_gap(self):
        listed, _ = self.turn(self.lead, "Send the contract.", at(9))
        self.archives[self.lead].records.clear()
        result = self.read()
        self.assertEqual((result["items"], [gap["code"] for gap in result["gaps"]]), ([], ["turn_not_found"]))
        self.assertEqual(self.refused(self.runtime.coordinator, "owner_input_get", {"id": listed}), "operation_unavailable")
        self.assertEqual(self.refused(self.runtime.coordinator, "owner_input_get", {"id": "chat:" + "0" * 64}), "not_found")
        self.assertEqual(self.refused(self.runtime.coordinator, "owner_input_get", {"id": "other:1"}), "validation_error")

    def test_only_the_owner_and_the_coordinator_in_person_may_read(self):
        self.turn(self.lead, "Send the contract.", at(9))
        for operation, args in (("owner_inputs_list", {}), ("owner_input_get", {"id": "chat:" + "0" * 64})):
            self.assertEqual(self.refused(self.runtime.worker, operation, args), "forbidden")
        delegated = replace(self.runtime.coordinator, delegation_binding_id=new_id(), delegation_assignment_id=new_id(),
                            delegation_generation=1)
        with self.runtime.database.read() as uow:
            with self.assertRaises(DomainError) as refusal:
                self.runtime.owner_inputs.caller(delegated, uow)
        self.assertEqual(refusal.exception.code, "forbidden")

    def test_reading_writes_nothing(self):
        chat = create(self.runtime, "Did the supplier confirm?")
        reply(self.runtime, chat, "Yes.")
        self.turn(self.lead, "Send the contract.", at(9))
        with self.runtime.database.read() as uow:
            before = uow.connection.execute("SELECT total_changes()").fetchone()[0]
            reads = uow.connection.execute("SELECT count(*) FROM owner_conversation_reads").fetchone()[0]
        self.read(ctx=self.runtime.owner)
        self.read({"last_n": 5})
        with self.runtime.database.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT count(*) FROM owner_conversation_reads").fetchone()[0], reads)
            self.assertEqual(uow.connection.execute("SELECT count(*) FROM native_reads").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
