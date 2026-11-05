"""Capture, verification and relocated restore of notes, supervised edits and undelivered question output."""
import json
from unittest.mock import patch

from qorqut_context import Context, ContextError, Pipeline, recovery
from qorqut_context.schema import encode
from qorqut_context.recovery import RecoveryError
from .helpers import ContextCase
from .test_capture_recovery import Barrier, Recovered, sha
from .test_graph_edits import NoModel
from .test_pipeline import ScriptRunner, call, finish, new_node
from .test_recovery import ProcessStopped
from .test_supervised_edits import DOCUMENT, crafted_clone, note, plan

ASK = "Which Jesse is meant: the lawyer or the founder?"


class NoteRecovered(Recovered):
    """Published notes, a published supervised edit, an undelivered question payload and a pending supervised edit."""

    def setUp(self):
        ContextCase.setUp(self)
        event = self.source("one.md", b"Jesse met Alice in Lisbon.")
        self.node(event, "jesse", summary="Jesse")
        self.node(event, "alice", summary="Alice")
        self.edge(event, "edgea", "jesse", "alice")
        self.note(event, "Jesse reports meeting Alice in Lisbon.", [("node", "jesse")], note_id="notea")
        self.first = self.context.publish(event.event_id)
        self.pipeline = Pipeline(self.context, runner=NoModel())
        self.supervised = self.pipeline.submit_supervised(
            plan(self.first, note("noteb", "The owner states Alice lives in Lisbon.", [("node", "alice")]),
                 note("notea", "Jesse met Alice in Lisbon on a Tuesday.", [("node", "jesse"), ("node", "alice")],
                      citations=["self", self.first], support=[self.first, "self"])),
            content=DOCUMENT, submitted_by="owner", event_id="sup01")

        def action(tools, payload):
            new_node(tools, "A new person")
            call(tools, "emit_question", text=ASK, related=[{"kind": "event", "id": tools.event_id},
                                                           {"kind": "note", "id": "noteb"}])
            finish(tools)
        self.filing = Pipeline(self.context, runner=ScriptRunner(action)).submit(
            "add", "foundation/two.md", content=b"Jesse appears again.",
            metadata={"kind": "note", "title": "two.md", "origin": "focused-test"}, submitted_by="test")
        self.question_event = self.filing["event_id"]

        def stop(name):
            if name == "edit_staged":
                raise ProcessStopped()
        value = plan(self.filing["published_commit"], note("notec", "A remark about Bob.", []),
                     note("noted", "Another remark about Bob.", []))
        with patch.object(self.pipeline, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.pipeline.submit_supervised(value, content=DOCUMENT + b"More.\n", submitted_by="owner", event_id="sup02")
        self.pending = self.context.pending()
        self.barrier = Barrier()
        self.capture_directory = self.directory / "generation" / "context"
        self.capture_directory.parent.mkdir()


class CaptureNotes(NoteRecovered):
    def test_notes_supervised_edits_and_undelivered_questions_restore_with_identical_authority(self):
        before = self.authority(self.context)
        self.assertTrue((self.context.runtime / "questions" / f"{self.question_event}.json").exists())
        summary = self.capture()
        self.assertEqual(self.authority(self.context), before)
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["component"]["context_format"], 3)
        self.assertEqual((summary["complete"], summary["pending_event"]), (True, "sup02"))
        self.assertTrue((self.capture_directory / "runtime" / "questions" / f"{self.question_event}.json").is_file())
        self.assertEqual(recovery.verify(self.capture_directory, expected_manifest_sha256=summary["manifest_sha256"])["published_commit"],
                         self.filing["published_commit"])
        report = self.restore(summary)
        repository, runtime = self.targets()
        self.assertEqual(report["status"], "restored")
        restored = Context(repository, runtime)
        self.assertEqual(self.authority(restored), before)
        snapshot, original = restored.snapshot(), self.context.snapshot()
        self.assertEqual(snapshot.notes, original.notes)
        self.assertEqual(sorted(snapshot.notes), ["notea", "noteb"])
        self.assertEqual(snapshot.files["foundation/supervised-edits/sup01.md"], DOCUMENT)
        self.assertEqual(snapshot.history("notea", kind="note"), original.history("notea", kind="note"))
        self.assertEqual(snapshot.event("sup01")["execution"], self.supervised["execution"])
        self.assertEqual(restored.pending(), self.pending)

    def test_the_restored_pair_serves_notes_questions_and_finishes_the_pending_edit_without_a_model(self):
        summary = self.capture()
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual([hit.id for hit in restored.search("Tuesday", kind="note")], ["notea"])
        self.assertEqual(restored.snapshot().linked_notes("node", "alice")["items"][0]["record"]["id"], "notea")
        forbidden = NoModel()
        pipeline = Pipeline(restored, runner=forbidden)
        output = pipeline.question_output(self.question_event)
        self.assertEqual(output["published_commit"], self.filing["published_commit"])
        self.assertEqual([item["text"] for item in output["questions"]], [ASK])
        self.assertEqual(output["questions"][0]["related"],
                         [{"kind": "event", "id": self.question_event}, {"kind": "note", "id": "noteb"}])
        receipt = pipeline.retry("sup02")
        self.assertEqual((receipt["status"], receipt["model"], forbidden.calls), ("published", None, []))
        self.assertEqual(sorted(restored.snapshot().notes), ["notea", "noteb", "notec", "noted"])
        self.assertTrue(pipeline.acknowledge_question_output(self.question_event, output["published_commit"])["removed"])
        self.assertFalse((runtime / "questions" / f"{self.question_event}.json").exists())
        self.assertEqual(Pipeline(self.context, runner=NoModel()).question_output(self.question_event)["questions"][0]["text"], ASK)

    def test_a_delivered_payload_is_absent_from_a_later_capture(self):
        Pipeline(self.context, runner=NoModel()).acknowledge_question_output(self.question_event, self.filing["published_commit"])
        summary = self.capture()
        self.assertFalse((self.capture_directory / "runtime" / "questions" / f"{self.question_event}.json").exists())
        self.restore(summary)
        repository, runtime = self.targets()
        self.assertEqual(Pipeline(Context(repository, runtime), runner=NoModel()).question_output(self.question_event)["questions"], [])


class OutboxRefusals(NoteRecovered):
    def refused(self, code):
        with self.assertRaises(RecoveryError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, code)
        self.assertFalse(self.capture_directory.exists())
        return raised.exception

    def entry(self):
        return self.context.runtime / "questions" / f"{self.question_event}.json"

    def test_a_malformed_or_foreign_outbox_entry_refuses_capture(self):
        path = self.entry()
        original = path.read_bytes()
        value = json.loads(original)
        cases = {
            "not json": b"{",
            "extra field": json.dumps({**value, "extra": 1}).encode(),
            "other event": json.dumps({**value, "event_id": "sup01"}).encode(),
            "other commit": json.dumps({**value, "published_commit": self.first}).encode(),
            "old format": json.dumps({**value, "format_version": 2}).encode(),
            "invalid reference": json.dumps({**value, "questions": [{"text": ASK, "related": [{"kind": "source", "id": "jesse"}]}]}).encode(),
            "extra question field": json.dumps({**value, "questions": [{"text": ASK, "related": [], "answer": "x"}]}).encode(),
        }
        for label, data in cases.items():
            with self.subTest(label):
                path.write_bytes(data)
                self.refused("source_invalid")
        path.write_bytes(original)
        stray = self.context.runtime / "questions" / "zzzzz.json"
        stray.write_bytes(original)
        self.refused("source_invalid")
        stray.unlink()
        self.capture()

    def test_unknown_files_in_the_outbox_directory_are_never_dropped(self):
        for name in ("operator-note.txt", "ABCDE.json", "toolong1.json"):
            stray = self.context.runtime / "questions" / name
            stray.write_text("keep me")
            self.assertEqual(self.refused("unclassified_authority").details["paths"], [f"runtime/questions/{name}"])
            stray.unlink()
        folder = self.context.runtime / "questions" / "nested"
        folder.mkdir()
        self.refused("unclassified_authority")
        folder.rmdir()
        self.entry().unlink()
        link = self.context.runtime / "questions" / f"{self.question_event}.json"
        link.symlink_to(self.directory)
        self.refused("unclassified_authority")
        link.unlink()
        self.capture()


class VerifyNotes(NoteRecovered):
    def setUp(self):
        super().setUp()
        self.summary = self.capture()

    def verify(self):
        return recovery.verify(self.capture_directory, expected_manifest_sha256=self.summary["manifest_sha256"])

    def test_a_changed_or_missing_outbox_payload_or_a_format_two_manifest_fails(self):
        path = self.capture_directory / "runtime" / "questions" / f"{self.question_event}.json"
        original = path.read_bytes()
        for change in (lambda data: data[:-1] + bytes([data[-1] ^ 1]), lambda data: data + b" "):
            path.write_bytes(change(original))
            with self.assertRaises(RecoveryError) as raised:
                self.verify()
            self.assertEqual(raised.exception.code, "invalid_capture")
            path.write_bytes(original)
        path.unlink()
        with self.assertRaises(RecoveryError):
            self.verify()
        path.write_bytes(original)
        self.verify()
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        manifest["component"]["context_format"] = 2
        data = recovery.encode(manifest)
        (self.capture_directory / "manifest.json").write_bytes(data)
        with self.assertRaises(RecoveryError) as raised:
            recovery.verify(self.capture_directory, expected_manifest_sha256=sha(data))
        self.assertEqual(raised.exception.code, "invalid_capture")


class PendingQuestions(Recovered):
    """A pending filing whose interrupted attempt already staged question output."""

    def test_staged_questions_travel_with_the_pending_event_but_are_not_delivered(self):
        self.context.discard(self.pending["id"])

        def die(tools, payload):
            new_node(tools, "Only a draft")
            call(tools, "emit_question", text=ASK, related=[])
            raise ProcessStopped()
        with self.assertRaises(ProcessStopped):
            Pipeline(self.context, runner=ScriptRunner(die)).submit(
                "add", "foundation/four.md", content=b"Jesse once more.",
                metadata={"kind": "note", "title": "four.md", "origin": "focused-test"}, submitted_by="test")
        pending = self.context.pending()
        self.assertEqual(pending["questions"], [{"text": ASK, "related": []}])
        self.assertFalse((self.context.runtime / "questions").exists() and any((self.context.runtime / "questions").iterdir()))
        before = self.authority(self.context)
        summary = self.capture()
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual(self.authority(restored), before)
        self.assertEqual(restored.pending(), pending)
        forbidden = Pipeline(restored, runner=ScriptRunner(lambda tools, payload: (new_node(tools, "Fresh"), finish(tools))))
        with self.assertRaises(Exception):
            forbidden.question_output(pending["id"])
        receipt = forbidden.retry(pending["id"])
        self.assertEqual(forbidden.question_output(pending["id"])["questions"], [])
        self.assertEqual(receipt["status"], "published")


class SupervisedHistory(NoteRecovered):
    def test_capture_refuses_a_published_supervised_commit_whose_records_differ_from_its_plan(self):
        self.context.discard(self.pending["id"])
        head = self.context.git.head()
        record = json.loads(self.context.git.files(head)["notes/noteb.json"])

        def alter(event, files):
            files["notes/noteb.json"] = encode({**record, "text": "Different from the retained plan."})
        commit = crafted_clone(self.context, "sup0x", alter)
        self.context.git.publish(commit, head)
        with self.assertRaisesRegex(ContextError, "do not match its plan"):
            self.context.snapshot()
        with self.assertRaises(RecoveryError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, "source_invalid")
        self.assertIn("do not match its plan", str(raised.exception))
        self.assertFalse(self.capture_directory.exists())

    def test_capture_refuses_a_claimed_removal_whose_record_remains(self):
        self.context.discard(self.pending["id"])
        head = self.context.git.head()
        removal = plan(head, note("notec", "A short-lived remark.", []))
        published = self.pipeline.submit_supervised(removal, content=DOCUMENT + b"Short.\n", submitted_by="owner",
                                                    event_id="sup03")["published_commit"]
        later = plan(published, {"op": "remove_note", "id": "notec", "explanation": "Withdrawn.", "citations": [published]})
        self.pipeline.submit_supervised(later, content=DOCUMENT + b"Withdrawn.\n", submitted_by="owner", event_id="sup04")
        self.assertNotIn("notec", self.context.snapshot().notes)
        kept = self.context.git.files(published)["notes/notec.json"]
        tip = self.context.git.head()
        document = self.context.git.files(tip)["foundation/supervised-edits/sup04.md"]
        event = json.loads(self.context.git.files(tip)["graph/events/sup04.json"])
        event.update(id="sup05", source=dict(event["source"], path="foundation/supervised-edits/sup05.md",
                                             title="Supervised edit sup05"))
        commit = self.context.git.commit({"graph/events/sup05.json": encode(event),
                                          "foundation/supervised-edits/sup05.md": document,
                                          "notes/notec.json": kept}, self.context.runtime, tip, "crafted")
        self.context.git.publish(commit, tip)
        with self.assertRaises(RecoveryError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, "source_invalid")
        self.assertIn("do not match its plan", str(raised.exception))

    def test_a_capture_taken_after_later_legitimate_edits_restores(self):
        self.context.discard(self.pending["id"])
        head = self.context.git.head()
        later = plan(head, note("noteb", "A later account of Alice.", [("node", "alice")],
                                citations=["self", head], support=[head, "self"]),
                     {"op": "remove_note", "id": "notea", "explanation": "Withdrawn.", "citations": [head]})
        self.pipeline.submit_supervised(later, content=DOCUMENT + b"Later.\n", submitted_by="owner", event_id="sup03")
        summary = self.capture()
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual(sorted(restored.snapshot().notes), ["noteb"])
        self.assertEqual(restored.snapshot().notes["noteb"]["text"], "A later account of Alice.")
        self.assertEqual(restored.snapshot().event("sup01")["execution"], self.supervised["execution"])
