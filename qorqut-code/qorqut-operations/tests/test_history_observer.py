"""Focused checks for the inert coalescing history observer, with injected snapshot, wake and pause boundaries.

These checks exercise coalescing, restart and failure handling with ``min_gap=0`` so that a following pass is not held back;
the minimum gap between automatic passes has its own file (``test_history_cadence.py``).
"""
import queue
import threading
import time

import pytest

from qorqut_operations.history_exports import HistoryObserver
from qorqut_operations.contracts import DomainError
from history_snapshot_support import build, git

LONG = 30.0  # no periodic pass happens while a test runs unless the test injects one


def count(env):
    """Commits on the history ref, or zero before the first one."""
    try:
        return len(git(env, "rev-list", "refs/heads/history").split())
    except AssertionError:
        return 0


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def observer_threads():
    return [thread for thread in threading.enumerate() if thread.name == "operations-history-observer" and thread.is_alive()]


class Gate:
    """A snapshot callable the test can hold open and release."""

    def __init__(self):
        self.entered, self.release, self.calls = threading.Event(), threading.Event(), 0
        self.block = True

    def __call__(self):
        self.calls += 1
        if self.block:
            self.entered.set()
            self.release.wait(10)


class ScriptedWake:
    """An Event-compatible wake whose waits return what the test scripts: True for a hint, False for the periodic timeout."""

    def __init__(self):
        self.script, self.timeouts = queue.Queue(), []
        self.lock = threading.Lock()

    def set(self):
        self.script.put(True)

    def clear(self):
        with self.lock:
            while True:
                try:
                    self.script.get_nowait()
                except queue.Empty:
                    return

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        try:
            return self.script.get(timeout=10)
        except queue.Empty:
            return False


def test_nothing_starts_on_construction_and_a_running_observer_refuses_a_second_start():
    calls = []
    observer = HistoryObserver(lambda: calls.append(1), interval=LONG)
    time.sleep(0.05)
    assert calls == [] and observer.state().started is False
    observer.start()
    assert wait_for(lambda: len(calls) == 1)  # the first pass reconciles anything missed while the host was down
    with pytest.raises(RuntimeError):
        observer.start()
    assert observer.stop(5).stopped is True
    assert observer.stop(5).stopped is True  # stop is idempotent
    assert observer.state().started is False and observer.state().stopping is False

    unused = HistoryObserver(lambda: calls.append(2), interval=LONG)
    assert unused.stop(0).stopped is True  # stopping something never started is a clean no-op
    assert calls == [1]
    for invalid in (0, -1, 7200):
        with pytest.raises(ValueError):
            HistoryObserver(lambda: None, interval=invalid)
    with pytest.raises(ValueError):
        HistoryObserver(lambda: None, retry_delay=-1)
    with pytest.raises(TypeError):
        HistoryObserver("snapshot")
    with pytest.raises(ValueError):
        observer.stop(-1)


def test_a_burst_of_commit_hints_during_a_snapshot_leaves_exactly_one_further_pass():
    gate = Gate()
    observer = HistoryObserver(gate, interval=LONG, min_gap=0)
    observer.start()
    assert gate.entered.wait(5) and observer.state().snapshot_running is True
    for _ in range(500):
        observer.notify_commit()
    assert observer.state().pending is True and gate.calls == 1
    gate.release.set()  # the held pass ends; the coalesced burst owes exactly one more (which is no longer held)
    assert wait_for(lambda: observer.state().passes == 2 and not observer.state().snapshot_running)
    time.sleep(0.15)  # nothing else is owed: the burst was one coalesced pass
    assert gate.calls == 2 and observer.state().pending is False
    assert observer.stop(5).stopped is True


def test_a_hint_after_the_burst_schedules_another_single_pass_and_hints_before_start_are_kept():
    gate = Gate()
    gate.block = False
    observer = HistoryObserver(gate, interval=LONG, min_gap=0)
    observer.notify_commit()
    observer.notify_commit()
    assert observer.state().pending is True and gate.calls == 0
    observer.start()
    assert wait_for(lambda: gate.calls == 1)
    time.sleep(0.1)
    assert gate.calls == 1  # the early hints and the start's first pass were one pass
    observer.notify_commit()
    assert wait_for(lambda: gate.calls == 2)
    assert observer.stop(5).stopped is True
    observer.notify_commit()  # after stop, hints are ignored
    time.sleep(0.1)
    assert gate.calls == 2


def test_periodic_scan_recovers_a_missed_hint_without_any_notification():
    wake = ScriptedWake()
    calls = []
    observer = HistoryObserver(lambda: calls.append(1), interval=45.0, min_gap=0, wake=wake)
    observer.start()                      # start's own hint: the first pass
    assert wait_for(lambda: len(calls) == 1)
    wake.script.put(False)                # the interval elapsed with no hint at all
    assert wait_for(lambda: len(calls) == 2)
    wake.script.put(False)
    assert wait_for(lambda: len(calls) == 3)
    assert set(wake.timeouts) == {45.0}   # every wait is bounded by the configured interval
    assert observer.stop(5).stopped is True


def test_stop_is_bounded_and_never_claims_a_stopped_thread_while_a_snapshot_runs():
    gate = Gate()
    observer = HistoryObserver(gate, interval=LONG)
    observer.start()
    assert gate.entered.wait(5)
    observer.notify_commit()
    started = time.monotonic()
    result = observer.stop(0.2)
    assert time.monotonic() - started < 2.0
    assert result.stopped is False and result.snapshot_running is True and result.pending is True
    assert observer.state().stopping is True
    gate.release.set()
    final = observer.stop(5)
    assert final.stopped is True and final.snapshot_running is False
    assert gate.calls == 1  # the abandoned pending pass was not started after the stop request


def test_failures_are_recorded_and_never_retried_in_a_tight_loop():
    pauses, attempts = [], []

    def failing():
        attempts.append(1)
        raise DomainError("history_git_failed", "synthetic", retryable=True)

    # A frozen monotonic clock makes the remaining delay exactly the configured one.
    observer = HistoryObserver(failing, interval=LONG, retry_delay=7.0, min_gap=0, pause=pauses.append, clock=lambda: 100.0)
    observer.start()
    assert wait_for(lambda: len(attempts) == 1 and observer.state().passes == 1)
    time.sleep(0.2)
    assert len(attempts) == 1 and pauses == []  # no hint and no elapsed interval: no retry
    state = observer.state()
    assert state.failures == 1 and state.last_error == "history_git_failed" and state.snapshot_running is False
    observer.notify_commit()
    assert wait_for(lambda: len(attempts) == 2 and pauses == [7.0])  # a later hint retries, after the delay
    assert wait_for(lambda: observer.state().passes == 2)
    observer.notify_commit()
    assert wait_for(lambda: len(attempts) == 3 and pauses == [7.0, 7.0])
    assert observer.stop(5).stopped is True


def test_a_successful_pass_clears_the_last_error_and_unexpected_exceptions_do_not_kill_the_worker():
    outcomes = iter([RuntimeError("unexpected"), None, None])

    def snapshot():
        outcome = next(outcomes)
        if outcome:
            raise outcome

    observer = HistoryObserver(snapshot, interval=LONG, retry_delay=0, min_gap=0)
    observer.start()
    assert wait_for(lambda: observer.state().failures == 1 and observer.state().last_error == "RuntimeError")
    observer.notify_commit()
    assert wait_for(lambda: observer.state().passes == 2 and observer.state().last_error is None)
    assert observer.stop(5).stopped is True


def test_a_proven_stop_lets_the_same_observer_restart_with_one_initial_pass_and_preserved_counters():
    calls = []
    observer = HistoryObserver(lambda: calls.append(1), interval=LONG, min_gap=0)
    observer.start()
    assert wait_for(lambda: len(calls) == 1)
    assert observer.stop(5).stopped is True
    state = observer.state()
    assert (state.started, state.stopping, state.passes, state.failures) == (False, False, 1, 0)
    observer.notify_commit()  # ignored while stopped: the restart's own initial pass reconciles current state
    time.sleep(0.1)
    assert calls == [1]
    observer.start()
    assert wait_for(lambda: len(calls) == 2)
    time.sleep(0.15)
    assert len(calls) == 2 and observer.state().started is True  # exactly one initial pass, however many hints preceded it
    observer.notify_commit()
    assert wait_for(lambda: len(calls) == 3)
    assert observer.stop(5).stopped is True
    observer.start()  # and once more
    assert wait_for(lambda: len(calls) == 4)
    assert observer.stop(5).stopped is True and observer.state().passes == 4
    assert observer_threads() == []

    unused = HistoryObserver(lambda: calls.append(5), interval=LONG)
    assert unused.stop(0).stopped is True  # stopping a never-started observer is a proven stop
    unused.start()
    assert wait_for(lambda: len(calls) == 5)
    assert unused.stop(5).stopped is True


def test_a_timed_out_stop_refuses_restart_even_after_the_thread_ends_until_a_later_stop_confirms():
    gate = Gate()
    observer = HistoryObserver(gate, interval=LONG)
    observer.start()
    assert gate.entered.wait(5)
    first = observer.stop(0.1)
    assert first.stopped is False and first.snapshot_running is True
    assert observer.stop(0.05).stopped is False  # still unproven while the snapshot holds the thread
    with pytest.raises(RuntimeError):
        observer.start()  # the previous thread and snapshot are alive
    gate.block = False
    gate.release.set()
    assert wait_for(lambda: observer_threads() == [])  # the old generation ends by itself ...
    state = observer.state()
    assert state.passes == 1 and state.snapshot_running is False and state.started is True and state.stopping is True
    with pytest.raises(RuntimeError):
        observer.start()  # ... but nothing has confirmed it, so the unproven stop still refuses a start
    assert gate.calls == 1  # and the ended generation never ran another pass: its stop signal was never cleared
    confirmed = observer.stop(5)
    assert confirmed.stopped is True and confirmed.snapshot_running is False
    assert observer.state().started is False
    observer.start()
    assert wait_for(lambda: gate.calls == 2)
    time.sleep(0.1)
    assert gate.calls == 2
    assert observer.stop(5).stopped is True and observer.state().passes == 2


def test_start_is_refused_while_a_stop_is_joining_and_a_second_stop_neither_blocks_nor_changes_anything():
    gate = Gate()
    observer = HistoryObserver(gate, interval=LONG)
    observer.start()
    assert gate.entered.wait(5)
    outcomes = queue.Queue()
    stopper = threading.Thread(target=lambda: outcomes.put(observer.stop(10)))
    stopper.start()
    assert wait_for(lambda: observer.state().stopping)  # the stop owns the transition and is now joining the busy thread
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        observer.start()
    observer.notify_commit()  # the join does not hold the state lock the worker and these calls need
    second = observer.stop(0.05)
    assert second.stopped is False and second.snapshot_running is True
    assert time.monotonic() - started < 2.0 and outcomes.empty()
    gate.block = False
    gate.release.set()
    stopper.join(10)
    assert outcomes.get(timeout=5).stopped is True
    observer.start()  # only after the first stop positively completed
    assert wait_for(lambda: gate.calls == 2)
    assert observer.stop(5).stopped is True


def test_concurrent_starts_stops_and_hints_never_overlap_snapshots_or_fail_unexpectedly():
    guard = threading.Lock()
    active = {"now": 0, "most": 0, "calls": 0}

    def snapshot():
        with guard:
            active["now"] += 1
            active["calls"] += 1
            active["most"] = max(active["most"], active["now"])
        time.sleep(0.001)
        with guard:
            active["now"] -= 1

    observer = HistoryObserver(snapshot, interval=LONG, min_gap=0)
    failures, deadline = [], time.monotonic() + 1.5

    def churn(kind):
        try:
            while time.monotonic() < deadline:
                if kind == "start":
                    try:
                        observer.start()
                    except RuntimeError:
                        pass  # refused: running, unconfirmed or mid-transition
                elif kind == "stop":
                    observer.stop(0.05)
                else:
                    observer.notify_commit()
                    observer.state()
                time.sleep(0.0005)
        except BaseException as error:  # noqa: BLE001 - any escape from the observer is a failure of this test
            failures.append(error)

    workers = [threading.Thread(target=churn, args=(kind,)) for kind in ("start", "start", "stop", "stop", "hint")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    assert failures == [] and not any(worker.is_alive() for worker in workers)
    assert observer.stop(10).stopped is True
    assert observer_threads() == [] and active["now"] == 0 and active["most"] == 1
    before = active["calls"]
    assert before > 0
    observer.start()  # still usable after the churn
    assert wait_for(lambda: active["calls"] > before)
    assert observer.stop(10).stopped is True and active["most"] == 1


def test_hint_bursts_coalesce_and_never_overlap_in_a_restarted_generation():
    gate = Gate()
    gate.block = False
    observer = HistoryObserver(gate, interval=LONG, min_gap=0)
    observer.start()
    assert wait_for(lambda: gate.calls == 1)
    assert observer.stop(5).stopped is True
    gate.block = True
    observer.start()
    assert gate.entered.wait(5) and observer.state().snapshot_running is True
    for _ in range(300):
        observer.notify_commit()
    assert observer.state().pending is True and gate.calls == 2  # the held initial pass is the only one running
    gate.block = False
    gate.release.set()
    assert wait_for(lambda: observer.state().passes == 3 and not observer.state().snapshot_running)
    time.sleep(0.15)
    assert gate.calls == 3 and observer.state().pending is False  # the burst owed exactly one more pass
    assert observer.stop(5).stopped is True


def test_counters_diagnostics_and_the_failure_delay_survive_a_restart():
    pauses, attempts, fail = [], [], [True]

    def snapshot():
        attempts.append(1)
        if fail[0]:
            raise DomainError("history_git_failed", "synthetic", retryable=True)

    observer = HistoryObserver(snapshot, interval=LONG, retry_delay=7.0, min_gap=0, pause=pauses.append, clock=lambda: 100.0)
    observer.start()
    assert wait_for(lambda: len(attempts) == 1 and observer.state().passes == 1)
    assert observer.stop(5).stopped is True
    state = observer.state()
    assert (state.passes, state.failures, state.last_error) == (1, 1, "history_git_failed")
    observer.start()  # the initial pass runs promptly: a failure delay never carries into the next generation's first pass
    assert wait_for(lambda: len(attempts) == 2 and observer.state().passes == 2)
    time.sleep(0.2)
    assert len(attempts) == 2 and pauses == []  # no tight retry loop across the restart
    state = observer.state()
    assert (state.passes, state.failures, state.last_error) == (2, 2, "history_git_failed")
    fail[0] = False
    observer.notify_commit()  # the second failure's delay is still owed within this generation
    assert wait_for(lambda: len(attempts) == 3 and observer.state().last_error is None)
    assert observer.state().failures == 2 and observer.state().passes == 3 and pauses == [7.0]
    assert observer.stop(5).stopped is True


def test_the_default_failure_delay_is_interrupted_by_stop_in_every_generation():
    attempts = []

    def failing():
        attempts.append(1)
        raise DomainError("history_git_failed", "synthetic", retryable=True)

    observer = HistoryObserver(failing, interval=LONG, retry_delay=60.0, min_gap=0)  # the real pause: waits on that start's stop signal
    for generation in (1, 2, 3):
        observer.start()
        assert wait_for(lambda: len(attempts) == generation and observer.state().passes == generation)
        observer.notify_commit()  # a hint inside the failure delay: the worker now waits the 60 seconds out
        time.sleep(0.1)
        started = time.monotonic()
        assert observer.stop(5).stopped is True  # interrupts the 60 second delay instead of waiting it out
        assert time.monotonic() - started < 4.0
        assert len(attempts) == generation  # and the interrupted wait never turned into another pass
    assert observer.state().failures == 3 and observer_threads() == []


def test_the_periodic_scan_and_wake_continue_after_a_restart():
    wake = ScriptedWake()
    calls = []
    observer = HistoryObserver(lambda: calls.append(1), interval=45.0, min_gap=0, wake=wake)
    observer.start()
    assert wait_for(lambda: len(calls) == 1)
    assert observer.stop(5).stopped is True
    observer.start()
    assert wait_for(lambda: len(calls) == 2)  # one initial pass, whatever the earlier stop left in the wake
    wake.script.put(False)                    # then the interval elapses with no hint at all
    assert wait_for(lambda: len(calls) == 3)
    assert set(wake.timeouts) == {45.0}
    assert observer.stop(5).stopped is True


def test_observer_drives_a_real_history_snapshot_after_a_commit_hint_and_reconciles_a_change_made_while_stopped(tmp_path):
    env = build(tmp_path)
    try:
        env.history.initialize()
        observer = HistoryObserver(env.history.snapshot, interval=LONG, min_gap=0)
        observer.start()
        assert wait_for(lambda: count(env) == 1)  # the first pass versions the existing state
        with env.database.transaction() as uow:
            uow.connection.execute("INSERT INTO work_items(id, scope, title, state) VALUES ('item-9','global','Hinted','open')")
        observer.notify_commit()  # sent only after the transaction closed
        assert wait_for(lambda: count(env) == 2)
        assert wait_for(lambda: not observer.state().snapshot_running)
        assert observer.state().failures == 0 and observer.stop(10).stopped is True
        # A change made while the observer is stopped (its hint is ignored) is versioned by the restart's initial pass.
        with env.database.transaction() as uow:
            uow.connection.execute("INSERT INTO work_items(id, scope, title, state) VALUES ('item-10','global','Resumed','open')")
        observer.notify_commit()
        time.sleep(0.1)
        assert count(env) == 2
        observer.start()
        assert wait_for(lambda: count(env) == 3)
        assert wait_for(lambda: observer.state().passes == 3 and not observer.state().snapshot_running)
        assert observer.state().failures == 0 and observer.stop(10).stopped is True
        # The database stays open and usable: the observer never closes it.
        with env.database.read() as uow:
            assert uow.connection.execute("SELECT count(*) FROM work_items").fetchone()[0] == 4
    finally:
        env.database.close()
