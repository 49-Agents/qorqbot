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
            validate_arguments(["teleport"])
        message = str(raised.exception)
        self.assertIn("'teleport' is not a supported browser command", message)
        self.assertIn("frame", message)
        self.assertIn("click", message)

    def test_a_command_closed_on_purpose_says_so_instead_of_listing(self):
        with self.assertRaises(BrowserError) as raised:
            validate_arguments(["launch"])
        self.assertIn("'launch' is closed on purpose", str(raised.exception))




class FrameAdviceTest(unittest.TestCase):
    def test_a_frame_that_is_not_found_gets_the_way_to_reach_its_fields(self):
        told = agent_browser.frame_advice(["frame", "iframe[title*=payment]"], "✗ Frame not found\n")
        self.assertTrue(told.startswith("✗ Frame not found. "))
        self.assertIn("snapshot", told)
        self.assertIn(agent_browser.SHOW_FRAMES, told)
        validate_arguments(["eval", agent_browser.SHOW_FRAMES])

    def test_other_output_is_left_alone(self):
        self.assertEqual(agent_browser.frame_advice(["click", "e1"], "✗ Frame not found\n"), "✗ Frame not found\n")
        self.assertEqual(agent_browser.frame_advice(["frame", "main"], ""), "")


if __name__ == "__main__":
    unittest.main()
