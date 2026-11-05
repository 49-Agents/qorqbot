from unittest.mock import patch

from qorqut_context import Context, SearchHit
from qorqut_context.search import StaleIndex
from .helpers import ContextCase


class MemoryIndex:
    """A second index implementation exercising the engine's public boundary."""
    def __init__(self, path):
        self.records, self.current, self.applied = {}, None, []

    def revision(self):
        return self.current

    def rebuild(self, revision, records):
        self.current, self.records = revision, {r.key: r for r in records}

    def apply(self, changes):
        if self.current != changes.base_revision:
            raise StaleIndex("Wrong base")
        for key in changes.removed:
            self.records.pop(key, None)
        self.records.update({r.key: r for r in changes.changed})
        self.current = changes.revision
        self.applied.append(changes)

    def search(self, revision, query="", *, kind=None, attributes=None, limit=20):
        if revision != self.current:
            raise StaleIndex("Wrong revision")
        return [SearchHit(r.kind, r.id, revision, r.content_digest, r.text)
                for r in self.records.values() if (kind is None or kind == r.kind)
                and query.casefold() in r.text.casefold()][:limit]


class Retrieval(ContextCase):
    def test_text_sources_attributes_and_type_aware_filters(self):
        event = self.source(content=b"Zebras feature in this document.")
        self.node(event, "bool1", summary="Jesse", attrs={"basic_entity_type": ["person"], "value": [True]})
        self.node(event, "num01", summary="Numeric example", attrs={"basic_entity_type": ["company"], "value": [1]})
        self.node(event, "text1", summary="Text example", attrs={"basic_entity_type": ["person"], "value": ["1"]})
        commit = self.context.publish(event.event_id)
        self.assertEqual([h.id for h in self.context.search("Jess", kind="node")], ["bool1"])
        self.assertEqual([h.id for h in self.context.search("Zebras", kind="source")], [event.source_id])
        for value, expected in ((True, "bool1"), (1, "num01"), ("1", "text1")):
            hits = self.context.search(kind="node", attributes={"value": [value]})
            self.assertEqual([h.id for h in hits], [expected])
            self.assertEqual(hits[0].revision, commit)
        self.assertEqual(self.context.search("Jesse OR *"), [])
        self.assertEqual(self.context.search(kind="node", attributes={"value": [1], "basic_entity_type": ["person"]}), [])

    def test_historical_queries_rebuild_to_the_exact_selected_revision(self):
        one = self.source()
        self.node(one, summary="Old description")
        a = self.context.publish(one.event_id)
        two = self.source("new.md")
        self.node(two, summary="New description", support=[a, "self"])
        b = self.context.publish(two.event_id)
        self.assertEqual([h.revision for h in self.context.search("Old", kind="node", revision=a)], [a])
        self.assertEqual(self.context.index.revision(), a)
        self.assertEqual(self.context.search("Old", kind="node"), [])
        self.assertEqual(self.context.index.revision(), b)
        self.assertEqual([h.revision for h in self.context.search("New", kind="node")], [b])

    def test_rebuild_preserves_pending_jobs_and_corrupt_index_is_disposable(self):
        one = self.source()
        self.node(one)
        self.context.publish(one.event_id)
        pending = self.source("pending.md")
        self.context.index.path.write_bytes(b"Not a SQLite database")
        self.assertEqual([h.id for h in self.context.search("Jesse", kind="node")], ["jesse"])
        self.assertEqual(self.context.pending()["id"], pending.event_id)
        self.assertTrue((self.context.runtime / "events" / pending.event_id / "input.bin").exists())

    def test_failed_rebuild_does_not_answer_from_a_stale_index(self):
        first = self.source()
        self.node(first)
        a = self.context.publish(first.event_id)
        later = self.source("later.md")
        self.context.publish(later.event_id)
        with patch.object(self.context.index, "rebuild", side_effect=RuntimeError("rebuild failed")):
            with self.assertRaisesRegex(RuntimeError, "rebuild failed"):
                self.context.search("Jesse", revision=a)

    def test_alternate_backend_receives_updates_deletions_and_stable_references(self):
        alternate = Context(self.context.root, index_factory=MemoryIndex)
        alternate.search()
        self.context = alternate
        first = self.source()
        self.node(first)
        a = alternate.publish(first.event_id)
        hit = alternate.search("Jesse", kind="node")[0]
        self.assertEqual((hit.id, hit.revision), ("jesse", a))
        self.assertEqual(len(hit.content_digest), 64)
        deletion = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        self.context.stage_handoff_note(deletion.event_id, "")
        alternate.remove_node(deletion.event_id, "jesse", explanation="Source gone.", citations=["self"])
        b = alternate.publish(deletion.event_id)
        self.assertEqual(alternate.search(), [])
        delta = alternate.index.applied[-1]
        self.assertEqual((delta.base_revision, delta.revision), (a, b))
        self.assertEqual(set(delta.removed), {"node:jesse", f"source:{first.source_id}"})
        self.assertEqual(set(alternate.snapshot(a).nodes), {"jesse"})
