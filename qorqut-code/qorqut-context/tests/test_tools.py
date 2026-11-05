from qorqut_context import Conflict
from qorqut_context.tools import EventTools
from .helpers import ContextCase


class BoundTools(ContextCase):
    def call(self, tools, name, **arguments):
        response = tools.dispatch(name, arguments)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def test_overlay_scope_reassessment_and_digest_publication(self):
        first = self.source()
        self.node(first)
        self.edge(first, "edge1", "jesse", "jesse")
        commit = self.context.publish(first.event_id)
        second = self.source("two.md", b"Jesse also appears here.")
        tools = EventTools(self.context, second.event_id)
        record = self.call(tools, "read_object", kind="node", id="jesse")["record"]
        self.call(tools, "put_node", record=record, explanation="Additional support.",
                  citations=["self"], support_after=[commit, "self"])
        created = self.call(tools, "put_node", record={"summary": "A project", "attributes": {
            "basic_entity_type": ["project"], "count": [17], "active": [True]}},
            explanation="This source records the project.", citations=["self"], support_after=["self"])["id"]
        self.assertEqual(self.call(tools, "read_object", kind="node", id=created)["support"], ["self"])
        self.assertIn("project", self.call(tools, "read_catalog", kind="node", attribute="basic_entity_type")["items"])
        self.assertEqual(self.call(tools, "read_catalog", kind="node", attribute="count")["items"], [])
        self.assertEqual(self.call(tools, "search_context", query="project")["items"], [])
        listed = self.call(tools, "list_objects", kind="node", limit=1)
        self.assertEqual(listed["total"], 2)
        self.assertTrue(listed["has_more"])
        self.assertEqual(self.call(tools, "read_evidence", commit=commit, version="after", limit=5)["text"], "Jesse")
        for name, arguments in [
            ("read_source", {"id": first.source_id, "path": "/etc/passwd"}),
            ("read_evidence", {"commit": "f" * 40, "version": "after"}),
            ("publish", {}),
            ("put_node", {"record": record, "explanation": "Foreign evidence", "citations": ["f" * 40], "support_after": [commit]}),
        ]:
            self.assertFalse(tools.dispatch(name, arguments)["ok"])
        done = self.call(tools, "finish_propagation", status="complete", summary="Two supported entities.", reassessed=[])
        self.assertFalse(tools.dispatch("remove_node", {"id": created, "explanation": "Too late", "citations": ["self"]})["ok"])
        self.assertEqual(self.call(tools, "finish_propagation", status="complete", summary="Two supported entities.", reassessed=[]), done)
        before = self.context.git.head()
        self.assertEqual(before, commit)
        # An independent low-level writer cannot publish through a stale seal.
        self.node(second, object_id="extra")
        with self.assertRaisesRegex(Conflict, "draft changed"):
            self.context.publish(second.event_id, expected_draft_digest=done["draft_digest"])
        self.context.remove_node(second.event_id, "extra", explanation="Undo test interference.", citations=["self"])
        second_commit = self.context.publish(second.event_id, expected_draft_digest=done["draft_digest"])
        history = self.context.snapshot().history("jesse")
        self.assertEqual(history[-1]["support_after"], [commit, second_commit])
        self.assertNotIn(b"graph/nodes/jesse.json", self.context.git.run("diff-tree", "--no-commit-id", "--name-only", "-r", second_commit))

        removal = self.context.begin("remove", "foundation/one.md", submitted_by="owner")
        tools = EventTools(self.context, removal.event_id)
        self.call(tools, "stage_handoff_note", text="")
        self.assertEqual({(r["kind"], r["id"]) for r in tools.required_reassessment},
                         {("node", "jesse"), ("edge", "edge1")})
        self.call(tools, "remove_node", id="jesse", explanation="Staged deletion.", citations=["self"])
        self.assertEqual(self.call(tools, "neighbors", id="jesse")["total"], 1)
        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Missing edges", "reassessed": tools.required_reassessment})["ok"])
        self.call(tools, "put_node", record=record, explanation="Other source still supports Jesse.",
                  citations=[second_commit], support_after=[second_commit])
        self.call(tools, "remove_edge", id="edge1", explanation="Last relationship support withdrawn.", citations=["self"])
        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Missing coverage", "reassessed": []})["ok"])
        done = self.call(tools, "finish_propagation", status="complete", summary="Reassessed all affected objects.", reassessed=tools.required_reassessment)
        self.context.publish(removal.event_id, expected_draft_digest=done["draft_digest"])
        self.assertEqual(self.context.snapshot().read("node", "jesse")["support"], [second_commit])
        self.assertEqual(self.context.snapshot(commit).read("node", "jesse")["support"], [commit])

    def test_response_limit_is_explicit(self):
        event = self.source()
        self.node(event, summary="x" * 1000)
        tools = EventTools(self.context, event.event_id, max_response_bytes=500)
        result = tools.dispatch("read_object", {"kind": "node", "id": "jesse"})
        self.assertFalse(result["ok"])
        self.assertIn("byte limit", result["error"]["message"])
