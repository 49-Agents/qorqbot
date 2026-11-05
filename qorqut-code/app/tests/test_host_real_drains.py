"""The retained services' REAL bounded stop methods either prove their drain or refuse to: the lifetime closes SQLite only
beneath a drain it can trust.

A stop method that joins its thread with a timeout and returns although the thread survived would let
``ServiceLifetime`` report a clean stop and close SQLite under a running writer. Each case here installs a synthetic
thread, blocked on an Event, in the slot of the real service and calls the service's own stop method (and the real
``Runtime.stop``); no stop method is replaced by a recording controller. The blocked thread's bounded join is shortened
so the production waits (3-35 seconds) do not slow the cases; the thread is real and still alive when the join expires.
No provider, model, terminal, native session or operational data is involved."""

import threading

import pytest
from qorqut_operations.contracts import DomainError

from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from bazarlyk.service.lifetime import ShutdownIncomplete
from host_foundation_support import build_instance

# The wired services whose drain is a single worker thread, and the slot that holds it. The first six are the ones
# whose stop used to return after a timed join regardless of the thread's fate; signoffs and email_history already
# raised and are kept here so a regression in them is noticed too.
SLOTS = {"native_chat": "_thread", "inbox_nudges": "_thread", "activity": "_thread", "incidents": "_thread", "changes": "_ticker_thread",
         "terminal_control": "thread", "broadcast_dispatcher": "thread", "signoffs": "_thread", "email_history": "_thread"}


class BlockedWork(threading.Thread):
    """A synthetic observer that stays alive until released."""

    def __init__(self):
        self.release = threading.Event()
        super().__init__(target=self.release.wait, name="synthetic-blocked-observer", daemon=True)

    def join(self, timeout=None):
        super().join(0.05 if timeout is None else min(timeout, 0.05))

    def finish(self):
        self.release.set()
        super().join(5)
        assert not self.is_alive()


def holder(runtime, name):
    """The object that owns the worker thread: the native Chat backend sits behind the runtime's Chat facade."""
    return runtime.native_chat.backend if name == "native_chat" else getattr(runtime, name)


def block(runtime, name):
    work = BlockedWork()
    work.start()
    setattr(holder(runtime, name), SLOTS[name], work)
    return work


def stop_method(service):
    return service.stop_observer if hasattr(service, "stop_observer") else service.stop


@pytest.fixture
def host(tmp_path):
    instance, _ = build_instance(tmp_path)
    runtime, events = instance.runtime, []
    runtime.broadcast_dispatcher = BroadcastDispatcher(runtime, None, None)  # constructed only; never started
    real_close = runtime.database.close
    runtime.database.close = lambda: (events.append("close"), real_close())[1]
    yield instance, runtime, events
    for name in SLOTS:  # nothing synthetic may outlive its case
        thread = getattr(holder(runtime, name), SLOTS[name], None)
        if isinstance(thread, BlockedWork):
            thread.finish()


@pytest.mark.parametrize("name", SLOTS)
def test_a_real_stop_with_nothing_running_returns_and_one_that_times_out_raises_and_keeps_its_thread(host, name):
    _, runtime, _ = host
    service, stop = holder(runtime, name), stop_method(getattr(runtime, name))
    assert getattr(service, SLOTS[name]) is None
    stop()  # never started: nothing to drain

    work = block(runtime, name)
    with pytest.raises(TimeoutError):
        stop()
    # The surviving thread is still the service's: a later stop can prove its eventual drain.
    assert work.is_alive() and getattr(service, SLOTS[name]) is work
    with pytest.raises(TimeoutError):
        stop()

    work.finish()
    stop()
    assert getattr(service, SLOTS[name]) is None


@pytest.mark.parametrize("name", SLOTS)
def test_the_lifetime_reports_a_real_unproved_drain_leaves_sqlite_open_and_a_retry_completes(host, name):
    instance, runtime, events = host
    installed = {step.name for step in runtime.lifetime().steps if step.installed}
    assert name in installed
    work = block(runtime, name)

    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    report = incomplete.value.report
    assert [failed for failed, _ in report.failed] == [name] and not report.clean
    assert "TimeoutError" in report.failed[0][1]
    # Every other service was still asked to drain, admission was closed, and SQLite stayed open beneath the thread.
    assert set(report.stopped) == installed - {name}
    assert report.admission_closed and not report.database_closed
    assert events == [] and work.is_alive() and getattr(holder(runtime, name), SLOTS[name]) is work
    with instance.database.read() as uow:
        assert uow.connection.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(DomainError) as closed:
        with runtime.application.writer_admission.writer():
            pass
    assert closed.value.code == "maintenance"

    work.finish()
    report = runtime.stop()
    assert report.clean and report.database_closed and set(report.stopped) == installed
    assert events == ["close"] and getattr(holder(runtime, name), SLOTS[name]) is None


def test_every_unproved_drain_is_reported_and_only_a_full_drain_closes_sqlite(host):
    _, runtime, events = host
    first, second = block(runtime, "native_chat"), block(runtime, "incidents")

    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    assert [name for name, _ in incomplete.value.report.failed] == ["native_chat", "incidents"]  # the retained stop order

    first.finish()
    with pytest.raises(ShutdownIncomplete) as incomplete:
        runtime.stop()
    assert [name for name, _ in incomplete.value.report.failed] == ["incidents"] and events == []

    second.finish()
    assert runtime.stop().clean and events == ["close"]


def test_the_change_feed_never_runs_two_tickers_but_replaces_one_that_has_ended(host):
    instance, runtime, _ = host
    feed = runtime.changes
    work = block(runtime, "changes")
    feed.start_external_writer_ticker(lambda: pytest.fail("a live ticker must not be duplicated"))
    assert feed._ticker_thread is work

    work.finish()  # the thread ended after an earlier unproved stop kept its reference
    feed.start_external_writer_ticker(instance.database.dedicated_connection)
    ticker = feed._ticker_thread
    assert ticker is not work and ticker.is_alive()
    feed.stop()
    assert not ticker.is_alive() and feed._ticker_thread is None
