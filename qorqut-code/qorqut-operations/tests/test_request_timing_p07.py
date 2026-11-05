"""Per-request storage timing: wait versus hold, read versus write, and what the sums do and do not mean.

Synthetic only. Durations come from a scripted clock, so nothing here asserts a wall time; only counts and exact
sums of the scripted values are checked.
"""

from concurrent.futures import ThreadPoolExecutor
import contextvars
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import Database
import qorqut_operations.storage.database as database_module
from qorqut_operations.storage.database import REQUEST_TIMING, RequestTiming
from support import BUSINESS, HUMAN, database_at


class Clock:
    """The three samples every unit takes (start, transaction open, end); a fourth would exhaust it."""

    def __init__(self, *values):
        self.values = iter(values)

    def install(self, monkeypatch):
        monkeypatch.setattr(database_module, "time", SimpleNamespace(monotonic=lambda: next(self.values)))
        monkeypatch.setattr(database_module, "_SLOW_UNIT_LAST_WARNED_AT", {})


def timed(callback):
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        callback()
    finally:
        REQUEST_TIMING.reset(token)
    return timing


def test_new_timing_starts_empty_and_keeps_the_existing_fields():
    timing = RequestTiming()
    assert timing.started > 0
    assert (timing.db_seconds, timing.units) == (0.0, 0)
    assert (timing.db_wait_seconds, timing.db_read_seconds, timing.db_write_seconds) == (0.0, 0.0, 0.0)
    assert (timing.read_units, timing.write_units) == (0, 0)


def test_read_and_write_units_are_counted_and_classified(tmp_path):
    database = database_at(tmp_path)

    def work():
        with database.read(business_id=BUSINESS) as uow:
            assert uow.repository("items").list() == []
        with database.transaction(business_id=BUSINESS) as uow:
            uow.repository("items").insert({"id": "a", "title": "A", "created_by": HUMAN})
        with database.read(business_id=BUSINESS) as uow:
            assert [row["id"] for row in uow.repository("items").list()] == ["a"]

    timing = timed(work)
    assert (timing.units, timing.read_units, timing.write_units) == (3, 2, 1)
    assert timing.units == timing.read_units + timing.write_units
    assert timing.db_seconds == pytest.approx(timing.db_read_seconds + timing.db_write_seconds)
    assert timing.db_wait_seconds >= 0 and timing.db_read_seconds >= 0 and timing.db_write_seconds >= 0


def test_wait_is_separate_from_hold_and_each_goes_to_its_kind(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    clock = Clock(100.0, 100.25, 100.5,    # read: waited 0.25, held 0.25
                  200.0, 200.5, 203.0)     # write: waited 0.5, held 2.5
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with database.read():
            pass
        with database.transaction():
            pass
    finally:
        REQUEST_TIMING.reset(token)
    assert timing.db_wait_seconds == pytest.approx(0.75)
    assert timing.db_read_seconds == pytest.approx(0.25)
    assert timing.db_write_seconds == pytest.approx(2.5)
    assert timing.db_seconds == pytest.approx(2.75)  # the established field is unchanged: hold time of every unit
    assert (timing.units, timing.read_units, timing.write_units) == (2, 1, 1)


def test_a_zero_valued_clock_is_still_a_clock_reading(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    clock = Clock(0.0, 0.0, 0.5)
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with database.read():
            pass
    finally:
        REQUEST_TIMING.reset(token)
    assert (timing.db_wait_seconds, timing.db_read_seconds) == (0.0, 0.5)


def test_a_write_that_cannot_open_its_transaction_still_records_its_wait(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    impatient = Database(database.path, assembly=database.assembly, busy_timeout=0.05).open_existing()
    blocker = sqlite3.connect(database.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    clock = Clock(10.0, 10.75)  # a unit whose transaction never opened takes two samples: start and end
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with pytest.raises(DomainError) as refused:
            with impatient.transaction():
                pytest.fail("the body must not run without a transaction")
    finally:
        REQUEST_TIMING.reset(token)
        blocker.rollback()
        blocker.close()
        impatient.close()
    assert refused.value.code == "busy"
    assert timing.db_wait_seconds == pytest.approx(0.75)
    assert (timing.db_seconds, timing.db_write_seconds, timing.db_read_seconds) == (0, 0, 0)
    assert (timing.units, timing.write_units, timing.read_units) == (1, 1, 0)


def test_a_read_that_cannot_open_its_transaction_still_records_its_wait(tmp_path, monkeypatch):
    database = database_at(tmp_path)

    class Refusing:
        in_transaction = False

        def execute(self, statement, *arguments):
            if statement == "BEGIN":
                raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(database, "_acquire", Refusing)
    clock = Clock(1.0, 1.5)
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with pytest.raises(DomainError) as refused:
            with database.read():
                pytest.fail("the body must not run without a transaction")
    finally:
        REQUEST_TIMING.reset(token)
    assert refused.value.code == "storage_error"
    assert timing.db_wait_seconds == pytest.approx(0.5)
    assert (timing.db_seconds, timing.db_write_seconds, timing.db_read_seconds) == (0, 0, 0)
    assert (timing.units, timing.read_units, timing.write_units) == (1, 1, 0)


def test_a_unit_that_fails_in_its_body_records_wait_and_hold_and_rolls_back(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    clock = Clock(5.0, 5.125, 6.125)
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with pytest.raises(RuntimeError):
            with database.transaction(business_id=BUSINESS) as uow:
                uow.repository("items").insert({"id": "lost", "title": "Lost", "created_by": HUMAN})
                raise RuntimeError("handler failed")
    finally:
        REQUEST_TIMING.reset(token)
    assert timing.db_wait_seconds == pytest.approx(0.125)
    assert timing.db_write_seconds == pytest.approx(1.0)
    assert (timing.units, timing.write_units) == (1, 1)
    monkeypatch.undo()
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").list() == []


def test_nested_units_each_count_and_their_durations_overlap(tmp_path, monkeypatch):
    database = database_at(tmp_path)
    # Outer write: start 0, open 1, end 10 (held 9). Inner read inside it: start 2, open 2.5, end 4 (held 1.5).
    clock = Clock(0.0, 1.0, 2.0, 2.5, 4.0, 10.0)
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    try:
        clock.install(monkeypatch)
        with database.transaction():
            with database.read():
                pass
    finally:
        REQUEST_TIMING.reset(token)
    assert (timing.units, timing.read_units, timing.write_units) == (2, 1, 1)
    assert timing.db_write_seconds == pytest.approx(9.0)
    assert timing.db_read_seconds == pytest.approx(1.5)
    assert timing.db_wait_seconds == pytest.approx(1.5)
    # The sum covers more than the 10 seconds the request spanned: summed durations are not wall time.
    assert timing.db_seconds == pytest.approx(10.5)


def test_units_finishing_on_other_threads_all_land_on_the_shared_timing(tmp_path):
    database = database_at(tmp_path)
    timing = RequestTiming()
    token = REQUEST_TIMING.set(timing)
    readers, writers, rounds = 6, 3, 8
    start = threading.Barrier(readers + writers)

    def reader():
        start.wait()
        for _ in range(rounds):
            with database.read():
                pass

    def writer():
        start.wait()
        for _ in range(rounds):
            with database.transaction():
                pass

    try:
        with ThreadPoolExecutor(readers + writers) as pool:
            # Worker threads run on a copy of the request's context, as anyio.to_thread does: the one timing object is shared.
            futures = [pool.submit(contextvars.copy_context().run, task) for task in [reader] * readers + [writer] * writers]
            for future in futures:
                future.result()
    finally:
        REQUEST_TIMING.reset(token)
    assert (timing.read_units, timing.write_units) == (readers * rounds, writers * rounds)
    assert timing.units == (readers + writers) * rounds
    assert timing.db_seconds == pytest.approx(timing.db_read_seconds + timing.db_write_seconds)


def test_no_timing_in_context_changes_nothing_for_the_unit(tmp_path):
    database = database_at(tmp_path)
    assert REQUEST_TIMING.get() is None
    with database.transaction(business_id=BUSINESS) as uow:
        uow.repository("items").insert({"id": "plain", "title": "Plain", "created_by": HUMAN})
    with database.read(business_id=BUSINESS) as uow:
        assert uow.repository("items").get("plain")["title"] == "Plain"


def test_timing_exposes_only_numbers_and_the_slow_unit_log_keeps_its_fields(tmp_path, monkeypatch, caplog):
    database = database_at(tmp_path)
    assert set(RequestTiming.__slots__) == {"started", "db_seconds", "units", "db_wait_seconds", "db_read_seconds",
                                            "db_write_seconds", "read_units", "write_units", "_lock"}
    clock = Clock(300.0, 300.5, 301.5)
    with database.observe("demo.write"):
        clock.install(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="qorqut_operations.storage.database"):
            with database.transaction(business_id=BUSINESS, operation="demo.write") as uow:
                uow.repository("items").insert({"id": "secret-id", "title": "private title", "created_by": HUMAN})
    assert [record.getMessage() for record in caplog.records] == [
        "storage_transaction operation=demo.write request_id=None job_id=None receipt_id=None "
        "wait_ms=500.0 hold_ms=1000.0 outcome=ok"]
