"""Notes through the model-facing tools and the bootstrap sample."""
import random
from types import SimpleNamespace

from qorqut_context import ContextError
from qorqut_context.bootstrap import NOTE_EXCERPT, NOTE_SAMPLE, note_sample
from qorqut_context.tools import EventTools
from .helpers import ContextCase


class NoteToolCase(ContextCase):
    def call(self, tools, name, **arguments):
        response = tools.dispatch(name, arguments)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def refused(self, tools, name, pattern, **arguments):
        response = tools.dispatch(name, arguments)
        self.assertFalse(response["ok"], response)
        self.assertRegex(response["error"]["message"], pattern)

    def setUp(self):
        super().setUp()
        first = self.source("one.md", b"Jesse met Alice in Lisbon.")
        self.node(first, "jesse")
        self.node(first, "alice", summary="Alice")
        self.edge(first, "edge1", "jesse", "alice")
        self.first_note = self.note(first, "Jesse reports meeting Alice in Lisbon.", [("node", "jesse")])
        self.commit = self.context.publish(first.event_id)


class NoteTools(NoteToolCase):
    def test_notes_are_staged_read_listed_and_reassessed_through_tools(self):
        second = self.source("two.md", b"Alice confirms.")
        tools = EventTools(self.context, second.event_id)
        created = self.call(tools, "put_note", record={"text": "Alice confirms the Lisbon meeting.", "objects": [
            {"kind": "node", "id": "alice"}, {"kind": "edge", "id": "edge1"}]}, explanation="Alice confirms.",
            citations=["self"], support_after=["self"])
        self.assertEqual((created["kind"], created["staged"]), ("note", True))
        read = self.call(tools, "read_object", kind="note", id=created["id"])
        self.assertEqual(read["support"], ["self"])
        self.assertEqual(read["links"], [{"kind": "node", "id": "alice", "status": "current"},
                                         {"kind": "edge", "id": "edge1", "status": "current"}])
        self.assertTrue(read["staged"])
        jesse = self.call(tools, "read_object", kind="node", id="jesse")
        self.assertEqual(jesse["linked_notes"], {"count": 1, "ids": [self.first_note], "has_more": False})
        alice = self.call(tools, "read_object", kind="node", id="alice")
        self.assertEqual(alice["linked_notes"]["ids"], [created["id"]])
        listed = self.call(tools, "list_objects", kind="note")
        self.assertEqual([item["record"]["id"] for item in listed["items"]], sorted([self.first_note, created["id"]]))
        linked = self.call(tools, "list_linked_notes", kind="edge", id="edge1")
        self.assertEqual([item["record"]["id"] for item in linked["items"]], [created["id"]])
        self.assertTrue(linked["current"])
        self.assertEqual([hit["id"] for hit in self.call(tools, "search_context", query="Lisbon", kind="note")["items"]],
                         [self.first_note])
        self.assertEqual(self.call(tools, "object_history", id=self.first_note, kind="note")["total"], 1)
        draft = self.call(tools, "read_draft")
        self.assertEqual([(item["kind"], item["id"]) for item in draft["items"]], [("note", created["id"])])
        done = self.call(tools, "finish_propagation", status="complete", summary="Filed one note.", reassessed=[])
        commit = self.context.publish(second.event_id, expected_draft_digest=done["draft_digest"])
        self.assertEqual(self.context.snapshot(commit).read("note", created["id"])["support"], [commit])

    def test_creation_ids_are_allocated_and_link_targets_are_checked_immediately(self):
        second = self.source("two.md", b"Second.")
        tools = EventTools(self.context, second.event_id)
        self.refused(tools, "put_note", "Omit id when creating a note", record={"id": "newid", "text": "x", "objects": []},
                     explanation="x", citations=["self"], support_after=["self"])
        for label, link in {"invented": {"kind": "node", "id": "zzzzz"}, "wrong kind": {"kind": "edge", "id": "jesse"}}.items():
            with self.subTest(label):
                self.refused(tools, "put_note", "may link only", record={"text": "x", "objects": [link]},
                             explanation="x", citations=["self"], support_after=["self"])
        self.refused(tools, "put_note", r"objects\[\]\.kind must be one of", record={"text": "x", "objects": [{"kind": "note", "id": self.first_note}]},
                     explanation="x", citations=["self"], support_after=["self"])
        self.refused(tools, "put_note", "Unknown field: arguments.record.attributes", record={"text": "x", "objects": [], "attributes": {}},
                     explanation="x", citations=["self"], support_after=["self"])
        self.refused(tools, "put_note", "within this published base", record={"text": "x", "objects": []},
                     explanation="x", citations=["f" * 40], support_after=["self"])
        self.assertEqual(self.call(tools, "read_draft")["total"], 0)
        node = self.call(tools, "put_node", record={"summary": "Bobby", "attributes": {"basic_entity_type": ["person"]}},
                         explanation="Bobby appears.", citations=["self"], support_after=["self"])["id"]
        revised = self.call(tools, "put_note", record={"id": self.first_note, "text": "Jesse met Alice and Bobby in Lisbon.",
                            "objects": [{"kind": "node", "id": "jesse"}, {"kind": "node", "id": node}]},
                            explanation="Adds Bobby.", citations=["self"], support_after=[self.commit, "self"])
        self.assertEqual(revised["id"], self.first_note)

    def test_removal_and_sealed_attempts(self):
        second = self.source("two.md", b"Second.")
        tools = EventTools(self.context, second.event_id)
        self.call(tools, "remove_note", id=self.first_note, explanation="Retracted.", citations=["self"])
        read = self.call(tools, "read_object", kind="note", id=self.first_note)
        self.assertEqual((read["record"], read["removed"], read["historical"]), (None, True, True))
        self.assertEqual(self.call(tools, "read_object", kind="node", id="jesse")["linked_notes"]["count"], 0)
        self.refused(tools, "remove_note", "No draft/current note", id="nnnnn", explanation="x", citations=["self"])
        done = self.call(tools, "finish_propagation", status="complete", summary="Withdrew the note.", reassessed=[])
        self.refused(tools, "put_note", "sealed", record={"text": "late", "objects": []}, explanation="x",
                     citations=["self"], support_after=["self"])
        self.refused(tools, "remove_note", "sealed", id=self.first_note, explanation="x", citations=["self"])
        self.refused(tools, "emit_question", "sealed", text="Too late?")
        self.assertEqual(self.call(tools, "finish_propagation", status="complete", summary="Withdrew the note.", reassessed=[]), done)

    def test_a_note_must_be_reassessed_when_its_only_source_is_removed(self):
        removal = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        tools = EventTools(self.context, removal.event_id)
        self.call(tools, "stage_handoff_note", text="")
        self.assertEqual({(r["kind"], r["id"]) for r in tools.required_reassessment},
                         {("node", "jesse"), ("node", "alice"), ("edge", "edge1"), ("note", self.first_note)})
        self.refused(tools, "finish_propagation", "required_reassessment", status="complete", summary="Skipped the note.",
                     reassessed=[{"kind": kind, "id": key} for kind, key in
                                 {("node", "jesse"), ("node", "alice"), ("edge", "edge1")}])
        self.assertEqual(self.call(tools, "read_draft")["required_reassessment"], tools.required_reassessment)

    def test_note_tools_exist_in_both_modes_and_notes_cannot_record_developments(self):
        general = EventTools(self.context, self.source("two.md", b"Second.").event_id)
        names = {tool["name"] for tool in general.definitions}
        self.assertTrue({"put_note", "remove_note", "emit_question", "list_linked_notes"} <= names)
        self.assertNotIn("developments", {tool["name"]: tool["inputSchema"] for tool in general.definitions}["put_note"]["properties"])
        self.context.discard(general.event_id)
        action = self.context.begin_action("add", "routine", text="Routine done.", submitted_by="recorder")
        tools = EventTools(self.context, action.event_id)
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools.definitions}
        self.assertTrue({"put_note", "remove_note", "emit_question", "stage_action_associations"} <= set(schemas))
        self.assertNotIn("developments", schemas["put_note"]["properties"])
        self.refused(tools, "put_note", "Unknown field: arguments.developments", record={"text": "x", "objects": []},
                     explanation="x", citations=["self"], support_after=["self"], developments=[{"date": 0, "description": "d", "citations": ["self"]}])
        with self.assertRaisesRegex(ContextError, "A note cannot record developments"):
            self.context._put(action.event_id, "note", {"text": "x", "objects": []}, explanation="x",
                              citations=["self"], support_after=["self"],
                              developments=[{"date": 0, "description": "d", "citations": ["self"]}])
        note = self.call(tools, "put_note", record={"text": "The routine was reported done by the recorder.", "objects": []},
                         explanation="Recorded.", citations=["self"], support_after=["self"])["id"]
        self.context.stage_action_associations(action.event_id, 0, "Recorded.", [])
        self.context.stage_handoff_note(action.event_id, "")
        commit = self.context.publish(action.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.actions_history()["total"], 1)
        self.assertEqual(snapshot.event(action.event_id)["action_associations"]["associations"], [])
        self.assertEqual(snapshot.read("note", note)["support"], [commit])


class BootstrapNotes(NoteToolCase):
    def graph(self, notes):
        return SimpleNamespace(notes=notes)

    def test_sample_is_bounded_deterministic_with_a_seeded_rng_and_reports_omissions(self):
        notes = {f"n{i:04d}"[:5]: {"id": f"n{i:04d}"[:5], "text": f"Lisbon remark {i} " + "x" * (NOTE_EXCERPT + 5),
                                   "objects": [{"kind": "node", "id": "jesse"}]} for i in range(NOTE_SAMPLE + 12)}
        change = {"before": None, "after": "Jesse went to Lisbon."}
        result = note_sample(self.graph(notes), change, {"jesse"}, set(), rng=random.Random(1))
        self.assertEqual(result["counts"], {"eligible": NOTE_SAMPLE + 12, "returned": NOTE_SAMPLE})
        self.assertTrue(result["truncated"])
        self.assertEqual(result["note_ids"], sorted(result["note_ids"]))
        self.assertEqual(len(result["notes"]), NOTE_SAMPLE)
        self.assertTrue(all(len(item["text"]) == NOTE_EXCERPT and item["text_truncated"] for item in result["notes"]))
        self.assertIn("search_context with kind note", result["notice"])
        again = note_sample(self.graph(notes), change, {"jesse"}, set(), rng=random.Random(1))
        self.assertEqual(result, again)

    def test_sample_selects_linked_or_word_sharing_notes_only(self):
        notes = {"aaaaa": {"id": "aaaaa", "text": "Unrelated topic entirely.", "objects": []},
                 "bbbbb": {"id": "bbbbb", "text": "Different words.", "objects": [{"kind": "edge", "id": "edge1"}]},
                 "ccccc": {"id": "ccccc", "text": "Mentions the Lisbon trip.", "objects": []}}
        result = note_sample(self.graph(notes), {"before": "Old text", "after": "Trip to Lisbon"}, set(), {"edge1"})
        self.assertEqual(result["note_ids"], ["bbbbb", "ccccc"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["notes"][0]["objects"], [{"kind": "edge", "id": "edge1"}])
        self.assertEqual(note_sample(SimpleNamespace(), {"before": "x", "after": "y"}, set(), set())["note_ids"], [])
