"""Exercise real paste/composer checks with a fake terminal; never contact a live agent."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from qorqut.transport.agent_terminal_control import AgentTerminalBackend


class Terminal:
    def __init__(self, client, outcomes, *, busy=False, draft=""):
        self.client, self.outcomes = client, iter(outcomes)
        self.composer, self.history, self.buffer = draft, "", ""
        self.busy, self.mode, self.unreadable = busy, "", False
        self.now, self.enters, self.pastes, self.clears = 0, [], 0, 0
        self.identity = {"state": "present", "pane_id": "%1", "command": client,
                         "foreground_pid": 123, "foreground_started": "original"}

    def sleep(self, seconds):
        self.now += seconds

    def screen(self):
        prompt = ("› " if self.client == "codex" else "❯ ") + self.composer
        rule = "─" * 60
        rows = [self.history, "", prompt] if self.client == "codex" else [self.history, rule, prompt, rule]
        rows.extend(["", "  ? for shortcuts"])
        if self.busy:
            rows.append("  esc to interrupt")
        if self.mode:
            rows.append(self.mode)
        return "\n".join(rows)

    def tmux(self, *args):
        command = args[0]
        if command == "display-message":
            x = len(self.composer.split('\n')[-1]) + 2
            y = self.history.count('\n') + 2 + self.composer.count('\n')
            return 0, f"0|0|1|{x}|{y}"
        if command == "capture-pane":
            return (1, "") if self.unreadable else (0, self.screen())
        if command == "load-buffer":
            self.buffer = Path(args[-1]).read_text()
        elif command == "paste-buffer":
            self.composer = self.buffer
            self.pastes += 1
        elif command == "send-keys":
            if args[-1] != "Enter":
                assert self.pastes == 0, "The retry must not clear or replace the pending paste"
                self.composer = ""
                self.clears += 1
            else:
                self.enters.append(self.now)
                outcome = next(self.outcomes)
                if outcome == "delivered":
                    self.history = ("› " if self.client == "codex" else "❯ ") + self.composer + "\n\nAnswer"
                    self.composer = ""
                elif outcome == "different":
                    self.composer = "owner's new draft"
                elif outcome == "modal":
                    self.mode = "\x1b[2mDo you trust this folder? Enter to confirm\x1b[0m"
                elif outcome == "interrupt":
                    self.mode = "Enter to interrupt and send"
                elif outcome == "changed":
                    self.identity["foreground_started"] = "replacement"
                elif outcome == "unreadable":
                    self.unreadable = True
                elif outcome == "failed":
                    return 1, ""
                else:
                    assert outcome == "missed", outcome
        else:
            assert command == "delete-buffer", args
        return 0, ""


class TerminalEnterRetryTest(unittest.TestCase):
    def deliver(self, terminal, *, replace=False, text="Please do the next task", echo=True):
        backend = AgentTerminalBackend(SimpleNamespace(runtime=SimpleNamespace()), sleep=terminal.sleep)
        backend.selected_client = lambda row: terminal.client
        backend.selected = lambda row: (terminal, None, "registered-session", None, None)
        backend.snapshot = lambda row, client=None: dict(terminal.identity)
        checkpoints = []
        with TemporaryDirectory() as workdir:
            outcome = backend.owner_input({"agent_id": "agent"}, text, workdir=workdir,
                                          checkpoint=lambda: checkpoints.append(True), allow_busy=terminal.busy,
                                          replace_composer=replace, require_paste_echo=echo)
            self.assertEqual(list(Path(workdir).iterdir()), [])
        self.assertEqual(checkpoints, [True])
        self.assertEqual(terminal.pastes, 1)
        self.assertNotIn(text, repr(outcome.receipt))
        return outcome

    def test_missed_enter_retries_once_after_400ms_without_repasting(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                terminal = Terminal(client, ["missed", "delivered"])
                outcome = self.deliver(terminal)
                self.assertEqual(outcome.status, "submitted")
                self.assertEqual(outcome.receipt["submit_attempts"], 2)
                self.assertEqual(len(terminal.enters), 2)
                self.assertAlmostEqual(terminal.enters[1] - terminal.enters[0], .4)
                self.assertEqual(terminal.clears, 0)

    def test_successful_submit_is_not_repeated_even_with_message_in_history(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                terminal = Terminal(client, ["delivered"])
                outcome = self.deliver(terminal)
                self.assertEqual(outcome.status, "submitted")
                self.assertEqual(outcome.receipt["submit_attempts"], 1)
                self.assertEqual(len(terminal.enters), 1)

    def test_trailing_blank_lines_reach_enter_and_can_retry_a_missed_submit(self):
        for client in ('codex', 'claude'):
            for text in ('Check the calendar\n', 'Check the calendar\n\n', 'First task\nSecond task\n  '):
                with self.subTest(client=client, text=text):
                    terminal = Terminal(client, ['missed', 'delivered'])
                    outcome = self.deliver(terminal, text=text, replace=True)
                    self.assertEqual(outcome.status, 'submitted')
                    self.assertEqual(outcome.receipt['submit_attempts'], 2)
                    self.assertAlmostEqual(terminal.enters[1] - terminal.enters[0], .4)

    def test_two_missed_enters_leave_uncertain_delivery_without_a_third_attempt(self):
        terminal = Terminal("codex", ["missed", "missed"])
        outcome = self.deliver(terminal)
        self.assertEqual((outcome.status, outcome.reason), ("uncertain", "submit_unconfirmed"))
        self.assertEqual(outcome.receipt["submit_attempts"], 2)
        self.assertEqual(len(terminal.enters), 2)

    def test_changed_draft_modal_or_unreadable_screen_never_authorizes_retry(self):
        for client in ("codex", "claude"):
            for change in ("different", "modal", "interrupt", "unreadable"):
                with self.subTest(client=client, change=change):
                    terminal = Terminal(client, [change], busy=True)
                    outcome = self.deliver(terminal)
                    self.assertEqual(outcome.receipt["submit_attempts"], 1)
                    self.assertEqual(len(terminal.enters), 1)
                    self.assertEqual(terminal.clears, 0)

    def test_changed_session_is_uncertain_and_receives_no_second_enter(self):
        terminal = Terminal("codex", ["changed"])
        outcome = self.deliver(terminal)
        self.assertEqual((outcome.status, outcome.reason), ("uncertain", "session_changed"))
        self.assertEqual(len(terminal.enters), 1)

    def test_busy_composer_can_retry_without_interrupting_the_running_turn(self):
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                terminal = Terminal(client, ["missed", "delivered"], busy=True)
                outcome = self.deliver(terminal)
                self.assertEqual(outcome.receipt["submit_attempts"], 2)
                self.assertEqual(terminal.clears, 0)

    def test_owner_chat_replacement_clears_only_before_paste(self):
        terminal = Terminal("codex", ["missed", "delivered"], draft="previous draft")
        outcome = self.deliver(terminal, replace=True)
        self.assertEqual(outcome.receipt["submit_attempts"], 2)
        self.assertGreater(terminal.clears, 0)

    def test_optional_echo_still_requires_matching_pending_text_for_retry(self):
        terminal = Terminal("codex", ["different"])
        self.deliver(terminal, echo=False)
        self.assertEqual(len(terminal.enters), 1)

    def test_key_transport_failure_remains_uncertain(self):
        terminal = Terminal("codex", ["failed"])
        outcome = self.deliver(terminal)
        self.assertEqual((outcome.status, outcome.reason), ("uncertain", "submit_failed"))
        self.assertEqual(len(terminal.enters), 1)


if __name__ == "__main__":
    unittest.main()
