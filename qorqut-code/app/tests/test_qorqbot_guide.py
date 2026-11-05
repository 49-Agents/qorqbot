from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from bazarlyk.bootstrap import always_skills, qorqbot_guide
from bazarlyk.bootstrap.preparation import OWNER_START, managed_skill_file


class QorqbotGuideTest(unittest.TestCase):
    def test_written_for_both_clients_under_its_own_name(self):
        files = qorqbot_guide.skill_files()
        self.assertEqual(set(files), {".claude/skills/bazarlyk-qorqbot-guide/SKILL.md", ".agents/skills/bazarlyk-qorqbot-guide/SKILL.md"})
        front = yaml.safe_load(files[".claude/skills/bazarlyk-qorqbot-guide/SKILL.md"].decode().split("---", 2)[1])
        self.assertEqual(front["name"], qorqbot_guide.SKILL_NAME)
        self.assertTrue(front["description"])
        self.assertTrue(all(managed_skill_file(name) for name in files))

    def test_ordubashy_loads_it_and_agents_are_pointed_to_it(self):
        self.assertIn(qorqbot_guide.SKILL_NAME, always_skills.SKILLS)
        self.assertIn(qorqbot_guide.SKILL_NAME, OWNER_START)

    def test_it_never_tells_the_owner_to_use_role_commands(self):
        text = qorqbot_guide.SKILL
        self.assertIn("Never tell the owner to start an agent from Terminal.app", text)
        self.assertNotIn("bazarlyk-role-qorqbot-guide", text)


class GuideProjectionTest(unittest.TestCase):
    """The guide sits beside managed skills without being one, and must not read as drift or a manual addition."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.workspace)
        from bazarlyk.bootstrap.preparation import content_hash
        self.files = {}
        for name, body in qorqbot_guide.skill_files().items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            self.files[name] = content_hash(body)

    def drift(self):
        from bazarlyk.bootstrap.preparation import inspect_projection
        projection = {"files": self.files, "selected_skills": {}}
        return inspect_projection(self.workspace, projection, runtime="claude-code") + \
            inspect_projection(self.workspace, projection, runtime="codex")

    def test_the_generated_guide_is_not_drift(self):
        self.assertEqual(self.drift(), [])

    def test_a_changed_or_extra_file_in_it_still_is(self):
        (self.workspace / ".claude/skills/bazarlyk-qorqbot-guide/notes.md").write_text("added by hand")
        (self.workspace / ".agents/skills/bazarlyk-qorqbot-guide/SKILL.md").write_text("edited")
        self.assertEqual(sorted(self.drift()), [".agents/skills/bazarlyk-qorqbot-guide/SKILL.md",
                                                ".claude/skills/bazarlyk-qorqbot-guide/notes.md"])

    def test_other_unselected_managed_names_are_still_drift(self):
        stray = self.workspace / ".claude/skills/bazarlyk-something-else"
        stray.mkdir(parents=True)
        self.assertIn(".claude/skills/bazarlyk-something-else", self.drift())


if __name__ == "__main__":
    unittest.main()
