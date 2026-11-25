"""Every agent's start says, for each installed skill it does not load, which work to read that skill before."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from qorqut_agentic.behavior import instructions as policy
from qorqut_agentic.behavior.instructions import from_catalog, skill_front_matter
from qorqut.bootstrap import qorqbot_guide
from qorqut.bootstrap import claude_session
from qorqut.bootstrap.claude_session import CLAUDE_HOOK_LIMIT, STARTUP_CONTEXT, handle_hook

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = ROOT / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
INSTRUCTIONS = DEFAULTS / "operations/runtime-instructions"
SKILLS = DEFAULTS / "operations/imported-role-skills/native/skills"
HOST = DEFAULTS / "operations/source-instructions/host/app"
HEADER = "# Skills to read first"
RULES = "## Core Working Rules\nManaged instruction text."
FACTS = "## Identity And Assignment\nYou are the test agent."
SESSION = "00000000-0000-4000-8000-000000000001"


def catalog():
    return from_catalog(lambda path: (INSTRUCTIONS / path).read_text())


def install(workspace, *sources, runtime="claude-code"):
    """Copy skill files into the agent's skill folder, named as the skill names itself."""
    for source in sources:
        text = source.read_text() if isinstance(source, Path) else source
        folder = Path(workspace) / policy.SKILL_ROOTS[runtime] / skill_front_matter(text)["name"]
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(text)


def lines(section):
    return [line for line in section.split("\n") if line.startswith("- ")]


BROWSER = SKILLS / "managed-browser-control/SKILL.md"
REMINDERS = SKILLS / "reminders/SKILL.md"
ENVIRONMENT = SKILLS / "environment/SKILL.md"
CALENDAR = SKILLS / "calendar/SKILL.md"
ROLE = HOST / "qorqut/agents/skills/role/SKILL.md"
CLEANUP = HOST / "qorqut/agents/skills/cleanup/SKILL.md"
EMAIL = DEFAULTS / "operations/source-instructions/email/skills/email-management/SKILL.md"
TERMINALS = HOST / "plugins/agent-terminal-control/skills/agent-terminal-control/SKILL.md"
ORDUBASHY = yaml.safe_load((DEFAULTS / "roles/imported-roles/native/roles/ordubashy/role.yaml").read_text())


def ordubashy_skills():
    return [DEFAULTS / item["path"] for item in ORDUBASHY["skills"]] + [ROLE, CLEANUP, EMAIL, TERMINALS]


class DiscoverySectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def section(self, runtime="claude-code", loaded=()):
        return policy.render_skill_discovery(catalog(), self.root, runtime, loaded=loaded)

    def test_a_worker_gets_exactly_a_line_for_each_installed_skill(self):
        install(self.root, BROWSER, REMINDERS, ENVIRONMENT)
        section = self.section()
        self.assertTrue(section.startswith(HEADER))
        found = lines(section)
        self.assertEqual(len(found), 3)
        self.assertIn("- Browser work, such as opening a site, reading a page or acting on one (`browser_turn`): read qorqut-role-browser-control.", found)
        self.assertIn("- Reminders, whether creating, changing, snoozing or answering one (`reminders_*`): read qorqut-role-reminders.", found)
        self.assertTrue(any("`environment_*`" in line and line.endswith("read qorqut-role-environment.") for line in found))
        self.assertIn("When you are given a task of a kind listed here, or you want to work with something listed here", section)
        self.assertIn("with the Skill tool, by the name given", section)
        self.assertNotIn("SKILL.md", section)
        self.assertNotIn("open the file", section)
        self.assertEqual(found, sorted(found, key=lambda line: re.search(r": read ([^\s.]+)", line)[1]))

    def test_a_codex_folder_gets_the_same_section(self):
        install(self.root, BROWSER, REMINDERS, runtime="codex")
        section = self.section("codex")
        self.assertEqual(len(lines(section)), 2)
        self.assertIn("- Browser work, such as opening a site, reading a page or acting on one (`browser_turn`): read qorqut-role-browser-control "
                      "(.agents/skills/qorqut-role-browser-control/SKILL.md).", section)
        self.assertIn("(.agents/skills/qorqut-role-reminders/SKILL.md)", section)
        self.assertNotIn(".claude/skills", section)
        self.assertIn("by opening the file given", section)
        self.assertNotIn("Skill tool", section)

    def test_a_skill_without_a_phrase_gets_its_description_and_nothing_is_left_out(self):
        plain = ("---\nname: qorqut-plugin-plain\ndescription: >-\n  Do the plain thing. A second sentence that "
                 "must not appear.\n---\nBody")
        quoted = '---\nname: qorqut-plugin-quoted\ndescription: "Quoted one."\nread_for: "Flying a kite (`kite_*`)"\n---\n'
        install(self.root, plain, quoted, REMINDERS)
        broken = self.root / ".claude/skills/qorqut-plugin-broken"
        broken.mkdir()
        (broken / "SKILL.md").write_bytes(b"\xff\xfe not text")
        (self.root / ".claude/skills/not-a-skill").mkdir()
        found = lines(self.section())
        self.assertEqual(len(found), 4)
        self.assertIn("- Do the plain thing: read qorqut-plugin-plain.", found)
        self.assertIn("- Flying a kite (`kite_*`): read qorqut-plugin-quoted.", found)
        self.assertTrue(any("Work this skill covers: read qorqut-plugin-broken" in line for line in found))

    def test_the_role_skill_and_loaded_skills_are_not_listed(self):
        install(self.root, ROLE, REMINDERS, ENVIRONMENT)
        self.assertEqual(len(lines(self.section())), 2)
        section = self.section(loaded=("qorqut-role-environment",))
        self.assertEqual(len(lines(section)), 1)
        self.assertNotIn("environment", section.lower().split("\n", 2)[2])
        self.assertEqual(self.section(loaded=("qorqut-role-reminders", "qorqut-role-environment")), "")

    def test_long_lines_are_cut_to_the_limit_and_a_large_installation_stays_under_its_cap(self):
        install(self.root, "---\nname: qorqut-plugin-long\ndescription: " + "word " * 100 + ".\n---\n",
                "---\nname: qorqut-plugin-longer\ndescription: x\nread_for: " + "Phrase " * 40 + "\n---\n")
        for line in lines(self.section()):
            self.assertLessEqual(len(line), policy.DISCOVERY_LINE_LIMIT)
        for number in range(120):
            install(self.root, f"---\nname: qorqut-plugin-n{number:03}\ndescription: x\nread_for: "
                               f"{'use the thing ' * 8}{number}\n---\n")
        section = self.section()
        self.assertEqual(len(lines(section)), 122)
        # The fixed part of a line (name and path) cannot shrink, so the cap is met by shortening phrases only.
        self.assertTrue(all(len(line) <= policy.DISCOVERY_LINE_LIMIT for line in lines(section)))
        self.assertLess(len(section), 122 * 150)

    def test_the_default_ordubashy_section_is_small(self):
        install(self.root, *ordubashy_skills())
        guide = self.root / ".claude/skills" / qorqbot_guide.SKILL_NAME
        guide.mkdir()
        (guide / "SKILL.md").write_text(qorqbot_guide.SKILL)
        section = self.section(loaded=policy.ALWAYS_SKILLS)
        self.assertLess(len(section), 3500)
        listed = " ".join(re.search(r": read ([^\s.]+)", line)[1] for line in lines(section))
        for name in ("qorqut-role-reminders", "qorqut-role-calendar", "qorqut-role-browser-control",
                     "qorqut-role-get-set-up"):
            self.assertIn(name, listed)
        for name in (*policy.ALWAYS_SKILLS, "role"):
            self.assertNotIn(name + " ", listed + " ")
        # While the owner is new the first-conversation skill is loaded, so it is not discovered.
        self.assertNotIn("qorqut-role-get-set-up", self.section(loaded=(*policy.ALWAYS_SKILLS, "qorqut-role-get-set-up")))


class DefaultPhraseTests(unittest.TestCase):
    def default_skills(self):
        found = {}
        for path in DEFAULTS.rglob("SKILL.md"):
            if "instance-template" in path.parts or path == ROLE:
                continue
            found[path.relative_to(DEFAULTS).as_posix()] = path.read_text()
        found["qorqbot_guide"] = qorqbot_guide.SKILL
        return found

    def test_every_default_skill_names_the_kind_of_work_it_is_for(self):
        skills = self.default_skills()
        self.assertGreaterEqual(len(skills), 28)
        for where, text in skills.items():
            with self.subTest(skill=where):
                phrase = skill_front_matter(text).get("read_for")
                self.assertTrue(phrase, "no read_for phrase")
                self.assertLess(len(phrase), 120)
                self.assertRegex(phrase, r"^[A-Z]")
                self.assertNotIn(": ", phrase)
                self.assertNotRegex(phrase, r"[.:]\s*$")

    def test_front_matter_loads_with_a_real_yaml_parser_and_agrees_with_the_reader(self):
        """Claude Code parses the front matter with YAML: one bad value would hide the whole skill from the agent."""
        everything = {path.relative_to(DEFAULTS).as_posix(): path.read_text()
                      for path in DEFAULTS.rglob("SKILL.md") if "instance-template" not in path.parts}
        everything["qorqbot_guide"] = qorqbot_guide.SKILL
        self.assertGreaterEqual(len(everything), 29)
        for where, text in everything.items():
            with self.subTest(skill=where):
                front = yaml.safe_load(re.match(r"\A---\n(.*?)\n---\n", text, re.S)[1])
                expected = {"name", "description"} | (set() if front["name"] == "role" else {"read_for"})
                self.assertEqual(set(front), expected)
                reader = skill_front_matter(text)
                self.assertEqual(reader.get("read_for"), front.get("read_for"))
                self.assertEqual(reader["name"], front["name"])
                self.assertEqual(reader["description"], " ".join(front["description"].split()))

    def test_tool_names_in_phrases_are_real_operation_names(self):
        quoted = set()
        for folder in ("qorqut-code/app/qorqut", "qorqut-code/qorqut-operations", "qorqut-code/qorqut-context"):
            for path in (ROOT / folder).rglob("*.py"):
                quoted.update(re.findall(r"[\"']([a-z]+(?:_[a-z0-9]+)+)", path.read_text(errors="ignore")))
        checked = 0
        for where, text in self.default_skills().items():
            for name in re.findall(r"`([a-z_]+\*?)`", skill_front_matter(text).get("read_for", "")):
                checked += 1
                with self.subTest(skill=where, tool=name):
                    if name.endswith("*"):
                        self.assertTrue(any(known.startswith(name[:-1]) for known in quoted))
                    else:
                        self.assertIn(name, quoted)
        self.assertGreater(checked, 10)


class StartDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registration = {"registration_id": "synthetic", "workspace": str(self.root)}
        (self.root / "runtime-instructions.json").write_bytes(catalog().bytes())
        self.response = {"ready": True, "effective_config_id": "config-1",
                         "mandatory_context": {"rendered": RULES + "\n\n" + FACTS, "instructions": RULES, "context": FACTS}}
        for target, value in (("qorqut.bootstrap.claude_session.load_registration", self.registration),
                              ("qorqut.bootstrap.native_binding.attest", None),
                              ("qorqut.bootstrap.handoff.request", None),
                              ("qorqut.bootstrap.recovery.read", None)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def hook(self, event="SessionStart", source="startup", runtime="claude-code", session=SESSION):
        client = SimpleNamespace(post=lambda *_args: self.response)
        hook = {"hook_event_name": event, "session_id": session}
        if event == "SessionStart":
            hook["source"] = source
        output, _, _ = handle_hook(hook, self.root / "registration.json", runtime=runtime, client_factory=lambda _: client)
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")

    def test_a_worker_start_carries_the_section_once_and_every_fresh_start_carries_it_again(self):
        install(self.root, BROWSER, REMINDERS, ENVIRONMENT)
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                text = self.hook(source=source)
                self.assertEqual(text.count(HEADER), 1)
                self.assertEqual(len(lines(text)), 3)
                self.assertLess(text.index(HEADER), text.index("## Identity"))

    def test_a_prompt_with_unchanged_context_carries_no_second_copy(self):
        install(self.root, BROWSER)
        receipt = self.root / ".qorqut/delivery" / (hashlib.sha256(SESSION.encode()).hexdigest() + ".json")
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"registration_id": "synthetic", "effective_config_id": "config-1"}))
        self.assertNotIn(HEADER, self.hook(event="UserPromptSubmit"))
        # A changed configuration replaces the whole context, so the section comes with it, once.
        self.response["effective_config_id"] = "config-2"
        self.assertEqual(self.hook(event="UserPromptSubmit").count(HEADER), 1)

    def test_codex_start_carries_its_own_skill_folder(self):
        install(self.root, BROWSER, runtime="codex")
        text = self.hook(runtime="codex")
        self.assertEqual(text.count(HEADER), 1)
        self.assertIn("(.agents/skills/qorqut-role-browser-control/SKILL.md)", text)

    def test_ordubashy_start_leaves_out_what_it_loads_and_the_role_skill(self):
        self.registration["operational_intake_enabled"] = True
        install(self.root, *ordubashy_skills())
        loaded = "# Always-loaded skills (already loaded)\nSkill text."
        with mock.patch("qorqut.bootstrap.always_skills.handle",
                        return_value={"hookSpecificOutput": {"additionalContext": loaded}}):
            text = self.hook()
            self.assertEqual(text.count(HEADER), 1)
            self.assertTrue(any("get-set-up" in line for line in lines(text)))
            for name in ("qorqut-role-calendar", "qorqut-role-reminders", "qorqut-role-browser-control"):
                self.assertTrue(any(f": read {name}." in line for line in lines(text)), name)
            for name in (*policy.ALWAYS_SKILLS, "role"):
                self.assertFalse(any(f": read {name}." in line for line in lines(text)), name)
            # The first-conversation skill is loaded while the owner is new.
            self.response["first_conversation"] = "FIRST CONVERSATION: welcome the owner."
            self.assertFalse(any("get-set-up" in line for line in lines(self.hook(session="00000000-0000-4000-8000-000000000002"))))

    def many_skills(self, count):
        for number in range(count):
            install(self.root, f"---\nname: qorqut-plugin-n{number:03}\ndescription: Do thing number {number} "
                               "with some care and a good many words to read.\n---\n")

    def test_a_section_that_fits_stays_inline_when_the_context_goes_to_a_file(self):
        install(self.root, BROWSER, REMINDERS)
        facts = "## Context Home\n" + "A published node summary. " * 600
        self.response["mandatory_context"] = {"rendered": RULES + "\n\n" + facts, "instructions": RULES, "context": facts}
        text = self.hook()
        self.assertLessEqual(len(text), CLAUDE_HOOK_LIMIT)
        self.assertEqual(text.count(HEADER), 1)
        self.assertNotIn(HEADER, (self.root / STARTUP_CONTEXT).read_text())

    def test_a_section_that_would_pass_the_hook_limit_leads_the_context_file_instead(self):
        self.many_skills(60)
        facts = "## Context Home\n" + "A published node summary. " * 600
        self.response["mandatory_context"] = {"rendered": RULES + "\n\n" + facts, "instructions": RULES, "context": facts}
        with mock.patch.object(claude_session, "CLAUDE_HOOK_LIMIT", 4000):
            text = self.hook()
        self.assertLessEqual(len(text), 4000)
        self.assertNotIn(HEADER, text)
        body = (self.root / STARTUP_CONTEXT).read_text()
        self.assertTrue(body.startswith(HEADER))
        self.assertEqual(body.count(HEADER), 1)
        self.assertEqual(len(lines(body)), 60)
        self.assertIn(STARTUP_CONTEXT, text)
        self.assertIn(facts, body)

    def test_a_snapshot_from_before_the_messages_still_starts(self):
        install(self.root, BROWSER)
        document = json.loads(catalog().bytes())
        for key in [key for key in document["messages"] if key.startswith("skill-discovery")]:
            del document["messages"][key]
        (self.root / "runtime-instructions.json").write_text(json.dumps(document))
        text = self.hook()
        self.assertNotIn(HEADER, text)
        self.assertIn(FACTS, text)


if __name__ == "__main__":
    unittest.main()
