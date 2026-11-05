from pathlib import Path
from unittest.mock import patch

from qorqut_context import Conflict, Context, ContextError
from .helpers import ContextCase


class EventHistory(ContextCase):
    def test_source_and_graph_publish_once_with_exact_bytes(self):
        original = b"\xef\xbb\xbfJesse joined.\r\nAnother development.\r\n"
        event = self.source(content=original)
        self.node(event)
        self.assertEqual(self.context.snapshot().nodes, {})
        self.assertNotIn("foundation/one.md", self.context.snapshot().files)
        commit = self.context.publish(event.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.files["foundation/one.md"], original)
        self.assertEqual(snapshot.read("node", "jesse")["support"], [commit])
        changed = self.context.git.run("diff-tree", "--no-commit-id", "--name-only", "-r", commit).decode().splitlines()
        self.assertEqual([p for p in changed if p.startswith("foundation/")], ["foundation/one.md"])
        self.assertIn("graph/nodes/jesse.json", changed)
        self.assertIn(f"graph/events/{event.event_id}.json", changed)
        history = snapshot.history("jesse")
        self.assertEqual(history[0]["developments"], [])
        self.assertEqual(history[0]["citations"], [commit])
        self.assertTrue(history[0]["recorded_at"].endswith("Z"))
        self.assertEqual(snapshot.source_change(commit)["after"], original)
        self.assertEqual(snapshot.source_change(commit)["event"]["changes"][0]["citations"], [commit])
        self.assertEqual(snapshot.events[event.event_id]["source"]["created_at"], "2026-01-01T00:00:00Z")
        self.assertIsNone(self.context.pending())
        self.context.git.require_clean()
        receipt = self.source(content=original, event_id=event.event_id)
        self.assertEqual(receipt.published_commit, commit)
        self.assertEqual(self.context.publish(event.event_id), commit)
        self.assertEqual(self.context.git.head(), commit)
        with self.assertRaises(Conflict):
            self.source(content=b"Changed input", event_id=event.event_id)

    def test_support_only_change_and_self_are_stable_after_rebuild(self):
        first = self.source()
        self.node(first)
        a = self.context.publish(first.event_id)
        second = self.source("two.md", b"A second source also identifies Jesse.")
        self.node(second, support=[a, "self"])
        b = self.context.publish(second.event_id)
        self.assertEqual(self.context.git.run("diff", "--name-only", a, b, "--", "graph/nodes/"), b"")
        self.assertEqual(len(self.context.snapshot().history("jesse")), 2)
        unrelated = self.source("three.txt", b"No graph effect.")
        self.context.publish(unrelated.event_id)
        Path(self.context.index.path).unlink()
        self.context.search("Jesse")
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.read("node", "jesse")["support"], [a, b])
        self.assertEqual(snapshot.history("jesse")[0]["citations"], [a])
        self.assertEqual(snapshot.history("jesse")[1]["citations"], [b])
        self.assertEqual([item["id"] for item in snapshot.supported_by(second.source_id)], ["jesse"])
        self.assertEqual(snapshot.events[unrelated.event_id]["changes"], [])

    def test_type_only_attribute_edits_publish_and_refresh_search(self):
        original = self.source()
        self.node(original, attrs={"basic_entity_type": ["person"], "value": [1]})
        first = self.context.publish(original.event_id)
        correction = self.source("correction.md", b"The attribute is boolean.")
        self.node(correction, attrs={"basic_entity_type": ["person"], "value": [True]},
                  support=[first])
        second = self.context.publish(correction.event_id)
        snapshot = self.context.snapshot()
        self.assertIs(snapshot.read("node", "jesse")["record"]["attributes"]["value"][0], True)
        self.assertEqual([change["commit"] for change in snapshot.history("jesse")], [first, second])
        self.assertEqual(snapshot.catalogs["node"]["value"], [True])
        self.assertEqual([hit.id for hit in self.context.search(kind="node", attributes={"value": [True]})], ["jesse"])
        self.assertEqual(self.context.search(kind="node", attributes={"value": [1]}), [])

    def test_source_removal_retains_other_support_then_removes_unsupported_object(self):
        one = self.source()
        self.node(one)
        a = self.context.publish(one.event_id)
        two = self.source("two.txt", b"Jesse is supported here too.")
        self.node(two, support=[a, "self"])
        b = self.context.publish(two.event_id)
        remove_one = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        self.context.stage_handoff_note(remove_one.event_id, "")
        with self.assertRaises(ContextError):
            self.context.publish(remove_one.event_id)
        self.node(remove_one, support=[b])
        self.context.publish(remove_one.event_id)
        self.assertEqual(self.context.snapshot().read("node", "jesse")["support"], [b])
        self.assertEqual(self.context.snapshot().supported_by(one.source_id), [])
        remove_two = self.context.begin("remove", "foundation/two.txt", submitted_by="owner")
        self.context.stage_handoff_note(remove_two.event_id, "")
        self.context.remove_node(remove_two.event_id, "jesse", explanation="Last supporting source removed.", citations=["self"])
        deletion = self.context.publish(remove_two.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.nodes, {})
        self.assertEqual(snapshot.source_change(deletion)["before"], b"Jesse is supported here too.")
        self.assertIsNone(snapshot.source_change(deletion)["after"])
        self.assertEqual(self.context.snapshot(a).nodes["jesse"]["summary"], "Jesse")
        self.assertIn("Jesse", snapshot.catalogs["node"]["name"])
        next_event = self.source("new.md")
        with self.assertRaises(Conflict):
            self.node(next_event, "jesse")

    def test_parallel_reverse_and_self_edges_and_merge_by_basic_operations(self):
        original = self.source()
        self.node(original, "nodea", summary="A")
        self.node(original, "nodeb", summary="B")
        for edge_id, left, right in (("edge1", "nodea", "nodeb"), ("edge2", "nodea", "nodeb"),
                                     ("edge3", "nodeb", "nodea"), ("edge4", "nodea", "nodea")):
            self.edge(original, edge_id, left, right)
        first_commit = self.context.publish(original.event_id)
        self.assertEqual(len(self.context.snapshot().neighbors("nodea")["edges"]), 4)
        correction = self.source("correction.md", b"A and B identify the same entity.")
        self.context.remove_node(correction.event_id, "nodea", explanation="The note identifies A with B.", citations=["self"])
        with self.assertRaises(ContextError):
            self.context.publish(correction.event_id)
        self.assertEqual(self.context.git.head(), first_commit)
        for edge_id in ("edge1", "edge2", "edge3", "edge4"):
            self.edge(correction, edge_id, "nodeb", "nodeb", support=[first_commit, "self"])
        self.node(correction, "nodeb", support=[first_commit, "self"], summary="A and B")
        self.context.publish(correction.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(set(snapshot.nodes), {"nodeb"})
        self.assertEqual(len(snapshot.edges), 4)
        self.assertEqual(len(snapshot.history("edge1")), 2)

    def test_metadata_only_deferred_and_source_id_stays_stable(self):
        event = self.source(content=b"Version one")
        first = self.context.publish(event.event_id)
        with self.assertRaises(ContextError):
            self.context.begin("edit", "foundation/one.md", content=b"Version one",
                               metadata={"kind": "note", "title": "Renamed", "origin": "test"}, submitted_by="owner")
        second = self.context.begin("edit", "foundation/one.md", content=b"Version two", submitted_by="owner")
        self.context.stage_handoff_note(second.event_id, "")
        self.assertEqual(second.source_id, event.source_id)
        self.context.publish(second.event_id)
        history = self.context.snapshot().history(event.source_id, kind="source")
        self.assertEqual([e["operation"] for e in history], ["add", "edit"])
        self.assertEqual(self.context.snapshot().source_change(first)["after"], b"Version one")

    def test_id_collision_checks_span_source_event_and_objects(self):
        event = self.source()
        candidates = iter([event.event_id, event.source_id, "fresh"])
        with patch.object(self.context, "_random_id", side_effect=lambda: next(candidates)):
            object_id = self.context.put_node(event.event_id, {"summary": "Generated", "attributes": {"basic_entity_type": ["person"]}},
                                              explanation="From source.", citations=["self"], support_after=["self"])
        self.assertEqual(object_id, "fresh")
        with self.assertRaises(Conflict):
            self.edge(event, "fresh", "fresh", "fresh")
        self.context.publish(event.event_id)

    def test_two_businesses_have_independent_state_and_reject_foreign_evidence(self):
        event = self.source()
        self.node(event)
        commit = self.context.publish(event.event_id)
        other = Context.initialize(self.directory / "other")
        self.assertEqual(other.snapshot().nodes, {})
        self.assertEqual(other.snapshot().catalogs["node"], {"basic_entity_type": []})
        pending = other.begin("add", "foundation/other.md", content=b"Other business.",
                              metadata={"kind": "note", "title": "Other", "origin": "test"}, submitted_by="owner")
        other.put_node(pending.event_id, {"id": "jesse", "summary": "Other", "attributes": {"basic_entity_type": ["person"]}},
                       explanation="Foreign evidence rejected.", citations=[commit], support_after=[commit])
        with self.assertRaises(ContextError):
            other.publish(pending.event_id)
        with self.assertRaises(Conflict):
            Context(other.root, self.context.runtime).search()

    def test_catalogs_keep_all_names_and_only_categorical_values(self):
        event = self.source()
        self.node(event, attrs={"basic_entity_type": ["person", "investor"], "count": [12, 47],
                                "mixed": [True, 1, "1", True], "unused": []})
        self.edge(event, "edge1", "jesse", "jesse")
        self.context.publish(event.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.catalogs["node"]["count"], [])
        self.assertEqual(snapshot.catalogs["node"]["mixed"], [True, "1"])
        self.assertNotIn("unused", snapshot.catalogs["node"])
        self.assertNotIn("relation", snapshot.catalogs["node"])
        self.assertIn("relation", snapshot.catalogs["edge"])
