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


if __name__ == "__main__":
    unittest.main()
