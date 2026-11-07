import unittest

from bazarlyk.transport.terminal_composer import read


class TrailingLinesTest(unittest.TestCase):
    def screen(self, client, content, *, footer='  ? for shortcuts'):
        if client == 'codex':
            return '\n'.join(['Earlier answer', '› ' + content, '', footer]), 1
        rule = '─' * 60
        return '\n'.join(['Earlier answer', rule, '❯ ' + content, rule, footer]), 2

    def test_cursor_on_a_pasted_trailing_blank_line_is_inside_the_composer(self):
        for client in ('codex', 'claude'):
            for suffix in ('\n', '\n\n', '\n  ', '\r\n'):
                text = 'First task\nSecond task' + suffix
                raw, first = self.screen(client, text)
                cursor = (2, first + text.count('\n'), True)
                with self.subTest(client=client, suffix=suffix):
                    self.assertEqual(read(client, raw, cursor, expect=text).state, 'pasted')

    def test_unexpected_blank_row_or_cursor_on_footer_is_not_a_paste(self):
        for client in ('codex', 'claude'):
            for expected, actual, delta in (
                ('Task', 'Task\n', 1),
                ('Task\n', 'Task\n\n', 2),
                ('Task\n', 'Task\n', 2),
                ('Task\n', 'Task\nextra', 1),
                ('Task\n', 'Task\n\x1b[2msuggestion\x1b[0m', 1),
                ('Task\n', '[Pasted Content 5 chars]\n', 1),
            ):
                raw, first = self.screen(client, actual)
                with self.subTest(client=client, expected=expected, actual=actual, delta=delta):
                    self.assertNotEqual(read(client, raw, (2, first + delta, True), expect=expected).state, 'pasted')

    def test_trailing_line_does_not_bypass_modal_or_interrupt_checks(self):
        for client in ('codex', 'claude'):
            for footer in ('\x1b[2mDo you trust this folder? Enter to confirm\x1b[0m', 'Enter to interrupt and send'):
                raw, first = self.screen(client, 'Task\n', footer=footer)
                with self.subTest(client=client, footer=footer):
                    self.assertNotEqual(read(client, raw, (2, first + 1, True), expect='Task\n', allow_busy=True).state, 'pasted')


if __name__ == '__main__':
    unittest.main()
