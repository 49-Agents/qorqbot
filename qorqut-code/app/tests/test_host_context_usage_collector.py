"""Acceptance cases: the Context usage collector against real accepted library receipts and real queue/usage tables.

A real host runtime (with the Context integration's usage ledger), a real ``qorqut_context`` store and ``Pipeline`` driven by a scripted
injected runner, and queue inputs advanced through the real Context integration queue transitions. The collector reads only the library's
read-only ``usage_receipts`` and writes only the two usage tables. Synthetic only: no model, provider, network, native session,
role adoption or operational data."""

import json
import sqlite3
import subprocess
import sys
import textwrap

import pytest
from qorqut_context.integration import usage as context_usage

from qorqut_context.integration.usage import CollectorStorageError, collection_coverage
from qorqut_context.integration.usage import collector as collector_module
from context_usage_support import SYSTEM, ProcessStopped, World, fail, propagate, snap, stop, with_cost

USAGE_TABLES = ("context_usage_calls", "context_usage_sync")
QUEUE_TABLES = ("context_inputs", "context_input_attempts", "context_input_controls", "inbox_events", "inbox_deliveries")


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class PageReader:
    """A fake reader over prepared pages (or exceptions); every request is recorded."""

    def __init__(self, *pages):
        self.pages, self.requests = list(pages), []

    def usage_receipts(self, event_id, *, limit, cursor=None):
        self.requests.append((event_id, limit, cursor))
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page(event_id) if callable(page) else page


def page(event_id, items=(), *, status="ready", cursor=None, error=None):
    return {"status": status, "event_id": event_id, "items": list(items), "next_cursor": cursor, "error": error}


def drain(collector, steps=20):
    """Step until the collector reports the end of a full traversal (a ``ready``/``unavailable`` pair followed by a wrap)."""
    return [collector.collect_next() for _ in range(steps)]


# -- the ordinary traversal --------------------------------------------------------------------------------------------------

def test_a_pair_is_collected_in_bounded_pages_and_a_repeat_or_restart_never_counts_twice(world):
    input_id, event = world.process("one")
    collector = world.collector(page_size=1, max_pages=1)
    first = collector.collect_next()
    assert (first.status, first.pages, first.inserted, first.input_id, first.context_event_id) == ("partial", 1, 1, input_id, event)
    assert world.sync() == {(input_id, event): "partial"}
    assert collector.diagnostics()["current"] == {"input_id": input_id, "context_event_id": event, "pages_read": 1, "continues": True}
    second = collector.collect_next()
    assert (second.status, second.pages, second.inserted) == ("ready", 1, 1)
    assert world.sync() == {(input_id, event): "ready"}
    calls = world.usage_calls()
    assert [row["stage"] for row in calls] == ["bootstrap", "propagation"]
    assert collector.diagnostics()["current"] is None
    assert collector.diagnostics()["last_completed"] == {"input_sequence": 1, "context_event_id": event}

    # the next traversal (a wrap in this process, then a fresh process-like restart with different paging) replays unchanged
    again = collector.collect_next(), collector.collect_next()
    assert [step.status for step in again] == ["partial", "ready"] and sum(step.unchanged for step in again) == 2
    assert collector.diagnostics()["traversals"] == 1
    restarted = world.collector(page_size=100, max_pages=5).collect_next()
    assert (restarted.status, restarted.pages, restarted.inserted, restarted.unchanged) == ("ready", 1, 0, 2)
    assert world.usage_calls() == calls                                   # no row was rewritten or added


def test_a_step_reads_at_most_max_pages_pages_and_the_next_step_continues(world):
    for name in ("one", "two"):
        world.process(name)
    collector = world.collector(page_size=1, max_pages=1)
    steps = [collector.collect_next() for _ in range(4)]
    assert [(step.status, step.pages, step.inserted) for step in steps] == [("partial", 1, 1), ("ready", 1, 1)] * 2
    assert len(world.usage_calls()) == 4
    bounded = world.collector(page_size=1, max_pages=2).collect_next()
    assert (bounded.status, bounded.pages) == ("ready", 2)                 # two pages reach the end in one step


def test_pairs_are_visited_by_input_sequence_then_event_and_the_traversal_wraps(world):
    (first_input, first_event), (second_input, second_event) = world.process("one"), world.process("two")
    collector = world.collector()
    assert collector.collect_next().status == "ready"
    steps = [collector.collect_next() for _ in range(2)]
    assert [(step.input_id, step.context_event_id) for step in steps] == [(second_input, second_event), (first_input, first_event)]
    assert world.sync() == {(first_input, first_event): "ready", (second_input, second_event): "ready"}
    assert collector.diagnostics()["traversals"] == 1


def test_an_empty_queue_is_idle_and_changes_nothing(world):
    collector = world.collector()
    world.enqueue("never-started")                                        # an input with no attempt has no event to read
    before = world.dump(*USAGE_TABLES, *QUEUE_TABLES)
    step = collector.collect_next()
    assert (step.status, step.input_id, step.pages) == ("idle", None, 0)
    assert world.dump(*USAGE_TABLES, *QUEUE_TABLES) == before
    assert collector.diagnostics()["steps"]["idle"] == 1 and collector.diagnostics()["state"] == "idle"


def test_a_retry_attempt_on_the_same_event_is_one_pair(world):
    from qorqut_context.integration.queue import transitions

    input_id, event = world.process("one", action=fail, ending="blocked")
    retry = world.instance.invoke(world.instance.human(), "context_queue_retry", {"input_id": input_id, "expected_state": "blocked"})
    assert retry.status == "ok", retry.error
    with world.database.transaction() as uow:
        transitions.start_attempt(uow, driver_id=SYSTEM, input_id=input_id, context_event_id=event)
    assert world.rows("SELECT COUNT(*) AS n FROM context_input_attempts WHERE input_id=?", input_id)[0]["n"] == 2
    with world.database.read() as uow:
        assert collection_coverage(uow.connection)["reserved_events"] == 1            # two attempts, one reserved Context event
    collector = world.collector()
    first, second = collector.collect_next(), collector.collect_next()
    assert (first.status, first.inserted) == ("ready", 2) and (second.status, second.unchanged) == ("ready", 2)
    assert (first.input_id, first.context_event_id) == (second.input_id, second.context_event_id) == (input_id, event)
    assert collector.diagnostics()["traversals"] == 1                      # the second step wrapped to the only pair


# -- real library evidence ---------------------------------------------------------------------------------------------------

def test_later_cumulative_and_terminal_evidence_updates_the_same_call_in_place(world):
    collector = world.collector()
    during = []

    def usage(stage, index, emit):
        if stage == "propagation":
            emit(snap(index, 10))
            during.append((collector.collect_next(), world.usage_calls()))      # mid-call: a started call, partial counters
        final = snap(index, 40 if stage == "propagation" else 10)
        emit(final)
        return final

    input_id, event = world.process("one", usage=usage)
    (mid_step, mid_calls), = during
    assert (mid_step.status, mid_step.inserted) == ("ready", 2)
    assert [(row["stage"], row["state"], row["input_tokens"]) for row in mid_calls] == [
        ("bootstrap", "completed", 10), ("propagation", "started", 10)]
    assert mid_calls[1]["finished_at"] is None
    step = collector.collect_next()
    assert (step.status, step.inserted, step.updated, step.unchanged) == ("ready", 0, 1, 1)
    final = {row["stage"]: row for row in world.usage_calls()}["propagation"]
    assert (final["state"], final["input_tokens"], final["finished_at"] is not None) == ("completed", 40, True)
    assert final["call_id"] == mid_calls[1]["call_id"] and final["recorded_at"] == mid_calls[1]["recorded_at"]   # same row
    assert world.sync() == {(input_id, event): "ready"}
    again = collector.collect_next()
    assert (again.updated, again.unchanged) == (0, 2)


def test_failed_and_unfinished_calls_keep_their_observed_usage_and_a_retry_is_a_separate_call(world):
    (first_input, first_event) = world.process("one", action=fail, ending="blocked")
    collector = world.collector()
    assert collector.collect_next().status == "ready"
    failed = {row["stage"]: row for row in world.usage_calls()}
    assert (failed["bootstrap"]["state"], failed["propagation"]["state"]) == ("completed", "failed")
    assert (failed["propagation"]["input_tokens"], failed["propagation"]["error_type"]) == (20, "RuntimeError")
    assert failed["propagation"]["error_message"] == "transport disappeared"

    world.runner.action = propagate
    receipt = world.pipeline.retry(first_event)                            # the library retry: a new propagation call
    assert receipt["event_id"] == first_event
    step = collector.collect_next()
    assert (step.status, step.inserted, step.unchanged) == ("ready", 1, 2)
    calls = world.usage_calls()
    assert [(row["library_attempt_sequence"], row["stage"], row["state"]) for row in calls] == [
        (1, "bootstrap", "completed"), (1, "propagation", "failed"), (2, "propagation", "completed")]
    assert calls[2]["bootstrap_call_id"] == calls[0]["call_id"]            # the retry reuses the original bootstrap sample
    assert sum(row["stage"] == "bootstrap" for row in calls) == 1
    assert calls[1]["input_tokens"] == 20 and calls[2]["input_tokens"] == 30   # separate calls, never summed or replaced


def test_a_crashed_call_is_unfinished_with_its_observed_usage_not_free(world):
    input_id, event = world.process("one", action=stop, ending="open")
    collector = world.collector()
    assert collector.collect_next().status == "ready"
    calls = {row["stage"]: row for row in world.usage_calls()}
    assert (calls["bootstrap"]["state"], calls["propagation"]["state"]) == ("completed", "started")
    assert calls["propagation"]["finished_at"] is None and calls["propagation"]["input_tokens"] == 20
    assert world.rows("SELECT state FROM context_inputs WHERE id=?", input_id)[0]["state"] == "processing"   # the queue is untouched


def test_observed_provider_costs_are_collected_exactly_and_an_absent_charge_stays_null(world):
    world.process("one", usage=with_cost(0.1234567))
    world.process("two")
    collector = world.collector()
    drain(collector, 4)
    costs = [row["cost_usd"] for row in world.usage_calls()]
    assert sorted(cost for cost in costs if cost is not None) == ["0.1234567", "0.1234567"]
    assert costs.count(None) == 2


def test_an_unavailable_pair_keeps_prior_rows_and_never_blocks_the_next_pair(world):
    (first_input, first_event), (second_input, second_event) = world.process("one"), world.process("two")
    collector = world.collector()
    drain(collector, 2)
    kept = world.usage_calls()
    path = world.receipt_files(first_event)[0]
    good = path.read_bytes()
    path.write_bytes(b"{ not json")                                       # corrupt private evidence
    steps = drain(collector, 4)
    corrupt = [step for step in steps if step.context_event_id == first_event]
    assert corrupt and all((step.status, step.error["code"]) == ("unavailable", "corrupt_evidence") for step in corrupt)
    assert any(step.status == "ready" and step.context_event_id == second_event for step in steps)   # the other pair progressed
    assert world.usage_calls() == kept                                    # earlier imports stay as they were
    row = world.rows("SELECT status, error_code, error_message FROM context_usage_sync WHERE input_id=?", first_input)[0]
    assert row["status"] == "unavailable" and row["error_code"] and len(row["error_message"]) <= 300
    assert collector.diagnostics()["last_error"]["input_id"] == first_input
    path.write_bytes(good)                                                # evidence restored: the next traversal recovers
    assert any(step.status == "ready" and step.context_event_id == first_event for step in drain(collector, 4))
    assert world.sync()[(first_input, first_event)] == "ready"


# -- the page contract -------------------------------------------------------------------------------------------------------

def real_page(world, event):
    return world.pipeline.usage_receipts(event, limit=100)


def test_a_valid_empty_ready_page_proves_only_that_nothing_was_observed(world):
    input_id, event = world.process("one", usage=None)                    # a runner reporting no usage at all
    step = world.collector().collect_next()
    assert (step.status, step.inserted) == ("ready", 2)
    assert [(row["state"], row["input_tokens"], row["cost_usd"]) for row in world.usage_calls()] == [
        ("completed", None, None)] * 2                                    # calls without counters: unknown, never zero
    kept = world.usage_calls()
    empty = world.collector(PageReader(page(event))).collect_next()       # a later empty ready page removes and invents nothing
    assert (empty.status, empty.inserted, empty.updated, empty.unchanged) == ("ready", 0, 0, 0)
    assert world.usage_calls() == kept and world.sync() == {(input_id, event): "ready"}


@pytest.mark.parametrize("make, code", [
    (lambda e, items: "not a page", "invalid_page"),
    (lambda e, items: {**page(e, items), "extra": 1}, "invalid_page"),
    (lambda e, items: page("zzzzz", items), "invalid_page"),                                   # another event's page
    (lambda e, items: page(e, [{**items[0], "event_id": "zzzzz"}]), "invalid_page"),           # another event's receipt
    (lambda e, items: page(e, [], cursor="c1"), "invalid_page"),                               # empty page claiming continuation
    (lambda e, items: page(e, items, cursor=7), "invalid_page"),                               # continuation of the wrong type
    (lambda e, items: page(e, items, cursor=""), "invalid_page"),
    (lambda e, items: page(e, items, status="ready", error={"code": "x", "message": "y"}), "invalid_page"),
    (lambda e, items: page(e, items, status="unavailable", error={"code": "corrupt_evidence", "message": "m"}), "invalid_page"),
    (lambda e, items: page(e, [], status="unavailable", error={"code": "", "message": "m"}), "invalid_page"),
    (lambda e, items: page(e, [], status="unavailable", error={"code": "x" * 65, "message": "m"}), "invalid_page"),
    (lambda e, items: page(e, [], status="unavailable", error={"code": "corrupt_evidence", "message": "m" * 301}), "invalid_page"),
    (lambda e, items: page(e, [], status="unavailable", error=None), "invalid_page"),
    (lambda e, items: page(e, items, status="weird"), "invalid_page"),
    (lambda e, items: page(e, items + items + items), "invalid_page"),                         # more receipts than requested
    (lambda e, items: {**page(e, items), "items": "x"}, "invalid_page"),
    (lambda e, items: page(e, [{**items[0], "stage": "propagation", "usage": {"input_tokens": -1}}]), "invalid_receipt"),
])
def test_a_page_that_breaks_the_contract_is_never_ingested_and_the_scan_moves_on(world, make, code):
    input_id, event = world.process("one")
    items = real_page(world, event)["items"]
    reader = PageReader(lambda e: make(e, items))
    collector = world.collector(reader, page_size=2)
    step = collector.collect_next()
    assert (step.status, step.error["code"]) == ("unavailable", code)
    assert world.usage_calls() == []
    sync = world.rows("SELECT status, error_code, error_message FROM context_usage_sync")[0]
    assert (sync["status"], sync["error_code"]) == ("unavailable", code) and len(sync["error_message"]) <= 300
    assert collector.diagnostics()["current"] is None and collector.diagnostics()["state"] == "unavailable"


def test_a_repeated_cursor_stops_the_pair_and_keeps_what_was_already_saved(world):
    input_id, event = world.process("one")
    first, second = real_page(world, event)["items"]
    reader = PageReader(page(event, [first], cursor="c1"), page(event, [second], cursor="c1"))
    collector = world.collector(reader, page_size=1, max_pages=5)
    step = collector.collect_next()
    assert (step.status, step.pages, step.error["code"]) == ("unavailable", 2, "invalid_page")
    assert [row["stage"] for row in world.usage_calls()] == ["bootstrap"]          # the first page was valid and stays
    assert world.sync() == {(input_id, event): "unavailable"}
    assert reader.requests == [(event, 1, None), (event, 1, "c1")]


def test_a_library_unavailable_page_and_a_read_failure_are_recorded_and_advance(world):
    (first_input, first_event), (second_input, second_event) = world.process("one"), world.process("two")
    first_items, second_items = (real_page(world, event)["items"] for event in (first_event, second_event))
    unavailable = page(first_event, status="unavailable", error={"code": "corrupt_evidence", "message": "A receipt is corrupt"})
    reader = PageReader(unavailable, RuntimeError("boom " * 200), page(first_event, first_items), page(second_event, second_items))
    collector = world.collector(reader)
    first, failed, recovered, last = (collector.collect_next() for _ in range(4))
    assert (first.status, first.input_id, first.error) == ("unavailable", first_input,
                                                           {"code": "corrupt_evidence", "message": "A receipt is corrupt"})
    assert (failed.status, failed.input_id, failed.error["code"]) == ("unavailable", second_input, "read_failed")
    assert len(failed.error["message"]) <= 300 and "boom" not in failed.error["message"]      # the reader's text is not echoed
    assert (recovered.status, recovered.input_id, recovered.inserted) == ("ready", first_input, 2)   # the wrap revisited it
    assert (last.status, last.input_id, last.inserted) == ("ready", second_input, 2)
    assert world.sync() == {(first_input, first_event): "ready", (second_input, second_event): "ready"}


@pytest.mark.parametrize("change", [lambda item: {**item, "configured": {**item["configured"], "model": "another-model"}},
                                    lambda item: {**item, "started_at": "2026-01-01T00:00:00.000000Z"},
                                    lambda item: {**item, "state": "failed", "error": {"type": "RuntimeError", "message": "late"}}])
def test_ledger_identity_and_outcome_conflicts_are_refused_as_unavailable_never_overwritten(world, change):
    input_id, event = world.process("one")
    items = real_page(world, event)["items"]
    assert world.collector().collect_next().status == "ready"
    kept = world.usage_calls()
    step = world.collector(PageReader(page(event, [change(items[0])]))).collect_next()
    assert (step.status, step.error["code"]) == ("unavailable", "receipt_conflict")
    assert world.usage_calls() == kept
    assert world.sync() == {(input_id, event): "unavailable"}


# -- crash windows and storage loss ------------------------------------------------------------------------------------------

def test_a_failure_while_saving_a_page_rolls_it_back_and_a_later_traversal_completes_it(world, monkeypatch):
    input_id, event = world.process("one")
    real = context_usage.ingest

    def broken(uow, **kwargs):
        real(uow, **kwargs)
        raise sqlite3.OperationalError("disk I/O error")                  # after the rows were written, before the commit

    monkeypatch.setattr(collector_module.context_usage, "ingest", broken)
    collector = world.collector()
    step = collector.collect_next()
    assert (step.status, step.error["code"]) == ("unavailable", "storage_error")
    assert world.usage_calls() == []                                      # the page and its status were one unit of work
    assert world.sync() == {(input_id, event): "unavailable"}
    monkeypatch.undo()
    assert collector.collect_next().status == "ready"
    assert len(world.usage_calls()) == 2 and world.sync() == {(input_id, event): "ready"}


def test_a_process_stop_after_a_committed_page_replays_safely_after_restart(world):
    input_id, event = world.process("one")
    first = real_page(world, event)["items"][0]
    reader = PageReader(page(event, [first], cursor="c1"), ProcessStopped())
    collector = world.collector(reader, page_size=1, max_pages=2)
    with pytest.raises(ProcessStopped):
        collector.collect_next()                                          # page one is committed, then the process "stops"
    assert [row["stage"] for row in world.usage_calls()] == ["bootstrap"] and world.sync() == {(input_id, event): "partial"}
    restarted = world.collector(page_size=100)
    step = restarted.collect_next()                                       # fresh traversal: no in-memory state survives a stop
    assert (step.status, step.inserted, step.unchanged) == ("ready", 1, 1)
    assert [row["stage"] for row in world.usage_calls()] == ["bootstrap", "propagation"]
    assert world.collector().collect_next().unchanged == 2                # the lock was released and replays change nothing


def test_losing_the_write_database_is_an_explicit_error_never_a_ready_scan(world, monkeypatch):
    input_id, event = world.process("one")
    collector = world.collector()
    before = world.dump(*USAGE_TABLES)

    def lost(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(world.database, "transaction", lost)
    with pytest.raises(CollectorStorageError):
        collector.collect_next()
    diagnostics = collector.diagnostics()
    assert diagnostics["storage_failures"] == 1 and diagnostics["last_error"]["code"] == "storage_error"
    assert diagnostics["steps"]["ready"] == 0 and diagnostics["state"] is None
    monkeypatch.undo()
    assert world.dump(*USAGE_TABLES) == before
    assert collector.collect_next().status == "ready"                     # the lock was released and the pair is still collectable


def test_an_unreadable_inventory_is_an_explicit_error_not_idle(world, monkeypatch):
    world.process("one")
    collector = world.collector()
    monkeypatch.setattr(world.database, "read", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(CollectorStorageError):
        collector.collect_next()
    assert collector.diagnostics()["steps"]["idle"] == 0 and collector.diagnostics()["storage_failures"] == 1


def test_a_driver_that_is_not_the_system_principal_cannot_record_anything(world):
    world.process("one")
    collector = world.collector(driver_id=world.owner)
    with pytest.raises(CollectorStorageError):
        collector.collect_next()
    assert world.usage_calls() == [] and world.sync() == {}


# -- the installation lock ---------------------------------------------------------------------------------------------------

def test_another_process_holding_the_installation_lock_makes_a_step_busy(world):
    input_id, event = world.process("one")
    world.lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
        import fcntl, os, sys
        descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        print("held", flush=True)
        sys.stdin.readline()
    """), str(world.lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        collector = world.collector()
        before = world.dump(*USAGE_TABLES)
        step = collector.collect_next()
        assert (step.status, step.pages, step.input_id) == ("busy", 0, None)
        assert world.dump(*USAGE_TABLES) == before and collector.diagnostics()["steps"]["busy"] == 1
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
    assert collector.collect_next().status == "ready"                     # released: the next step proceeds


def test_the_lock_is_private_nonblocking_and_must_be_an_explicit_absolute_path(world, tmp_path):
    collector = world.collector()
    collector.collect_next()
    assert world.lock.stat().st_mode & 0o777 == 0o600 and world.lock.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError):
        world.collector(lock_path="relative.lock")
    target = tmp_path / "elsewhere"
    target.write_text("x")
    (tmp_path / "link.lock").symlink_to(target)
    with pytest.raises(OSError):
        world.collector(lock_path=tmp_path / "link.lock").collect_next()      # a symlinked lock is refused, never followed


@pytest.mark.parametrize("options", [{"driver_id": ""}, {"driver_id": None}, {"page_size": 0}, {"page_size": 501},
                                     {"page_size": True}, {"max_pages": 0}, {"max_pages": 101}, {"max_pages": 1.5}])
def test_constructor_options_are_closed_and_bounded(world, options):
    with pytest.raises(ValueError):
        world.collector(**options)


# -- what a step never does --------------------------------------------------------------------------------------------------

def test_collection_changes_only_the_two_usage_tables(world):
    world.process("one"), world.process("two", action=fail, ending="blocked")
    queue_before = world.dump(*QUEUE_TABLES)
    other = ("principals", "businesses", "installation", "jobs", "idempotency_receipts")
    others_before = world.dump(*other)
    collector = world.collector(page_size=1, max_pages=1)
    drain(collector, 12)
    assert world.dump(*QUEUE_TABLES) == queue_before                      # no attempt started or retried, no state, no notice
    assert world.dump(*other) == others_before
    assert len(world.sync()) == 2 and set(world.sync().values()) == {"ready"}
    assert len(world.usage_calls()) == 4


def test_diagnostics_distinguish_busy_idle_partial_ready_and_unavailable(world):
    collector = world.collector(page_size=1, max_pages=1)
    assert collector.diagnostics()["state"] is None
    assert collector.collect_next().status == "idle"
    input_id, event = world.process("one")
    assert collector.collect_next().status == "partial" and collector.diagnostics()["state"] == "partial"
    assert collector.collect_next().status == "ready" and collector.diagnostics()["state"] == "ready"
    world.receipt_files(event)[0].write_bytes(b"[")
    drain(collector, 2)
    diagnostics = collector.diagnostics()
    assert diagnostics["steps"]["unavailable"] >= 1 and diagnostics["last"]["status"] == "unavailable"
    assert diagnostics["steps"]["idle"] == 1 and set(diagnostics["steps"]) == {"busy", "idle", "partial", "ready", "unavailable"}
    assert json.loads(json.dumps(diagnostics)) == diagnostics             # primitive values: the lifecycle owner can log it
    assert diagnostics["page_size"] == 1 and diagnostics["max_pages"] == 1


def test_the_collector_has_no_scheduler_thread_or_model_dependency():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(collector_module))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        if isinstance(node, ast.ImportFrom):
            names |= {(node.module or "").split(".")[0]} | {alias.name for alias in node.names}
    owned = {"Thread", "Timer", "asyncio", "sched", "subprocess", "multiprocessing", "qorqut_context", "start_attempt",
             "record_published", "record_blocked", "record_discarded", "emit_event", "retry", "submit", "transitions"}
    assert not names & owned, names & owned
    assert {"ingest", "record_scan", "usage_receipts"} <= names
