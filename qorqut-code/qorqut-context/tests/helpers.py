from pathlib import Path
import tempfile
import unittest

from qorqut_context import Context


class ContextCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.context = Context.initialize(self.directory / "business",
                                          basic_entity_types=["person"])

    def source(self, name="one.md", content=b"Jesse is a person.", **kwargs):
        event = self.context.begin("add", "foundation/" + name, content=content,
                                  metadata={"kind": "note", "title": name, "origin": "test",
                                            "created_at": "2026"}, submitted_by="test-owner", **kwargs)
        if not event.published_commit:
            self.context.stage_handoff_note(event.event_id, "")
        return event

    def node(self, event, object_id="jesse", *, support=None, summary="Jesse", attrs=None, developments=None):
        return self.context.put_node(event.event_id,
                                     {"id": object_id, "summary": summary,
                                      "attributes": attrs or {"basic_entity_type": ["person"], "name": [summary]}},
                                     explanation="The source supplies this context.", citations=["self"],
                                     support_after=support if support is not None else ["self"],
                                     developments=developments)

    def edge(self, event, edge_id, left, right, *, support=None):
        return self.context.put_edge(event.event_id,
                                     {"id": edge_id, "from_id": left, "to_id": right,
                                      "summary": "A recorded relationship", "attributes": {"relation": ["family", "manager"]}},
                                     explanation="The source describes their relationship.", citations=["self"],
                                     support_after=support if support is not None else ["self"])

    def note(self, event, text="A reported happening.", objects=(), *, note_id=None, support=None,
             explanation="The source reports this."):
        """Stage a note; ``objects`` are ``(kind, id)`` pairs. A missing ``note_id`` is allocated by Context."""
        record = {"text": text, "objects": [{"kind": kind, "id": target} for kind, target in objects]}
        if note_id is not None:
            record["id"] = note_id
        return self.context.put_note(event.event_id, record, explanation=explanation, citations=["self"],
                                     support_after=support if support is not None else ["self"])
