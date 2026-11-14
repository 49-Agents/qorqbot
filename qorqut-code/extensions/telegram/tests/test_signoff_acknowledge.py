"""Telegram announces something to read and takes comments on it, but never acknowledges it."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut_operations.signoffs.inputs import validate_form
from qorqut_telegram.signoffs import SignoffBridge, form_of


class AcknowledgeSignoffTelegramTests(unittest.TestCase):
    def setUp(self):
        self.form = validate_form({'kind': 'acknowledge', 'title': 'Daily report, 12 Nov', 'selection_mode': 'none',
                                   'explanation': 'Nothing was dropped. The Linden contract was sent.'})
        self.row = {'id': 'example', 'category': 'input_request', 'state': 'pending', 'superseded_by_id': None,
                    'proposal_revision': 1, 'payload_hash': 'hash', 'status_revision': 1, 'preview_json': {'input': self.form}}
        self.bridge = object.__new__(SignoffBridge)

    def test_the_post_says_to_read_and_a_reply_is_only_a_comment(self):
        self.assertEqual(form_of(self.row), 'input')
        self.assertFalse(SignoffBridge.answers_alone(self.row, 'Read it, thanks'))
        text = self.bridge.render({'category': 'input_request', 'title': self.form['title'], 'input': self.form,
            'preview': {'input': self.form}}, 'input', 'https://example.test/signoffs')
        self.assertTrue(text.startswith('To read: Daily report, 12 Nov'))
        self.assertIn('The Linden contract was sent.', text)
        self.assertIn('Acknowledge it in the dashboard. Replies add comments; reactions do not acknowledge.', text)
        self.assertNotIn('Reply to this message with your answer', text)

    def test_a_reaction_does_not_acknowledge(self):
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
        self.assertIn('a reaction does not acknowledge it', self.bridge._notice.call_args.args[3])


if __name__ == '__main__': unittest.main()
