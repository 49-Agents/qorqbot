"""The Context model-call usage ledger on the real engine queue: genuine library-format receipts bound to real disposable
queue inputs and attempts, atomic ingest, immutable identity, cumulative replacement, stale protection, truthful sync
status, stable pages and exact money. Nothing external: no Context library, model, provider or filesystem."""

from __future__ import annotations

import ast
import copy
import decimal
from decimal import Decimal
import json
import math
import pathlib
import sqlite3

import pytest

from operational_support import AGENT, BUSINESS, COORDINATOR, HUMAN, OTHER, OTHER_AGENT, OperationalHost
from qorqut_context.integration import usage as context_usage
from qorqut_operations.contracts import DomainError
from support import WORKER
from usage_support import EVENT, FULL, NOON, TOKENS, Ledger, attempt_id, native, receipt

SECOND = "cd34e"


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path)


@pytest.fixture
def bound(ledger):
    """One business input with its first queue attempt on EVENT."""
    input_id = ledger.submit()
    ledger.attempt(input_id)
    return input_id


def refused(ledger, input_id, *receipts, **kwargs):
    with pytest.raises(DomainError) as error:
        ledger.ingest(input_id, *receipts, **kwargs)
    return error.value


def snapshot(ledger):
    return ledger.host.query("SELECT * FROM context_usage_calls ORDER BY call_id")


# -- 1. assembly and shape ---------------------------------------------------------------------------------------------------

def test_the_ledger_is_an_explicit_contribution_with_no_endpoint_and_no_base_engine_import(ledger):
    contribution = context_usage.storage_contribution()
    assert contribution.owner == "context-usage" and set(contribution.tables) == {"context_usage_calls", "context_usage_sync"}
    assert contribution.required_tables == {"context_inputs", "context_input_attempts"}
    assert not [name for name in ledger.host.registry._entries if "usage" in name]              # no catalog endpoint is declared
    # The ledger modules (the write API, queries and storage; the collector and the owner read beside them are the host's
    # collection side) import no process, thread, network or file machinery and nothing of the base Context engine.
    package = pathlib.Path(context_usage.__file__).parent
    own = "qorqut_context.integration.usage"
    imported = set()
    for name in ("contract", "ledger", "queries", "schema"):
        for node in ast.walk(ast.parse((package / f"{name}.py").read_text())):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module or "")
    imported = {name for name in imported if not name.startswith(own)}
    assert not {name.split(".")[0] for name in imported} & {"subprocess", "threading", "socket", "urllib", "http", "asyncio", "sched", "os", "pathlib", "shutil"}
    assert not [name for name in imported if name.split(".")[0] == "qorqut_context"]
    assembly = ledger.database.assembly
    assert {"context_usage_calls", "context_usage_sync"} <= set(assembly.tables)
    assert assembly.required_tables["context_inputs"] == ("context-usage",) or "context-usage" in assembly.required_tables["context_inputs"]
    assert "context-usage" in assembly.required_tables["context_input_attempts"]
    assert assembly.tables["context_usage_calls"].scope_column == "business_id"


def test_a_host_composed_without_the_contribution_has_no_ledger_tables(tmp_path):
    plain = OperationalHost(tmp_path)
    assert plain.query("SELECT name FROM sqlite_master WHERE name LIKE 'context_usage%'") == []


# -- 2. initial ingest: binding, inferred namespace, separate configured and observed facts ---------------------------------------

def test_initial_ingest_binds_calls_to_the_input_event_and_the_namespace_inferred_from_the_input(ledger):
    mine = ledger.submit()
    ledger.attempt(mine, finish="published")
    global_input = ledger.submit(ctx=ledger.host.owner(None))
    ledger.attempt(global_input, SECOND)
    result = ledger.ingest(mine, receipt(1, "bootstrap"), native(1, "propagation", state="completed", bootstrap=attempt_id(1) + ":bootstrap"))
    assert (result.inserted, result.updated, result.unchanged, result.stale) == (2, 0, 0, 0)
    assert [(item.call_id, item.outcome) for item in result.calls] == [(attempt_id(1) + ":bootstrap", "inserted"),
                                                                        (attempt_id(1) + ":propagation", "inserted")]
    ledger.ingest(global_input, receipt(2, "propagation", event=SECOND, state="failed"))
    business_call = ledger.row(attempt_id(1) + ":propagation")
    assert business_call["business_id"] == BUSINESS and business_call["input_id"] == mine and business_call["context_event_id"] == EVENT
    assert business_call["library_attempt_sequence"] == 1 and business_call["stage"] == "propagation"
    assert ledger.row(attempt_id(2) + ":propagation")["business_id"] is None                  # Global is inferred, never supplied
    # Configured and observed identities stay separate; unknown stays unknown.
    assert (business_call["configured_model"], business_call["observed_model"], business_call["observed_provider"]) == (
        "gpt-6-astra", "gpt-6-observed", "openai")
    unknown = ledger.row(attempt_id(1) + ":bootstrap")
    assert (unknown["configured_model"], unknown["observed_model"], unknown["observed_provider"], unknown["native_thread_id"]) == (
        "gpt-6-astra", None, None, None)
    assert all(unknown[name] is None for name in TOKENS) and unknown["cost_usd"] is None
    with pytest.raises(TypeError):
        context_usage.ingest(None, driver_id=WORKER, input_id=mine, receipts=[], business_id=BUSINESS)       # no caller-provided scope


# -- 3. replay, cumulative replacement, terminal advance -------------------------------------------------------------------------

def test_identical_replay_is_a_no_op_and_a_later_observation_replaces_cumulative_counters(ledger, bound):
    first = native(1, "propagation", tokens={"input_tokens": 10, "total_tokens": 12}, cost=0.5, updates=1)
    assert ledger.ingest(bound, first).inserted == 1
    before = ledger.row(first["call_id"])
    replay = ledger.ingest(bound, copy.deepcopy(first))
    assert (replay.inserted, replay.updated, replay.unchanged, replay.stale) == (0, 0, 1, 0)
    assert ledger.row(first["call_id"]) == before                                              # nothing, not even updated_at, moved
    later = native(1, "propagation", tokens={"input_tokens": 25, "output_tokens": 3, "total_tokens": 30}, cost=0.75, updates=2)
    assert ledger.ingest(bound, later).updated == 1
    row = ledger.row(first["call_id"])
    assert (row["input_tokens"], row["output_tokens"], row["total_tokens"], row["cost_usd"], row["usage_updates"]) == (25, 3, 30, "0.75", 2)
    assert row["cached_input_tokens"] is None and row["reasoning_output_tokens"] is None      # replaced as a whole, never merged or added
    assert row["recorded_at"] == before["recorded_at"]
    done = native(1, "propagation", tokens={"input_tokens": 25, "output_tokens": 3, "total_tokens": 30}, cost=0.75, updates=2, state="completed")
    assert ledger.ingest(bound, done).updated == 1
    final = ledger.row(first["call_id"])
    assert final["state"] == "completed" and final["finished_at"] and final["input_tokens"] == 25
    assert ledger.ingest(bound, copy.deepcopy(done)).unchanged == 1


def test_failed_and_unfinished_calls_are_retained_with_unknown_values_left_unknown(ledger, bound):
    failed = native(1, "propagation", state="failed", tokens={"input_tokens": 30}, error={"type": "RuntimeError", "message": "transport disappeared"})
    unfinished = receipt(1, "bootstrap")
    ledger.ingest(bound, unfinished, failed)
    row = ledger.row(failed["call_id"])
    assert (row["state"], row["error_type"], row["error_message"], row["input_tokens"], row["total_tokens"]) == (
        "failed", "RuntimeError", "transport disappeared", 30, None)
    open_call = ledger.row(unfinished["call_id"])
    assert open_call["state"] == "started" and open_call["finished_at"] is None and all(open_call[name] is None for name in TOKENS)
    page = ledger.page()
    by_state = {call.state: call for call in page.calls}
    assert by_state["started"].evidence["limitations"] == ["unfinished_call", "usage_unreported", "cost_unreported"]
    assert by_state["failed"].error == {"type": "RuntimeError", "message": "transport disappeared"}
    assert by_state["failed"].evidence["limitations"] == ["partial_counters", "final_totals_unproven", "cost_unreported"]
    assert by_state["failed"].evidence["usage_complete"] is False and by_state["failed"].evidence["counters_complete"] is False
    totals = ledger.totals()
    assert (totals.calls, totals.unfinished_calls, totals.unknown_cost_calls, totals.calls_without_token_usage) == (2, 1, 2, 1)
    assert totals.known_cost_usd is None                                                         # not an invented zero


# -- 4. bootstrap reuse, retries, the library attempt sequence ---------------------------------------------------------------------

def test_a_reused_bootstrap_and_a_distinct_retry_are_each_counted_once(ledger):
    input_id = ledger.submit()
    ledger.attempt(input_id, finish="blocked")
    ledger.retry(input_id)
    ledger.attempt(input_id)                                      # queue attempt 2, the same Context event
    original = native(1, "bootstrap", state="completed", cost=0.1, thread="t1", turn="u1")
    failed = native(1, "propagation", state="failed", cost=0.2, thread="t2", turn="u2", bootstrap=original["call_id"])
    reused = native(2, "propagation", state="completed", cost=0.05, thread="t3", turn="u3", sequence=7, bootstrap=original["call_id"])
    # The retry's propagation may arrive before the original bootstrap it references; the reference is stored, not charged.
    assert ledger.ingest(input_id, reused).inserted == 1
    assert ledger.ingest(input_id, original, failed, reused).inserted == 2
    totals = ledger.totals(input_id=input_id)
    assert (totals.calls, totals.known_cost_usd) == (3, Decimal("0.35"))                         # 0.1 once, not once per reference
    assert ledger.row(reused["call_id"])["library_attempt_sequence"] == 7                         # its own number, never the queue attempt
    assert ledger.row(reused["call_id"])["bootstrap_call_id"] == original["call_id"]
    assert [call.stage for call in ledger.page(input_id=input_id).calls if call.stage == "bootstrap"] == ["bootstrap"]
    assert ledger.host.query("SELECT attempt FROM context_input_attempts WHERE input_id=? ORDER BY attempt", input_id) == [
        {"attempt": 1}, {"attempt": 2}]


def test_a_bootstrap_reference_must_name_a_call_of_the_same_event(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit()
    ledger.attempt(second, SECOND)
    ledger.ingest(first, receipt(1, "bootstrap", state="completed"))
    error = refused(ledger, second, receipt(2, "propagation", event=SECOND, bootstrap=attempt_id(1) + ":bootstrap"))
    assert error.code == "validation_error" and "receipts[0].bootstrap_call_id" in error.field_errors
    assert ledger.row(attempt_id(2) + ":propagation") == {}


def test_a_bootstrap_stays_in_one_input_and_event_in_either_arrival_order(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit(path="foundation/notes/b.md")
    ledger.attempt(second, SECOND, finish="published")
    reference = attempt_id(9) + ":bootstrap"
    ledger.ingest(first, receipt(10, "propagation", bootstrap=reference, state="completed"))      # the bootstrap is not stored yet
    before = snapshot(ledger)
    # The reversed order: the late bootstrap of another input and event cannot take the reference over, and nothing in its batch lands.
    error = refused(ledger, second, receipt(11, "bootstrap", event=SECOND), receipt(9, "bootstrap", event=SECOND, state="completed"))
    assert error.code == "validation_error" and list(error.field_errors) == ["receipts[1].call_id"]
    assert snapshot(ledger) == before and ledger.row(reference) == {}                             # and no bootstrap row is invented
    # The same input and event may deliver it later; the reference is then resolved and counted once.
    assert ledger.ingest(first, receipt(9, "bootstrap", state="completed")).inserted == 1
    assert ledger.ingest(first, receipt(9, "bootstrap", state="completed")).unchanged == 1
    assert ledger.totals(input_id=first).calls == 2 and ledger.row(reference)["input_id"] == first
    assert ledger.row(attempt_id(10) + ":propagation")["bootstrap_call_id"] == reference


def test_a_second_unresolved_reference_from_another_input_or_event_is_refused_atomically(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit(path="foundation/notes/b.md")
    ledger.attempt(second, SECOND, finish="published")
    reference = attempt_id(9) + ":bootstrap"
    ledger.ingest(first, receipt(10, "propagation", bootstrap=reference))
    before = snapshot(ledger)
    error = refused(ledger, second, receipt(12, "bootstrap", event=SECOND),
                    receipt(11, "propagation", event=SECOND, bootstrap=reference))
    assert error.code == "validation_error" and list(error.field_errors) == ["receipts[1].bootstrap_call_id"]
    assert snapshot(ledger) == before                                                             # the valid new call was not written either
    # Further references from the owning input and event are accepted, including before the bootstrap exists, and replay is a no-op.
    result = ledger.ingest(first, receipt(10, "propagation", bootstrap=reference), receipt(13, "propagation", bootstrap=reference))
    assert (result.inserted, result.unchanged) == (1, 1)
    # A batch may carry the reference and its bootstrap in either order.
    for number, ordered in ((20, lambda a, b: (a, b)), (30, lambda a, b: (b, a))):
        ref = attempt_id(number) + ":bootstrap"
        propagation = receipt(number + 1, "propagation", bootstrap=ref)
        bootstrap = receipt(number, "bootstrap", state="completed")
        assert ledger.ingest(first, *ordered(bootstrap, propagation)).inserted == 2
    assert ledger.totals(input_id=first).calls == 6 and ledger.totals(input_id=second).calls == 0


def test_a_bootstrap_reused_across_library_attempts_and_arriving_late_is_charged_once(ledger, bound):
    original = native(1, "bootstrap", state="completed", cost=0.1)
    first = native(1, "propagation", state="failed", cost=0.2, bootstrap=original["call_id"])
    retry = native(2, "propagation", state="completed", cost=0.3, sequence=2, bootstrap=original["call_id"])
    assert ledger.ingest(bound, retry, first).inserted == 2                                       # both name the not-yet-stored bootstrap
    assert ledger.totals(input_id=bound).known_cost_usd == Decimal("0.5")
    assert ledger.ingest(bound, original, retry, first).inserted == 1                             # it arrives late; the others are unchanged
    assert ledger.totals(input_id=bound).known_cost_usd == Decimal("0.6")
    assert ledger.totals(input_id=bound).calls == 3


def test_the_database_refuses_a_bootstrap_link_that_leaves_its_input_and_event_and_uses_its_index(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit(path="foundation/notes/b.md")
    ledger.attempt(second, SECOND, finish="published")
    reference = attempt_id(9) + ":bootstrap"
    ledger.ingest(first, receipt(10, "propagation", bootstrap=reference))
    insert = ("INSERT INTO context_usage_calls(call_id, business_id, input_id, context_event_id, attempt_id, library_attempt_sequence, "
              "stage, state, started_at, configured_model, configured_effort, configured_service_tier, usage_updates, ignored_updates, "
              "bootstrap_call_id, recorded_at, updated_at) VALUES (?,?,?,?,?,1,?,'started',?, 'm','e','t',0,0,?,'now','now')")
    for call_id, attempt, stage, link in ((reference, attempt_id(9), "bootstrap", None),
                                          (attempt_id(11) + ":propagation", attempt_id(11), "propagation", reference)):
        with pytest.raises(DomainError) as guard:
            with ledger.database.transaction() as uow:
                uow.connection.execute(insert, (call_id, BUSINESS, second, SECOND, attempt, stage, NOON, link))
        assert isinstance(guard.value.__cause__, sqlite3.IntegrityError) and "bootstrap call" in str(guard.value.__cause__), call_id
    with ledger.database.transaction() as uow:                                                    # the owning input and event are fine
        uow.connection.execute(insert, (reference, BUSINESS, first, EVENT, attempt_id(9), "bootstrap", NOON, None))
        plan = uow.connection.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT bootstrap_call_id, input_id, context_event_id FROM context_usage_calls "
            "WHERE bootstrap_call_id IN (?, ?)", (reference, "x")).fetchall()
    assert any("context_usage_calls_by_bootstrap" in str(row[-1]) for row in plan), [tuple(row) for row in plan]


# -- 5. refusals: caller, event, scope, call identity; always atomic -----------------------------------------------------------------

def test_only_the_system_principal_writes_and_the_input_and_event_must_exist(ledger, bound):
    good = receipt(1, "bootstrap")
    for impostor in (AGENT, HUMAN, COORDINATOR, OTHER_AGENT, "missing", None):
        error = refused(ledger, bound, good, driver_id=impostor)
        assert error.code == "forbidden"
    with pytest.raises(DomainError) as unknown:
        ledger.ingest("00000000-0000-4000-8000-0000000000ff", good)
    assert unknown.value.code == "not_found"
    assert snapshot(ledger) == []
    queued = ledger.submit(path="foundation/notes/b.md")                                         # an input with no reserved event at all
    error = refused(ledger, queued, good)
    assert error.code == "validation_error" and "receipts[0].event_id" in error.field_errors


def test_a_receipt_for_another_input_or_event_refuses_the_whole_batch(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit(ctx=ledger.host.owner(None))
    ledger.attempt(second, SECOND)
    valid = receipt(1, "bootstrap")
    elsewhere = receipt(2, "bootstrap", event=SECOND)
    unknown_event = receipt(3, "bootstrap", event="zz99z")
    for foreign in (elsewhere, unknown_event):
        error = refused(ledger, first, valid, foreign)
        assert error.code == "validation_error" and list(error.field_errors) == ["receipts[1].event_id"]
    assert snapshot(ledger) == []                                                                # the valid receipt was not applied either
    ledger.ingest(second, elsewhere)
    assert ledger.row(elsewhere["call_id"])["business_id"] is None


def test_a_reused_call_id_with_another_immutable_identity_is_refused_without_changing_anything(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit()
    ledger.attempt(second, SECOND)
    stored = native(1, "propagation", bootstrap=attempt_id(1) + ":bootstrap")
    ledger.ingest(first, stored)
    before = snapshot(ledger)
    changed = (
        ("another input", second, receipt(1, "propagation", event=SECOND, bootstrap=stored["bootstrap_call_id"])),
        ("another start", first, native(1, "propagation", started="2026-10-12T12:00:00.000001Z", bootstrap=stored["bootstrap_call_id"])),
        ("another sequence", first, native(1, "propagation", sequence=2, bootstrap=stored["bootstrap_call_id"])),
        ("another model", first, native(1, "propagation", bootstrap=stored["bootstrap_call_id"],
                                        configured={"model": "other", "effort": "max", "service_tier": "default"})),
        ("another bootstrap", first, native(1, "propagation", bootstrap=attempt_id(9) + ":bootstrap")),
        ("no bootstrap", first, native(1, "propagation")),
    )
    for label, input_id, other in changed:
        error = refused(ledger, input_id, native(2, "bootstrap"), other) if input_id == first else refused(ledger, input_id, other)
        assert error.code == "usage_identity_conflict", label
        assert error.details["call_id"] == stored["call_id"]
    assert snapshot(ledger) == before                                                            # including the unrelated valid receipt


def test_invalid_receipts_are_refused_before_any_write(ledger, bound):
    ok = receipt(1, "bootstrap")

    def mutated(path, value):
        copied = copy.deepcopy(ok)
        target = copied
        for key in path[:-1]:
            target = target[key]
        if value is KeyError:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return copied

    bad = [
        mutated(("usage", "input_tokens"), True), mutated(("usage", "input_tokens"), -1), mutated(("usage", "input_tokens"), 1.5),
        mutated(("usage", "input_tokens"), "7"), mutated(("usage", "input_tokens"), 2 ** 63),
        mutated(("usage", "cost_usd"), math.nan), mutated(("usage", "cost_usd"), math.inf), mutated(("usage", "cost_usd"), -0.1),
        mutated(("usage", "cost_usd"), "0.1"), mutated(("usage", "cost_usd"), True), mutated(("usage", "cost_usd"), 1e10),
        mutated(("usage", "cost_usd"), 1e-60), mutated(("usage", "cost_usd"), KeyError), mutated(("usage", "extra"), 1),
        mutated(("evidence", "usage_complete"), True), mutated(("evidence", "usage_observed"), 1), mutated(("evidence", "cost_observed"), 0),
        mutated(("evidence", "counters_complete"), None), mutated(("evidence", "limitations"), ["unfinished_call"]),
        mutated(("evidence", "limitations"), "unfinished_call"), mutated(("evidence", "usage_scope"), "native"),
        mutated(("evidence", "usage_updates"), 1), mutated(("evidence", "usage_source"), "x"), mutated(("evidence", "ignored_updates"), True),
        mutated(("evidence", "extra"), 1), mutated(("evidence", "usage_scope"), KeyError),
        mutated(("call_id",), attempt_id(1) + ":propagation"), mutated(("call_id",), "short"), mutated(("attempt_id",), "A" * 32),
        mutated(("attempt_id",), "ab"), mutated(("stage",), "other"), mutated(("state",), "done"), mutated(("event_id",), "AB12C"),
        mutated(("event_id",), "ab12"), mutated(("attempt_sequence",), 0), mutated(("attempt_sequence",), True),
        mutated(("started_at",), "2026-10-12T12:00:00Z"), mutated(("started_at",), "2026-10-12 12:00:00.000000"),
        mutated(("finished_at",), NOON), mutated(("error",), {"type": "X", "message": "y"}),
        mutated(("configured", "model"), ""), mutated(("configured", "model"), "m" * 201), mutated(("configured", "extra"), "x"),
        mutated(("observed", "model"), 3), mutated(("observed", "thread_id"), ""), mutated(("bootstrap_call_id",), attempt_id(1) + ":bootstrap"),
        mutated(("format",), "other-v2"), mutated(("surprise",), 1), mutated(("prompt",), "secret"),
        {key: value for key, value in ok.items() if key != "error"},
        native(1, "propagation", state="completed", finished=None, thread="t", turn="u") | {"finished_at": None},
        native(1, "propagation", state="failed", error=None) | {"error": None},
        native(1, "propagation", state="failed", error={"type": "X", "message": "m" * 301}),
        native(1, "propagation", state="failed", error={"type": "X", "message": "m", "extra": 1}),
        native(1, "propagation", state="failed", error={"type": "", "message": "m"}),
        "not an object", None, ["list"],
    ]
    for index, invalid in enumerate(bad):
        with pytest.raises(DomainError) as error:
            ledger.ingest(bound, receipt(2, "bootstrap"), invalid)
        assert error.value.code == "validation_error", (index, invalid)
        assert error.value.field_errors, index
    assert snapshot(ledger) == []                                                                # every refusal was atomic
    tuple_batch = ledger.ingest(bound, receipt(3, "bootstrap"))
    assert tuple_batch.inserted == 1
    for container in ("not a list", {"a": 1}, None, 7):
        with pytest.raises(DomainError):
            with ledger.database.transaction() as uow:
                context_usage.ingest(uow, driver_id=WORKER, input_id=bound, receipts=container)
    # A stored-file item may carry its format marker; it must be the documented one.
    marked = receipt(4, "bootstrap") | {"format": "qorqut-context-call-v1"}
    assert ledger.ingest(bound, marked).inserted == 1


def test_a_batch_has_at_most_500_receipts_with_no_repeated_call_id(ledger, bound):
    batch = [receipt(index, "bootstrap", started=f"2026-10-12T12:00:{index % 60:02d}.{index:06d}Z") for index in range(1, 501)]
    result = ledger.ingest(bound, *batch)
    assert result.inserted == 500 and ledger.totals().calls == 500
    error = refused(ledger, bound, *batch, receipt(255, "propagation"))
    assert error.code == "validation_error" and "receipts" in error.field_errors
    duplicate = refused(ledger, bound, receipt(40, "bootstrap"), receipt(40, "bootstrap"))
    assert duplicate.field_errors == {"receipts[1].call_id": "repeats receipts[0]"}
    assert ledger.ingest(bound).inserted == 0                                                    # an empty batch is a valid no-op


# -- 6. stale observations, immutability of outcome and native identity --------------------------------------------------------------

def test_older_observations_never_roll_a_newer_row_back_and_conflicting_outcomes_are_refused(ledger, bound):
    key = attempt_id(1) + ":propagation"
    newer = native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.4, updates=3, ignored=2)
    ledger.ingest(bound, newer)
    kept = ledger.row(key)
    older = native(1, "propagation", tokens={"input_tokens": 10, "total_tokens": 12}, cost=0.1, updates=1)
    fewer_ignored = native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.4, updates=3, ignored=1)
    lost_identity = receipt(1, "propagation", tokens={"input_tokens": 99}, updates=4, provider="openai", model="gpt-6-observed")
    for stale in (older, fewer_ignored, lost_identity):
        result = ledger.ingest(bound, stale)
        assert (result.inserted, result.updated, result.unchanged, result.stale) == (0, 0, 0, 1)
        assert ledger.row(key) == kept                                                           # totals untouched
    # A reroute updates the observed model without changing the counters or the native identity.
    rerouted = native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.4, updates=3, ignored=2, model="gpt-6-rerouted")
    assert ledger.ingest(bound, rerouted).updated == 1
    assert (ledger.row(key)["observed_model"], ledger.row(key)["configured_model"], ledger.row(key)["input_tokens"]) == (
        "gpt-6-rerouted", "gpt-6-astra", 30)
    # The same number of updates cannot carry different counters or a different cost.
    for conflicting in (native(1, "propagation", tokens={"input_tokens": 31, "total_tokens": 32}, cost=0.4, updates=3, ignored=2, model="gpt-6-rerouted"),
                        native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.9, updates=3, ignored=2, model="gpt-6-rerouted")):
        assert refused(ledger, bound, conflicting).code == "usage_evidence_conflict"
    # A native thread or turn identity is never reassigned.
    for other in (native(1, "propagation", tokens=FULL, updates=4, thread="thread-2"), native(1, "propagation", tokens=FULL, updates=4, turn="turn-2")):
        assert refused(ledger, bound, other).code == "usage_identity_conflict"
    assert ledger.row(key)["native_thread_id"] == "thread-1" and ledger.row(key)["native_turn_id"] == "turn-1"

    done = native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.4, updates=3, ignored=2, model="gpt-6-rerouted",
                  state="completed")
    assert ledger.ingest(bound, done).updated == 1
    final = ledger.row(key)
    # A terminal call is final: a started snapshot is stale, a different outcome or content is a conflict.
    assert ledger.ingest(bound, rerouted).stale == 1 and ledger.row(key) == final
    smaller_terminal = native(1, "propagation", tokens={"input_tokens": 10}, updates=1, state="completed")
    assert ledger.ingest(bound, smaller_terminal).stale == 1
    failed = native(1, "propagation", tokens={"input_tokens": 30, "total_tokens": 32}, cost=0.4, updates=3, ignored=2, model="gpt-6-rerouted",
                    state="failed")
    assert refused(ledger, bound, failed).code == "usage_outcome_conflict"
    more = native(1, "propagation", tokens={"input_tokens": 40}, updates=4, ignored=2, model="gpt-6-rerouted", state="completed")
    assert refused(ledger, bound, more).code == "usage_outcome_conflict"
    assert ledger.row(key) == final
    # Even around the engine, the database keeps a terminal call and a native identity as they are.
    with pytest.raises(DomainError) as guard:
        with ledger.database.transaction() as uow:
            uow.connection.execute("UPDATE context_usage_calls SET input_tokens=1 WHERE call_id=?", (key,))
    assert isinstance(guard.value.__cause__, sqlite3.IntegrityError) and "final" in str(guard.value.__cause__)
    assert ledger.row(key) == final


def test_a_started_row_can_only_advance_to_its_real_terminal_state(ledger, bound):
    key = attempt_id(1) + ":bootstrap"
    ledger.ingest(bound, receipt(1, "bootstrap"))
    failed = receipt(1, "bootstrap", state="failed", error={"type": "TimeoutError", "message": "no answer"})
    assert ledger.ingest(bound, failed).updated == 1
    assert ledger.row(key)["state"] == "failed" and ledger.row(key)["error_type"] == "TimeoutError"
    assert refused(ledger, bound, receipt(1, "bootstrap", state="completed")).code == "usage_outcome_conflict"
    assert ledger.row(key)["state"] == "failed"


# -- 7. unknown, partial and exact money -------------------------------------------------------------------------------------------------

def test_unknown_and_partial_cost_stay_explicit_and_money_is_exact(ledger, bound):
    ledger.ingest(bound,
                  receipt(1, "bootstrap", state="completed", started="2026-10-12T12:00:01.000000Z"),
                  receipt(2, "bootstrap", state="completed", started="2026-10-12T12:00:02.000000Z", cost=0.1, tokens={"total_tokens": 5}),
                  receipt(3, "bootstrap", state="completed", started="2026-10-12T12:00:03.000000Z", cost=0.2, tokens=FULL),
                  receipt(4, "bootstrap", state="completed", started="2026-10-12T12:00:04.000000Z", cost=1, thread="t", turn="u", tokens=FULL),
                  receipt(5, "bootstrap", state="completed", started="2026-10-12T12:00:05.000000Z", cost=1e-07, tokens=FULL))
    totals = ledger.totals()
    assert totals.calls == 5 and totals.unknown_cost_calls == 1 and totals.calls_without_token_usage == 1
    assert totals.known_cost_usd == Decimal("1.3000001") and isinstance(totals.known_cost_usd, Decimal)       # 0.1 + 0.2 exactly
    stored = {call.attempt_id: call for call in ledger.page().calls}
    assert stored[attempt_id(1)].usage["cost_usd"] is None and stored[attempt_id(1)].evidence["limitations"] == [
        "usage_unreported", "cost_unreported"]
    assert stored[attempt_id(2)].evidence["counters_complete"] is False and stored[attempt_id(3)].evidence["counters_complete"] is True
    assert stored[attempt_id(3)].evidence["usage_complete"] is False and stored[attempt_id(3)].usage["cost_usd"] == Decimal("0.2")
    assert stored[attempt_id(5)].as_dict()["usage"]["cost_usd"] == "0.0000001"
    assert ledger.row(attempt_id(5) + ":bootstrap")["cost_usd"] == "0.0000001"
    # Unknown provider/model is never filled from the configured name.
    assert stored[attempt_id(1)].observed == {"provider": None, "model": None, "thread_id": None, "turn_id": None}
    assert stored[attempt_id(1)].configured["model"] == "gpt-6-astra"
    assert ledger.totals(start_date="2026-10-13").known_cost_usd is None and ledger.totals(start_date="2026-10-13").calls == 0


# -- 8. sync status ------------------------------------------------------------------------------------------------------------------------

def test_sync_status_is_truthful_system_only_and_never_deletes_calls_or_proves_zero(ledger, bound):
    ledger.ingest(bound, native(1, "propagation", state="completed", cost=0.25))
    assert ledger.page().calls[0].sync is None                                                   # no scan yet: nothing is known to be complete
    assert (ledger.totals().events, ledger.totals().events_not_ready) == (1, 1)
    partial = ledger.scan(bound, "partial")
    assert (partial.status, partial.error, partial.business_id) == ("partial", None, BUSINESS)
    ready = ledger.scan(bound, "ready")
    assert ready.status == "ready" and ledger.totals().events_not_ready == 0
    unavailable = ledger.scan(bound, "unavailable", {"code": "corrupt_evidence", "message": "A call receipt is invalid"})
    assert unavailable.error == {"code": "corrupt_evidence", "message": "A call receipt is invalid"}
    # Recorded calls and totals are preserved while the source is unavailable, and flagged as not ready.
    totals = ledger.totals()
    assert (totals.calls, totals.known_cost_usd, totals.events_not_ready) == (1, Decimal("0.25"), 1)
    assert ledger.page().calls[0].sync.status == "unavailable" and ledger.page().calls[0].sync.error["code"] == "corrupt_evidence"
    assert [record.status for record in context_sync(ledger, bound)] == ["unavailable"]
    ledger.scan(bound, "ready")                                                                 # an empty ready traversal also proves nothing
    assert ledger.totals().calls == 1
    for status, error in (("unknown", None), ("ready", {"code": "x", "message": "y"}), ("unavailable", None), ("partial", {"code": "", "message": "y"}),
                          ("partial", {"code": "x" * 65, "message": "y"}), ("partial", {"code": "x", "message": "y" * 301}),
                          ("partial", {"code": "x"}), ("partial", "text"), (None, None)):
        with pytest.raises(DomainError) as invalid:
            ledger.scan(bound, status, error)
        assert invalid.value.code == "validation_error", (status, error)
    for impostor in (AGENT, HUMAN, COORDINATOR, "missing"):
        with pytest.raises(DomainError) as error:
            ledger.scan(bound, "ready", driver_id=impostor)
        assert error.value.code == "forbidden"
    with pytest.raises(DomainError) as foreign:
        ledger.scan(bound, "ready", event="zz99z")
    assert foreign.value.code == "validation_error"
    with pytest.raises(DomainError) as unknown_input:
        ledger.scan("00000000-0000-4000-8000-0000000000ff", "ready")
    assert unknown_input.value.code == "not_found"
    assert ledger.host.query("SELECT COUNT(*) AS n FROM context_usage_sync")[0]["n"] == 1


def context_sync(ledger, input_id):
    with ledger.database.read() as uow:
        return context_usage.sync_records(uow, input_id=input_id)


# -- 9. pages, filters and date boundaries -----------------------------------------------------------------------------------------------------

def test_pages_are_stable_bound_to_their_filters_and_inclusive_at_utc_date_boundaries(ledger):
    first = ledger.submit()
    ledger.attempt(first, finish="published")
    second = ledger.submit(ctx=ledger.host.owner(None))
    ledger.attempt(second, SECOND)
    times = ["2026-10-10T23:59:59.999999Z", "2026-10-11T00:00:00.000000Z", "2026-10-11T12:00:00.000000Z", "2026-10-11T12:00:00.000000Z",
             "2026-10-11T23:59:59.999999Z", "2026-10-12T00:00:00.000000Z"]
    mine = [receipt(number, "bootstrap", started=when, cost=0.01 * number, tokens={"total_tokens": number}) for number, when in enumerate(times[:5], 1)]
    ledger.ingest(first, *mine)
    ledger.ingest(second, receipt(6, "bootstrap", event=SECOND, started=times[5]))
    ordered = [call.call_id for call in ledger.page(limit=100).calls]
    assert ordered == sorted(ordered, key=lambda call_id: ({item["call_id"]: item["started_at"] for item in mine + [receipt(6, "bootstrap", started=times[5])]}[call_id], call_id))

    seen, cursor = [], None
    while True:
        page = ledger.page(limit=2, cursor=cursor)
        assert len(page.calls) <= 2 and page.aggregates.calls == 6                              # aggregates cover the whole set, not the page
        seen += [call.call_id for call in page.calls]
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ordered and len(set(seen)) == 6
    exact = ledger.page(limit=6)
    assert exact.next_cursor is None and [call.call_id for call in exact.calls] == ordered

    day = ledger.page(start_date="2026-10-11", end_date="2026-10-11", limit=100)
    assert [call.started_at for call in day.calls] == times[1:5]                                  # both ends inclusive, nothing from 10-10 or 10-12
    assert day.aggregates.calls == 4
    assert ledger.page(end_date="2026-10-10").aggregates.calls == 1 and ledger.page(start_date="2026-10-12").aggregates.calls == 1
    assert ledger.page(input_id=second).aggregates.calls == 1 and ledger.page(input_id=second).calls[0].business_id is None
    assert ledger.page(input_id=first, start_date="2026-10-11").aggregates.known_cost_usd == Decimal("0.14")     # 0.02 + 0.03 + 0.04 + 0.05

    # A cursor is bound to every filter and to the limit; garbage is refused.
    cursor = ledger.page(limit=2, start_date="2026-10-11").next_cursor
    assert ledger.page(limit=2, start_date="2026-10-11", cursor=cursor).calls
    for changed in ({"limit": 3, "start_date": "2026-10-11"}, {"limit": 2}, {"limit": 2, "start_date": "2026-10-11", "end_date": "2026-10-12"},
                    {"limit": 2, "start_date": "2026-10-11", "input_id": first}):
        with pytest.raises(DomainError) as error:
            ledger.page(cursor=cursor, **changed)
        assert error.value.code == "validation_error"
    for garbage in ("", "not-a-cursor", "e30=", cursor + "x", 7):
        with pytest.raises(DomainError):
            ledger.page(limit=2, start_date="2026-10-11", cursor=garbage)
    for bad in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"start_date": "2026-13-01"}, {"start_date": "20261011"},
                {"end_date": "2026-10-11T00:00:00Z"}, {"start_date": "2026-10-12", "end_date": "2026-10-11"}, {"input_id": 3}):
        with pytest.raises(DomainError) as error:
            ledger.page(**bad)
        assert error.value.code == "validation_error", bad
    assert ledger.page(limit=100).calls and ledger.page(limit=1).next_cursor


# -- 10. retention and database guards ---------------------------------------------------------------------------------------------------------

def test_the_database_keeps_calls_bound_to_their_input_namespace_and_event(ledger, bound):
    ledger.ingest(bound, receipt(1, "bootstrap", tokens={"total_tokens": 3}))
    key = attempt_id(1) + ":bootstrap"
    with ledger.database.transaction() as uow:
        for statement, parameters in (
                ("DELETE FROM context_usage_calls WHERE call_id=?", (key,)),
                ("UPDATE context_usage_calls SET input_id=input_id WHERE call_id=?", (key,)),
                ("UPDATE context_usage_calls SET started_at=? WHERE call_id=?", (NOON, key)),
                ("UPDATE context_usage_calls SET usage_updates=0 WHERE call_id=?", (key,))):
            uow.connection.execute("SAVEPOINT guard")
            try:
                uow.connection.execute(statement, parameters)
            except sqlite3.IntegrityError:
                uow.connection.execute("ROLLBACK TO guard")
            else:
                raise AssertionError(statement)
            uow.connection.execute("RELEASE guard")
    insert = ("INSERT INTO context_usage_calls(call_id, business_id, input_id, context_event_id, attempt_id, library_attempt_sequence, "
              "stage, state, started_at, configured_model, configured_effort, configured_service_tier, usage_updates, ignored_updates, "
              "recorded_at, updated_at) VALUES (?,?,?,?,?,1,'bootstrap','started',?, 'm','e','t',0,0,'now','now')")
    for label, business, event in (("another namespace", OTHER, EVENT), ("an unreserved event", BUSINESS, "zz99z"), ("the Global namespace", None, EVENT)):
        with pytest.raises(DomainError) as guard:
            with ledger.database.transaction() as uow:
                uow.connection.execute(insert, (attempt_id(7) + ":bootstrap", business, bound, event, attempt_id(7), NOON))
        assert isinstance(guard.value.__cause__, sqlite3.IntegrityError) and "namespace" in str(guard.value.__cause__), label
    assert ledger.host.query("SELECT call_id FROM context_usage_calls") == [{"call_id": key}]


# -- 11. exact money at every decimal precision --------------------------------------------------------------------------------------------

def test_decimal_text_is_exact_and_independent_of_the_ambient_precision():
    cases = {"1000000000.000000000000000000000000000001": "1000000000.000000000000000000000000000001", "1E+9": "1000000000",
             "100": "100", "100.00": "100", "0.000": "0", "-0.0": "0", "0": "0", "1.2300": "1.23", "1E-30": "0." + "0" * 29 + "1",
             "123456789012345678901234567890.5": "123456789012345678901234567890.5", "0.5": "0.5", "5E-1": "0.5", "-2.50": "-2.5"}
    for precision in (1, 5, 28, 80):
        with decimal.localcontext() as local:
            local.prec = precision
            for source, expected in cases.items():
                assert context_usage.contract.decimal_text(Decimal(source)) == expected, (precision, source)
    for unusable in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(ValueError):
            context_usage.contract.decimal_text(unusable)


def test_the_largest_and_a_tiny_cost_sum_exactly_in_the_subtotal_and_the_rendered_call_at_any_precision(ledger):
    exact = Decimal("1000000000.000000000000000000000000000001")
    tiny = "0." + "0" * 29 + "1"
    for precision, event, big in ((28, EVENT, 1), (5, SECOND, 3), (1, "ef56g", 5)):
        small = big + 1
        input_id = ledger.submit(path=f"foundation/notes/p{precision}.md")
        ledger.attempt(input_id, event, finish="published")
        with decimal.localcontext() as local:
            local.prec = precision
            ledger.ingest(input_id, receipt(big, "bootstrap", event=event, state="completed", cost=1_000_000_000, tokens=FULL),
                          receipt(small, "bootstrap", event=event, state="completed", cost=1e-30, tokens=FULL))
            totals = ledger.totals(input_id=input_id)
            page = ledger.page(input_id=input_id)
            assert totals.known_cost_usd == exact and totals.as_dict()["known_cost_usd"] == str(exact), precision
            assert json.loads(json.dumps(totals.as_dict()))["known_cost_usd"] == "1000000000.000000000000000000000000000001"
            rendered = {call.attempt_id: call.as_dict()["usage"]["cost_usd"] for call in page.calls}
            assert rendered == {attempt_id(big): "1000000000", attempt_id(small): tiny}, precision
            assert page.aggregates.as_dict() == totals.as_dict()
        stored = {row["attempt_id"]: row["cost_usd"] for row in ledger.host.query(
            "SELECT attempt_id, cost_usd FROM context_usage_calls WHERE input_id=?", input_id)}
        assert stored == {attempt_id(big): "1000000000", attempt_id(small): tiny}, precision
        assert ledger.totals().known_cost_usd == decimal.Context(prec=80).multiply(exact, {28: 1, 5: 2, 1: 3}[precision])
