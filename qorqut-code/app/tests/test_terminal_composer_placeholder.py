import unittest

from bazarlyk.transport import terminal_composer as tc

RULE = "\x1b[38;5;245m" + "─" * 66 + "\x1b[39m"
FOOTER = "  \x1b[38;5;137m⏵⏵ bypass permissions on\x1b[38;5;241m (shift+tab to cycle)\x1b[39m"
PLAIN, DIM, GREY, END = "\x1b[39m", "\x1b[2m", "\x1b[38;5;241m", "\x1b[0m"
TEXT = "first\nsecond\nthird"
PLACEHOLDER = "[Pasted text #1 +2 lines]"


def claude(prompt, *between):
    return "\n".join(["\x1b[39m● Earlier answer", RULE, prompt, *between, RULE, FOOTER])


def row(content, style=PLAIN):
    return f"{PLAIN}❯\xa0{style}{content}{END}"


class PlaceholderTest(unittest.TestCase):
    """A collapsed paste is recognised as the whole composer row, whether the client draws it plain, dim or grey."""

    def read(self, screen, expect=TEXT, cursor=(27, 2, True)):
        reading = tc.read("claude", screen, cursor, expect=expect)
        return reading.state, reading.reason

    def test_a_placeholder_in_any_styling_is_the_paste(self):
        for style in (PLAIN, DIM, GREY, "\x1b[38;2;120;120;120m", "\x1b[90m"):
            with self.subTest(style=repr(style)):
                self.assertEqual(self.read(claude(row(PLACEHOLDER, style))), ("pasted", "paste_in_composer"))

    def test_a_placeholder_without_a_count_or_with_a_count_that_fits(self):
        self.assertEqual(self.read(claude(row("[Pasted text #2]", DIM))), ("pasted", "paste_in_composer"))
        for count in (2, 3, 4):
            with self.subTest(count=count):
                self.assertEqual(self.read(claude(row(f"[Pasted text #1 +{count} lines]", DIM))), ("pasted", "paste_in_composer"))
        chars = "x" * 1002
        self.assertEqual(self.read(claude(row("[Pasted text #1 1002 chars]", DIM)), expect=chars), ("pasted", "paste_in_composer"))

    def test_codex_placeholder_in_any_styling_is_the_paste(self):
        chars = "word " * 200
        for style in (PLAIN, DIM, GREY):
            screen = "\n".join(["• ok", "", f"{PLAIN}›{END} {style}[Pasted Content {len(chars)} chars]{END}", "", "  ? for shortcuts"])
            with self.subTest(style=repr(style)):
                reading = tc.read("codex", screen, None, expect=chars)
                self.assertEqual((reading.state, reading.reason), ("pasted", "paste_in_composer"))

    def test_a_count_that_does_not_fit_the_text_is_not_the_paste(self):
        for style in (PLAIN, DIM):
            for content in ("[Pasted text #1 +9 lines]", "[Pasted text #1 +0 lines]", "[Pasted text #1 5000 chars]"):
                with self.subTest(style=repr(style), content=content):
                    self.assertEqual(self.read(claude(row(content, style))), ("text", "paste_not_in_composer"))

    def test_extra_visible_text_makes_it_something_else(self):
        for screen in (claude(row(PLACEHOLDER + " and more")), claude(row("typed " + PLACEHOLDER)),
                       claude(row(PLACEHOLDER, DIM) + PLAIN + " typed"), claude(row(PLACEHOLDER, DIM) + END + " typed")):
            with self.subTest(screen=screen.splitlines()[2]):
                self.assertEqual(self.read(screen), ("text", "paste_not_in_composer"))

    def test_a_dim_suggestion_beside_the_placeholder_is_not_part_of_it(self):
        for style in (PLAIN, DIM):
            suggestion = f"{PLAIN}❯\xa0{style}{PLACEHOLDER}{END} {DIM}run the tests{END}"
            with self.subTest(style=repr(style)):
                self.assertEqual(self.read(claude(suggestion)), ("text", "paste_not_in_composer"))

    def test_a_dim_suggestion_alone_is_not_the_paste(self):
        for suggestion in ("run the tests", "[Pasted", "[Pasted text #1 +2 lines] run the tests"):
            with self.subTest(suggestion=suggestion):
                self.assertEqual(self.read(claude(row(suggestion, DIM))), ("text", "paste_not_in_composer"))
        self.assertEqual(self.read(claude(row("", DIM))), ("text", "paste_not_in_composer"))

    def test_a_menu_row_is_not_the_paste(self):
        for content in ("1. " + PLACEHOLDER, "1) " + PLACEHOLDER):
            for style in (PLAIN, DIM):
                with self.subTest(content=content, style=repr(style)):
                    self.assertEqual(self.read(claude(row(content, style))), ("text", "paste_not_in_composer"))

    def test_the_composer_checks_still_apply_to_a_placeholder(self):
        # Typed text on a row below the placeholder, a cursor outside the composer, and a dialog under it all stay refused.
        self.assertEqual(self.read(claude(row(PLACEHOLDER, DIM), "more typed text")), ("text", "paste_not_in_composer"))
        self.assertEqual(self.read(claude(row(PLACEHOLDER, DIM)), cursor=(27, 4, True)), ("text", "paste_not_in_composer"))
        dialog = "\n".join([claude(row(PLACEHOLDER, DIM)), " Do you want to proceed?"])
        self.assertEqual(self.read(dialog), ("text", "paste_not_in_composer"))

    def test_the_command_hint_after_a_command_is_still_not_part_of_the_paste(self):
        hint = f"{PLAIN}❯\xa0\x1b[38;5;105m/compact{PLAIN}  {GREY}<optional custom summarization instructions>{PLAIN}"
        reading = tc.read("claude", claude(hint), (11, 2, True), expect="/compact ")
        self.assertEqual((reading.state, reading.reason), ("pasted", "paste_in_composer"))
        self.assertEqual(tc.read("claude", claude(hint), (11, 2, True), expect="/compact keep").state, "text")


if __name__ == "__main__":
    unittest.main()
