import unittest

from qorqut.transport import terminal_composer as tc

RULE = "\x1b[38;5;245m" + "─" * 66 + "\x1b[39m"
HINT = "\x1b[39m❯\xa0\x1b[38;5;105m/compact\x1b[39m  \x1b[38;5;241m<optional custom summarization instructions>\x1b[39m"
FOOTER = "  \x1b[38;5;137m⏵⏵ bypass permissions on\x1b[38;5;241m (shift+tab to cycle)\x1b[39m"


class CommandHintTest(unittest.TestCase):
    """Captured from Claude Code 2.1.318 after Chat pasted "/compact " (the trailing space closes the command menu)."""

    def screen(self, prompt):
        return "\n".join(["\x1b[39m● Earlier answer", RULE, prompt, RULE, FOOTER])

    def test_the_argument_hint_is_not_part_of_the_paste(self):
        reading = tc.read("claude", self.screen(HINT), (11, 2, True), expect="/compact ")
        self.assertEqual((reading.state, reading.reason), ("pasted", "paste_in_composer"))

    def test_typed_text_after_the_command_still_has_to_match(self):
        typed = "\x1b[39m❯\xa0\x1b[38;5;105m/compact\x1b[39m keep only decisions"
        self.assertEqual(tc.read("claude", self.screen(typed), (30, 2, True), expect="/compact keep only decisions").state, "pasted")
        self.assertEqual(tc.read("claude", self.screen(typed), (30, 2, True), expect="/compact ").state, "text")


if __name__ == "__main__":
    unittest.main()
