"""Telegram may discuss a grouped form, but never completes it implicitly."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut_operations.signoffs.inputs import validate_form
from qorqut_telegram.signoffs import SignoffBridge, form_of


class GroupedSignoffTelegramTests(unittest.TestCase):
    def setUp(self):
        self.form = validate_form({'schema_version': 2, 'kind': 'question', 'title': 'Workshop details',
            'explanation': 'Answer together.', 'questions': [{'id': 'room', 'prompt': 'Which room?', 'selection_mode': 'none'},
                                                          {'id': 'time', 'prompt': 'Which time?', 'selection_mode': 'none'}]})
        self.row = {'id': 'example', 'category': 'input_request', 'state': 'pending', 'superseded_by_id': None,
                    'proposal_revision': 1, 'payload_hash': 'hash', 'status_revision': 1, 'preview_json': {'input': self.form}}
        self.bridge = object.__new__(SignoffBridge)

    def test_text_reply_remains_a_comment_and_notification_points_to_dashboard(self):
        self.assertEqual(form_of(self.row), 'input')
        self.assertFalse(SignoffBridge.answers_alone(self.row, 'Room A, Tuesday'))
        text = self.bridge.render({'category': 'input_request', 'title': self.form['title'], 'input': self.form,
            'preview': {'input': self.form}}, 'input', 'https://example.test/signoffs')
        self.assertIn('Answer all 2 questions together in the dashboard', text)
        self.assertIn('Replies add comments; reactions do not submit answers', text)

    def test_reaction_cannot_submit_any_question(self):
        self.bridge.bridge = NS(config=NS(chat=lambda _: {}))
        self.bridge.post_of = Mock(return_value={'input_form': 'input', 'state': 'active', 'signoff_id': 'example',
            'proposal_revision': 1, 'payload_hash': 'hash', 'expected_status_revision': 1, 'decision_json': None})
        self.bridge.signoff = Mock(return_value=self.row)
        self.bridge._record_link = Mock(return_value='https://example.test/signoffs')
        self.bridge._notice = Mock()
        with patch('qorqut_telegram.signoffs.store.message_by_provider', return_value={'id': 'post'}), \
             patch('qorqut_telegram.signoffs.store.in_destination', return_value=True):
            result = self.bridge.route_reaction(None, {'new': ['👍'], 'old': [], 'chat_id': 1, 'message_id': 2, 'update_id': 3}, 'now')
        self.assertEqual(result[:2], ('ignored', 'reaction_not_supported'))
        self.assertIn('reaction does not submit', self.bridge._notice.call_args.args[3])


if __name__ == '__main__': unittest.main()
