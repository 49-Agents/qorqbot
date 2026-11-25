"""Ordubashy's copies of the shared role procedure and of his workspace file carry nothing twice and nothing that is
addressed to the other agents."""
import re
import unittest

from qorqut.agents import role_workflow
from qorqut_agentic.behavior import workspace


class Instructions:
    def render(self, name, **values):
        return f"<{name}>\n"


def paragraphs(text):
    return re.split(r"\n{2,}", text)


class RoleTextForOrdubashy(unittest.TestCase):
    def sources(self):
        return {"role-workflow.md": role_workflow.INSTRUCTIONS,
                "SKILL.md": role_workflow.skill_files()["SKILL.md"].decode("utf-8")}

    def test_paragraphs_for_the_other_agents_are_left_out_and_nothing_else_changes(self):
        for name, text in self.sources().items():
            with self.subTest(name):
                his = role_workflow.for_ordubashy(text)
                dropped = [block for block in paragraphs(text) if block not in paragraphs(his)]
                self.assertEqual(len(dropped), 3)
                self.assertTrue(all(role_workflow.NOT_FOR_ORDUBASHY.match(block) for block in dropped))
                self.assertNotIn("other than Ordubashy", his)
                self.assertEqual([block for block in paragraphs(text) if block not in dropped], paragraphs(his))

    def test_every_sentence_for_the_other_agents_opens_its_own_paragraph(self):
        # A sentence addressed to the other agents inside a shared paragraph would stay in his copy.
        for name, text in self.sources().items():
            with self.subTest(name):
                self.assertEqual(len(re.findall(r"other than Ordubashy", text)),
                                 sum(bool(role_workflow.NOT_FOR_ORDUBASHY.match(block)) for block in paragraphs(text)))

    def test_text_without_such_paragraphs_is_returned_as_it_is(self):
        text = "One.\n\nTwo mentions agents other than Ordubashy inside.\n\n\nThree.\n"
        self.assertEqual(role_workflow.for_ordubashy(text), text)

    def test_closing_a_task_names_result_everywhere(self):
        for name, text in self.sources().items():
            with self.subTest(name):
                self.assertNotIn("tasks_complete(note=", text)
                self.assertIn("tasks_complete(result=", text)


class WorkspaceFiles(unittest.TestCase):
    def documents(self, ordubashy):
        return workspace.documents(Instructions(), display_name="A", agent_id="a", software_boundary="", extension_orientations=(),
                                   ordubashy=ordubashy, code_roots=(), delegate_operations=False, inbox_available=False)

    def test_claude_file_leaves_the_core_rules_to_the_imported_instructions(self):
        files = self.documents(True)
        self.assertNotIn(b"<ordubashy-core>", files["CLAUDE.md"])
        self.assertIn(b"<workspace-claude>", files["CLAUDE.md"])
        self.assertTrue(files["AGENTS.md"].startswith(b"<ordubashy-core>"))

    def test_a_worker_never_had_them(self):
        files = self.documents(False)
        self.assertNotIn(b"<ordubashy-core>", files["CLAUDE.md"] + files["AGENTS.md"])


if __name__ == "__main__":
    unittest.main()
