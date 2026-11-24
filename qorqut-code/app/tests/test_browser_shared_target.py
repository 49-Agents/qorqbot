"""A workspace's browser target can name the Shared browser, and commands against it keep to their own tab."""

import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from qorqut.bootstrap import agent_browser, browser
from qorqut.browser import idle_tabs


def target(**extra):
    return {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:20400", "runtime_dir": "/runtime", **extra}


def keep_off_real_browsers(test):
    """The endpoints here are ports the product gives to real browsers, and ``browser.run`` looks at its browser's tabs
    before each command (``idle_tabs.before_command``: on a live browser it would load unloaded tabs again and mark a
    real session as used). So the hook is stood in for, and a test that opens any connection fails. The attempt is
    counted and checked afterwards because the hook itself swallows every error."""
    connect = mock.patch.object(socket.socket, "connect", side_effect=OSError("no network in this test"))
    attempts = connect.start()
    test.addCleanup(lambda: test.assertEqual(attempts.call_args_list, [], "this test tried to open a network connection"))
    test.addCleanup(connect.stop)
    before = mock.patch.object(idle_tabs, "before_command")
    before.start()
    test.addCleanup(before.stop)


class SharedTargetTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name).resolve()  # the temporary folder is behind a symbolic link on macOS
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
        self.workspace = Path(folder.name).resolve()  # the temporary folder is behind a symbolic link on macOS
        (self.workspace / ".qorqut").mkdir()
        keep_off_real_browsers(self)

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


class CommandTargetTest(unittest.TestCase):
    """A command's target is read with the browser's use lock held, so a switch between own and Shared cannot slip in."""

    def test_the_target_is_read_only_after_the_use_lock_is_taken_and_the_command_runs_inside_it(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from qorqut.browser.access import BrowserAccess
        access = BrowserAccess.__new__(BrowserAccess)
        access.runtime = SimpleNamespace()
        held, order = [], []

        @contextmanager
        def use(_self, _key):
            held.append(True)
            order.append("lock")
            try:
                yield
            finally:
                held.pop()
                order.append("unlock")

        def read(_key, _owner):
            order.append("read" if held else "read WITHOUT the lock")
            return {"cdp_endpoint": "http://127.0.0.1:20400", "runtime_dir": "/runtime", "instance": "configured:agent-1", "shared": True}

        def run(*_args, **_options):
            order.append("run" if held else "run WITHOUT the lock")
            return 0, "ok", ""

        access._runner = lambda _actor, _command: run
        access._artifact_root = lambda *_args: Path("/artifacts")
        access._artifact_handles = lambda *_args: []
        with mock.patch.object(BrowserAccess, "_worker_target", side_effect=read), mock.patch.object(BrowserAccess, "_managed_use", use), \
                mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/agent-browser")):
            access._native_command(SimpleNamespace(principal=SimpleNamespace(id="agent-1")), {"command": ["click", "e3"]},
                                   "configured:agent-1", "b0123456789ab")
        self.assertEqual(order, ["lock", "read", "run", "unlock"])


GONE = ("\u2717 tab_gone: bound tab is gone (target 9380C06E0BAAFB3B9EA609B1E1A70588, last url http://shop.example/cart). "
        "Run `agent-browser tab new <url>` to bind a new tab, or `agent-browser tab list` to pick an existing one\n")


class FailedCommandTest(unittest.TestCase):
    """What the caller gets back when the browser runtime fails, as it does for every agent after the Shared browser
    restarts: its tab is gone."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name).resolve()  # the temporary folder is behind a symbolic link on macOS
        self.target = ("http://127.0.0.1:20999", Path("/runtime/agent-browser"))
        keep_off_real_browsers(self)

    def run_command(self, args, outcome, pinned=True):
        effect = outcome if isinstance(outcome, Exception) else None
        with mock.patch.object(browser.subprocess, "run", return_value=outcome, side_effect=effect):
            return browser.run(self.workspace, args, target=self.target, capture=True, pinned=pinned)

    def sent(self, args, *, new, pinned=True):
        """The ``agent-browser`` arguments after the address for one command, for a session that has or has not had a tab."""
        with mock.patch.object(idle_tabs, "is_new", return_value=new), \
                mock.patch.object(browser.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            browser.run(self.workspace, args, target=self.target, capture=True, pinned=pinned)
        command = run.call_args.args[0]
        return command[command.index(self.target[0]) + 1:]

    def test_a_new_pinned_sessions_first_tab_new_loads_in_its_own_tab_and_leaves_no_blank_one(self):
        # agent-browser gives a session without a tab a blank one at its first command; ``tab new`` then opened a second.
        self.assertEqual(self.sent(["tab-new", "https://shop.example/"], new=True), ["open", "https://shop.example/"])
        self.assertEqual(self.sent(["tab-new", "https://shop.example/"], new=False), ["tab", "new", "https://shop.example/"])
        self.assertEqual(self.sent(["tab-new", "https://shop.example/"], new=True, pinned=False), ["tab", "new", "https://shop.example/"])
        self.assertEqual(self.sent(["tab-new"], new=True), ["tab", "new"])

    def test_a_session_is_new_until_some_session_directory_holds_its_target_file(self):
        first, second = self.workspace / "one", self.workspace / "two"
        first.mkdir(), second.mkdir()
        self.assertTrue(idle_tabs.is_new("qorqut-abc", [first, second]))
        (second / "qorqut-abc.target").write_text("{}")
        self.assertFalse(idle_tabs.is_new("qorqut-abc", [first, second]))
        self.assertTrue(idle_tabs.is_new("qorqut-other", [first, second]))

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
