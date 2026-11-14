"""The reminder sent to an agent that holds Todo work without a focus tells it to set one."""
import unittest
from pathlib import Path

import qorqut_agentic
from qorqut_agentic.behavior import attention
from qorqut_agentic.behavior.instructions import from_catalog

CATALOG = Path(qorqut_agentic.__file__).parent / 'resources/defaults/operations/runtime-instructions'
TASK = {'id': 'task-1', 'business_id': None, 'title': 'Example task', 'status': 'working', 'workers': [],
        'workers_omitted': 0, 'user_needed': False}


class UnfocusedReminderTest(unittest.TestCase):
    def setUp(self):
        self.instructions = from_catalog(lambda name: (CATALOG / name).read_text())

    def snapshot(self, focus=None):
        return {'focus': focus, 'focused_for': '5 minutes' if focus else None, 'work': TASK if focus else None,
                'next_step': 'Next', 'intent': '', 'candidates': [TASK], 'more': False}

    def test_board_reminders_ask_an_unfocused_agent_to_set_its_focus(self):
        for kind in ('board_normal', 'board_reassess'):
            text = attention.render(kind, self.snapshot(), instructions=self.instructions)
            self.assertIn('You have no focus set.', text)
            self.assertIn('attention_focus_set', text)
            self.assertIn('Focus: none', text)
            # The instruction is part of the lead, above the quoted read-only snapshot.
            self.assertLess(text.index('attention_focus_set'), text.index('Todo snapshot'))

    def test_the_request_survives_an_unavailable_snapshot(self):
        self.assertIn('attention_focus_set', attention.render('board_normal', None, instructions=self.instructions))

    def test_a_focused_agent_is_not_told_it_has_no_focus(self):
        text = attention.render('focus', self.snapshot({'type': 'task', 'id': 'task-1', 'business_id': None}),
                                instructions=self.instructions)
        self.assertNotIn('You have no focus set.', text)


if __name__ == '__main__':
    unittest.main()
