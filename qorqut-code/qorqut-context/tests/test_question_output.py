"""Add-only question output: staged outside the Context, deliverable only after publication, acknowledged once."""
import json
from unittest.mock import patch

from qorqut_context import Conflict, ContextError
from qorqut_context.pipeline import Pipeline, PropagationError
from qorqut_context.tools import EventTools
from .helpers import ContextCase
from .test_graph_edits import NoModel
from .test_pipeline import ScriptRunner, call, finish, new_node
from .test_recovery import ProcessStopped

ASK = "Which Jesse is meant: the lawyer or the founder?"


def ask(tools, text, related=()):
    return call(tools, "emit_question", text=text, related=list(related))


def everything(context):
    """All bytes the Context repository holds at its head, to prove no question text ever reaches them."""
    return b"\n".join(context.git.files(context.git.head()).values())


class QuestionCase(ContextCase):
    def submit(self, pipeline, name="one.md", content=b"Jesse appears in this note.", **kwargs):
        return pipeline.submit("add", "foundation/" + name, content=content,
                               metadata={"kind": "note", "title": name, "origin": "focused-test"},
                               submitted_by="test", **kwargs)

    def outbox(self, event_id):
        return self.context.runtime / "questions" / f"{event_id}.json"

    def stop_context(self, phase):
        def stop(name):
            if name == phase:
                raise ProcessStopped()
        return patch.object(self.context, "_checkpoint", side_effect=stop)

    def published_base(self):
        """A published node, edge, note and a later-removed node, for reference validation."""
        event = self.source("base.md", b"Jesse met Alice and Old.")
        for key in ("jesse", "alice", "old01"):
            self.node(event, key, summary=key.title())
        self.edge(event, "edge1", "jesse", "alice")
        self.note(event, "Jesse met Alice.", [("node", "jesse")], note_id="note1")
        self.base_event = event.event_id
        self.base = self.context.publish(event.event_id)
        removal = self.source("later.md", b"Old is gone.")
        self.context.remove_node(removal.event_id, "old01", explanation="Gone.", citations=["self"])
        self.later_event = removal.event_id
        self.later = self.context.publish(removal.event_id)


class Filing(QuestionCase):
    def test_questions_are_deliverable_after_publication_and_never_enter_the_context(self):
        seen = {}

        def action(tools, payload):
            seen["event"] = tools.event_id
            node = new_node(tools)
            note = call(tools, "put_note", record={"text": "The narrator mentions a Jesse without a surname.",
                                                   "objects": [{"kind": "node", "id": node}]},
                        explanation="Reported.", citations=["self"], support_after=["self"])["id"]
            first = ask(tools, ASK, [{"kind": "event", "id": tools.event_id}, {"kind": "node", "id": node},
                                     {"kind": "note", "id": note}, {"kind": "node", "id": node}])
            self.assertEqual((first["staged"], first["duplicate"], first["count"]), (True, False, 1))
            ask(tools, "Is the date in the second paragraph the filing date?")
            seen.update(node=node, note=note)
            finish(tools)
        runner = ScriptRunner(action)
        pipeline = Pipeline(self.context, runner=runner)
        receipt = self.submit(pipeline)
        event_id, commit = receipt["event_id"], receipt["published_commit"]
        output = pipeline.question_output(event_id)
        self.assertEqual(output, {"event_id": event_id, "published_commit": commit, "questions": [
            {"text": ASK, "related": [{"kind": "event", "id": event_id}, {"kind": "node", "id": seen["node"]},
                                      {"kind": "note", "id": seen["note"]}]},
            {"text": "Is the date in the second paragraph the filing date?", "related": []}]})
        self.assertEqual(pipeline.question_output(event_id), output)
        self.assertEqual(json.loads(self.outbox(event_id).read_bytes())["questions"], output["questions"])
        self.assertFalse(self.outbox(event_id).is_relative_to(self.context.root))
        for needle in (b"Which Jesse", b"filing date?", b"second paragraph"):
            self.assertNotIn(needle, everything(self.context))
        self.context.git.require_clean()
        snapshot = self.context.snapshot()
        self.assertNotIn("questions", snapshot.event(event_id))
        self.assertEqual(snapshot.handoff_note(event_id)["text"], "")
        self.assertEqual(self.context.search("lawyer founder"), [])
        self.assertNotIn("Which Jesse", json.dumps(receipt))
        self.assertEqual(sorted(snapshot.notes), [seen["note"]])
        self.assertNotIn("emit_question", json.dumps(snapshot.event(event_id)["changes"]))

    def test_acknowledgement_removes_the_payload_idempotently_and_checks_the_commit(self):
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools), ask(tools, ASK), finish(tools))))
        receipt = self.submit(pipeline)
        event_id, commit = receipt["event_id"], receipt["published_commit"]
        for bad, error in (("f" * 40, Conflict), ("short", ContextError)):
            with self.subTest(bad), self.assertRaises(error):
                pipeline.acknowledge_question_output(event_id, bad)
        self.assertTrue(self.outbox(event_id).exists())
        first = pipeline.acknowledge_question_output(event_id, commit)
        self.assertEqual(first, {"event_id": event_id, "published_commit": commit, "acknowledged": True, "removed": True})
        self.assertFalse(self.outbox(event_id).exists())
        again = pipeline.acknowledge_question_output(event_id, commit)
        self.assertEqual((again["acknowledged"], again["removed"]), (True, False))
        self.assertEqual(pipeline.question_output(event_id)["questions"], [])
        with self.assertRaises(Conflict):
            pipeline.acknowledge_question_output("nnnnn", commit)

    def test_no_emitted_question_yields_an_empty_list_and_no_outbox_file(self):
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools))))
        receipt = self.submit(pipeline)
        self.assertEqual(pipeline.question_output(receipt["event_id"]),
                         {"event_id": receipt["event_id"], "published_commit": receipt["published_commit"], "questions": []})
        self.assertFalse(self.outbox(receipt["event_id"]).exists())
        self.assertFalse((self.context.runtime / "questions").exists() and any((self.context.runtime / "questions").iterdir()))
        self.assertEqual(pipeline.acknowledge_question_output(receipt["event_id"], receipt["published_commit"])["removed"], False)

    def test_unpublished_failed_and_discarded_events_expose_nothing(self):
        def unfinished(tools, payload):
            new_node(tools)
            ask(tools, ASK)
        pipeline = Pipeline(self.context, runner=ScriptRunner(unfinished))
        with self.assertRaises(PropagationError):
            self.submit(pipeline, event_id="evt01")
        self.assertEqual(self.context.pending()["questions"][0]["text"], ASK)
        self.assertFalse(self.outbox("evt01").exists())
        for call_ in (pipeline.question_output, lambda event: pipeline.acknowledge_question_output(event, "f" * 40)):
            with self.assertRaises(Conflict):
                call_("evt01")
        pipeline.discard("evt01")
        self.assertIsNone(self.context.pending())
        self.assertFalse(self.outbox("evt01").exists())
        with self.assertRaises(Conflict):
            pipeline.question_output("evt01")
        self.assertNotIn(b"Which Jesse", everything(self.context))
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools))))
        self.assertEqual(pipeline.question_output(self.submit(pipeline, event_id="evt01")["event_id"])["questions"], [])

    def test_a_retry_delivers_only_the_questions_of_the_attempt_that_published(self):
        attempt = []

        def action(tools, payload):
            attempt.append(1)
            new_node(tools)
            if len(attempt) == 1:
                ask(tools, "A stale question from a failed attempt?")
                return
            ask(tools, ASK)
            finish(tools)
        pipeline = Pipeline(self.context, runner=ScriptRunner(action))
        with self.assertRaises(PropagationError):
            self.submit(pipeline, event_id="evt01")
        self.assertEqual(len(self.context.pending()["questions"]), 1)
        for key, staged in list(self.context.pending()["draft"].items()):
            self.context.remove_node("evt01", staged["id"], explanation="Discard the failed attempt's node.", citations=["self"])
        receipt = pipeline.retry("evt01")
        self.assertEqual([item["text"] for item in pipeline.question_output("evt01")["questions"]], [ASK])
        self.assertEqual(receipt["published_commit"], pipeline.question_output("evt01")["published_commit"])
        self.assertNotIn(b"stale question", self.outbox("evt01").read_bytes())

    def test_a_ready_event_publishes_on_retry_without_a_model_and_keeps_its_questions(self):
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools), ask(tools, ASK), finish(tools))))
        with patch.object(pipeline, "_checkpoint", side_effect=lambda name: (_ for _ in ()).throw(ProcessStopped())
                          if name == "after_ready" else None), self.assertRaises(ProcessStopped):
            self.submit(pipeline, event_id="evt01")
        self.assertEqual(self.context.git.head(), self.context.snapshot().revision)
        self.assertNotIn("evt01", self.context.snapshot().events)
        forbidden = NoModel()
        with patch("qorqut_context.pipeline.bootstrap.prepare", side_effect=AssertionError("model machinery reached")):
            receipt = Pipeline(self.context, runner=forbidden).retry("evt01")
        self.assertEqual(forbidden.calls, [])
        self.assertEqual(Pipeline(self.context, runner=forbidden).question_output("evt01"),
                         {"event_id": "evt01", "published_commit": receipt["published_commit"],
                          "questions": [{"text": ASK, "related": []}]})
        again = Pipeline(self.context, runner=forbidden).retry("evt01")
        self.assertEqual((again["published_commit"], again["recovered"], forbidden.calls), (receipt["published_commit"], True, []))
        self.assertEqual(Pipeline(self.context, runner=forbidden).question_output("evt01")["questions"][0]["text"], ASK)

    def test_crashes_around_publication_are_recovered_without_losing_or_resurrecting_output(self):
        for phase in ("after_publication", "after_questions", "after_checkout"):
            with self.subTest(phase):
                event_id = f"ev{phase[6:9]}"
                pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (
                    new_node(tools), ask(tools, f"{ASK} ({phase})"), finish(tools))))
                with self.stop_context(phase), self.assertRaises(ProcessStopped):
                    self.submit(pipeline, f"{event_id}.md", f"Source for {phase}".encode(), event_id=event_id)
                self.assertIn(event_id, self.context.snapshot().events)
                with self.assertRaisesRegex(Conflict, "still completing"):
                    pipeline.question_output(event_id)
                with self.assertRaisesRegex(Conflict, "still completing"):
                    pipeline.acknowledge_question_output(event_id, self.context.snapshot().event_commits[event_id])
                self.assertEqual(pipeline.recover()["status"], "completed")
                output = pipeline.question_output(event_id)
                self.assertEqual([item["text"] for item in output["questions"]], [f"{ASK} ({phase})"])
                self.assertEqual(pipeline.acknowledge_question_output(event_id, output["published_commit"])["removed"], True)
                pipeline.recover()
                self.assertEqual(pipeline.question_output(event_id)["questions"], [])
                self.assertFalse(self.outbox(event_id).exists())


class References(QuestionCase):
    def setUp(self):
        super().setUp()
        self.published_base()
        self.event = self.source("two.md", b"Second source.")

    def emit(self, text="Is this right?", related=()):
        return self.context.emit_question(self.event.event_id, text, list(related))

    def test_published_historical_and_draft_references_are_accepted(self):
        self.node(self.event, "bobby", summary="Bobby")
        self.edge(self.event, "edge2", "bobby", "alice")
        created = self.note(self.event, "Bobby is mentioned.", [("node", "bobby")])
        related = [{"kind": "event", "id": self.event.event_id}, {"kind": "event", "id": self.base_event},
                   {"kind": "node", "id": "jesse"}, {"kind": "node", "id": "old01"}, {"kind": "edge", "id": "edge1"},
                   {"kind": "note", "id": "note1"}, {"kind": "node", "id": "bobby"}, {"kind": "edge", "id": "edge2"},
                   {"kind": "note", "id": created}]
        self.assertEqual(self.emit(related=related)["count"], 1)
        commit = self.context.publish(self.event.event_id)
        output = self.context.question_output(self.event.event_id)
        self.assertEqual(output["published_commit"], commit)
        self.assertEqual(output["questions"][0]["related"], related)

    def test_invented_wrong_kind_and_withdrawn_references_are_refused(self):
        self.node(self.event, "scrat", summary="Scratch")
        cases = {
            "invented node": {"kind": "node", "id": "zzzzz"},
            "invented event": {"kind": "event", "id": "zzzzz"},
            "invented note": {"kind": "note", "id": "zzzzz"},
            "node id as edge": {"kind": "edge", "id": "jesse"},
            "node id as note": {"kind": "note", "id": "jesse"},
            "edge id as node": {"kind": "node", "id": "edge1"},
            "note id as event": {"kind": "event", "id": "note1"},
            "unknown kind": {"kind": "source", "id": "jesse"},
            "extra field": {"kind": "node", "id": "jesse", "explanation": "x"},
            "missing id": {"kind": "node"},
            "bad id": {"kind": "node", "id": "JESSE"},
        }
        for label, reference in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                self.emit(related=[reference])
        self.assertNotIn("questions", self.context.pending())
        for text in ("", "  ", 7, None):
            with self.subTest(repr(text)), self.assertRaises(ContextError):
                self.emit(text)
        with self.assertRaises(ContextError):
            self.context.emit_question(self.event.event_id, "Q?", "node")
        self.context.remove_node(self.event.event_id, "scrat", explanation="Never published.", citations=["self"])
        with self.assertRaises(ContextError):
            self.emit(related=[{"kind": "node", "id": "scrat"}])
        self.node(self.event, "tempo", summary="Temp")
        self.emit(related=[{"kind": "node", "id": "tempo"}])
        self.context.remove_node(self.event.event_id, "tempo", explanation="Never published.", citations=["self"])
        with self.assertRaisesRegex(ContextError, "may reference only"):
            self.context.inspect_draft(self.event.event_id, validate=True)
        with self.assertRaisesRegex(ContextError, "may reference only"):
            self.context.publish(self.event.event_id)
        self.assertEqual(self.context.git.head(), self.later)
        self.assertFalse(self.outbox(self.event.event_id).exists())

    def test_duplicates_collapse_and_questions_can_neither_be_edited_nor_removed(self):
        self.assertEqual(self.emit(ASK, [{"kind": "node", "id": "jesse"}])["duplicate"], False)
        again = self.emit(ASK, [{"kind": "node", "id": "jesse"}, {"kind": "node", "id": "jesse"}])
        self.assertEqual((again["duplicate"], again["count"]), (True, 1))
        self.assertEqual(self.emit(ASK, [{"kind": "node", "id": "alice"}])["count"], 2)
        self.assertEqual(self.emit(ASK)["count"], 3)
        tools = EventTools(self.context, self.event.event_id)
        self.assertTrue({"emit_question"} <= {tool["name"] for tool in tools.definitions})
        self.assertFalse({"edit_question", "remove_question", "list_questions", "answer_question"} &
                         {tool["name"] for tool in tools.definitions})
        draft = self.context.inspect_draft(self.event.event_id)
        self.assertEqual([item["text"] for item in draft["questions"]], [ASK] * 3)
        self.assertNotIn("questions", {kind for kind in draft["draft"]})

    def test_the_sealed_digest_covers_questions_and_publication_is_refused_after_a_later_emit(self):
        tools = EventTools(self.context, self.event.event_id)
        before = self.context.inspect_draft(self.event.event_id)["digest"]
        ask(tools, ASK)
        after = self.context.inspect_draft(self.event.event_id)["digest"]
        self.assertNotEqual(before, after)
        sealed = finish(tools)["draft_digest"]
        self.assertEqual(sealed, after)
        self.emit("A question added after the seal?")
        with self.assertRaisesRegex(Conflict, "draft changed"):
            self.context.publish(self.event.event_id, expected_draft_digest=sealed)
        self.assertEqual(self.context.git.head(), self.later)

    def test_output_size_is_bounded_by_the_source_byte_ceiling(self):
        self.context.discard(self.event.event_id)
        event = self.source("tiny.md", b"x", source_bytes=400)
        self.context.emit_question(event.event_id, "q" * 150, [])
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.emit_question(event.event_id, "r" * 400, [])
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.emit_question(event.event_id, "s" * 300, [])
        self.assertEqual(len(self.context.pending()["questions"]), 1)

    def test_a_published_event_without_output_and_an_unknown_event_are_distinguished(self):
        self.assertEqual(self.context.question_output(self.base_event)["questions"], [])
        self.assertEqual(self.context.question_output(self.base_event)["published_commit"], self.base)
        with self.assertRaises(Conflict):
            self.context.question_output("zzzzz")
        with self.assertRaises(ContextError):
            self.context.question_output("BAD")
        with self.assertRaisesRegex(Conflict, "not published"):
            self.context.question_output(self.event.event_id)


class Boundaries(QuestionCase):
    def test_question_text_in_a_source_is_ordinary_evidence_preserved_byte_for_byte(self):
        content = b"Who is Jesse?\r\nWhat is the surname of Alice?\r\n\xe2\x80\x94 end\r\n"
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools))))
        receipt = self.submit(pipeline, content=content)
        self.assertEqual(self.context.snapshot().files["foundation/one.md"], content)
        self.assertEqual(pipeline.question_output(receipt["event_id"])["questions"], [])
        self.assertEqual(self.context.search("surname", kind="source")[0].kind, "source")

    def test_deterministic_edits_cannot_carry_questions(self):
        self.published_base()
        value = {"format": "qorqut-supervised-edit-v1", "expected_revision": self.later, "rationale": "Record it.",
                 "operations": [{"op": "put_note", "record": {"id": "noten", "text": "A remark.", "objects": []},
                                 "explanation": "Stated.", "citations": ["self"], "support_after": ["self"]}]}
        pipeline = Pipeline(self.context, runner=NoModel())
        receipt = pipeline.submit_supervised(value, content=b"# Remark\n", submitted_by="owner", event_id="sup01")
        self.assertEqual(pipeline.question_output("sup01"),
                         {"event_id": "sup01", "published_commit": receipt["published_commit"], "questions": []})
        self.assertFalse(self.outbox("sup01").exists())

    def test_action_events_may_ask_without_gaining_associations_or_authority(self):
        self.published_base()
        action = self.context.begin_action("add", "routine", text="Routine done.", submitted_by="recorder")
        tools = EventTools(self.context, action.event_id)
        ask(tools, "Which business owns this routine?", [{"kind": "event", "id": action.event_id},
                                                         {"kind": "node", "id": "jesse"}])
        self.context.stage_action_associations(action.event_id, 0, "Recorded.", [])
        self.context.stage_handoff_note(action.event_id, "")
        commit = self.context.publish(action.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.event(action.event_id)["action_associations"]["associations"], [])
        self.assertEqual(snapshot.action_timeline("node", "jesse")["total"], 0)
        self.assertEqual(snapshot.actions_history()["total"], 1)
        self.assertEqual(self.context.question_output(action.event_id)["questions"][0]["related"],
                         [{"kind": "event", "id": action.event_id}, {"kind": "node", "id": "jesse"}])
        self.assertNotIn(b"Which business", everything(self.context))
        self.assertEqual(self.context.question_output(action.event_id)["published_commit"], commit)

    def test_emitting_after_the_event_is_published_or_without_a_pending_event_is_refused(self):
        self.published_base()
        with self.assertRaises(Conflict):
            self.context.emit_question(self.base_event, "Too late?", [])
        with self.assertRaises(Conflict):
            self.context.emit_question("zzzzz", "No such event?", [])
