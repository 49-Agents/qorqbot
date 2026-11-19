"""The startup text says what the published graph holds and how to read it, and lists none of its content."""

import unittest

from qorqut_context.integration.reading.text import MAX_CHARACTERS, render_home


def ready(**overrides):
    dto = {"status": "ready", "revision": "abc123", "empty": False,
           "personal_context": {"ref": "node:owner"},
           "totals": {"nodes": 412, "edges": 377, "sources": 505, "events": 520},
           "nodes": [{"ref": "node:hub01", "summary": "A hub that must not be listed", "attributes": {},
                      "relationship_count": 40, "relationships": []}]}
    return {**dto, **overrides}


TYPES = [{"type": name, "count": count} for name, count in
         (("person", 120), ("company", 64), ("entity", 51), ("topic", 40), ("organization", 33), ("event", 29),
          ("document", 21), ("service", 15), ("project", 9))]


class HomeTextTests(unittest.TestCase):
    def test_it_states_totals_types_and_the_reads_without_listing_nodes(self):
        text = render_home(ready(), types=TYPES)
        self.assertIn("Revision abc123: 412 nodes, 377 relationships, 505 sources, 520 events.", text)
        self.assertIn("Nodes by type: person 120, company 64, entity 51, topic 40, organization 33, event 29, "
                      "document 21, service 15; other types fewer.", text)
        for read in ("context_find(query)", "context_object(ref)", "context_expand(ref)", "context_home"):
            self.assertIn(read, text)
        self.assertNotIn("node:hub01", text)
        self.assertNotIn("A hub that must not be listed", text)
        self.assertLess(len(text.split("Revision ", 1)[1]), 1800)

    def test_the_owner_record_comes_first_and_the_pointer_follows(self):
        owner = {"ref": "node:owner", "summary": "The installation owner.", "attributes": {"phone": ["+1 555 0100"]}}
        text = render_home(ready(personal_context=owner), types=TYPES)
        self.assertTrue(text.startswith("Shared Context (published graph)\nAbout the owner (personal record node:owner)"))
        self.assertIn("- phone: +1 555 0100", text)
        self.assertLess(text.index("About the owner"), text.index("Revision abc123"))
        self.assertNotIn("About the owner", render_home(ready(personal_context=None), types=TYPES))

    def test_missing_type_counts_leave_the_line_out(self):
        text = render_home(ready())
        self.assertNotIn("Nodes by type", text)
        self.assertIn("context_find(query)", text)

    def test_a_long_type_name_is_clipped(self):
        text = render_home(ready(), types=[{"type": "x" * 200, "count": 3}])
        self.assertIn("x" * 29 + "… 3.", text)

    def test_states_that_are_not_a_graph_are_said_plainly(self):
        self.assertIn("Not initialized", render_home({"status": "not_initialized"}))
        self.assertIn("Unavailable (read_failed)", render_home({"status": "unavailable", "reason": {"code": "read_failed"}}))
        self.assertIn("Initialized and empty", render_home(ready(empty=True), types=TYPES))

    def test_the_text_stays_within_its_budget(self):
        # The owner's record has its own bound on top of this one (see test_context_personal_facts).
        self.assertLessEqual(len(render_home(ready(personal_context=None), characters=300, types=TYPES)), 300)
        self.assertLessEqual(len(render_home(ready(personal_context=None), types=TYPES)), MAX_CHARACTERS)


if __name__ == "__main__":
    unittest.main()
