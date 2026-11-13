"""Who holds SQLite's one writer, and for how long.

Write units of one process queue for the writer in arrival order, and ``Database.write_if_needed`` runs a unit that
takes the writer only when it writes. Every case here uses a real store and real second connections: the claims are
about SQLite's lock, so nothing less would prove them.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import database as storage
from qorqut_operations.storage.database import Database, REQUEST_TIMING, RequestTiming, WriterQueue
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

COUNTER = "writer-admission-test"
WAIT = 20  # seconds a helper thread may take before the test gives up on it; never slept in full


class StoreCase(unittest.TestCase):
    busy_timeout = 5.0

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "operations.sqlite3"
        self.feed = ChangeFeed()
        self.db = Database(self.path, assembly=host_assembly(self.feed), busy_timeout=self.busy_timeout).initialize_target()
        self.addCleanup(self.db.close)
        with self.db.transaction() as uow:
            uow.connection.execute("INSERT INTO change_sequences(name,value) VALUES(?,0)", (COUNTER,))

    def other(self, timeout=0.0):
        """A connection of another process, as far as SQLite's locks are concerned."""
        connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None, check_same_thread=False)
        self.addCleanup(connection.close)
        return connection

    def value(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            return connection.execute("SELECT value FROM change_sequences WHERE name=?", (COUNTER,)).fetchone()[0]
        finally:
            connection.close()

    @staticmethod
    def read(uow):
        return uow.connection.execute("SELECT value FROM change_sequences WHERE name=?", (COUNTER,)).fetchone()[0]

    @staticmethod
    def bump(uow):
        return uow.connection.execute("UPDATE change_sequences SET value=value+1 WHERE name=? RETURNING value",
                                      (COUNTER,)).fetchone()[0]

    def writer_is_free(self):
        """Whether a writer outside this process could start right now."""
        probe = self.other()
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        probe.execute("ROLLBACK")
        return True


class WriteIfNeededTests(StoreCase):
    def test_a_unit_that_only_reads_never_needs_the_writer(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")  # somebody else writes for the whole duration
        runs = []

        def body(uow):
            runs.append(uow.writable)
            return self.read(uow)

        started = time.monotonic()
        self.assertEqual(self.db.write_if_needed(body, operation="test.idle"), 0)
        self.assertLess(time.monotonic() - started, self.busy_timeout / 2)
        self.assertEqual(runs, [True])  # a write unit in every respect, run once
        holder.execute("ROLLBACK")

    def test_the_writer_is_taken_with_the_first_write_and_kept_until_commit(self):
        seen = {}

        def body(uow):
            self.read(uow)
            seen["before"] = self.writer_is_free()
            value = self.bump(uow)
            seen["after"] = self.writer_is_free()
            return value

        self.assertEqual(self.db.write_if_needed(body, operation="test.write"), 1)
        self.assertEqual(seen, {"before": True, "after": False})
        self.assertEqual(self.value(), 1)
        self.assertTrue(self.writer_is_free())

    def test_a_write_refused_by_a_running_writer_repeats_the_unit_holding_the_writer(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        runs = []

        def body(uow):
            runs.append(self.read(uow))
            if len(runs) == 1:
                # The writer is released only after this attempt has been refused, so the repeat can have it.
                threading.Timer(0.05, lambda: holder.execute("ROLLBACK")).start()
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.contended"), 1)
        self.assertEqual(runs, [0, 0])
        self.assertEqual(self.value(), 1)  # written once: nothing of the refused attempt was kept

    def test_a_write_on_a_stale_snapshot_repeats_the_unit_on_current_data(self):
        elsewhere = self.other()
        runs = []

        def body(uow):
            runs.append(self.read(uow))
            if len(runs) == 1:
                # Committed after this unit read: what it read is no longer the truth, so its write must not land.
                elsewhere.execute("UPDATE change_sequences SET value=40 WHERE name=?", (COUNTER,))
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.stale"), 41)
        self.assertEqual(runs, [0, 40])
        self.assertEqual(self.value(), 41)

    def test_a_refusal_the_body_swallows_still_repeats_the_unit(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        runs = []

        def body(uow):
            runs.append(len(runs))
            if len(runs) == 1:
                try:
                    self.bump(uow)
                except BaseException:
                    pass  # the worst a handler can do: carry on as though the write had happened
                holder.execute("ROLLBACK")
                return "from the abandoned attempt"
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.swallowed"), 1)
        self.assertEqual(runs, [0, 1])
        self.assertEqual(self.value(), 1)

    def test_an_error_raised_after_a_refusal_belongs_to_the_abandoned_attempt(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        runs = []

        def body(uow):
            runs.append(len(runs))
            if len(runs) == 1:
                try:
                    self.bump(uow)
                except BaseException:
                    holder.execute("ROLLBACK")
                    raise DomainError("storage_error", "a handler's own account of the refused write")
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.misreported"), 1)
        self.assertEqual(runs, [0, 1])

    def test_nothing_runs_in_an_attempt_once_it_has_been_refused(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        later = []

        def body(uow):
            if not later:
                try:
                    self.bump(uow)
                except BaseException:
                    holder.execute("ROLLBACK")
                    try:
                        self.read(uow)
                    except BaseException as error:
                        later.append(type(error).__name__)
                    return None
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.poisoned"), 1)
        self.assertEqual(later, ["_WriterRequired"])

    def test_the_refusal_is_not_an_ordinary_exception(self):
        # Handlers written for failures (log it, defer it, compensate) must not see an abandoned attempt.
        self.assertFalse(issubclass(storage._WriterRequired, Exception))
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        handled = []

        def body(uow):
            try:
                return self.bump(uow)
            except Exception as error:
                handled.append(error)
                raise
            finally:
                if holder.in_transaction:
                    holder.execute("ROLLBACK")

        self.assertEqual(self.db.write_if_needed(body, operation="test.handlers"), 1)
        self.assertEqual(handled, [])

    def test_a_statement_run_through_a_cursor_is_covered_too(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        runs = []

        def body(uow):
            runs.append(len(runs))
            cursor = uow.connection.cursor()
            try:
                cursor.execute("UPDATE change_sequences SET value=value+1 WHERE name=?", (COUNTER,))
            finally:
                if holder.in_transaction:
                    holder.execute("ROLLBACK")
            return cursor.rowcount

        self.assertEqual(self.db.write_if_needed(body, operation="test.cursor"), 1)
        self.assertEqual(runs, [0, 1])
        self.assertEqual(self.value(), 1)

    def test_the_bodys_own_failure_is_raised_once_and_nothing_is_kept(self):
        runs = []

        def body(uow):
            runs.append(self.bump(uow))
            raise DomainError("validation_error", "the work itself is wrong")

        with self.assertRaises(DomainError) as raised:
            self.db.write_if_needed(body, operation="test.failure")
        self.assertEqual(raised.exception.code, "validation_error")
        self.assertEqual(runs, [1])
        self.assertEqual(self.value(), 0)
        self.assertTrue(self.writer_is_free())

    def test_a_storage_error_other_than_a_refusal_is_reported_as_before(self):
        with self.assertRaises(DomainError) as raised:
            self.db.write_if_needed(lambda uow: uow.connection.execute("INSERT INTO change_sequences(name,value) VALUES(?,0)",
                                                                       (COUNTER,)), operation="test.conflict")
        self.assertEqual(raised.exception.code, "revision_conflict")

    def test_commit_hooks_and_publication_run_as_for_any_write_unit(self):
        published = []
        self.feed.publish_commit = lambda **details: published.append((details["operation"], details["wrote"]))
        self.db.write_if_needed(self.read, operation="test.quiet")
        self.db.write_if_needed(self.bump, operation="test.loud")
        self.assertEqual(published, [("test.quiet", False), ("test.loud", True)])

    def test_only_a_unit_that_changed_a_row_is_timed_as_a_write(self):
        timing = RequestTiming()
        token = REQUEST_TIMING.set(timing)
        try:
            self.db.write_if_needed(self.read, operation="test.quiet")
            self.assertEqual((timing.read_units, timing.write_units), (1, 0))
            self.db.write_if_needed(self.bump, operation="test.loud")
            self.assertEqual((timing.read_units, timing.write_units), (1, 1))
        finally:
            REQUEST_TIMING.reset(token)

    def test_only_the_time_the_writer_was_held_is_reported_as_slow(self):
        clock = time.monotonic

        def slow_reader(uow):
            self.read(uow)
            time.sleep(0.3)

        def slow_writer(uow):
            self.bump(uow)
            time.sleep(0.3)

        storage._SLOW_UNIT_LAST_WARNED_AT.clear()
        with self.assertNoLogs(storage.LOGGER, level="WARNING"):
            self.db.write_if_needed(slow_reader, operation="test.slowread")
        with self.assertLogs(storage.LOGGER, level="WARNING") as logs:
            self.db.write_if_needed(slow_writer, operation="test.slowwrite")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("operation=test.slowwrite", logs.output[0])
        self.assertIn("outcome=ok", logs.output[0])
        self.assertIs(time.monotonic, clock)


class WriterQueueTests(StoreCase):
    busy_timeout = 0.4

    def test_writers_of_this_process_are_served_in_arrival_order(self):
        release, order, threads = threading.Event(), [], []

        def hold():
            with self.db.transaction(operation="test.holder"):
                entered.set()
                release.wait(WAIT)

        def wait(name):
            with self.db.transaction(operation="test.waiter"):
                order.append(name)

        entered = threading.Event()
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(WAIT))
        for name in range(6):
            thread = threading.Thread(target=wait, args=(name,))
            thread.start()
            threads.append(thread)
            deadline = time.monotonic() + WAIT
            while len(self.db._writers._waiting) <= name:  # queued before the next one arrives
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
        release.set()
        for thread in [holder, *threads]:
            thread.join(WAIT)
        self.assertEqual(order, list(range(6)))
        self.assertFalse(self.db._writers._held)
        self.assertFalse(self.db._writers._waiting)

    def test_a_writer_that_cannot_have_its_turn_in_time_is_busy_and_the_queue_moves_on(self):
        release, entered, outcomes = threading.Event(), threading.Event(), {}

        def hold():
            with self.db.transaction(operation="test.holder"):
                entered.set()
                release.wait(WAIT)

        def impatient():
            started = time.monotonic()
            try:
                with self.db.transaction(operation="test.impatient"):
                    outcomes["impatient"] = "entered"
            except DomainError as error:
                outcomes["impatient"] = (error.code, error.retryable, time.monotonic() - started)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(WAIT))
        waiter = threading.Thread(target=impatient)
        waiter.start()
        waiter.join(WAIT)
        code, retryable, waited = outcomes["impatient"]
        self.assertEqual((code, retryable), ("busy", True))
        self.assertGreaterEqual(waited, self.busy_timeout * 0.9)
        self.assertFalse(self.db._writers._waiting)  # it left the queue; nobody is handed a turn it gave up
        release.set()
        holder.join(WAIT)
        with self.db.transaction(operation="test.next") as uow:
            self.assertEqual(self.bump(uow), 1)

    def test_a_write_unit_inside_another_on_one_thread_fails_instead_of_waiting_for_itself(self):
        started = time.monotonic()
        with self.db.transaction(operation="test.outer") as outer:
            self.bump(outer)
            with self.assertRaises(DomainError) as raised:
                with self.db.transaction(operation="test.inner"):
                    self.fail("the inner unit cannot have the writer the outer one holds")
            self.assertEqual(raised.exception.code, "busy")
        self.assertLess(time.monotonic() - started, WAIT)
        self.assertEqual(self.value(), 1)  # the outer unit is unaffected

    def test_waiting_in_the_queue_and_for_the_lock_shares_one_busy_timeout(self):
        release, entered = threading.Event(), threading.Event()
        elsewhere, leave = self.other(), self.db._writers.leave

        def hold():
            with self.db.transaction(operation="test.holder"):
                entered.set()
                release.wait(WAIT)

        def taken_over_elsewhere():
            # Another process takes SQLite's writer between this process's units: the holder has committed, and
            # the unit that waited behind it in the queue now finds the lock itself taken.
            if not elsewhere.in_transaction:
                elsewhere.execute("BEGIN IMMEDIATE")
            leave()

        self.db._writers.leave = taken_over_elsewhere
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(WAIT))
        statements, acquire = [], self.db._acquire

        def traced():
            connection = acquire()
            connection.set_trace_callback(statements.append)
            return connection

        self.db._acquire = traced
        threading.Timer(self.busy_timeout / 2, release.set).start()
        started = time.monotonic()
        with self.assertRaises(DomainError) as raised:
            with self.db.transaction(operation="test.waiter"):
                pass
        waited = time.monotonic() - started
        self.assertEqual(raised.exception.code, "busy")
        # Half the timeout went on the queue, so only the other half may go on SQLite's lock: not a second full one.
        budgets = [int(text.rsplit("=", 1)[1]) for text in statements if text.startswith("PRAGMA busy_timeout")]
        self.assertTrue(budgets)
        self.assertLessEqual(budgets[0], self.busy_timeout * 1000 * 0.6)
        self.assertGreaterEqual(waited, self.busy_timeout * 0.9)
        if sys.platform == "linux":  # elsewhere SQLite's short sleeps can overrun, so only the budget above is exact
            self.assertLess(waited, self.busy_timeout * 1.6)
        holder.join(WAIT)
        elsewhere.execute("ROLLBACK")

    def test_a_connection_that_waited_keeps_the_configured_busy_timeout(self):
        release, entered = threading.Event(), threading.Event()

        def hold():
            with self.db.transaction(operation="test.holder"):
                entered.set()
                release.wait(WAIT)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(WAIT))
        threading.Timer(self.busy_timeout / 4, release.set).start()
        with self.db.transaction(operation="test.waiter") as uow:
            self.assertEqual(uow.connection.execute("PRAGMA busy_timeout").fetchone()[0], int(self.busy_timeout * 1000))
        holder.join(WAIT)

    def test_read_units_and_optimistic_units_do_not_queue(self):
        release, entered = threading.Event(), threading.Event()

        def hold():
            with self.db.transaction(operation="test.holder"):
                entered.set()
                release.wait(WAIT)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(WAIT))
        try:
            with self.db.read() as uow:
                self.assertEqual(self.read(uow), 0)
            self.assertEqual(self.db.write_if_needed(self.read, operation="test.idle"), 0)
        finally:
            release.set()
            holder.join(WAIT)


class UnitMemoTests(StoreCase):
    """What a unit has worked out is kept for that unit and no longer."""

    def test_there_is_a_memo_only_inside_a_unit_and_each_unit_starts_empty(self):
        self.assertIsNone(storage.unit_memo())
        with self.db.read():
            first = storage.unit_memo()
            self.assertEqual(first, {})
            first["answer"] = 1
            self.assertIs(storage.unit_memo(), first)
        self.assertIsNone(storage.unit_memo())
        with self.db.transaction():
            self.assertEqual(storage.unit_memo(), {})
        self.assertIsNone(storage.unit_memo())

    def test_a_unit_opened_inside_another_has_its_own_memo(self):
        with self.db.read():
            outer = storage.unit_memo()
            outer["answer"] = 1
            with self.db.read():
                self.assertEqual(storage.unit_memo(), {})
                storage.unit_memo()["answer"] = 2
            self.assertIs(storage.unit_memo(), outer)
            self.assertEqual(outer, {"answer": 1})

    def test_a_unit_that_failed_takes_its_memo_with_it(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction():
                storage.unit_memo()["answer"] = 1
                raise RuntimeError("no")
        self.assertIsNone(storage.unit_memo())

    def test_a_repeated_unit_does_not_inherit_what_the_abandoned_attempt_worked_out(self):
        holder = self.other()
        holder.execute("BEGIN IMMEDIATE")
        seen = []

        def body(uow):
            seen.append(dict(storage.unit_memo()))
            storage.unit_memo()["answer"] = len(seen)
            self.read(uow)   # the unit has read, so its write below is refused at once rather than made to wait
            if len(seen) == 1:
                threading.Timer(0.05, lambda: holder.execute("ROLLBACK")).start()
            return self.bump(uow)

        self.assertEqual(self.db.write_if_needed(body, operation="test.memo"), 1)
        self.assertEqual(seen, [{}, {}])

    def test_units_on_other_threads_do_not_share_a_memo(self):
        found = []
        with self.db.read():
            storage.unit_memo()["answer"] = 1

            def elsewhere():
                found.append(storage.unit_memo())
                with self.db.read():
                    found.append(dict(storage.unit_memo()))
            thread = threading.Thread(target=elsewhere)
            thread.start()
            thread.join(WAIT)
        self.assertEqual(found, [None, {}])


class QueueUnitTests(unittest.TestCase):
    def test_a_turn_handed_over_at_the_moment_of_giving_up_is_used_not_lost(self):
        queue = WriterQueue()
        self.assertEqual(queue.enter(1), WriterQueue.AT_ONCE)

        class Handed:
            """A turn whose wait times out although ``leave`` has, by then, handed it over."""
            def __init__(self):
                self.lock = threading.Lock()

            def acquire(self, timeout=-1):
                if timeout == -1:
                    return self.lock.acquire()
                queue.leave()  # the holder releases exactly as this waiter's timeout expires
                return False

            def release(self):
                self.lock.release()

        queue.Turn = Handed
        self.assertEqual(queue.enter(0.01), WriterQueue.AFTER_WAITING)
        self.assertTrue(queue._held)
        queue.leave()
        self.assertFalse(queue._held)
        self.assertEqual(queue.enter(1), WriterQueue.AT_ONCE)


if __name__ == "__main__":
    unittest.main()
