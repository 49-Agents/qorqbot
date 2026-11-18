"""A workspace's browser target can name the Shared browser, and commands against it keep to their own tab."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qorqut.bootstrap import agent_browser, browser


def target(**extra):
    return {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:20400", "runtime_dir": "/runtime", **extra}


class SharedTargetTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        (self.workspace / ".qorqut").mkdir()

    def save(self, content):
        (self.workspace / ".qorqut/browser.json").write_text(json.dumps(content))

    def test_an_own_target_reads_as_before_and_is_not_shared(self):
        self.save(target())
        self.assertEqual(browser.read_target(self.workspace), ("http://127.0.0.1:20400", "/runtime"))
        self.assertFalse(browser.target_shared(self.workspace))

    def test_a_shared_target_reads_the_same_endpoint_and_is_shared(self):
        self.save(target(shared=True))
        self.assertEqual(browser.read_target(self.workspace), ("http://127.0.0.1:20400", "/runtime"))
        self.assertTrue(browser.target_shared(self.workspace))

    def test_only_true_marks_a_shared_target(self):
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                self.save(target(shared=value))
                with self.assertRaises(browser.BrowserConfigurationInvalid):
                    browser.read_target(self.workspace)
                self.assertFalse(browser.target_shared(self.workspace))

    def test_any_other_entry_is_still_refused(self):
        self.save(target(shared=True, profile="/elsewhere"))
        with self.assertRaises(browser.BrowserConfigurationInvalid):
            browser.read_target(self.workspace)

    def test_a_missing_target_is_not_shared(self):
        self.assertFalse(browser.target_shared(self.workspace))
        with self.assertRaises(browser.BrowserNotConfigured):
            browser.read_target(self.workspace)


class PinnedCommandTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        (self.workspace / ".qorqut").mkdir()

    def command(self, args, **options):
        with tempfile.TemporaryDirectory() as directory:
            return agent_browser.command("agent-browser", "session", "http://cdp", directory, args, **options)

    def test_an_own_browser_command_is_unchanged(self):
        self.assertNotIn("--pin-tab", self.command(["click", "e3"]))

    def test_a_pinned_command_keeps_the_session_on_its_tab(self):
        command = self.command(["click", "e3"], pinned=True)
        self.assertEqual(command[command.index("--session"):],
                         ["--session", "session", "--pin-tab", "--cdp", "http://cdp", "click", "@e3"])

    def test_a_caller_cannot_pin_or_unpin_through_arguments(self):
        for option in ("--pin-tab", "--no-pin-tab"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.command(["click", "e3", option], pinned=True)

    def ran(self, args, **options):
        """The argv ``browser.run`` hands to the browser runtime."""
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(browser.subprocess, "run", return_value=completed) as run:
            browser.run(self.workspace, args, capture=True, **options)
        return run.call_args.args[0]

    def save(self, **extra):
        (self.workspace / ".qorqut/browser.json").write_text(json.dumps(target(**extra)))

    def test_run_pins_a_workspace_whose_own_target_is_shared(self):
        self.save(shared=True)
        with mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/agent-browser")):
            self.assertIn("--pin-tab", self.ran(["click", "e3"]))
            self.assertIn("--pin-tab", self.ran(["observe", "overview"]))

    def test_run_does_not_pin_an_own_target(self):
        self.save()
        with mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/agent-browser")):
            self.assertNotIn("--pin-tab", self.ran(["click", "e3"]))
            self.assertNotIn("--pin-tab", self.ran(["observe", "overview"]))

    def test_a_supplied_target_is_pinned_only_when_the_caller_says_so(self):
        # The workspace's own file does not describe a target the server selected.
        self.save(shared=True)
        supplied = ("http://127.0.0.1:20999", Path("/runtime/agent-browser"))
        self.assertNotIn("--pin-tab", self.ran(["click", "e3"], target=supplied))
        self.assertIn("--pin-tab", self.ran(["click", "e3"], target=supplied, pinned=True))


GONE = ("\u2717 tab_gone: bound tab is gone (target 9380C06E0BAAFB3B9EA609B1E1A70588, last url http://shop.example/cart). "
        "Run `agent-browser tab new <url>` to bind a new tab, or `agent-browser tab list` to pick an existing one\n")


class FailedCommandTest(unittest.TestCase):
    """What the caller gets back when the browser runtime fails, as it does for every agent after the Shared browser
    restarts: its tab is gone."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        self.target = ("http://127.0.0.1:20999", Path("/runtime/agent-browser"))

    def run_command(self, args, outcome, pinned=True):
        effect = outcome if isinstance(outcome, Exception) else None
        with mock.patch.object(browser.subprocess, "run", return_value=outcome, side_effect=effect):
            return browser.run(self.workspace, args, target=self.target, capture=True, pinned=pinned)

    def test_a_gone_tab_is_reported_with_the_command_the_agent_has(self):
        code, _output, errors = self.run_command(["click", "e3"], mock.Mock(returncode=1, stdout="", stderr=GONE))
        self.assertEqual(code, 1)
        self.assertIn("tab_gone: bound tab is gone (target 9380C06E0BAAFB3B9EA609B1E1A70588, last url http://shop.example/cart).", errors)
        self.assertIn("tab-new <url>", errors)
        self.assertNotIn("agent-browser tab", errors)          # neither a command it lacks nor another agent's tab is offered

    def test_an_own_browser_keeps_the_runtimes_own_words(self):
        _code, _output, errors = self.run_command(["click", "e3"], mock.Mock(returncode=1, stdout="", stderr=GONE), pinned=False)
        self.assertEqual(errors, GONE)

    def test_other_errors_of_a_pinned_command_are_unchanged(self):
        _code, _output, errors = self.run_command(["click", "e3"], mock.Mock(returncode=1, stdout="", stderr="\u2717 Element not found\n"))
        self.assertEqual(errors, "\u2717 Element not found\n")

    def test_a_failed_observation_is_returned_to_the_caller_like_any_other_result(self):
        # A failed snapshot answers in JSON on standard output (the runtime is run with --json for an observation).
        gone = json.dumps({"success": False, "data": {"targetId": "9380C06E0BAAFB3B9EA609B1E1A70588"}, "error": GONE[2:].strip()})
        outcomes = {"the runtime fails": (mock.Mock(returncode=1, stdout=gone, stderr=""), 1,
                                          "last url http://shop.example/cart). Open a new tab with tab-new <url> and continue there."),
                    "the runtime fails without an answer": (mock.Mock(returncode=1, stdout="", stderr="\u2717 Chrome is gone\n"), 1, "Chrome is gone"),
                    "the runtime times out": (browser.subprocess.TimeoutExpired("agent-browser", 45), 2, "timed out"),
                    "the runtime cannot be run": (OSError("missing"), 2, "Could not execute")}
        for name, (outcome, expected, words) in outcomes.items():
            with self.subTest(name), mock.patch("builtins.print", side_effect=AssertionError("printed, not returned")):
                code, output, errors = self.run_command(["observe", "overview"], outcome)
                answer = json.loads(output)
                self.assertEqual((code, answer["status"], answer["exit_code"], errors), (expected, "failed", expected, ""))
                self.assertIn(words, answer["message"])
                self.assertNotIn('"success"', answer["message"])


if __name__ == "__main__":
    unittest.main()
