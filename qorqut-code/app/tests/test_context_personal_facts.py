"""Every agent starts with what the owner already said about themselves, so nobody asks for it twice."""
from pathlib import Path
import tempfile
import unittest

from qorqut_context.engine import Context
from qorqut_context.integration.reading import PublishedContextText, SharedContextReader, render_home
from qorqut_context.integration.reading import text as home_text

REF = "node:own3r"
REVISION = "a" * 40


def home(attributes, summary="Sam Example is the installation owner.", nodes=()):
    return {"status": "ready", "revision": REVISION, "empty": False,
            "personal_context": {"ref": REF, "summary": summary, "attributes": attributes},
            "totals": {"nodes": 1 + len(nodes), "edges": 0, "sources": 1, "events": 1}, "nodes": list(nodes),
            "page": {"limit": 12, "returned": len(nodes), "has_more": False, "next_cursor": None}}


class OwnerRecordInStartupTextTests(unittest.TestCase):
    def test_the_recorded_facts_are_in_the_text_with_the_rule_not_to_ask_again(self):
        text = render_home(home({"basic_entity_type": ["person"], "date_of_birth": ["1990-01-02"],
                                 "phone": ["+1 555 0100"], "email": ["sam@example.com", "s@example.org"]}))
        self.assertIn(f"About the owner (personal record {REF})", text)
        self.assertIn("Never ask the owner for a fact this record holds", text)
        self.assertIn(f"read context_object(ref='{REF}') fresh", text)
        self.assertIn("- date_of_birth: 1990-01-02", text)
        self.assertIn("- phone: +1 555 0100", text)
        self.assertIn("- email: sam@example.com; s@example.org", text)
        self.assertIn("Sam Example is the installation owner.", text)
        self.assertNotIn("basic_entity_type", text)               # the engine's own type marker is not a fact to use
        self.assertTrue(text.startswith("Shared Context (published graph)\nAbout the owner"))

    def test_short_facts_survive_a_record_too_long_to_show_whole(self):
        history = [f"Employer {index}: " + "a long description of the role. " * 12 for index in range(12)]
        text = render_home(home({"work_history": history, "notes": ["x" * 900] * 6,
                                 "date_of_birth": ["1990-01-02"], "phone": ["+1 555 0100"]}))
        owner = text.split("Revision ")[0]
        self.assertIn("- date_of_birth: 1990-01-02", owner)
        self.assertIn("- phone: +1 555 0100", owner)
        self.assertLessEqual(len(owner), home_text.PERSONAL_CHARACTERS + 700)   # the rule line and the closing line
        # Whatever was cut or left out is named, so a reader knows the record holds more and where.
        self.assertIn(f"The record holds more under: notes, work_history. Open {REF} for all of it.", owner)

    def test_the_owner_record_does_not_shrink_the_graph_index(self):
        node = {"kind": "node", "id": "n0001", "ref": "node:n0001", "summary": "A company.", "attributes": {},
                "truncated": False, "relationship_count": 0, "relationships": [], "relationships_truncated": False}
        without = render_home({**home({}, nodes=[node]), "personal_context": None})
        with_owner = render_home(home({"phone": ["+1 555 0100"]}, nodes=[node]))
        self.assertIn("- node:n0001 — A company.", with_owner)
        self.assertEqual(without.split("\n", 1)[1], "Revision " + with_owner.split("Revision ", 1)[1])

    def test_a_graph_without_an_owner_record_says_nothing_about_one(self):
        text = render_home({**home({}), "personal_context": None})
        self.assertNotIn("About the owner", text)


class OwnerRecordInContextHomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        Context.initialize(root / "context", root / "runtime")
        self.reader = SharedContextReader(root / "context", root / "runtime")
        self.addCleanup(self.reader.close)

    def test_context_home_returns_the_whole_record_on_one_read(self):
        page = self.reader.home()
        personal = page["personal_context"]
        self.assertEqual(set(personal), {"ref", "summary", "attributes"})
        self.assertEqual(personal["summary"], "Installation owner")
        self.assertEqual(personal["attributes"], {"basic_entity_type": ["person"]})
        self.assertEqual(self.reader.object(personal["ref"])["object"]["attributes"], personal["attributes"])

    def test_the_startup_text_is_rendered_from_that_same_page(self):
        text = PublishedContextText(self.reader).home_text()
        ref = self.reader.home()["personal_context"]["ref"]
        self.assertIn(f"About the owner (personal record {ref})", text)
        self.assertIn("Installation owner", text)


if __name__ == "__main__":
    unittest.main()
