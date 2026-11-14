"""Claude Code shows the model at most 10,000 characters of hook output, so its agents' instructions travel in a file."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_agentic.behavior.instructions import from_catalog
from qorqut.bootstrap.claude_session import (CLAUDE_HOOK_LIMIT, INSTRUCTIONS_CHANGED, STARTUP_CONTEXT,
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

    def test_codex_still_gets_everything_in_the_hook(self):
        text = self.start("codex")
        self.assertIn(RULES, text)
        self.assertFalse((self.root / STARTUP_INSTRUCTIONS).exists())


if __name__ == "__main__":
    unittest.main()
