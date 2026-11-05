"""Ordubashy's Chat references: the one tool grant, its own skill and brief pointer, and nothing shared moves."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def manifest(name):
    return yaml.safe_load((ROOT / f"roles/{name}/role.yaml").read_text())


def flat(path):
    return " ".join((ROOT / path).read_text().split())


class ChatReferencesTest(unittest.TestCase):
    def test_only_the_two_coordinators_hold_the_presentation_tool_and_ordubashy_selects_the_skill(self):
        role = manifest("ordubashy")
        self.assertEqual(role["version"], "1.72.0")
        self.assertEqual(role["default_tool_policy"]["request_user_input"], "allow")
        self.assertEqual({skill["id"]: skill["path"] for skill in role["skills"]}["chat-references"],
                         "skills/chat-references/SKILL.md")
        self.assertEqual(role["default_tool_policy"]["browser_turn"], "allow")
        for other in sorted((ROOT / "roles").iterdir()):
            if other.name == "ordubashy" or not (other / "role.yaml").is_file():
                continue
            with self.subTest(role=other.name):
                other_role = yaml.safe_load((other / "role.yaml").read_text())
                if other.name == "learning-applier":
                    self.assertEqual(other_role["default_tool_policy"]["request_user_input"], "allow")
                    continue
                self.assertNotIn("request_user_input", other_role.get("default_tool_policy", {}))
                self.assertNotIn("chat-references", [skill["id"] for skill in other_role.get("skills", [])])

    def test_skill_documents_the_tool_references_and_exact_browser_selection(self):
        text = (ROOT / "skills/chat-references/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: bazarlyk-role-chat-references\ndescription: "))
        guidance = flat("skills/chat-references/SKILL.md")
        for phrase in ("request_user_input", "returns at once", "creates no request", "grants no approval",
                       "at most 4000 characters", "at most 8", "{type, id, business_id}", "no URL, label or other field",
                       "`action=list`", '{"browsers": [{"id", "label", "business_id"}]}',
                       'browser_id="<id from action=list>"', "only on `acquire` with `protocol=\"handles\"`",
                       "bound to that exact browser", "cannot move to another browser", "no fallback to another browser",
                       "takes no browser turn", "signoffs_input_create",
                       '{"type": "agent", "id": "<agent uuid>"}', "the registered agent's UUID", "no `business_id`, name or icon",
                       # Projects wording: the identifier stays `business_id`, the owner is a project.
                       "is the owning project, omitted (or null) for a Global record", "always required for a project browser",
                       "each reachable project's browser", "selects that project's shared browser"):
            self.assertIn(phrase, guidance)
        self.assertNotRegex(guidance, r"business's|business browser|owning business|<business uuid>")
        for forbidden in ("cdp", "CDP", "viewer_url", "http://", "https://", "token"):
            self.assertNotIn(forbidden, guidance)

    def test_brief_points_to_the_skill_and_stays_inside_its_limit(self):
        brief = (ROOT / "roles/ordubashy/BRIEF.md").read_text()
        self.assertLessEqual(len(brief.strip()), 10000)
        self.assertIn("request_user_input (chat-references)", " ".join(brief.split()))

    def test_shared_browser_skills_do_not_mention_the_new_selection(self):
        for shared in ("skills/managed-browser-control/SKILL.md", "skills/ordubashy-browser-control/SKILL.md"):
            text = (ROOT / shared).read_text()
            self.assertNotIn("request_user_input", text)
            self.assertNotIn("browser_id", text)


if __name__ == "__main__":
    unittest.main()
