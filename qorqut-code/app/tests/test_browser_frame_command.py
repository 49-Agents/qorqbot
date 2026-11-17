"""Agents can step into iframes, and unsupported commands are named in the rejection."""

import tempfile
import unittest

from qorqut.bootstrap import agent_browser
from qorqut.bootstrap.browser import BrowserError, validate_arguments


class FrameCommandTest(unittest.TestCase):
    def translate(self, args):
        with tempfile.TemporaryDirectory() as directory:
            command = agent_browser.command("agent-browser", "session", "http://cdp", directory, args)
        return command[command.index("http://cdp") + 1:]

    def test_frame_is_an_allowed_page_command(self):
        validate_arguments(["frame", "iframe[title='Secure card number input frame']"])
        validate_arguments(["frame", "main"])

    def test_frame_translates_selectors_refs_and_main(self):
        self.assertEqual(self.translate(["frame", "iframe[name=card]"]), ["frame", "iframe[name=card]"])
        self.assertEqual(self.translate(["frame", "e12"]), ["frame", "@e12"])
        self.assertEqual(self.translate(["frame", "main"]), ["frame", "main"])

    def test_frame_needs_exactly_one_target(self):
        for args in (["frame"], ["frame", "a", "b"]):
            with self.assertRaises(ValueError):
                self.translate(args)

    def test_frame_does_not_accept_session_overrides(self):
        with self.assertRaises(BrowserError):
            validate_arguments(["frame", "--cdp=http://elsewhere"])

    def test_rejection_names_the_command_and_lists_supported_ones(self):
        with self.assertRaises(BrowserError) as raised:
            validate_arguments(["launch"])
        message = str(raised.exception)
        self.assertIn("'launch' is not a supported browser command", message)
        self.assertIn("frame", message)
        self.assertIn("click", message)


if __name__ == "__main__":
    unittest.main()
