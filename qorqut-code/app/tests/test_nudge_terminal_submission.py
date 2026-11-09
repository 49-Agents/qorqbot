"""Exercise the arbiter's nudge policy through the real terminal adapter and composer reader."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from qorqut.native_chat.adapters import TerminalAdapter
from qorqut.native_chat.arbiter import InputArbiter
from qorqut.native_chat.contracts import TYPE_DIRECT, TYPE_NUDGE, TYPE_TELEGRAM
from qorqut.transport.agent_terminal_control import AgentTerminalBackend
from test_terminal_enter_retry import Terminal


class ClippedTerminal(Terminal):
    def screen(self):
        screen = super().screen()
        if self.pastes and self.composer:
            # A valid ordinary composer whose visible text cannot prove the full paste.
            return screen.replace(self.composer, self.composer[:30] + "…", 1)
        return screen


class NudgeTerminalSubmissionTest(unittest.TestCase):
    def send(self, terminal, input_type=TYPE_NUDGE):
        runtime = SimpleNamespace()
        backend = AgentTerminalBackend(SimpleNamespace(runtime=runtime), sleep=terminal.sleep)
        backend.selected_client = lambda row: terminal.client
        backend.selected = lambda row: (terminal, None, "registered-session", None, None)
        backend.snapshot = lambda row, client=None: dict(terminal.identity)
        arbiter = InputArbiter(runtime, None, None, {"terminal": TerminalAdapter(lambda: backend)})
        claimed = {}

        def checkpoint():
            claimed["row"] = {"id": "test-input"}

        text = "Review the pending assignments and continue authorized work.\n" * 40
        text = arbiter._typed("system", "test-marker", input_type, text) if input_type == TYPE_NUDGE else text
        with TemporaryDirectory() as workspace:
            outcome, adapter = arbiter._run(
                {"agent_id": "agent", "workspace": workspace, "profile_json": "{}"},
                {"transport": "terminal", "provider": terminal.client}, text, True,
                input_type, checkpoint, claimed)
            self.assertEqual(list((Path(workspace) / ".qorqut/chat-input").iterdir()), [])
        self.assertEqual(adapter, "terminal")
        return outcome, claimed

    def test_clipped_nudge_submits_once_like_telegram(self):
        for client in ("claude", "codex"):
            for input_type in (TYPE_NUDGE, TYPE_TELEGRAM):
                with self.subTest(client=client, input_type=input_type):
                    terminal = ClippedTerminal(client, ["delivered"])
                    outcome, claimed = self.send(terminal, input_type)
                    self.assertEqual(outcome.status, "submitted")
                    self.assertTrue(claimed)
                    self.assertEqual((terminal.pastes, len(terminal.enters), terminal.clears), (1, 1, 0))
                    if input_type == TYPE_NUDGE:
                        self.assertIn('<qorqut-system-input id="test-marker"', terminal.history)

    def test_nudge_preserves_existing_draft(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                terminal = ClippedTerminal(client, [], draft="Owner's unfinished message")
                outcome, claimed = self.send(terminal)
                self.assertEqual((outcome.status, outcome.reason), ("deferred", "composer_not_empty"))
                self.assertFalse(claimed)
                self.assertEqual((terminal.pastes, len(terminal.enters), terminal.clears), (0, 0, 0))
                self.assertEqual(terminal.composer, "Owner's unfinished message")

    def test_nudge_retries_only_enter_when_exact_paste_remains(self):
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                terminal = Terminal(client, ["missed", "delivered"])
                outcome, _ = self.send(terminal)
                self.assertEqual(outcome.status, "submitted")
                self.assertEqual((terminal.pastes, len(terminal.enters), terminal.clears), (1, 2, 0))

    def test_other_producers_keep_paste_verification(self):
        terminal = ClippedTerminal("claude", [])
        outcome, _ = self.send(terminal, TYPE_DIRECT)
        self.assertEqual((outcome.status, outcome.reason), ("uncertain", "paste_unverified"))
        self.assertEqual((terminal.pastes, len(terminal.enters)), (1, 0))


if __name__ == "__main__":
    unittest.main()
