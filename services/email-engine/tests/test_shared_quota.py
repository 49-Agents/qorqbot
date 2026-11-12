"""Offline checks for the shared cap; no provider, browser, or live ledger."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from engine_email.models import Outgoing, SharedSender
from engine_email.store import Store


class SharedQuotaTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'email.sqlite3'
        self.store = Store(self.path)
        self.senders = [SharedSender(ref='sender-' + str(i),
            address=f'sender-{i}@example.test', account_ref='account-' + str(i),
            business_scope='all', enabled=True, rolling_limit=500) for i in range(2)]

    def enqueue(self, identity, sender):
        message = Outgoing(request_id=identity, business_id='business-' + sender.ref,
            mailbox='archive-' + sender.ref, from_address=sender.address,
            sender_ref=sender.ref, to=['recipient@example.test'],
            subject='Synthetic quota check', body='Never submitted to a provider.')
        self.store.begin_send(message, queued=True)

    def reserve_many(self, count):
        for index in range(count):
            sender = self.senders[index % 2]
            identity = 'message-' + str(index)
            self.enqueue(identity, sender)
            admitted, _row = self.store.reserve(identity, sender)
            self.assertTrue(admitted, identity)

    def test_220_total_across_accounts_then_221st_waits_without_duplicate_reservation(self):
        self.reserve_many(220)
        self.enqueue('over-cap', self.senders[0])
        admitted, row = self.store.reserve('over-cap', self.senders[0])
        self.assertFalse(admitted)
        self.assertIn('shared_sender_quota', row['result'])
        quota = self.store.shared_quota()
        self.assertEqual(quota['limit'], 220)
        self.assertEqual(quota['window_hours'], 24)
        self.assertEqual(quota['used'], 220)
        self.assertEqual(quota['remaining'], 0)
        self.assertEqual(quota['queued'], 1)
        self.assertTrue(self.store.reserve('message-0', self.senders[0])[0])
        self.assertEqual(self.store.shared_quota()['used'], 220)

    def test_two_stores_compete_for_last_shared_slot_atomically(self):
        self.reserve_many(219)
        other_store = Store(self.path)
        for i, sender in enumerate(self.senders):
            self.enqueue('last-slot-' + str(i), sender)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(store.reserve, 'last-slot-' + str(i), self.senders[i])
                for i, store in enumerate((self.store, other_store))]
            results = [future.result(timeout=10)[0] for future in futures]
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.shared_quota()['used'], 220)
        self.assertEqual(other_store.shared_quota()['remaining'], 0)

    def test_lower_sender_limit_still_wins(self):
        sender = self.senders[0].model_copy(update={'rolling_limit': 12})
        for index in range(12):
            identity = 'lower-' + str(index)
            self.enqueue(identity, sender)
            self.assertTrue(self.store.reserve(identity, sender)[0])
        self.enqueue('sender-full', sender)
        admitted, row = self.store.reserve('sender-full', sender)
        self.assertFalse(admitted)
        self.assertIn('sender_quota', row['result'])
        self.assertNotIn('shared_sender_quota', row['result'])
        self.assertEqual(self.store.shared_quota()['remaining'], 208)


if __name__ == '__main__':
    unittest.main()
