"""Claude Code shows the model at most 10,000 characters of hook output, so its agents' instructions travel in a file."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_agentic.behavior.instructions import from_catalog
from qorqut.bootstrap.claude_session import (CLAUDE_HOOK_LIMIT, INSTRUCTIONS_CHANGED, STARTUP_CONTEXT, STARTUP_SKILLS,
                                             handle_hook)
from qorqut.bootstrap.client import STARTUP_INSTRUCTIONS

RULES = "## Core Working Rules\n" + "Managed instruction text. " * 4000
FACTS = "## Identity And Assignment\nYou are the test agent."


class ClaudeStartupContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.registration = {"registration_id": "synthetic", "workspace": str(self.root)}
        instruction_root = (Path(__file__).resolve().parents[3]
                            / "qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/runtime-instructions")
        (self.root / "runtime-instructions.json").write_bytes(
            from_catalog(lambda path: (instruction_root / path).read_text()).bytes())
        self.response = {"ready": True, "effective_config_id": "config-1",
                         "mandatory_context": {"rendered": RULES + "\n\n" + FACTS, "instructions": RULES, "context": FACTS}}
        for target, value in (("qorqut.bootstrap.claude_session.load_registration", self.registration),
                              ("qorqut.bootstrap.native_binding.attest", None),
                              ("qorqut.bootstrap.handoff.request", None),
                              ("qorqut.bootstrap.recovery.read", None)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self, runtime="claude-code", session="00000000-0000-4000-8000-000000000001"):
        client = SimpleNamespace(post=lambda *_args: self.response)
        output, _, _ = handle_hook({"hook_event_name": "SessionStart", "session_id": session, "source": "startup"},
                                   self.root / "registration.json", runtime=runtime, client_factory=lambda _: client)
        return output["hookSpecificOutput"]["additionalContext"]

    def test_claude_code_gets_its_instructions_from_the_imported_file(self):
        text = self.start()
        self.assertNotIn("Managed instruction text.", text)
        self.assertIn(FACTS, text)
        self.assertLessEqual(len(text), CLAUDE_HOOK_LIMIT)
        self.assertEqual((self.root / STARTUP_INSTRUCTIONS).read_text(), RULES)

    def test_changed_instructions_tell_the_session_to_read_them(self):
        self.assertIn(INSTRUCTIONS_CHANGED, self.start())
        # The next session loaded the current copy through CLAUDE.md, so it needs no notice.
        self.assertNotIn(INSTRUCTIONS_CHANGED, self.start(session="00000000-0000-4000-8000-000000000002"))

    def test_a_long_session_context_moves_to_a_file_with_a_pointer(self):
        facts = "## Context Home\n" + "A published node summary. " * 600
        self.response["mandatory_context"] = {"rendered": RULES + "\n\n" + facts, "instructions": RULES, "context": facts}
        text = self.start()
        self.assertLessEqual(len(text), CLAUDE_HOOK_LIMIT)
        self.assertIn(STARTUP_CONTEXT, text)
        self.assertIn(facts, (self.root / STARTUP_CONTEXT).read_text())

    def with_skills(self, skills):
        self.registration["operational_intake_enabled"] = True
        patcher = mock.patch("qorqut.bootstrap.always_skills.handle",
                             return_value={"hookSpecificOutput": {"additionalContext": skills}})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_long_always_loaded_skills_get_their_own_file_so_one_read_returns_the_context(self):
        skills = "# Always-loaded skills\n" + "A skill rule the agent follows.\n" * 3000
        self.with_skills(skills)
        self.response["work_briefing"] = {"rendered": "## Saved handoff\nOpen work."}
        text = self.start()
        self.assertLessEqual(len(text), CLAUDE_HOOK_LIMIT)
        self.assertIn(STARTUP_CONTEXT, text)
        self.assertIn(STARTUP_SKILLS, text)
        self.assertEqual((self.root / STARTUP_SKILLS).read_text(), skills)
        context = (self.root / STARTUP_CONTEXT).read_text()
        self.assertNotIn("A skill rule the agent follows.", context)
        self.assertIn(FACTS, context)
        self.assertIn("## Saved handoff\nOpen work.", context)
        self.assertLess(len(context), len(skills) // 10)

    def test_a_short_operational_reminder_stays_in_the_hook_output(self):
        self.with_skills("Act on this input.")
        facts = "## Context Home\n" + "A published node summary. " * 600
        self.response["mandatory_context"] = {"rendered": RULES + "\n\n" + facts, "instructions": RULES, "context": facts}
        text = self.start()
        self.assertIn("Act on this input.", text)
        self.assertNotIn(STARTUP_SKILLS, text)
        self.assertFalse((self.root / STARTUP_SKILLS).exists())
        self.assertNotIn("Act on this input.", (self.root / STARTUP_CONTEXT).read_text())

    def test_codex_gets_skills_and_context_in_the_hook(self):
        skills = "# Always-loaded skills\n" + "A skill rule the agent follows.\n" * 3000
        self.with_skills(skills)
        text = self.start("codex")
        self.assertIn(skills, text)
        self.assertIn(FACTS, text)
        self.assertFalse((self.root / STARTUP_SKILLS).exists())

    def test_codex_still_gets_everything_in_the_hook(self):
        text = self.start("codex")
        self.assertIn(RULES, text)
        self.assertFalse((self.root / STARTUP_INSTRUCTIONS).exists())


if __name__ == "__main__":
    unittest.main()
