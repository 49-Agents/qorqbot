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


if __name__ == "__main__":
    unittest.main()
