"""Focused checks for the minimum gap between automatic history passes.

Most cases drive the observer with a fake monotonic clock that only the worker thread advances (by a snapshot's duration, a
scripted periodic timeout or a recorded pause), so every delay is exact and nothing sleeps. A few use the real clock with a
small gap to prove that the default interruptible wait ends on ``stop`` and that real hints cannot beat the deadline.
"""
import queue
import threading
import time

import pytest

from qorqut_operations.history_exports import HistoryObserver
from qorqut_operations.contracts import DomainError
from history_snapshot_support import blob, build, tree

LONG = 30.0  # the periodic interval is never what ends a wait unless a test scripts it


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def observer_threads():
    return [thread for thread in threading.enumerate() if thread.name == "operations-history-observer" and thread.is_alive()]


class FakeTime:
    """A monotonic clock and a recording pause that advance it; only the worker thread calls either."""

    def __init__(self):
        self.now, self.pauses = 1000.0, []

    def clock(self):
        return self.now

    def pause(self, seconds):
        self.pauses.append(seconds)
        self.now += seconds


class TimedWake:
    """An Event-compatible wake scripted by the test whose scripted timeout advances the fake clock by the awaited interval."""

    def __init__(self, fake):
        self.fake, self.script, self.timeouts = fake, queue.Queue(), []

    def set(self):
        self.script.put(True)

    def clear(self):
        while True:
            try:
                self.script.get_nowait()
            except queue.Empty:
                return

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        try:
            hinted = self.script.get(timeout=10)
        except queue.Empty:
            hinted = False
        if not hinted:
            self.fake.now += timeout  # the interval really elapsed with no hint at all
        return hinted


def failing():
    raise DomainError("history_git_failed", "synthetic", retryable=True)


def test_a_sustained_hint_stream_cannot_cause_back_to_back_passes():
    gap, duration, wanted = 60.0, 5.0, 6
    fake, spans, reached, release = FakeTime(), [], threading.Event(), threading.Event()
    box = {}

    def snapshot():
        started = fake.now
        box["observer"].notify_commit()  # a hint during every pass
        fake.now += duration
        spans.append((started, fake.now))
        if len(spans) == wanted:
            reached.set()
            release.wait(10)

    def pause(seconds):
        fake.pause(seconds)
        for _ in range(200):  # and a burst inside every cooldown
            box["observer"].notify_commit()

    observer = box["observer"] = HistoryObserver(snapshot, interval=LONG, min_gap=gap, pause=pause, clock=fake.clock)
    observer.start()
    assert reached.wait(10)
    assert len(spans) == wanted and fake.pauses == [gap] * (wanted - 1)  # one coalesced pass per deadline, no matter the hints
    for (_, ended), (started, _) in zip(spans, spans[1:]):
        assert started - ended == gap
    first = observer.stop(0.1)  # the sixth pass is still running: the stop is honest about it
    assert first.stopped is False and first.snapshot_running is True and first.pending is True
    release.set()
    assert observer.stop(5).stopped is True
    assert len(spans) == wanted  # the abandoned pending pass was not started after the stop request


def test_hints_sent_during_a_cooldown_stay_coalesced_and_are_served_by_the_pass_after_the_deadline():
    fake, calls = FakeTime(), []
    cooling, proceed = threading.Event(), threading.Event()

    def pause(seconds):
        fake.pause(seconds)
        cooling.set()
        proceed.wait(10)

    observer = HistoryObserver(lambda: calls.append(1), interval=LONG, min_gap=60.0, pause=pause, clock=fake.clock)
    observer.start()
    assert wait_for(lambda: len(calls) == 1 and observer.state().passes == 1)
    observer.notify_commit()
    assert cooling.wait(10) and fake.pauses == [60.0]
    for _ in range(500):
        observer.notify_commit()
    state = observer.state()
    assert state.pending is True and state.snapshot_running is False and len(calls) == 1  # waiting, not lost, not running
    proceed.set()
    assert wait_for(lambda: len(calls) == 2 and observer.state().passes == 2)
    time.sleep(0.15)
    assert len(calls) == 2 and observer.state().pending is False and fake.pauses == [60.0]  # the whole burst was one pass
    assert observer.stop(5).stopped is True


def test_the_default_gap_is_the_interval_and_a_zero_gap_waits_for_nothing():
    pauses = []
    kept = HistoryObserver(lambda: None, pause=pauses.append, clock=lambda: 500.0)  # defaults: the 60 second interval
    kept.start()
    assert wait_for(lambda: kept.state().passes == 1)
    kept.notify_commit()
    assert wait_for(lambda: kept.state().passes == 2)
    assert kept.stop(5).stopped is True and pauses == [60.0]

    pauses.clear()
    none = HistoryObserver(lambda: None, interval=LONG, min_gap=0, pause=pauses.append, clock=lambda: 500.0)
    none.start()
    assert wait_for(lambda: none.state().passes == 1)
    none.notify_commit()
    assert wait_for(lambda: none.state().passes == 2)
    assert none.stop(5).stopped is True and pauses == []

    for invalid in (-0.1, 3600.1):
        with pytest.raises(ValueError):
            HistoryObserver(lambda: None, min_gap=invalid)


@pytest.mark.parametrize("gap, retry, fails, expected", [
    (3.0, 7.0, True, 7.0),     # after a failure the longer retry delay wins ...
    (30.0, 7.0, True, 30.0),   # ... unless the gap is longer
    (10.0, None, True, 45.0),  # the default retry delay is the interval
    (3.0, 7.0, False, 3.0),    # a success owes only the gap
    (None, None, False, 45.0),  # and the default gap is the interval
])
def test_the_wait_after_a_pass_is_the_gap_or_after_a_failure_the_larger_of_gap_and_retry_delay(gap, retry, fails, expected):
    pauses, attempts = [], []

    def snapshot():
        attempts.append(1)
        if fails:
            failing()

    observer = HistoryObserver(snapshot, interval=45.0, retry_delay=retry, min_gap=gap, pause=pauses.append, clock=lambda: 500.0)
    observer.start()
    assert wait_for(lambda: observer.state().passes == 1)
    assert pauses == []  # the first pass is prompt
    observer.notify_commit()
    assert wait_for(lambda: len(attempts) == 2)
    assert pauses == [expected]  # recorded before the second pass began
    assert observer.stop(5).stopped is True


def test_periodic_recovery_is_not_delayed_by_the_gap_and_never_beats_it():
    fake = FakeTime()
    starts = []
    wake = TimedWake(fake)
    observer = HistoryObserver(lambda: starts.append(fake.now), interval=60.0, wake=wake, pause=fake.pause, clock=fake.clock)
    observer.start()
    assert wait_for(lambda: len(starts) == 1)
    wake.script.put(False)
    assert wait_for(lambda: len(starts) == 2)
    wake.script.put(False)
    assert wait_for(lambda: len(starts) == 3)
    assert starts[1] - starts[0] == 60.0 and starts[2] - starts[1] == 60.0  # interval and gap coincide: not their sum
    assert fake.pauses == [] and set(wake.timeouts) == {60.0}
    assert observer.stop(5).stopped is True

    # With a gap longer than the periodic interval, the scan waits out only the remainder.
    fake, starts = FakeTime(), []
    wake = TimedWake(fake)
    observer = HistoryObserver(lambda: starts.append(fake.now), interval=45.0, min_gap=60.0, wake=wake, pause=fake.pause,
                               clock=fake.clock)
    observer.start()
    assert wait_for(lambda: len(starts) == 1)
    for expected in (2, 3):
        wake.script.put(False)
        assert wait_for(lambda: len(starts) == expected)
    assert [later - earlier for earlier, later in zip(starts, starts[1:])] == [60.0, 60.0]
    assert fake.pauses == [15.0, 15.0] and set(wake.timeouts) == {45.0}
    assert observer.stop(5).stopped is True


def test_periodic_recovery_after_a_failure_waits_out_the_retry_delay():
    fake, starts = FakeTime(), []
    wake = TimedWake(fake)

    def snapshot():
        starts.append(fake.now)
        failing()

    observer = HistoryObserver(snapshot, interval=45.0, retry_delay=100.0, min_gap=0, wake=wake, pause=fake.pause,
                               clock=fake.clock)
    observer.start()
    assert wait_for(lambda: len(starts) == 1)
    wake.script.put(False)
    assert wait_for(lambda: len(starts) == 2)
    assert starts[1] - starts[0] == 100.0 and fake.pauses == [55.0]  # the 45 second scan waits out the other 55
    assert wait_for(lambda: observer.state().failures == 2)
    assert observer.stop(5).stopped is True


def test_the_first_pass_after_every_start_is_prompt_and_a_stop_interrupts_the_cooldown_at_once():
    calls = []
    observer = HistoryObserver(lambda: calls.append(1), interval=LONG, min_gap=3600.0)  # the real, interruptible wait
    for generation in (1, 2, 3):
        started = time.monotonic()
        observer.start()  # never delayed by the previous generation's cooldown
        assert wait_for(lambda: len(calls) == generation and observer.state().passes == generation, 5)
        assert time.monotonic() - started < 5.0
        observer.notify_commit()  # inside the hour-long cooldown: coalesced, not served
        time.sleep(0.1)
        state = observer.state()
        assert len(calls) == generation and state.pending is True and state.snapshot_running is False
        stopping = time.monotonic()
        result = observer.stop(5)
        assert result.stopped is True and result.pending is True and result.snapshot_running is False
        assert time.monotonic() - stopping < 4.0  # not an hour: the wait ended on the stop signal
        assert len(calls) == generation and observer_threads() == []  # and the abandoned pass never ran
    assert observer.state().passes == 3 and observer.state().started is False


def test_an_unproven_stop_still_refuses_restart_and_the_cooldown_never_runs_a_pass_after_it():
    release, entered, calls = threading.Event(), threading.Event(), []
    mode = {"block": True}

    def snapshot():
        calls.append(1)
        if mode["block"]:
            entered.set()
            release.wait(10)

    observer = HistoryObserver(snapshot, interval=LONG, min_gap=3600.0)
    observer.start()
    assert entered.wait(5)
    observer.notify_commit()
    assert observer.stop(0.1).stopped is False  # honest: the snapshot is still running
    with pytest.raises(RuntimeError):
        observer.start()
    mode["block"] = False
    release.set()
    assert wait_for(lambda: observer_threads() == [])
    with pytest.raises(RuntimeError):
        observer.start()  # ended on its own, but nothing confirmed it
    assert observer.stop(5).stopped is True and len(calls) == 1  # the ended generation never began its cooldown pass
    observer.start()
    assert wait_for(lambda: len(calls) == 2)  # the new generation's first pass is prompt
    assert observer.stop(5).stopped is True


def test_a_real_time_hint_stream_never_starts_a_pass_sooner_than_the_gap_after_the_last_one_ended():
    gap, spans = 0.25, []

    def snapshot():
        started = time.monotonic()
        time.sleep(0.01)
        spans.append((started, time.monotonic()))

    observer = HistoryObserver(snapshot, interval=LONG, min_gap=gap)
    ending = threading.Event()

    def hints():
        while not ending.is_set():
            observer.notify_commit()
            time.sleep(0.001)

    feeder = threading.Thread(target=hints)
    begun = time.monotonic()
    observer.start()
    feeder.start()
    time.sleep(1.4)
    ending.set()
    feeder.join(5)
    assert observer.stop(10).stopped is True
    elapsed = time.monotonic() - begun
    assert len(spans) >= 2  # the stream was served, not starved ...
    assert len(spans) <= elapsed / gap + 1  # ... and bounded by the gap whatever the hint rate
    for (_, ended), (started, _) in zip(spans, spans[1:]):
        assert started - ended >= gap - 0.005


def test_hinted_changes_made_inside_the_gap_reach_the_real_history_in_one_deferred_snapshot(tmp_path):
    env = build(tmp_path)
    try:
        env.history.initialize()
        gap, marks = 0.4, []

        def snapshot():
            marks.append(("start", time.monotonic()))
            try:
                return env.history.snapshot()
            finally:
                marks.append(("end", time.monotonic()))

        observer = HistoryObserver(snapshot, interval=LONG, min_gap=gap)
        observer.start()
        assert wait_for(lambda: observer.state().passes == 1 and not observer.state().snapshot_running)
        for number in (9, 10, 11):
            with env.database.transaction() as uow:
                uow.connection.execute("INSERT INTO work_items(id, scope, title, state) VALUES (?,'global','Hinted','open')",
                                       (f"item-{number}",))
            observer.notify_commit()  # three committed changes inside one cooldown
        assert wait_for(lambda: observer.state().passes == 2 and not observer.state().snapshot_running)
        assert [kind for kind, _ in marks] == ["start", "end", "start", "end"]  # one deferred pass, not one per hint
        assert marks[2][1] - marks[1][1] >= gap - 0.005  # and not before the deadline
        exported = b"".join(blob(env, path) for path in sorted(tree(env)) if path.startswith("database/work_items/")).decode()
        assert all(f"item-{number}" in exported for number in (9, 10, 11))  # one snapshot saw all of them
        assert observer.state().failures == 0 and observer.state().pending is False
        assert observer.stop(10).stopped is True
    finally:
        env.database.close()
