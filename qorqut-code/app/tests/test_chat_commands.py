from pathlib import Path
import shutil
import tempfile
import unittest

from bazarlyk.native_chat import commands


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.home, self.agent = self.root / "home", self.root / "agent"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_built_ins_per_client_say_where_their_result_appears(self):
        claude = {item["name"]: item["where"] for item in commands.catalog("claude")["commands"]}
        codex = {item["name"]: item["where"] for item in commands.catalog("codex")["commands"]}
        self.assertEqual((claude["/compact"], claude["/model"], claude["/resume"]), ("chat", "terminal", "terminal"))
        self.assertEqual((codex["/new"], codex["/model"]), ("chat", "terminal"))
        self.assertNotIn("/clear", codex)
        self.assertEqual(commands.catalog("bash"), {"client": None, "commands": []})

    def test_the_owners_commands_and_skills_are_added_for_claude(self):
        self.write(self.home / ".claude/commands/standup.md", "---\ndescription: Write my standup\n---\nDo it")
        self.write(self.home / ".claude/commands/git/sync.md", "# Sync the branch\nsteps")
        self.write(self.home / ".claude/skills/pdf/SKILL.md", "---\nname: pdf\ndescription: Read PDFs\n---\n")
        self.write(self.agent / ".claude/skills/bazarlyk-role-owner-focus/SKILL.md", "---\nname: bazarlyk-role-owner-focus\ndescription: x\n---\n")
        self.write(self.agent / ".claude/skills/role/SKILL.md", "---\nname: role\ndescription: x\n---\n")
        self.write(self.agent / ".claude/commands/compact.md", "Shadow attempt")
        found = {item["name"]: item for item in commands.catalog("claude", home=self.home, workspace=self.agent)["commands"]}
        self.assertEqual(found["/standup"]["description"], "Write my standup")
        self.assertEqual(found["/git:sync"]["description"], "Sync the branch")
        self.assertEqual((found["/pdf"]["source"], found["/pdf"]["where"]), ("skill", "chat"))
        self.assertNotIn("/bazarlyk-role-owner-focus", found)
        self.assertNotIn("/role", found)
        self.assertEqual(found["/compact"]["source"], "built-in")
        self.assertNotIn("/standup", {item["name"] for item in commands.catalog("codex", home=self.home)["commands"]})


class DeliveryTextTest(unittest.TestCase):
    def test_only_a_bare_command_gets_a_trailing_space(self):
        self.assertEqual(commands.delivery_text("/compact"), "/compact ")
        self.assertEqual(commands.delivery_text("/git:sync"), "/git:sync ")
        self.assertEqual(commands.delivery_text("/compact keep decisions"), "/compact keep decisions")
        self.assertEqual(commands.delivery_text("hello /compact"), "hello /compact")
        self.assertEqual(commands.delivery_text("/usr/bin is a path"), "/usr/bin is a path")
        self.assertEqual(commands.delivery_text("/compact "), "/compact ")


if __name__ == "__main__":
    unittest.main()
