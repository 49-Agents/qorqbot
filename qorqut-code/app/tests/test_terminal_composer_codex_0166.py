import unittest

from bazarlyk.transport import terminal_composer

# The bottom of a real Codex 0.166 screen with "hello there friend" pasted into the composer: a status line, then a
# warnings row that replaces "? for shortcuts" while the composer holds text.
SCREEN = (
    "\x1b[2m• \x1b[0mok\n\n\x1b[2m  Worked for 2s • 11:59 AM\n\x1b[0m\n\n"
    "\x1b[1m›\x1b[0m hello there friend\n\n"
    "  \x1b[38;2;246;226;183mGPT-6-Luna high\x1b[39m · \x1b[38;2;171;223;167m/home/owner/agents/ordubashy\x1b[39m · "
    "\x1b[38;2;137;180;250mDefine …\n"
    "\x1b[39m" + " " * 94 + "⚠ \x1b[38;2;196;167;103m2 warnings\x1b[39m · \x1b[1mf2\x1b[0m to view\n"
)


class CodexWarningsRowTest(unittest.TestCase):
    def test_paste_is_recognised_above_the_warnings_row(self):
        reading = terminal_composer.read("codex", SCREEN, None, expect="hello there friend")
        self.assertEqual((reading.state, reading.reason), ("pasted", reading.reason))

    def test_different_text_is_not_taken_for_the_paste(self):
        reading = terminal_composer.read("codex", SCREEN, None, expect="something else")
        self.assertNotEqual(reading.state, "pasted")


if __name__ == "__main__":
    unittest.main()
