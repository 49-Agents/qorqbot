"""Notes as a separate Context collection: storage, identity, history, links, support, search and integrity."""
from unittest import TestCase
from unittest.mock import patch

from qorqut_context import Conflict, ContextError
from qorqut_context.git_store import Snapshot
from qorqut_context.schema import encode, note_record, record_path
from .helpers import ContextCase


class NoteRecords(TestCase):
    def test_a_note_is_exactly_three_fields_with_deduplicated_ordered_links(self):
        record = note_record({"id": "abc12", "text": "  Kept exactly as written.\n",
                              "objects": [{"kind": "node", "id": "aaaaa"}, {"kind": "edge", "id": "bbbbb"},
                                          {"kind": "node", "id": "aaaaa"}]})
        self.assertEqual(record, {"id": "abc12", "text": "  Kept exactly as written.\n",
                                  "objects": [{"kind": "node", "id": "aaaaa"}, {"kind": "edge", "id": "bbbbb"}]})
        self.assertEqual(note_record({"id": "abc12", "text": "Alone.", "objects": []})["objects"], [])

    def test_invalid_notes_and_links_are_refused(self):
        good = {"id": "abc12", "text": "A note.", "objects": []}
        cases = {
            "missing objects": {"id": "abc12", "text": "A note."},
            "extra field": {**good, "attributes": {}},
            "empty text": {**good, "text": ""},
            "blank text": {**good, "text": " \n\t "},
            "text type": {**good, "text": 7},
            "id syntax": {**good, "id": "ABC12"},
            "objects type": {**good, "objects": {"kind": "node", "id": "aaaaa"}},
            "note link": {**good, "objects": [{"kind": "note", "id": "aaaaa"}]},
            "event link": {**good, "objects": [{"kind": "event", "id": "aaaaa"}]},
            "link field": {**good, "objects": [{"kind": "node", "id": "aaaaa", "explanation": "x"}]},
            "link id": {**good, "objects": [{"kind": "node", "id": "toolong1"}]},
            "unpaired surrogate": {**good, "text": "bad \ud800"},
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                note_record(value)
        with self.assertRaisesRegex(ContextError, "byte limit"):
            note_record({**good, "text": "x" * 11}, 10)
        self.assertEqual(note_record({**good, "text": "x" * 10}, 10)["text"], "x" * 10)

    def test_every_record_path_comes_from_one_place(self):
        self.assertEqual(record_path("node", "aaaaa"), "graph/nodes/aaaaa.json")
        self.assertEqual(record_path("edge", "aaaaa"), "graph/edges/aaaaa.json")
        self.assertEqual(record_path("note", "aaaaa"), "notes/aaaaa.json")
        with self.assertRaises(ContextError):
            record_path("event", "aaaaa")


class NotesCase(ContextCase):
    """Published base: source one with people jesse/alice, edge edge1 and the note ``self.first`` on jesse."""

    def setUp(self):
        super().setUp()
        event = self.source("one.md", b"Jesse met Alice.")
        self.node(event, "jesse")
        self.node(event, "alice", summary="Alice")
        self.edge(event, "edge1", "jesse", "alice")
        self.first = self.note(event, "Jesse reports meeting Alice; the date is not anchored.", [("node", "jesse")])
        self.base = self.context.publish(event.event_id)

    def publish_with(self, name, content, body):
        event = self.source(name, content)
        body(event)
        return event, self.context.publish(event.event_id)


class NoteStorage(NotesCase):
    def test_a_mixed_batch_publishes_in_one_commit_and_notes_stay_out_of_the_graph(self):
        event = self.source("two.md", b"Mixed filing.")
        self.node(event, "bobby", summary="Bobby")
        self.edge(event, "edge2", "bobby", "alice")
        shared = self.note(event, "Bobby, Alice and their link are discussed together.",
                           [("node", "bobby"), ("node", "alice"), ("edge", "edge2")])
        unlinked = self.note(event, "An unlinked remark that needs no placeholder entity.")
        commit = self.context.publish(event.event_id)
        self.assertEqual(self.context.git.run("rev-list", "--count", f"{self.base}..{commit}").decode().strip(), "1")
        snapshot = self.context.snapshot()
        self.assertEqual(sorted(snapshot.nodes), ["alice", "bobby", "jesse"])
        self.assertEqual(sorted(snapshot.edges), ["edge1", "edge2"])
        self.assertEqual(sorted(snapshot.notes), sorted([self.first, shared, unlinked]))
        self.assertEqual(snapshot.notes[unlinked], {"id": unlinked, "text": "An unlinked remark that needs no placeholder entity.", "objects": []})
        changed = self.context.git.run("diff-tree", "--no-commit-id", "--name-only", "-r", commit).decode().split()
        self.assertEqual(sorted(changed), sorted([
            "foundation/two.md", "graph/catalogs/nodes.json", "graph/events/" + event.event_id + ".json",
            "graph/nodes/bobby.json", "graph/edges/edge2.json", f"notes/{shared}.json", f"notes/{unlinked}.json"]))
        recorded = snapshot.event(event.event_id)["changes"]
        self.assertEqual(sorted((c["kind"], c["operation"]) for c in recorded),
                         [("edge", "add"), ("node", "add"), ("note", "add"), ("note", "add")])
        self.assertEqual(sorted(edge["id"] for edge in snapshot.neighbors("alice")["edges"]), ["edge1", "edge2"])
        with self.assertRaises(ContextError):
            snapshot.objects("note")
        with self.assertRaises(ContextError):
            snapshot.neighbors(shared)
        self.assertEqual(snapshot.read("note", shared)["links"], [
            {"kind": "node", "id": "bobby", "status": "current"}, {"kind": "node", "id": "alice", "status": "current"},
            {"kind": "edge", "id": "edge2", "status": "current"}])
        self.assertEqual(snapshot.read("note", shared)["support"], [commit])
        self.assertEqual(self.context.snapshot(self.base).notes, {self.first: self.context.snapshot(self.base).notes[self.first]})

    def test_an_invalid_batch_publishes_nothing_and_a_corrected_retry_publishes_once(self):
        event = self.source("two.md", b"Second filing.")
        self.node(event, "bobby", summary="Bobby")
        bad = self.note(event, "Links an invented record.", [("node", "zzzzz")])
        with self.assertRaisesRegex(ContextError, "link only"):
            self.context.inspect_draft(event.event_id, validate=True)
        with self.assertRaisesRegex(ContextError, "link only"):
            self.context.publish(event.event_id)
        self.assertEqual(self.context.git.head(), self.base)
        self.assertEqual(sorted(self.context.snapshot().notes), [self.first])
        self.context.remove_note(event.event_id, bad, explanation="Withdraw the invalid note.", citations=["self"])
        commit = self.context.publish(event.event_id)
        self.assertEqual(self.context.publish(event.event_id), commit)
        self.assertEqual(sorted(self.context.snapshot().notes), [self.first])
        self.assertEqual(self.context.git.run("rev-list", "--count", f"{self.base}..{commit}").decode().strip(), "1")

    def test_a_note_cannot_record_developments(self):
        event = self.source("two.md", b"Second filing.")
        with self.assertRaisesRegex(ContextError, "cannot record developments"):
            self.context._put(event.event_id, "note", {"text": "x", "objects": []}, explanation="x",
                              citations=["self"], support_after=["self"],
                              developments=[{"date": 0, "description": "d", "citations": ["self"]}])
        with self.assertRaises(TypeError):
            self.context.put_note(event.event_id, {"text": "x", "objects": []}, explanation="x",
                                  citations=["self"], support_after=["self"], developments=[])

    def test_oversized_notes_respect_the_source_byte_ceiling(self):
        event = self.context.begin("add", "foundation/two.md", content=b"Small.", source_bytes=64,
                                   metadata={"kind": "note", "title": "t", "origin": "o"}, submitted_by="owner")
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.note(event, "x" * 65)
        self.note(event, "x" * 64)


class NoteIdentity(NotesCase):
    def test_clarification_keeps_the_id_and_history_and_leaves_old_revisions_readable(self):
        event, second = self.publish_with("two.md", b"Alice confirmed the date.", lambda e: self.note(
            e, "Jesse met Alice on 2026-10-05.", [("node", "jesse"), ("node", "alice")], note_id=self.first,
            support=[self.base, "self"]))
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.notes[self.first]["text"], "Jesse met Alice on 2026-10-05.")
        self.assertEqual(snapshot.read("note", self.first)["support"], [self.base, second])
        history = snapshot.history(self.first, kind="note")
        self.assertEqual([item["commit"] for item in history], [self.base, second])
        self.assertEqual([item["operation"] for item in history], ["add", "edit"])
        self.assertIsNone(history[0]["record_before"])
        self.assertEqual(history[1]["record_before"]["text"], "Jesse reports meeting Alice; the date is not anchored.")
        self.assertEqual(history[1]["record_after"]["objects"], [{"kind": "node", "id": "jesse"}, {"kind": "node", "id": "alice"}])
        self.assertEqual(history[1]["citations"], [second])
        old = self.context.snapshot(self.base)
        self.assertEqual(old.read("note", self.first)["record"]["text"], "Jesse reports meeting Alice; the date is not anchored.")
        self.assertEqual(len(old.history(self.first, kind="note")), 1)
        self.assertEqual(snapshot.history(self.first), history)

    def test_link_only_and_support_only_updates_are_recorded_without_losing_identity(self):
        record = self.context.snapshot().notes[self.first]
        _, linked = self.publish_with("two.md", b"Alice is relevant too.", lambda e: self.note(
            e, record["text"], [("node", "jesse"), ("edge", "edge1")], note_id=self.first, support=[self.base, "self"]))
        _, support_only = self.publish_with("three.md", b"Another source repeats it.", lambda e: self.note(
            e, record["text"], [("node", "jesse"), ("edge", "edge1")], note_id=self.first, support=[self.base, linked, "self"]))
        history = self.context.snapshot().history(self.first, kind="note")
        self.assertEqual([item["operation"] for item in history], ["add", "edit", "edit"])
        self.assertEqual(history[1]["record_after"]["objects"], [{"kind": "node", "id": "jesse"}, {"kind": "edge", "id": "edge1"}])
        self.assertEqual(history[2]["record_before"], history[2]["record_after"])
        changed = self.context.git.run("diff-tree", "--no-commit-id", "--name-only", "-r", support_only).decode().split()
        self.assertNotIn(f"notes/{self.first}.json", changed)
        self.assertEqual(self.context.snapshot().read("note", self.first)["support"], [self.base, linked, support_only])

    def test_removal_is_history_and_the_id_is_never_reused(self):
        _, removal = self.publish_with("two.md", b"Retraction.", lambda e: self.context.remove_note(
            e.event_id, self.first, explanation="The source retracts the report.", citations=["self"]))
        snapshot = self.context.snapshot()
        self.assertNotIn(self.first, snapshot.notes)
        self.assertEqual([item["operation"] for item in snapshot.history(self.first, kind="note")], ["add", "remove"])
        self.assertIsNone(snapshot.history(self.first, kind="note")[1]["record_after"])
        self.assertEqual(snapshot.history(self.first, kind="note")[1]["support_after"], [])
        with self.assertRaises(ContextError):
            snapshot.read("note", self.first)
        event = self.source("three.md", b"Third.")
        with self.assertRaisesRegex(Conflict, "already reserved or has been published"):
            self.note(event, "Reusing a retired ID.", note_id=self.first)
        draws = iter([self.first, "jesse", "edge1", "fresh"])
        with patch.object(self.context, "_random_id", side_effect=lambda: next(draws)):
            self.assertEqual(self.note(event, "A new note gets an unused ID."), "fresh")
        self.assertEqual(self.context.publish(event.event_id), self.context.git.head())

    def test_one_namespace_covers_nodes_edges_and_notes(self):
        event = self.source("two.md", b"Second.")
        for taken in ("jesse", "edge1", event.event_id):
            with self.subTest(taken=taken), self.assertRaises(Conflict):
                self.note(event, "Clashes with another record.", note_id=taken)
        with self.assertRaises(Conflict):
            self.node(event, self.first)

    def test_a_note_created_and_removed_in_one_draft_is_never_published(self):
        event = self.source("two.md", b"Second.")
        created = self.note(event, "Scratch note.")
        self.context.remove_note(event.event_id, created, explanation="Scratch.", citations=["self"])
        self.context.publish(event.event_id)
        self.assertEqual(sorted(self.context.snapshot().notes), [self.first])
        self.assertNotIn(created, self.context.snapshot().used_ids)
        with self.assertRaisesRegex(ContextError, "No draft/current note"):
            self.context.remove_note(self.source("three.md", b"Third.").event_id, "nnnnn",
                                     explanation="Unknown.", citations=["self"])


class NoteLinks(NotesCase):
    def test_unknown_wrong_kind_and_withdrawn_draft_targets_are_refused(self):
        cases = {"unknown node": ("node", "zzzzz"), "node id used as an edge": ("edge", "jesse"),
                 "edge id used as a node": ("node", "edge1")}
        for label, link in cases.items():
            with self.subTest(label):
                event = self.source(f"{label[:4]}.md", b"Links " + label.encode())
                note = self.note(event, "Bad link.", [link])
                with self.assertRaisesRegex(ContextError, "link only"):
                    self.context.inspect_draft(event.event_id, validate=True)
                self.context.remove_note(event.event_id, note, explanation="Drop it.", citations=["self"])
                self.context.discard(event.event_id)
        event = self.source("scratch.md", b"Scratch node.")
        self.node(event, "scrat", summary="Scratch")
        self.note(event, "Links a scratch node.", [("node", "scrat")])
        self.context.remove_node(event.event_id, "scrat", explanation="Never published.", citations=["self"])
        with self.assertRaisesRegex(ContextError, "link only"):
            self.context.publish(event.event_id)

    def test_a_target_staged_after_the_note_is_valid_and_order_is_irrelevant(self):
        event = self.source("two.md", b"Second.")
        self.note(event, "Mentions Bobby and his edge.", [("node", "bobby"), ("edge", "edge9")])
        self.node(event, "bobby", summary="Bobby")
        self.edge(event, "edge9", "bobby", "alice")
        commit = self.context.publish(event.event_id)
        self.assertEqual(len(self.context.snapshot(commit).notes), 2)

    def test_removing_a_graph_object_keeps_the_note_and_labels_its_link_removed(self):
        event = self.source("two.md", b"Jesse left.")
        self.context.remove_edge(event.event_id, "edge1", explanation="The relationship ended.", citations=["self"])
        self.context.remove_node(event.event_id, "jesse", explanation="Merged away.", citations=["self"])
        commit = self.context.publish(event.event_id)
        snapshot = self.context.snapshot()
        self.assertIn(self.first, snapshot.notes)
        self.assertEqual(snapshot.read("note", self.first)["links"], [{"kind": "node", "id": "jesse", "status": "removed"}])
        linked = snapshot.linked_notes("node", "jesse")
        self.assertEqual((linked["current"], [item["record"]["id"] for item in linked["items"]]), (False, [self.first]))
        self.assertEqual(snapshot.read("note", self.first)["support"], [self.base])
        _, later = self.publish_with("three.md", b"About Jesse.", lambda e: self.note(
            e, "Earlier account of Jesse.", [("node", "jesse"), ("edge", "edge1")]))
        self.assertEqual(len(self.context.snapshot().notes), 2)
        with self.assertRaises(ContextError):
            self.context.snapshot().linked_notes("node", "unknown")

    def test_legacy_endpoint_rule_is_unchanged_by_historical_note_links(self):
        event = self.source("two.md", b"Jesse left.")
        self.context.remove_node(event.event_id, "jesse", explanation="Removed.", citations=["self"])
        with self.assertRaisesRegex(ContextError, "endpoint"):
            self.context.publish(event.event_id)

    def test_links_to_edges_describe_the_relationship_without_implying_edges(self):
        event, commit = self.publish_with("two.md", b"Both people matter.", lambda e: self.note(
            e, "Jesse and Alice are both mentioned.", [("node", "jesse"), ("node", "alice")]))
        self.assertEqual(self.context.snapshot().neighbors("jesse")["edges"][0]["id"], "edge1")
        self.assertEqual(len(self.context.snapshot().edges), 1)
        self.assertEqual([item["record"]["id"] for item in self.context.snapshot().linked_notes("node", "alice")["items"]],
                         [self.context.snapshot().linked_notes("node", "alice")["items"][0]["record"]["id"]])


class NoteSupport(NotesCase):
    def test_support_is_source_revisions_never_a_note(self):
        event = self.source("two.md", b"Second.")
        record = {"text": "Attributed remark.", "objects": []}
        for label, arguments in {"note as citation": dict(citations=[self.first], support_after=["self"]),
                                 "note as support": dict(citations=["self"], support_after=[self.first])}.items():
            with self.subTest(label), self.assertRaisesRegex(ContextError, "full Git hashes or event-local self"):
                self.context.put_note(event.event_id, record, explanation="x", **arguments)
        created = self.context.put_note(event.event_id, record, explanation="x", citations=["f" * 40],
                                        support_after=["self"])
        with self.assertRaisesRegex(ContextError, "published source events"):
            self.context.inspect_draft(event.event_id, validate=True)
        self.context.put_note(event.event_id, {**record, "id": created}, explanation="x", citations=["self"],
                              support_after=[])
        with self.assertRaisesRegex(ContextError, "Current notes require supporting source events"):
            self.context.inspect_draft(event.event_id, validate=True)
        self.assertEqual(self.context.git.head(), self.base)

    def test_a_shared_note_survives_source_removal_only_with_remaining_support(self):
        _, second = self.publish_with("two.md", b"Alice repeats the report.", lambda e: self.note(
            e, "Jesse reports meeting Alice; the date is not anchored.", [("node", "jesse")], note_id=self.first,
            support=[self.base, "self"]))
        sole = self.context.snapshot().supported_by
        removal = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        self.context.stage_handoff_note(removal.event_id, "")
        required = {(r["kind"], r["id"]) for r in self.context.required_reassessment(self.context.pending())}
        self.assertEqual(required, {("node", "jesse"), ("node", "alice"), ("edge", "edge1"), ("note", self.first)})
        self.assertEqual([r["id"] for r in self.context.snapshot().supported_by(removal.source_id) if r["kind"] == "note"],
                         [self.first])
        self.context.remove_edge(removal.event_id, "edge1", explanation="Unsupported.", citations=["self"])
        self.context.remove_node(removal.event_id, "alice", explanation="Unsupported.", citations=["self"])
        self.context.remove_node(removal.event_id, "jesse", explanation="Unsupported.", citations=["self"])
        with self.assertRaisesRegex(ContextError, "Reassess graph support"):
            self.context.publish(removal.event_id)
        self.note(removal, "Jesse reports meeting Alice; the date is not anchored.", [("node", "jesse")],
                  note_id=self.first, support=[second])
        removed = self.context.publish(removal.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.read("note", self.first)["support"], [second])
        self.assertEqual(snapshot.read("note", self.first)["links"], [{"kind": "node", "id": "jesse", "status": "removed"}])

    def test_a_note_with_no_remaining_support_must_be_withdrawn(self):
        removal = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        self.context.stage_handoff_note(removal.event_id, "")
        for kind, target in (("edge", "edge1"), ("node", "alice"), ("node", "jesse")):
            getattr(self.context, f"remove_{kind}")(removal.event_id, target, explanation="Unsupported.", citations=["self"])
        with self.assertRaisesRegex(ContextError, "Reassess graph support"):
            self.context.publish(removal.event_id)
        self.context.remove_note(removal.event_id, self.first, explanation="Nothing remains supported.", citations=["self"])
        commit = self.context.publish(removal.event_id)
        self.assertEqual(self.context.snapshot().notes, {})
        self.assertEqual([c["operation"] for c in self.context.snapshot().event(removal.event_id)["changes"]],
                         ["remove"] * 4)

    def test_supported_by_lists_notes_after_nodes_and_edges(self):
        listed = self.context.snapshot().supported_by(self.context.snapshot().events[next(iter(self.context.snapshot().events))]["source"]["id"])
        self.assertEqual([(item["kind"], item["id"]) for item in listed],
                         [("edge", "edge1"), ("node", "alice"), ("node", "jesse"), ("note", self.first)])


class NoteSearch(NotesCase):
    def test_unlinked_note_text_is_found_even_when_no_entity_says_it(self):
        _, commit = self.publish_with("two.md", b"A passing remark.", lambda e: self.note(
            e, "Someone mentioned a sailing trip in Lisbon, but nothing is confirmed."))
        hits = self.context.search("Lisbon sailing")
        self.assertEqual([(hit.kind, hit.text) for hit in hits],
                         [("note", "Someone mentioned a sailing trip in Lisbon, but nothing is confirmed.")])
        self.assertEqual(self.context.search("Lisbon", kind="note")[0].kind, "note")
        self.assertEqual(self.context.search("Lisbon", kind="node"), [])
        self.assertEqual([hit.kind for hit in self.context.search("Jesse")], ["node", "note", "source"])
        self.assertEqual([hit.id for hit in self.context.search("Jesse", kind="note")], [self.first])

    def test_updates_removals_and_rebuilds_keep_the_projection_consistent(self):
        before = self.context.search("anchored", kind="note")
        self.assertEqual([hit.id for hit in before], [self.first])
        self.publish_with("two.md", b"Clarified.", lambda e: self.note(
            e, "Jesse met Alice on a Tuesday.", [("node", "jesse")], note_id=self.first, support=[self.base, "self"]))
        self.assertEqual(self.context.search("anchored", kind="note"), [])
        self.assertEqual([hit.id for hit in self.context.search("Tuesday", kind="note")], [self.first])
        self.context.index.path.unlink()
        self.assertEqual([hit.id for hit in self.context.search("Tuesday", kind="note")], [self.first])
        self.publish_with("three.md", b"Retraction.", lambda e: self.context.remove_note(
            e.event_id, self.first, explanation="Withdrawn.", citations=["self"]))
        self.assertEqual(self.context.search("Tuesday", kind="note"), [])
        self.assertEqual([hit.id for hit in self.context.search("anchored", kind="note", revision=self.base)], [self.first])

    def test_search_hits_expose_the_note_text_and_digest(self):
        hit = self.context.search("anchored", kind="note")[0]
        self.assertEqual((hit.kind, hit.id, hit.revision), ("note", self.first, self.base))
        self.assertEqual(hit.text, "Jesse reports meeting Alice; the date is not anchored.")
        self.assertEqual(len(hit.content_digest), 64)


class NoteIntegrity(NotesCase):
    def craft(self, files):
        return self.context.git.commit(files, self.context.runtime, self.base, "crafted")

    def test_a_published_snapshot_refuses_unsound_notes_and_event_kinds(self):
        self.assertEqual(Snapshot(self.context.git, self.base).notes, self.context.snapshot().notes)
        good = self.context.snapshot().notes[self.first]
        event = dict(next(iter(self.context.snapshot().events.values())))
        cases = {
            "extra field": {f"notes/{self.first}.json": encode({**good, "attributes": {}})},
            "invalid text": {f"notes/{self.first}.json": encode({**good, "text": ""})},
            "path mismatch": {"notes/other.json": encode(good)},
            "never published target": {f"notes/{self.first}.json": encode({**good, "objects": [{"kind": "node", "id": "zzzzz"}]})},
            "wrong kind link": {f"notes/{self.first}.json": encode({**good, "objects": [{"kind": "edge", "id": "jesse"}]})},
            "event kind": {"graph/events/" + event["id"] + ".json": encode(
                {**event, "changes": [{**event["changes"][0], "kind": "claim"}]})},
        }
        for label, files in cases.items():
            with self.subTest(label), self.assertRaises((ContextError, KeyError)):
                Snapshot(self.context.git, self.craft(files))
