"""A start command written for an earlier version still starts the service."""
import contextlib
import io
import unittest

from qorqut.service import main as service_main


class DeprecatedOptionsTest(unittest.TestCase):
    def start(self, *arguments):
        # Without --workspace the service refuses to assemble: reaching that refusal (status 1) means every option parsed.
        return service_main.main(list(arguments))

    def test_the_removed_terminal_workspace_option_is_accepted_ignored_and_warned_about_once(self):
        with self.assertLogs("qorqut.service.main", level="WARNING") as logged:
            status = self.start("--terminal-workspace", "/srv/qorqut/terminal", "--port", "1")
        self.assertEqual(status, 1)
        warnings = [record for record in logged.records if record.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("--terminal-workspace", warnings[0].getMessage())
        self.assertIn("no effect", warnings[0].getMessage())

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
