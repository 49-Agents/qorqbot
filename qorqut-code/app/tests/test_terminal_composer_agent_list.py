import unittest

from qorqut.transport.terminal_composer import read

# The rows are the escape sequences of ``tmux capture-pane -p -e`` on Claude Code 2.1.287 with one background agent running
# (the conversation text and the agent's description are replaced). The ``● main`` row is bold in the default colour.
RULE = '\x1b[37m' + '─' * 60
FOOTER = '\x1b[39m  \x1b[91m⏵⏵ bypass permissions on\x1b[2m\x1b[37m · \x1b[0m\x1b[96m1 monitor\x1b[39m'
RUNNING = ('\x1b[39m  \x1b[91m⏵⏵ bypass permissions on\x1b[2m\x1b[37m · \x1b[0m\x1b[96m1 monitor\x1b[2m\x1b[37m'
           ' · esc to interrupt · ↓ to manage\x1b[0m')
MAIN = '  \x1b[1m● main\x1b[0m'
TEXT = 'move what you can right now. what is still open?'


def agent(description='Preview the moves · since 1…', marker='◯', style='\x1b[2m\x1b[37m'):
    return f'  {style}{marker} general-purpose\x1b[0m  {style}waiting on 1 monitor · {description}\x1b[0m'


def screen(content, below, footer=FOOTER):
    """The capture and the composer's row: an earlier answer, the input box holding ``content``, the footer and ``below``."""
    lines = ['● Earlier answer.', '', RULE, '\x1b[39m❯\xa0' + content, RULE, footer, *below]
    return '\n'.join(lines), 3


class AgentListTest(unittest.TestCase):
    def test_paste_above_the_agent_list_is_recognized(self):
        for below in (['', MAIN, agent()], ['', MAIN, agent(), agent('Find the contract mail')], ['', MAIN]):
            raw, row = screen(TEXT, below)
            with self.subTest(agents=len(below) - 2):
                reading = read('claude', raw, (2 + len(TEXT), row, True), expect=TEXT, allow_busy=True)
                self.assertEqual((reading.state, reading.reason, reading.busy), ('pasted', 'paste_in_composer', False))

    def test_paste_above_the_agent_list_under_a_running_turn(self):
        raw, row = screen(TEXT, ['', MAIN, agent()], footer=RUNNING)
        cursor = (2 + len(TEXT), row, True)
        reading = read('claude', raw, cursor, expect=TEXT, allow_busy=True)
        self.assertEqual((reading.state, reading.busy), ('pasted', True))
        self.assertEqual(read('claude', raw, cursor, expect=TEXT).state, 'busy')

    def test_empty_composer_above_the_agent_list_is_ready(self):
        raw, row = screen('', ['', MAIN, agent()])
        self.assertEqual(read('claude', raw, (2, row, True)).state, 'empty')
        raw, row = screen('', ['', MAIN, agent()], footer=RUNNING)
        self.assertEqual(read('claude', raw, (2, row, True)).state, 'busy')
        reading = read('claude', raw, (2, row, True), allow_busy=True)
        self.assertEqual((reading.state, reading.busy), ('empty', True))

    def test_an_agent_description_is_not_read_as_a_dialog_or_a_running_turn(self):
        for description in ('Sign in and add the card', 'Do you want to keep it', 'esc to interrupt the old run'):
            with self.subTest(description=description):
                raw, row = screen('', ['', MAIN, agent(description)])
                self.assertEqual(read('claude', raw, (2, row, True)).state, 'empty')
                raw, row = screen(TEXT, ['', MAIN, agent(description)])
                reading = read('claude', raw, (2 + len(TEXT), row, True), expect=TEXT, allow_busy=True)
                self.assertEqual((reading.state, reading.busy), ('pasted', False))

    def test_a_dialog_or_interrupting_footer_still_wins_over_the_agent_list(self):
        for footer in ('\x1b[2mDo you trust this folder? Enter to confirm\x1b[0m', 'Enter to interrupt and send'):
            raw, row = screen(TEXT, ['', MAIN, agent()], footer=footer)
            with self.subTest(footer=footer):
                reading = read('claude', raw, (2 + len(TEXT), row, True), expect=TEXT, allow_busy=True)
                self.assertNotEqual(reading.state, 'pasted')

    def test_anything_that_is_not_exactly_the_list_stays_unrecognized(self):
        cases = {
            'the composer talks to an agent': ['', '  \x1b[2m\x1b[37m◯ main\x1b[0m', agent(marker='●', style='\x1b[1m')],
            'an agent row in another style': ['', MAIN, agent(style='\x1b[1m')],
            'a plain row after the list': ['', MAIN, agent(), 'Choose an agent to open'],
            'a plain row between footer and list': ['Choose an agent to open', '', MAIN, agent()],
            'no blank row before the list': [MAIN, agent()],
            'another label on the filled marker': ['', '  \x1b[1m● main thread\x1b[0m', agent()],
        }
        for name, below in cases.items():
            raw, row = screen(TEXT, below)
            with self.subTest(case=name):
                reading = read('claude', raw, (2 + len(TEXT), row, True), expect=TEXT, allow_busy=True)
                self.assertEqual((reading.state, reading.reason), ('text', 'paste_not_in_composer'))

    def test_the_same_rows_typed_inside_the_input_box_are_text(self):
        typed = TEXT + '\n\n  ● main\n  ◯ general-purpose  waiting'
        lines = ['● Earlier answer.', '', RULE, *('\x1b[39m❯\xa0' + typed).split('\n'), RULE, FOOTER]
        reading = read('claude', '\n'.join(lines), (2, 4, True), expect=TEXT, allow_busy=True)
        self.assertEqual((reading.state, reading.reason), ('text', 'paste_not_in_composer'))

    def test_codex_has_no_agent_list(self):
        raw = '\n'.join(['Earlier answer', '› ' + TEXT, '', '  ? for shortcuts', '', '  ● main', agent()])
        self.assertEqual(read('codex', raw, (2 + len(TEXT), 1, True), expect=TEXT).state, 'text')


if __name__ == '__main__':
    unittest.main()
