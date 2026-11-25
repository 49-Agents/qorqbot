"""Freshness and sender-facing health without provider calls or production state."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine_email.outlook_health import read_health
from engine_email.service import EmailService
from engine_email import routes


class Sender:
    ref = 'sender'
    account_ref = 'account'
    transport = 'outlook_web'
    enabled = True
    def permits_project(self, project):
        return True
    def model_dump(self):
        return {'ref': self.ref, 'transport': self.transport}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'health.json'
        self.service = EmailService.__new__(EmailService)
        self.service.settings = SimpleNamespace(outlook_health_file=self.path, shared_senders=(Sender(),))
        self.service.store = SimpleNamespace(shared_quota=lambda: {})
        self.provider_status = {'available': True, 'last_checked': None, 'error': None}
        self.service.outlook_provider = SimpleNamespace(status=lambda: self.provider_status)

    def write(self, **overrides):
        self.path.write_text(json.dumps({'account_ref': 'account', 'status': 'healthy',
                                        'checked_at': 10000, 'reason': 'healthy', **overrides}))

    def test_stale_missing_invalid_and_wrong_account_are_unknown(self):
        self.assertEqual(read_health(self.path, 'account', now=10000)['status'], 'unknown')
        for overrides in ({'checked_at': 9879}, {'checked_at': 10001},
                          {'checked_at': float('nan')}, {'checked_at': True},
                          {'account_ref': 'other'}, {'reason': 'private arbitrary text'},
                          {'status': 'unexpected'}):
            with self.subTest(overrides=overrides):
                self.write(**overrides)
                self.assertEqual(read_health(self.path, 'account', now=10000)['status'], 'unknown')
        for invalid in ('not json', '[]', 'null', '"text"', 'x' * 4097):
            self.path.write_text(invalid)
            self.assertEqual(read_health(self.path, 'account', now=10000)['status'], 'unknown')

    def test_fresh_healthy_and_login_results(self):
        for status, reason in (('healthy', 'healthy'), ('login_required', 'login_required'),
                               ('unavailable', 'connection_failed')):
            self.write(status=status, reason=reason)
            result = read_health(self.path, 'account', now=10120)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['reason'], reason)
            self.assertIn('+00:00', result['checked_at'])

    def test_cached_provider_success_cannot_override_dead_tab_or_stale_probe(self):
        with patch('engine_email.outlook_health.time.time', return_value=10000):
            for snapshot in ({'status': 'unavailable', 'reason': 'connection_failed'},
                             {'status': 'login_required', 'reason': 'login_required'},
                             {'checked_at': 9000}):
                self.write(**snapshot)
                sender = self.service.shared_sender_metadata()[0]
                self.assertFalse(sender['send_available'])
                self.assertNotEqual(sender['health']['status'], 'healthy')

    def test_newer_provider_failure_is_not_hidden_by_healthy_ui(self):
        self.write()
        self.provider_status.update(available=False, last_checked='1970-01-01T02:46:41+00:00',
                                    error='outlook_session_unavailable')
        with patch('engine_email.outlook_health.time.time', return_value=10002):
            sender = self.service.shared_sender_metadata()[0]
        self.assertFalse(sender['send_available'])
        self.assertEqual(sender['health']['reason'], 'outlook_session_unavailable')
        # A subsequent UI probe still does not prove the provider session works.
        self.write(checked_at=10002)
        with patch('engine_email.outlook_health.time.time', return_value=10003):
            self.assertFalse(self.service.shared_sender_metadata()[0]['send_available'])
            self.provider_status.update(available=True, error=None)
            self.assertTrue(self.service.shared_sender_metadata()[0]['send_available'])

    def test_route_exposes_health_without_changing_route_pin(self):
        self.write(status='unavailable', reason='connection_failed')
        row = dict(sender_ref='sender', project_id='project', state='ready', active=1,
                   blocker=None, route_id='route', revision=7, generation=2, client_id='paper',
                   mailbox_ref='archive', purpose='outreach', updated_at='unchanged')
        with patch('engine_email.outlook_health.time.time', return_value=10000):
            response = routes.view(self.service, row)
        self.assertTrue(response['ready'])  # Durable route still exists.
        self.assertEqual(response['revision'], 7)
        self.assertIsNone(response['blocker'])
        self.assertFalse(response['send_available'])
        self.assertEqual(response['sender_health']['status'], 'unavailable')
        self.write()
        with patch('engine_email.outlook_health.time.time', return_value=10000):
            self.assertFalse(routes.view(self.service, {**row, 'active': 0})['send_available'])

    def test_opt_in_preserves_unmonitored_installations_and_fastmail(self):
        self.service.settings.outlook_health_file = None
        self.assertTrue(self.service.shared_sender_metadata()[0]['send_available'])
        self.assertNotIn('health', self.service.shared_sender_metadata()[0])
        self.assertIsNone(self.service.sender_health(SimpleNamespace(transport='fastmail')))


if __name__ == '__main__':
    unittest.main()
