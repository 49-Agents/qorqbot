from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from qorqut.bootstrap import qorqbot_guide
from qorqut_agentic.behavior.instructions import ALWAYS_SKILLS
from qorqut.bootstrap.preparation import OWNER_START, managed_skill_file


class QorqbotGuideTest(unittest.TestCase):
    def test_written_for_both_clients_under_its_own_name(self):
        files = qorqbot_guide.skill_files()
        self.assertEqual(set(files), {".claude/skills/qorqut-qorqbot-guide/SKILL.md", ".agents/skills/qorqut-qorqbot-guide/SKILL.md"})
        front = yaml.safe_load(files[".claude/skills/qorqut-qorqbot-guide/SKILL.md"].decode().split("---", 2)[1])
        self.assertEqual(front["name"], qorqbot_guide.SKILL_NAME)
        self.assertTrue(front["description"])
        self.assertTrue(all(managed_skill_file(name) for name in files))

    def test_ordubashy_loads_it_and_agents_are_pointed_to_it(self):
        self.assertIn(qorqbot_guide.SKILL_NAME, ALWAYS_SKILLS)
        self.assertIn(qorqbot_guide.SKILL_NAME, OWNER_START)

    def test_it_never_tells_the_owner_to_use_role_commands(self):
        text = qorqbot_guide.SKILL
        self.assertIn("Never tell the owner to open a terminal window on their computer, `cd` into an agent folder, or type `/role`", text)
        self.assertIn("With managed sessions the owner types no command at all", text)
        self.assertIn("with Terminals the only command is `claude`, typed in\nthe agent's own Terminal in the dashboard", text)
        self.assertNotIn("qorqut-role-qorqbot-guide", text)

    def test_it_describes_both_ways_agents_run_and_no_terminal_start_by_the_owner(self):
        text = qorqbot_guide.SKILL
        for phrase in ("Qorqbot downloads Claude Code and Codex itself", "**Settings > Agents**", "closes after 15 idle minutes",
                       "**End session**", "stays off until the owner", "**How agents run**", "**Managed sessions (recommended)**",
                       "**Terminals (legacy)**", "the default", "Only the owner can switch", "tmux", "Claude Code agents only",
                       "conversations carry over", "`managed: true`", "nothing starts it by itself",
                       "until the owner opens that Terminal and runs `claude` there"):
            self.assertIn(phrase, text)
        for gone in (">_", "Trusted agent folders", "trust this folder", "Chat has no Start button.\n- To start Ordubashy",
                     "Qorqbot starts every agent itself", "starts and runs itself"):
            self.assertNotIn(gone, text)


class GuideProjectionTest(unittest.TestCase):
    """The guide sits beside managed skills without being one, and must not read as drift or a manual addition."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.workspace)
        from qorqut.bootstrap.preparation import content_hash
        self.files = {}
        for name, body in qorqbot_guide.skill_files().items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            self.files[name] = content_hash(body)

    def drift(self):
        from qorqut.bootstrap.preparation import inspect_projection
        projection = {"files": self.files, "selected_skills": {}}
        return inspect_projection(self.workspace, projection, runtime="claude-code") + \
            inspect_projection(self.workspace, projection, runtime="codex")

    def test_the_generated_guide_is_not_drift(self):
        self.assertEqual(self.drift(), [])

    def test_a_changed_or_extra_file_in_it_still_is(self):
        (self.workspace / ".claude/skills/qorqut-qorqbot-guide/notes.md").write_text("added by hand")
        (self.workspace / ".agents/skills/qorqut-qorqbot-guide/SKILL.md").write_text("edited")
        self.assertEqual(sorted(self.drift()), [".agents/skills/qorqut-qorqbot-guide/SKILL.md",
                                                ".claude/skills/qorqut-qorqbot-guide/notes.md"])

    def test_other_unselected_managed_names_are_still_drift(self):
        stray = self.workspace / ".claude/skills/qorqut-something-else"
        stray.mkdir(parents=True)
        self.assertIn(".claude/skills/qorqut-something-else", self.drift())


if __name__ == "__main__":
    unittest.main()
