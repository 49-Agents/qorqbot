"""Real-ledger concurrency regressions; no browser, provider or live state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engine_email.models import IntegrationClient, IntentSubmission, Mailbox, Settings
from engine_email.service import EmailService, IntentCancelled, IntentConflict
from engine_email.store import Store


class QueueConcurrencyTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = IntegrationClient(client_id='paper', secret='i' * 40,
            workflow_owner='paper', allowed_mailboxes=['project'], project_ids=['project'])
        settings = Settings(database=Path(directory.name) / 'mail.sqlite3',
            read_token='r' * 40, send_token='s' * 40, api_token='test-only',
            integrations=(self.client,), mailboxes=(Mailbox(ref='project', folder_id='folder',
                address='sender@example.test', project_id='project'),))
        self.service = EmailService(settings, Store(settings.database), SimpleNamespace(account_id='fake'))
        self.deadline = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    def submission(self, identity, body='Approved text'):
        return IntentSubmission.model_validate(dict(intent_id=identity, category='cold_outreach',
            expires_at=self.deadline, message=dict(request_id=identity, project_id='project',
                mailbox='project', from_address='sender@example.test', to=['recipient@example.test'],
                subject='Test', body=body, expires_at=self.deadline), correlation=dict(
                workflow_owner='paper', project_id='project',
                stop_key=dict(contact_key='recipient@example.test', project_id='project'))))

    def enqueue(self, identity):
        return self.service.submit_intent(self.client, self.submission(identity))

    def test_enqueue_lookup_and_duplicate_complete_while_provider_is_blocked(self):
        first = self.enqueue('first')
        entered, release = Event(), Event()
        def slow_provider(row, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release provider')
            return Store.receipt(row)
        with patch.object(self.service, 'reconcile', side_effect=slow_provider), ThreadPoolExecutor(4) as pool:
            worker = pool.submit(self.service.reconcile_pending)
            try:
                self.assertTrue(entered.wait(2))
                queued = pool.submit(self.enqueue, 'second').result(timeout=2)
                self.assertEqual(queued['state'], 'queued')
                self.assertFalse(queued['transmission_began'])
                self.assertEqual(pool.submit(self.enqueue, 'second').result(timeout=2), queued)
                got = pool.submit(self.service.intent, self.client, 'first').result(timeout=2)
                self.assertEqual(got['request_id'], first['request_id'])
                with self.assertRaises(IntentConflict):
                    pool.submit(self.service.submit_intent, self.client,
                                self.submission('second', 'Changed text')).result(timeout=2)
            finally:
                release.set()
            worker.result(timeout=2)
        with self.service.store.connect() as db:
            self.assertEqual(db.execute('select count(*) from sends').fetchone()[0], 2)

    def test_cancel_and_enqueue_remain_serialized(self):
        self.service.cancel_intent(self.client, 'cancelled')
        with self.assertRaises(IntentCancelled):
            self.enqueue('cancelled')
        with ThreadPoolExecutor(8) as pool:
            for index in range(20):
                identity = 'race-' + str(index)
                submit = pool.submit(self.enqueue, identity)
                cancel = pool.submit(self.service.cancel_intent, self.client, identity)
                try:
                    submit.result(timeout=2)
                except IntentCancelled:
                    pass
                receipt = cancel.result(timeout=2)
                self.assertEqual(receipt['state'], 'cancelled')
                self.assertFalse(receipt['transmission_began'])
                self.assertEqual(self.service.intent(self.client, identity)['state'], 'cancelled')

    def test_lookup_cannot_observe_half_written_intent(self):
        entered, release, lookup_started = Event(), Event(), Event()
        original = self.service.store.begin_send
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release ledger write')
            return original(*args, **kwargs)
        def lookup():
            lookup_started.set()
            return self.service.intent(self.client, 'new')
        with patch.object(self.service.store, 'begin_send', side_effect=delayed), ThreadPoolExecutor(2) as pool:
            submit = pool.submit(self.enqueue, 'new')
            try:
                self.assertTrue(entered.wait(2))
                receipt = pool.submit(lookup)
                self.assertTrue(lookup_started.wait(2))
                self.assertFalse(receipt.done())
            finally:
                release.set()
            self.assertEqual(submit.result(timeout=2), receipt.result(timeout=2))

    def test_batch_reloads_rows_cancelled_since_snapshot(self):
        first, second = self.enqueue('first'), self.enqueue('second')
        seen = []
        def reconcile(row, **kwargs):
            seen.append((row['request_id'], row['state']))
            if row['request_id'] == first['request_id']:
                self.service.store.cancel_send_if_queued(second['request_id'])
            return Store.receipt(row)
        with patch.object(self.service, 'reconcile', side_effect=reconcile):
            self.service.reconcile_pending()
        self.assertEqual(seen, [(first['request_id'], 'queued'), (second['request_id'], 'canceled')])


if __name__ == '__main__':
    unittest.main()
