"""Telegram owner input in Terminals mode: what the owner reads when an agent has no running terminal, and the offline
``composer-check`` command. The mode is the installation's, read from the service the bridge runs in; managed wording is
covered by ``test_owner_input_managed`` and stays as it is. The worker is exercised with stand-in bridge and store objects."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut.managed_runtime import mode
from qorqut.native_chat.contracts import InputResult, RESULT_DEFERRED
from qorqut_telegram import __main__ as cli
from qorqut_telegram import owner_input
from qorqut_telegram.owner_input import DEFERRALS, OwnerInputs
from test_owner_input_managed import Writer


class TerminalOwnerInputTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(mode, "current", return_value="terminal")
        self.mode = patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = NS(runtime="/r")

    def inputs(self):
        row = {"id": "i1", "agent_id": "a1", "state": "queued", "attempts": 0, "revision": 1, "purpose": "owner_message",
               "notice_sent_at": None, "message_id": "m1", "sensitive": 0}
        self.writer = Writer(row)
        bridge = NS(now=lambda: "2026-01-01T00:00:00Z", rng=NS(random=lambda: 1.0), write=lambda operation: self.writer, wake=lambda: None,
                    runtime=NS(paths=self.paths))
        return OwnerInputs(bridge), row

    def deliver(self, reason):
        worker, row = self.inputs()
        sent = []
        with patch.object(owner_input.store, "message", return_value={"chat_name": "ordubashy", "id": "m1"}), \
             patch.object(owner_input, "notice", side_effect=lambda bridge, uow, message, text, key: sent.append(text)):
            worker._defer(row, reason, "2026-01-01T00:00:00Z")
        return sent[0]

    def settled(self, state, code, receipt=None):
        worker, _ = self.inputs()
        sent = []
        row = {"id": "i1", "purpose": "owner_message", "sensitive": 0, "message_id": "m1", "state": state, "error_code": code,
               "receipt_json": receipt}
        with patch.object(owner_input.store, "message", return_value={"chat_name": "ordubashy", "id": "m1"}), \
             patch.object(owner_input, "notice", side_effect=lambda bridge, uow, message, text, key: sent.append(text)):
            worker._after(NS(), row, "now")
        return sent[0]

    def test_the_mode_is_read_from_the_service_and_only_the_service(self):
        worker, _ = self.inputs()
        self.assertTrue(worker.terminal())
        self.mode.return_value = "managed"
        self.assertFalse(worker.terminal())
        self.assertFalse(OwnerInputs(NS()).terminal())  # no service paths: managed

    def test_an_agent_with_no_running_terminal_is_deferred_and_the_owner_reads_about_a_terminal(self):
        self.assertEqual(DEFERRALS["terminal_offline"], "terminal_absent")
        outcome = OwnerInputs._outcome(InputResult(state=RESULT_DEFERRED, reason="terminal_offline"), terminal=True)
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "terminal_absent"))
        text = self.deliver("terminal_absent")
        self.assertIn("because its terminal is not running (nothing was started)", text)
        self.assertIn("will be typed into the terminal when the composer is free", text)
        self.assertIn("Nothing was started or interrupted.", text)

    def test_the_other_terminal_codes_and_a_reason_nobody_worded_read_as_a_terminal(self):
        self.assertIn("its terminal is not running the expected client", self.deliver("terminal_unverified"))
        self.assertIn("its terminal cannot be reached from here", self.deliver("terminal_unsupported"))
        for reason in ("composer_busy", "composer_not_empty", "terminal_unavailable"):
            with self.subTest(reason=reason):
                text = self.deliver(reason)
                self.assertIn("its terminal is busy or not ready for input", text)
                self.assertIn("typed into the terminal when the composer is free", text)
        self.assertEqual(OwnerInputs._outcome(InputResult(state=RESULT_DEFERRED), terminal=True).reason, "terminal_unavailable")

    def test_a_reason_only_a_session_reports_keeps_the_session_wording(self):
        text = self.deliver("session_closed")
        self.assertIn("its session is not open and could not be opened right now", text)
        self.assertIn("will be delivered when its session can take it", text)
        self.assertIn("Nothing was interrupted.", text)

    def test_a_terminal_agents_failures_read_as_a_terminal(self):
        self.assertIn("could not be typed into its terminal (terminal_unsupported)", self.settled("failed", "terminal_unsupported", {"client": "claude"}))
        for code in ("submit_unconfirmed", "session_changed"):
            text = self.settled("uncertain", code)
            self.assertIn("reached the terminal", text)
            self.assertIn("check the terminal before resending", text)

    def test_a_session_input_that_settles_after_a_switch_still_reads_as_a_session(self):
        for receipt in ({"transport": "managed"}, '{"transport": "managed"}'):
            self.assertIn("check its Chat before resending", self.settled("uncertain", "session_changed", receipt))
        self.assertIn("could not be delivered to its session (driver_unavailable)", self.settled("failed", "driver_unavailable"))

    def test_managed_mode_words_every_one_of_these_as_a_session(self):
        self.mode.return_value = "managed"
        for reason in ("terminal_absent", "composer_busy"):
            text = self.deliver(reason)
            self.assertNotIn("terminal", text.replace("Nothing was started", ""))
            self.assertIn("will be delivered when its session can take it", text)
        self.assertIn("could not be delivered to its session (terminal_unsupported)", self.settled("failed", "terminal_unsupported"))

    def test_an_unreachable_arbiter_defers_with_the_terminal_code_only_in_terminals_mode(self):
        worker, row = self.inputs()
        worker.arbiter = lambda: None
        worker._load = lambda row, now: ("text", {}, {})
        reasons = []
        worker._defer = lambda row, reason, now: reasons.append(reason)
        worker.attempt({**row, "expires_at": None}, "now")
        self.mode.return_value = "managed"
        worker.attempt({**row, "expires_at": None}, "now")
        self.assertEqual(reasons, ["terminal_unavailable", "session_unavailable"])


class ComposerCheckTests(unittest.TestCase):
    RULE = "─" * 66
    SCREEN = "\n".join(["● Earlier answer", RULE, "❯\xa0", RULE, "  ⏵⏵ bypass permissions on (shift+tab to cycle)"])

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_saved_idle_screen_is_ready_and_a_missing_file_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            screen = Path(folder) / "screen.txt"
            screen.write_text(self.SCREEN, encoding="utf-8")
            code, out, _ = self.run_cli("composer-check", "--client", "claude", "--screen", str(screen), "--cursor", "2,2")
            self.assertEqual((code, json.loads(out)["ready"], json.loads(out)["client"]), (0, True, "claude"))
            code, _, err = self.run_cli("composer-check", "--client", "claude", "--screen", str(Path(folder) / "none.txt"))
            self.assertEqual(code, 2)
            self.assertIn("Cannot read the screen file", err)

    def test_a_screen_that_shows_something_else_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            screen = Path(folder) / "screen.txt"
            screen.write_text("$ ls\nfile\n", encoding="utf-8")
            code, out, _ = self.run_cli("composer-check", "--client", "claude", "--screen", str(screen))
            self.assertEqual((code, json.loads(out)["ready"]), (1, False))


if __name__ == "__main__":
    unittest.main()
