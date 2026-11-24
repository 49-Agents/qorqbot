"""Cleanup of known Telegram messages: when a job deletes, when it gives up, and when it waits for the send.

The jobs run against the real Telegram tables in an in-memory database, with a stand-in bridge (clock, configuration and Bot
API); nothing here reaches Telegram.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from qorqut_operations.storage.repositories import UNSET, Repository
from qorqut_telegram import store
from qorqut_telegram.api import TelegramApiError
from qorqut_telegram.bridge import TelegramBridge
from qorqut_telegram.outbox import Cleanup
from qorqut_telegram.schema import IMMUTABLE_FIELDS, SCHEMA, TABLE_SPECS

CHAT = NS(name="ordubashy", chat_id=-1001234567, message_thread_id=7)
FINGERPRINT = "f" * 64


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        for statement in SCHEMA:
            self.connection.execute(statement)
        self.assembly = NS(tables=TABLE_SPECS, immutable_fields=IMMUTABLE_FIELDS)

    def columns(self, table):
        return {row["name"] for row in self.connection.execute(f'PRAGMA table_info("{table}")')}


class Unit:
    project_id = UNSET

    def __init__(self, database):
        self.database, self.connection = database, database.connection

    def repository(self, table):
        return Repository(self, table)

    def require_write(self):
        pass


class Bridge:
    backoff = TelegramBridge.backoff

    def __init__(self):
        self.database = Database()
        self.time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.timings = {"backoff_base": 5.0, "backoff_cap": 900.0, "cleanup_attempts": 8}
        # The top of the jitter range, so every delay is exactly its ceiling.
        self.rng = NS(random=lambda: 1.0)
        self.api = NS(delete_message=Mock())
        self.config = NS(destination=lambda chat_id, thread: CHAT if (chat_id, thread) == (CHAT.chat_id, CHAT.message_thread_id)
                         else None)
        self.sent_hooks = {}
        self.writes = []
        self.wakes = 0

    def current_config(self):
        return self.config

    def now(self):
        return store.iso(self.time)

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)

    def check_binding(self, uow):
        return {}

    def wake(self):
        self.wakes += 1

    @contextmanager
    def write(self, operation):
        self.writes.append(operation)
        yield Unit(self.database)

    @contextmanager
    def read(self):
        yield Unit(self.database)


class CleanupCase(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.uow = Unit(self.bridge.database)
        self.cleanup = Cleanup(self.bridge)

    def message(self, state="sent", provider_id=41):
        """One outbound message and its send in ``state``; a sent one carries its Telegram message ID."""
        now = self.bridge.now()
        row = store.insert_message(self.uow, direction="outbound", chat=CHAT, purpose="notice", body="Status line", now=now)
        outbox, _ = store.enqueue(self.uow, CHAT, 1, FINGERPRINT, source_kind="notice", source_key=row["id"],
                                  payload={"text": "Status line"}, now=now, message_id=row["id"])
        # Through the transitions the schema allows: queued -> dispatching -> sent / uncertain; queued -> failed / cancelled.
        if state in ("dispatching", "uncertain", "sent"):
            outbox = store.set_outbox(self.uow, outbox, state="dispatching", attempts=1, dispatch_started_at=now)
        if state == "sent":
            store.set_outbox(self.uow, outbox, state="sent", provider_message_id=provider_id, sent_at=now)
            store.update_message(self.uow, store.message(self.uow, row["id"]), telegram_message_id=provider_id)
        elif state == "uncertain":
            store.set_outbox(self.uow, outbox, state="uncertain", error_code="provider_timeout")
        elif state in ("failed", "cancelled"):
            store.set_outbox(self.uow, outbox, state=state, error_code="example")
        return store.message(self.uow, row["id"])

    def outbox_of(self, message):
        return store.outbox(self.uow, message["outbox_id"])

    def enqueue(self, message):
        return store.enqueue_cleanup(self.uow, message["id"], "secret_request_expired", now=self.bridge.now())

    def job(self, message):
        return dict(self.uow.connection.execute("SELECT * FROM telegram_cleanup_jobs WHERE message_id=?",
                                                (message["id"],)).fetchone())

    def jobs(self):
        return self.uow.connection.execute("SELECT count(*) FROM telegram_cleanup_jobs").fetchone()[0]

    def wait_due(self, message):
        """Move the clock to the moment the job is due again."""
        due = self.job(message)["next_attempt_at"]
        self.bridge.time = max(self.bridge.time, store.parse(due))


class DeletionTests(CleanupCase):
    def test_a_sent_message_is_deleted(self):
        message = self.message()
        self.enqueue(message)
        self.assertTrue(self.cleanup.one())
        self.bridge.api.delete_message.assert_called_once_with(CHAT.chat_id, 41)
        job = self.job(message)
        self.assertEqual((job["state"], job["error_code"], job["attempts"]), ("deleted", None, 1))
        self.assertIsNotNone(job["completed_at"])
        self.assertFalse(self.cleanup.one())

    def test_a_message_already_gone_counts_as_deleted(self):
        message = self.message()
        self.enqueue(message)
        self.bridge.api.delete_message.side_effect = TelegramApiError("refused", "Bad Request: message to delete not found",
                                                                      error_code=400)
        self.assertTrue(self.cleanup.one())
        job = self.job(message)
        self.assertEqual((job["state"], job["error_code"]), ("deleted", "already_gone"))

    def test_a_send_that_never_happened_has_nothing_to_delete(self):
        for state in ("failed", "cancelled"):
            with self.subTest(state=state):
                message = self.message(state)
                self.enqueue(message)
                self.assertFalse(self.cleanup.one())
                job = self.job(message)
                self.assertEqual((job["state"], job["error_code"], job["attempts"]), ("deleted", "never_sent", 0))
        self.bridge.api.delete_message.assert_not_called()

    def test_a_transient_failure_is_retried_and_fails_for_good_after_eight_attempts(self):
        message = self.message()
        self.enqueue(message)
        self.bridge.api.delete_message.side_effect = TelegramApiError("unreachable", "connection reset")
        delays = []
        for attempt in range(1, 9):
            self.assertTrue(self.cleanup.one())
            job = self.job(message)
            self.assertEqual(job["attempts"], attempt)
            if attempt < 8:
                self.assertEqual((job["state"], job["error_code"]), ("queued", "unreachable"))
                delays.append((store.parse(job["next_attempt_at"]) - self.bridge.time).total_seconds())
                self.assertFalse(self.cleanup.one(), "not due before its backoff")
                self.wait_due(message)
        job = self.job(message)
        self.assertEqual((job["state"], job["error_code"]), ("failed", "unreachable_exhausted"))
        self.assertEqual(self.bridge.api.delete_message.call_count, 8)
        # The bridge's backoff: doubling from its base, never above its cap.
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0])
        self.bridge.advance(3600)
        self.assertFalse(self.cleanup.one())

    def test_a_message_older_than_48_hours_is_left_for_manual_deletion(self):
        message = self.message()
        self.bridge.advance(48 * 3600)
        self.enqueue(message)
        self.assertFalse(self.cleanup.one())
        self.bridge.api.delete_message.assert_not_called()
        job = self.job(message)
        self.assertEqual((job["state"], job["error_code"], job["attempts"]), ("failed", "too_old_48h", 0))
        self.assertIn("48 hours", job["error_message"])

    def test_telegram_saying_too_old_fails_the_job(self):
        message = self.message()
        self.enqueue(message)
        self.bridge.api.delete_message.side_effect = TelegramApiError("refused", "Bad Request: message can't be deleted",
                                                                      error_code=400)
        self.assertTrue(self.cleanup.one())
        self.assertEqual((self.job(message)["state"], self.job(message)["error_code"]), ("failed", "too_old_48h"))


class WaitingForTheSendTests(CleanupCase):
    def assert_stopped_on_uncertain_send(self, message):
        job = self.job(message)
        self.assertEqual((job["state"], job["error_code"], job["attempts"]), ("failed", store.CLEANUP_SEND_UNCERTAIN, 0))
        self.assertIsNone(job["completed_at"])
        self.bridge.api.delete_message.assert_not_called()

    def test_a_job_on_an_uncertain_send_stops_with_nothing_to_delete(self):
        message = self.message("uncertain")
        self.enqueue(message)
        self.assertFalse(self.cleanup.one())
        self.assert_stopped_on_uncertain_send(message)
        # It is final: later passes do not pick it up again.
        self.bridge.advance(3600)
        self.assertFalse(self.cleanup.one())
        self.assert_stopped_on_uncertain_send(message)

    def test_a_job_stuck_in_the_old_endless_requeue_ends_on_its_next_pass(self):
        # The shape the old 30-second re-queue left behind: queued, no attempts, no Telegram message ID, send uncertain.
        message = self.message("uncertain")
        job = self.enqueue(message)
        self.uow.repository("telegram_cleanup_jobs").update(job["id"], {"next_attempt_at": store.plus(self.bridge.now(), 30)},
                                                            expected_revision=job["revision"])
        self.bridge.advance(3 * 24 * 3600)
        job = self.job(message)
        self.assertEqual((job["state"], job["attempts"], store.message(self.uow, message["id"])["telegram_message_id"],
                          self.outbox_of(message)["state"]), ("queued", 0, None, "uncertain"))
        self.assertFalse(self.cleanup.one())
        self.assert_stopped_on_uncertain_send(message)

    def test_a_job_on_a_queued_send_backs_off_without_spending_attempts_and_deletes_once_sent(self):
        message = self.message("queued")
        self.enqueue(message)
        delays = []
        for _ in range(11):
            self.assertFalse(self.cleanup.one())
            job = self.job(message)
            self.assertEqual((job["state"], job["attempts"]), ("queued", 0))
            delays.append((store.parse(job["next_attempt_at"]) - self.bridge.time).total_seconds())
            self.assertFalse(self.cleanup.one(), "not due before its backoff")
            self.wait_due(message)
        # The bridge's backoff, doubling from its base to its cap.
        self.assertEqual(delays, [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 320.0, 640.0, 900.0, 900.0, 900.0])
        outbox = store.set_outbox(self.uow, self.outbox_of(message), state="dispatching", attempts=1,
                                  dispatch_started_at=self.bridge.now())
        self.assertFalse(self.cleanup.one())
        self.assertEqual(self.job(message)["attempts"], 0)
        self.wait_due(message)
        store.set_outbox(self.uow, outbox, state="sent", provider_message_id=55, sent_at=self.bridge.now())
        store.update_message(self.uow, store.message(self.uow, message["id"]), telegram_message_id=55)
        self.assertTrue(self.cleanup.one())
        self.bridge.api.delete_message.assert_called_once_with(CHAT.chat_id, 55)
        job = self.job(message)
        self.assertEqual((job["state"], job["attempts"]), ("deleted", 1))

    def test_a_job_on_a_send_that_never_goes_out_ends_at_48_hours(self):
        for state in ("queued", "dispatching"):
            with self.subTest(state=state):
                start = self.bridge.time
                message = self.message(state)
                self.enqueue(message)
                passes = 0
                while self.job(message)["state"] == "queued":
                    self.assertLess(passes, 400, "the wait must end")
                    self.cleanup.one()
                    passes += 1
                    if self.job(message)["state"] == "queued":
                        self.wait_due(message)
                job = self.job(message)
                self.assertEqual((job["state"], job["error_code"], job["attempts"]), ("failed", "too_old_48h", 0))
                self.assertGreaterEqual(self.bridge.time - start, timedelta(hours=48))
                self.assertLess(self.bridge.time - start, timedelta(hours=48, seconds=901))
                # About one look every 15 minutes once at the cap, instead of one every 30 seconds.
                self.assertLess(passes, 210)
        self.bridge.api.delete_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
