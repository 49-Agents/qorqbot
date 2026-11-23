"""A start command written for an earlier version still starts the service."""
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.managed_runtime import mode
from qorqut.service import main as service_main


class DeprecatedOptionsTest(unittest.TestCase):
    def start(self, *arguments):
        # Without --workspace the service refuses to assemble: reaching that refusal (status 1) means every option parsed.
        return service_main.main(list(arguments))

    def test_the_terminal_workspace_option_is_accepted_ignored_in_managed_mode_and_warned_about_once(self):
        with self.assertLogs("qorqut.service.main", level="WARNING") as logged:
            status = self.start("--terminal-workspace", "/srv/qorqut/terminal", "--port", "1")
        self.assertEqual(status, 1)
        warnings = [record for record in logged.records if record.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("--terminal-workspace", warnings[0].getMessage())
        self.assertIn("no effect", warnings[0].getMessage())

    def test_the_terminal_workspace_option_is_not_warned_about_when_agents_run_in_terminals(self):
        # Terminals mode uses it as the folder of the owner's Workspace shell.
        with mock.patch.object(service_main, "instance_paths_from_arguments", return_value=SimpleNamespace(runtime=None)), \
                mock.patch.object(mode, "current", return_value="terminal"), \
                self.assertLogs("qorqut.service.main", level="WARNING") as logged:
            self.assertEqual(self.start("--terminal-workspace", "/srv/qorqut/terminal", "--port", "1"), 1)
        self.assertEqual([record.levelname for record in logged.records], ["ERROR"])

    def test_the_option_still_warns_when_the_setting_says_managed(self):
        with mock.patch.object(service_main, "instance_paths_from_arguments", return_value=SimpleNamespace(runtime=None)), \
                mock.patch.object(mode, "current", return_value="managed"), \
                self.assertLogs("qorqut.service.main", level="WARNING") as logged:
            self.start("--terminal-workspace", "/srv/qorqut/terminal", "--port", "1")
        self.assertEqual(len([record for record in logged.records if record.levelname == "WARNING"]), 1)

    def test_an_unknown_option_is_still_refused(self):
        with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
            self.start("--no-such-option")
        self.assertEqual(refused.exception.code, 2)

    def test_no_warning_without_the_option(self):
        with self.assertLogs("qorqut.service.main", level="WARNING") as logged:
            self.assertEqual(self.start("--port", "1"), 1)
        self.assertEqual([record.levelname for record in logged.records], ["ERROR"])


if __name__ == "__main__":
    unittest.main()
