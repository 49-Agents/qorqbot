"""The usage ledger against this repository's Context library: a small injected-runner pipeline (no model, provider or product
agent) produces genuine ``Pipeline.usage_receipts`` items in a subprocess that sees only the base ``qorqut_context`` engine (no
Operations and no integration), and those items are imported into disposable queue inputs. The library is never imported into
the producing process through this test's path and is never skipped."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from decimal import Decimal

import pytest

from usage_support import Ledger

LIBRARY_ROOT = pathlib.Path(__file__).resolve().parents[2]      # the directory holding the base ``qorqut_context`` package

COLLECT = r'''
import json, sys, tempfile
from pathlib import Path
from qorqut_context import Context, Pipeline, PropagationError, RunResult, Usage
from qorqut_context import usage as usage_module
from qorqut_context.runner import RunnerConfig


class ProcessStopped(BaseException):
    """Lose the process without error bookkeeping, like the library's own recovery checks."""


class ScriptRunner:
    config = RunnerConfig()

    def __init__(self, action, usage=None):
        self.action, self.calls, self.usage = action, [], usage

    def run(self, *, stage, instructions, payload, directory, tools=None, output_schema=None, on_identity=None, on_usage=None):
        self.calls.append(stage)
        if on_identity:
            on_identity({"stage": stage, "thread_id": "script", "turn_id": str(len(self.calls))})
        final = self.usage(stage, len(self.calls), on_usage) if self.usage else None
        if stage == "bootstrap":
            output = json.dumps({"node_ids": [v["record"]["id"] for v in payload["candidates"]]})
        else:
            self.action(tools, payload)
            output = "Finished."
        return RunResult("completed", output, "script", str(len(self.calls)), self.config.model, final)


def call(tools, name, **arguments):
    result = tools.dispatch(name, arguments)
    if not result["ok"]:
        raise AssertionError(result)
    return result["result"]


def finish(tools):
    call(tools, "stage_handoff_note", text="")
    return call(tools, "finish_propagation", status="complete", summary="Processed the supplied source.",
                reassessed=tools.required_reassessment)


def new_node(tools):
    return call(tools, "put_node", record={"summary": "A new person", "attributes": {
        "basic_entity_type": ["person"], "name": ["A new person"]}},
        explanation="Supported by this source.", citations=["self"], support_after=["self"])["id"]


def snap(index, input_tokens, **changes):
    return Usage(**{"input_tokens": input_tokens, "cached_input_tokens": 4, "output_tokens": 2, "reasoning_output_tokens": 1,
                    "total_tokens": input_tokens + 2, "thread_id": "script", "turn_id": str(index), "source": "synthetic", **changes})


def fresh(root):
    context = Context.initialize(Path(root) / "business", basic_entity_types=["person"])
    first = context.begin("add", "foundation/one.md", content=b"Jesse is a person.",
                          metadata={"kind": "note", "title": "one.md", "origin": "test", "created_at": "2026"}, submitted_by="test-owner")
    if not first.published_commit:
        context.stage_handoff_note(first.event_id, "")
    context.put_node(first.event_id, {"id": "jesse", "summary": "Jesse", "attributes": {"basic_entity_type": ["person"], "name": ["Jesse"]}},
                     explanation="The source supplies this context.", citations=["self"], support_after=["self"])
    context.publish(first.event_id)               # a graph node the next source mentions, so bootstrap calls the model
    return context


def submit(pipeline):
    return pipeline.submit("add", "foundation/two.md", content=b"Jesse appears in this note.",
                           metadata={"kind": "note", "title": "two.md", "origin": "focused-test"}, submitted_by="test")


def receipts(pipeline, event_id):
    page = pipeline.usage_receipts(event_id)
    assert page["status"] == "ready" and page["next_cursor"] is None and page["error"] is None, page
    return page["items"]


out = {"format": usage_module.RECEIPT_FORMAT, "max_page": usage_module.MAX_PAGE, "scenarios": {}}
with tempfile.TemporaryDirectory() as root:
    # 1. A successful pair with cumulative snapshots, cost and a verified native thread and turn.
    def pair_usage(stage, index, emit):
        emit(snap(index, 10 * index, cost_usd=0.1 * index))
        final = snap(index, 20 * index, cost_usd=0.1 * index)
        emit(final)
        return final

    pipeline = Pipeline(fresh(Path(root) / "pair"), runner=ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools)), pair_usage))
    event_id = submit(pipeline)["event_id"]
    out["scenarios"]["pair"] = {"event_id": event_id, "stages": [receipts(pipeline, event_id)]}

    # 2. A failed propagation that keeps the usage observed before it failed.
    def failing_usage(stage, index, emit):
        if stage == "propagation":
            emit(snap(index, 30))

    def fail(tools, payload):
        raise RuntimeError("transport disappeared")

    pipeline = Pipeline(fresh(Path(root) / "failed"), runner=ScriptRunner(fail, failing_usage))
    try:
        submit(pipeline)
    except PropagationError as error:
        event_id = error.event_id
    out["scenarios"]["failed"] = {"event_id": event_id, "stages": [receipts(pipeline, event_id)]}

    # 3. A crash after usage leaves an unfinished call; an explicit retry reuses the bootstrap sample and is charged separately.
    stopped = []

    def crash_once(tools, payload):
        if not stopped:
            stopped.append(True)
            raise ProcessStopped()
        new_node(tools)
        finish(tools)

    def crash_usage(stage, index, emit):
        final = snap(index, 10 * index, cost_usd=0.05)
        emit(final)
        return final

    context = fresh(Path(root) / "retry")
    runner = ScriptRunner(crash_once, crash_usage)
    pipeline = Pipeline(context, runner=runner)
    try:
        submit(pipeline)
    except ProcessStopped:
        pass
    event_id = context.pending()["id"]
    before = receipts(pipeline, event_id)
    pipeline.retry(event_id)
    out["scenarios"]["retry"] = {"event_id": event_id, "stages": [before, receipts(pipeline, event_id)], "runner_calls": runner.calls}

json.dump(out, sys.stdout)
'''


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    """The receipts this repository's Context library produces, from a subprocess that can import nothing else of the workspace."""
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"} | {"PYTHONPATH": str(LIBRARY_ROOT)}
    work = tmp_path_factory.mktemp("context-run")
    run = subprocess.run([sys.executable, "-P", "-c", COLLECT], cwd=work, env=environment, capture_output=True, timeout=300)
    if run.returncode:
        pytest.fail(f"The Context receipts could not be produced:\n{run.stderr.decode(errors='replace')[-3000:]}",
                    pytrace=False)
    return json.loads(run.stdout)


def matches(item: dict, call) -> None:
    """The stored call carries exactly the library item's facts."""
    assert (call.call_id, call.context_event_id, call.attempt_id, call.library_attempt_sequence, call.stage, call.state) == (
        item["call_id"], item["event_id"], item["attempt_id"], item["attempt_sequence"], item["stage"], item["state"])
    assert (call.started_at, call.finished_at, call.bootstrap_call_id) == (item["started_at"], item["finished_at"], item["bootstrap_call_id"])
    assert call.configured == item["configured"] and call.observed == item["observed"] and call.error == item["error"]
    assert call.as_dict()["evidence"] == item["evidence"]
    expected = dict(item["usage"])
    cost = expected.pop("cost_usd")
    assert {name: call.usage[name] for name in expected} == expected
    assert call.usage["cost_usd"] == (None if cost is None else Decimal(repr(cost)))


def test_the_library_receipts_have_the_format_and_page_bound_the_ledger_parses(library):
    from qorqut_context import usage as usage_module
    assert library["format"] == "qorqut-context-call-v1" == usage_module.RECEIPT_FORMAT and library["max_page"] >= 100


def test_genuine_library_receipts_import_bound_to_disposable_queue_inputs(library, tmp_path):
    ledger = Ledger(tmp_path)
    scenarios = library["scenarios"]
    totals = {}
    for name in ("pair", "failed"):
        event_id = scenarios[name]["event_id"]
        (items,) = scenarios[name]["stages"]
        input_id = ledger.submit(path=f"foundation/notes/{name}.md")
        ledger.attempt(input_id, event_id, finish="published" if name == "pair" else "blocked")
        first = ledger.ingest(input_id, *items)
        assert (first.inserted, first.updated, first.unchanged, first.stale) == (len(items), 0, 0, 0)
        replay = ledger.ingest(input_id, *json.loads(json.dumps(items)))
        assert (replay.inserted, replay.updated, replay.unchanged, replay.stale) == (0, 0, len(items), 0)
        page = ledger.page(input_id=input_id)
        assert len(page.calls) == len(items)
        by_id = {call.call_id: call for call in page.calls}
        for item in items:
            matches(item, by_id[item["call_id"]])
        totals[name] = ledger.totals(input_id=input_id)

    pair = scenarios["pair"]["stages"][0]
    assert [(item["stage"], item["state"]) for item in pair] == [("bootstrap", "completed"), ("propagation", "completed")]
    assert totals["pair"].calls == 2 and totals["pair"].unfinished_calls == 0
    assert totals["pair"].known_cost_usd == Decimal("0.1") + Decimal("0.2")                    # exact decimal arithmetic, not float
    row = ledger.row(pair[1]["call_id"])
    assert (row["input_tokens"], row["native_thread_id"], row["native_turn_id"], row["usage_scope"]) == (40, "script", "2", "native")
    assert row["bootstrap_call_id"] == pair[0]["call_id"]

    failed = scenarios["failed"]["stages"][0]
    assert [(item["stage"], item["state"]) for item in failed] == [("bootstrap", "completed"), ("propagation", "failed")]
    assert ledger.row(failed[1]["call_id"])["error_message"] == "transport disappeared"
    assert totals["failed"].calls == 2 and totals["failed"].unknown_cost_calls == 2 and totals["failed"].known_cost_usd is None
    assert totals["failed"].calls_without_token_usage == 1                                      # the bootstrap reported no usage


def test_a_crashed_call_stays_unfinished_and_the_retry_is_counted_separately_with_the_bootstrap_once(library, tmp_path):
    ledger = Ledger(tmp_path)
    scenario = library["scenarios"]["retry"]
    event_id = scenario["event_id"]
    before, after = scenario["stages"]
    assert scenario["runner_calls"] == ["bootstrap", "propagation", "propagation"]
    assert [(item["attempt_sequence"], item["stage"], item["state"]) for item in before] == [
        (1, "bootstrap", "completed"), (1, "propagation", "started")]
    assert [(item["attempt_sequence"], item["stage"], item["state"]) for item in after] == [
        (1, "bootstrap", "completed"), (1, "propagation", "started"), (2, "propagation", "completed")]
    assert after[:2] == before                                                                    # earlier receipts are never rewritten

    input_id = ledger.submit()
    ledger.attempt(input_id, event_id, finish="blocked")                                          # queue attempt 1 ends with the crash
    ledger.ingest(input_id, *before)
    interim = ledger.totals(input_id=input_id)
    assert (interim.calls, interim.unfinished_calls) == (2, 1)
    ledger.retry(input_id)
    ledger.attempt(input_id, event_id)                                                            # queue attempt 2 reuses the event
    result = ledger.ingest(input_id, *after)
    assert (result.inserted, result.updated, result.unchanged, result.stale) == (1, 0, 2, 0)
    calls = {call.call_id: call for call in ledger.page(input_id=input_id).calls}
    for item in after:
        matches(item, calls[item["call_id"]])
    totals = ledger.totals(input_id=input_id)
    assert (totals.calls, totals.unfinished_calls) == (3, 1)                                      # the unfinished attempt is never charged away
    assert [call.library_attempt_sequence for call in calls.values() if call.stage == "propagation"] == [1, 2]
    assert sum(1 for call in calls.values() if call.stage == "bootstrap") == 1
    assert all(item["bootstrap_call_id"] == after[0]["call_id"] for item in after[1:])
    assert ledger.host.query("SELECT attempt FROM context_input_attempts WHERE input_id=? ORDER BY attempt", input_id) == [
        {"attempt": 1}, {"attempt": 2}]
