"""P4B: the restart-safe ordered Context queue driver on an isolated synthetic fixture.

Everything real is the Context queue (``qorqut_context.integration.queue`` transitions and the ``qorqut_context.integration.filing``
source ownership), its unit of work over a disposable database, and a disposable real Context graph (real Git, the library's own
Pipeline and drafts). The only fake is the model runner injected into the Pipeline. There is no combined installed host, provider,
live data or session: the synthetic host (``OperationalHost``) is imported from the directory named by ``QORQUT_OPERATIONS_TESTS``
(the variable keeps its name; it points to the Context integration tests, ``qorqut-context/tests/integration``) and the library must
carry controlled ``record_context``; both are skipped otherwise."""

import inspect
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

OPERATIONS_TESTS = os.environ.get("QORQUT_OPERATIONS_TESTS")
if not OPERATIONS_TESTS or not (Path(OPERATIONS_TESTS) / "operational_support.py").is_file():
    pytest.skip("set QORQUT_OPERATIONS_TESTS to the Context integration tests directory (qorqut-context/tests/integration)",
                allow_module_level=True)
sys.path.insert(0, OPERATIONS_TESTS)
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_context import Context, Pipeline
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_context.integration.queue import transitions

if "record_context" not in inspect.signature(Context.begin).parameters:
    pytest.skip("the Context library must carry controlled record_context (library 2e59b52 or later)", allow_module_level=True)

from operational_support import AGENT, BUSINESS, COORDINATOR, OTHER, OTHER_AGENT, WORKER, OperationalHost
from bazarlyk.shared_context.service import coordinator_of
from qorqut_context.integration.driver import ContextQueueDriver, record_context_of
from qorqut_context.integration.loop import ContextDriverLoop

META = {"kind": "note", "title": "A note", "origin": "agent"}
NOTES = "foundation/notes/"


class ProcessStopped(BaseException):
    """Simulates losing the process without any normal error bookkeeping."""


class Runner:
    """The injected model runner: it finishes a propagation the way the Context tools require, and records every call."""

    config = RunnerConfig()

    def __init__(self):
        self.calls, self.handoff, self.fail, self.during, self.draft = [], "", None, None, None

    @property
    def propagations(self):
        return self.calls.count("propagation")

    def run(self, **args):
        self.calls.append(args["stage"])
        if args.get("on_identity"):
            args["on_identity"]({"stage": args["stage"], "thread_id": "script", "turn_id": str(len(self.calls))})
        if args["stage"] == "bootstrap":
            return RunResult("completed", json.dumps({"node_ids": [v["record"]["id"] for v in args["payload"]["candidates"]]}),
                             "script", str(len(self.calls)), self.config.model)
        tools, payload = args["tools"], args["payload"]
        if self.draft is not None:
            self.dispatch(tools, "stage_handoff_note", text=self.draft)       # a draft note, staged before the run fails
        if self.during:
            self.during(tools, payload)
        if self.fail:
            raise self.fail
        if payload["input_mode"] == "OPERATIONAL_ACTION":
            self.dispatch(tools, "stage_action_associations", date=0, summary="Revision interpreted.", associations=[])
        self.dispatch(tools, "stage_handoff_note", text=self.handoff)
        self.dispatch(tools, "finish_propagation", status="complete", summary="Processed the supplied input.",
                      reassessed=tools.required_reassessment)
        return RunResult("completed", "Finished.", "script", str(len(self.calls)), self.config.model)

    @staticmethod
    def dispatch(tools, name, **arguments):
        result = tools.dispatch(name, arguments)
        assert result["ok"], result
        return result["result"]


class NoPrograms:
    """The program catalog of a queue with no registered filing job: nothing is bound, so nothing is ever looked up."""

    @staticmethod
    def get(kind):
        return None


class Fixture:
    def __init__(self, tmp_path, ids=None):
        (tmp_path / "operations").mkdir()
        self.host = OperationalHost(tmp_path / "operations")
        self.context = Context.initialize(tmp_path / "context", tmp_path / "runtime")
        self.runner = Runner()
        self.ids = None if ids is None else iter(ids)
        self.driver = self.build(self.runner)
        self.coordinator(COORDINATOR)

    def build(self, runner):
        self.pipeline = Pipeline(self.context, runner=runner)
        return ContextQueueDriver(self.host.database, self.context, self.pipeline, driver_id=WORKER,
                                  outcomes=self.host.outcomes, programs=NoPrograms(), coordinator=coordinator_of,
                                  id_source=None if self.ids is None else lambda: next(self.ids))

    def coordinator(self, identity):
        with self.host.database.transaction() as uow:
            uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (identity,))

    # -- admission ---------------------------------------------------------------------------------------------------

    def submit(self, name="a.md", content="Body", operation="add", ctx=None):
        arguments = {"operation": operation, "path": NOTES + name,
                     **({"content": content, "metadata": META} if operation != "remove" else {})}
        return self.host.data("context_queue_submit", arguments, ctx or self.host.agent())["input"]

    def action(self, text="Shipped the release", **arguments):
        return self.host.record_action(text, **arguments)

    def correct(self, action, revision, **arguments):
        return self.host.data("actions_correct", {"action_id": action["action_id"], **arguments}, self.host.agent(),
                              expected_revision=revision)["action"]

    def withdraw(self, action, revision):
        return self.host.data("actions_withdraw", {"action_id": action["action_id"]}, self.host.agent(),
                              expected_revision=revision)

    def control(self, intent, input_id, state="blocked", ctx=None):
        return self.host.data("context_queue_" + intent, {"input_id": input_id, "expected_state": state},
                              ctx or self.host.agent())["result"]

    # -- driving and reading -----------------------------------------------------------------------------------------

    def drain(self, limit=12):
        """Run steps until one makes no queue progress; returns every result."""
        results = []
        for _ in range(limit):
            results.append(self.driver.process_next())
            if results[-1]["status"] not in ("published", "discarded"):
                break
        return results

    def rows(self):
        return self.host.query("SELECT * FROM context_inputs ORDER BY sequence")

    def row(self, input_id):
        return self.host.query("SELECT * FROM context_inputs WHERE id=?", input_id)[0]

    def notices(self, kind=None):
        return [row for row in self.host.query("SELECT event_type, summary FROM inbox_events WHERE source_type='context_inputs' "
                                               "ORDER BY id") if kind is None or row["event_type"] == kind]

    def event(self, event_id):
        return self.context.snapshot().events[event_id]

    def bindings(self):
        return {(row["source_kind"], row["source_key"]): row["business_id"]
                for row in self.host.query("SELECT * FROM context_source_bindings")}


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def published_event(fx, row):
    assert row["state"] == "published", row["state"]
    return fx.event(row["result_json"]["context_event_id"]), row["result_json"]


# -- ordered Action and GENERAL processing ----------------------------------------------------------------------------------

def test_general_then_action_correction_and_withdrawal_publish_in_queue_order_from_frozen_input(fx):
    general = fx.submit("first.md", "Exact body\r\nwith  spacing\n")
    action = fx.action("Shipped the release", tags=["Launch", "Alpha"],
                       related_objects=[{"kind": "node", "id": "ab12c", "explanation": "the release"}])
    fx.correct(action, 1, text="Sent the final", tags=["Final"])
    fx.withdraw(action, 2)
    assert [row["state"] for row in fx.rows()] == ["queued"] * 4
    # Everything is admitted before any step ran: what is published is the frozen revision, not the live Action.
    assert fx.host.read("actions_get", {"action_id": action["action_id"]}, fx.host.agent())["action"]["withdrawn"] is True

    results = fx.drain()
    assert [result["status"] for result in results] == ["published"] * 4 + ["idle"]
    rows = fx.rows()
    assert [row["state"] for row in rows] == ["published"] * 4
    events = [published_event(fx, row) for row in rows]
    commits = [result["published_commit"] for event, result in events]
    assert commits == [fx.context.snapshot().event_commits[event["id"]] for event, _ in events]
    assert [event["base_revision"] for event, _ in events[1:]] == commits[:-1]       # one linear order: no overlap, no skip

    first, add, edit, remove = (event for event, _ in events)
    assert first["operation"] == "add" and first["input_mode"] == "GENERAL" and first["source"]["path"] == NOTES + "first.md"
    assert fx.context.snapshot().files[NOTES + "first.md"] == b"Exact body\r\nwith  spacing\n"      # exact frozen bytes
    assert first["record_context"] == {"scope": {"kind": "business", "business_id": BUSINESS}, "provenance": {
        "input_id": rows[0]["id"], "submitted_by": AGENT, "queued_at": rows[0]["created_at"]}}
    assert first["submitted_by"] == AGENT and first["source"]["title"] == "A note"

    key = action["action_id"]
    assert [(e["operation"], e["input_mode"], e["action"]["action_ref"]) for e in (add, edit, remove)] == [
        ("add", "OPERATIONAL_ACTION", key), ("edit", "OPERATIONAL_ACTION", key), ("remove", "OPERATIONAL_ACTION", key)]
    assert [e["action"]["revision_ref"] for e in (add, edit, remove)] == [f"{key}:1", f"{key}:2", f"{key}:3"]
    payloads = [row["payload_json"] for row in rows[1:]]
    for event, payload, row in zip((add, edit, remove), payloads, rows[1:]):
        assert event["action"]["tags"] == payload["tags"] and all(tag["business_id"] is None for tag in event["action"]["tags"])
        assert event["record_context"] == {"scope": payload["scope"], "provenance": payload["provenance"]}
        assert event["record_context"]["scope"] == {"kind": "business", "business_id": BUSINESS}
        assert event["submitted_by"] == payload["submitted_by"] == row["submitted_by"]
    assert [tag["name"] for tag in add["action"]["tags"]] == ["Alpha", "Launch"]
    assert [tag["name"] for tag in edit["action"]["tags"]] == ["Final"]
    assert add["action"]["pointers"] == [{"kind": "node", "id": "ab12c", "explanation": "the release"}]
    assert "Shipped the release" in fx.context.snapshot(commits[1]).files[add["source"]["path"]].decode()
    assert "Sent the final" in fx.context.snapshot(commits[2]).files[edit["source"]["path"]].decode()
    assert remove["action"]["pointers"] == [] and add["source"]["id"] == edit["source"]["id"] == remove["source"]["id"]
    assert add["source"]["path"] not in fx.context.snapshot().files                    # withdrawn: the source is removed, history kept

    assert fx.bindings() == {("general", NOTES + "first.md"): BUSINESS, ("action", key): BUSINESS}
    assert fx.runner.propagations == 4 and fx.notices() == []                           # empty handoffs: explicit, no notice


def test_a_global_input_keeps_its_global_scope_and_a_business_never_reuses_it(fx):
    row = fx.submit("global.md", ctx=fx.host.owner(None))
    assert fx.drain()[0]["status"] == "published"
    event, _ = published_event(fx, fx.row(row["id"]))
    assert event["record_context"]["scope"] == {"kind": "global"} and fx.bindings() == {("general", NOTES + "global.md"): None}
    edit = fx.submit("global.md", "Changed", "edit", ctx=fx.host.agent())             # a business edit of a Global source
    result = fx.driver.process_next()
    assert result["status"] == "input_blocked" and result["error"]["code"] == "source_scope_conflict"
    assert fx.context.snapshot().files[NOTES + "global.md"] == b"Body" and fx.row(edit["id"])["state"] == "blocked"


def test_record_context_is_a_pure_function_of_the_frozen_input(fx):
    general = fx.submit()
    action = fx.action(tags=["Launch"])
    revision = fx.host.query("SELECT id FROM context_inputs WHERE input_kind='action'")[0]
    detail = lambda input_id: fx.host.read("context_queue_get", {"input_id": input_id}, fx.host.agent())["input"]
    assert record_context_of(detail(general["id"])) == {"scope": {"kind": "business", "business_id": BUSINESS}, "provenance": {
        "input_id": general["id"], "submitted_by": AGENT, "queued_at": general["created_at"]}}
    frozen = detail(revision["id"])
    assert record_context_of(frozen) == {"scope": frozen["payload"]["scope"], "provenance": frozen["payload"]["provenance"]}
    assert frozen["payload"]["provenance"]["origin"] == "agent" and frozen["item_id"] == action["action_id"]
    assert record_context_of(detail(general["id"])) == record_context_of(detail(general["id"]))       # deterministic


# -- a blocked head, explicit retry and discard ------------------------------------------------------------------------------

def test_a_blocked_head_holds_later_inputs_while_unrelated_operations_keep_working(fx):
    fx.runner.fail = RuntimeError("model unavailable")
    head = fx.submit("head.md")
    later = fx.submit("later.md")
    result = fx.driver.process_next()
    assert result["status"] == "input_blocked" and result["error"]["code"] == "propagation_failed" and result["attempt"] == 1
    blocked = fx.row(head["id"])
    assert blocked["state"] == "blocked" and blocked["context_event_id"] == result["context_event_id"]
    assert [row["event_type"] for row in fx.notices()] == ["context.input_blocked"]
    calls = len(fx.runner.calls)

    task = fx.host.create_task("Unrelated work")                                       # unrelated operations keep working
    other_action = fx.action("Another routine")
    assert fx.host.task(task["id"])["title"] == "Unrelated work" and other_action["revision"] == 1
    assert fx.driver.process_next() == {"status": "blocked", "input_id": head["id"], "attempt": 1}
    assert fx.driver.recover()["status"] == "blocked"
    assert len(fx.runner.calls) == calls and fx.row(later["id"])["state"] == "queued"   # later inputs wait; no model, no attempt
    assert fx.host.read("context_queue_list", {}, fx.host.agent())["queue_blocked"] is True
    assert len(fx.notices()) == 1                                                       # waiting does not notify again

    fx.runner.fail = None
    fx.control("retry", head["id"])
    retried = fx.driver.process_next()
    assert retried["status"] == "published" and retried["context_event_id"] == blocked["context_event_id"]
    final = fx.row(head["id"])
    assert final["attempt"] == 2 and final["result_json"]["context_event_id"] == blocked["context_event_id"]
    detail = fx.host.read("context_queue_get", {"input_id": head["id"]}, fx.host.agent())["input"]
    assert [a["cause"] for a in detail["attempts"]] == ["queue", "retry"] and detail["controls"][0]["status"] == "applied"
    assert [r["status"] for r in fx.drain()] == ["published", "published", "idle"]       # later work flows once it is published


def test_an_explicit_discard_closes_the_draft_and_unblocks_the_queue(fx):
    fx.runner.fail = RuntimeError("model unavailable")
    head, later = fx.submit("head.md"), fx.submit("later.md")
    blocked = fx.driver.process_next()
    event_id = blocked["context_event_id"]
    assert fx.context.pending()["id"] == event_id                                      # the unpublished draft is retained
    assert fx.driver.process_next()["status"] == "blocked"
    fx.control("discard", head["id"])
    assert fx.row(head["id"])["state"] == "discard_requested"
    fx.runner.fail = None
    calls = len(fx.runner.calls)
    discarded = fx.driver.process_next()
    assert discarded == {"status": "discarded", "input_id": head["id"], "context": "draft_discarded"}
    assert fx.context.pending() is None and event_id not in fx.context.snapshot().events and len(fx.runner.calls) == calls
    assert fx.row(head["id"])["state"] == "discarded" and fx.row(head["id"])["result_json"]["mechanical"] is True
    assert [r["status"] for r in fx.drain()] == ["published", "idle"]
    assert NOTES + "head.md" not in fx.context.snapshot().files and NOTES + "later.md" in fx.context.snapshot().files
    assert fx.row(later["id"])["state"] == "published"


def test_a_discard_never_rewrites_an_already_published_event(fx):
    fx.runner.fail = RuntimeError("model unavailable")
    head = fx.submit("head.md")
    event_id = fx.driver.process_next()["context_event_id"]
    fx.runner.fail = None
    fx.pipeline.retry(event_id)                                                         # Context published it outside the queue
    assert event_id in fx.context.snapshot().events
    fx.control("discard", head["id"])
    result = fx.driver.process_next()
    assert result["status"] == "published" and result["discard"] == "rejected"
    row = fx.row(head["id"])
    assert row["state"] == "published" and row["result_json"]["context_event_id"] == event_id
    detail = fx.host.read("context_queue_get", {"input_id": head["id"]}, fx.host.agent())["input"]
    assert detail["controls"][0]["status"] == "rejected"


def test_a_head_discarded_before_any_event_never_touches_context(fx):
    other = fx.host.agent(OTHER_AGENT, OTHER)
    fx.submit("shared.md")
    fx.drain()
    revision = fx.context.snapshot().revision
    blocked_edit = fx.submit("shared.md", "Mine", "edit", ctx=other)
    assert fx.driver.process_next()["status"] == "input_blocked"
    assert fx.row(blocked_edit["id"])["context_event_id"] is None                           # no event ID was ever allocated
    fx.control("discard", blocked_edit["id"], ctx=other)
    assert fx.driver.process_next() == {"status": "discarded", "input_id": blocked_edit["id"], "context": "no_event"}
    assert fx.context.snapshot().revision == revision and fx.context.pending() is None


# -- source ownership --------------------------------------------------------------------------------------------------------

def test_a_cross_scope_source_write_is_refused_before_context_or_a_model(fx):
    other = fx.host.agent(OTHER_AGENT, OTHER)
    fx.submit("shared.md", "Mine")
    assert fx.drain()[0]["status"] == "published"
    revision, calls = fx.context.snapshot().revision, len(fx.runner.calls)
    edit = fx.submit("shared.md", "Theirs", "edit", ctx=other)
    refused = fx.driver.process_next()
    assert refused["status"] == "input_blocked" and refused["error"]["code"] == "source_scope_conflict"
    assert refused["error"]["phase"] == "ownership" and refused["error"]["retryable"] is False
    assert BUSINESS not in refused["error"]["message"] and OTHER not in refused["error"]["message"]    # names no scope
    assert fx.context.snapshot().revision == revision and fx.context.pending() is None and len(fx.runner.calls) == calls
    assert refused["context_event_id"] is None and fx.row(edit["id"])["context_event_id"] is None
    assert fx.bindings() == {("general", NOTES + "shared.md"): BUSINESS}                 # a refusal binds nothing
    detail = fx.host.read("context_queue_get", {"input_id": edit["id"]}, other)["input"]
    assert [(a["cause"], a["context_event_id"], a["outcome"]) for a in detail["attempts"]] == [("queue", None, "blocked")]
    assert len(fx.notices("context.input_blocked")) == 1

    fx.control("retry", edit["id"], ctx=other)                                          # an explicit retry is refused the same way
    again = fx.driver.process_next()
    assert again["error"]["code"] == "source_scope_conflict" and again["attempt"] == 2 and again["context_event_id"] is None
    detail = fx.host.read("context_queue_get", {"input_id": edit["id"]}, other)["input"]
    assert [(a["cause"], a["context_event_id"]) for a in detail["attempts"]] == [("queue", None), ("retry", None)]
    assert len(fx.notices("context.input_blocked")) == 2 and fx.bindings() == {("general", NOTES + "shared.md"): BUSINESS}
    fx.control("discard", edit["id"], ctx=other)
    assert fx.driver.process_next() == {"status": "discarded", "input_id": edit["id"], "context": "no_event"}

    for operation in ("add", "remove"):                                                  # add-over and removal are refused alike
        fx.submit("shared.md", "Theirs", operation, ctx=other)
        assert fx.driver.process_next()["error"]["code"] == "source_scope_conflict"
        blocked = [row for row in fx.rows() if row["state"] == "blocked"][0]
        fx.control("discard", blocked["id"], ctx=other)
        assert fx.driver.process_next()["status"] == "discarded"
    assert fx.context.snapshot().files[NOTES + "shared.md"] == b"Mine" and len(fx.runner.calls) == calls
    assert fx.bindings() == {("general", NOTES + "shared.md"): BUSINESS}


def test_an_edit_or_removal_of_an_unowned_source_is_never_reinterpreted_as_an_add(fx):
    fx.submit("missing.md", "Edit of nothing", "edit")
    result = fx.driver.process_next()
    assert result["status"] == "input_blocked" and result["error"]["code"] == "source_unbound"
    assert NOTES + "missing.md" not in fx.context.snapshot().files and fx.runner.calls == [] and fx.bindings() == {}
    fx.control("discard", fx.rows()[0]["id"])
    assert fx.driver.process_next()["status"] == "discarded"
    # A source that exists in Context but was never filed through the queue has no owner, so nobody may change it.
    event = fx.context.begin("add", NOTES + "orphan.md", content=b"Existing", metadata=META, submitted_by="outside")
    fx.context.stage_handoff_note(event.event_id, "")
    fx.context.publish(event.event_id)
    for operation in ("edit", "remove", "add"):
        fx.submit("orphan.md", "Changed", operation)
        assert fx.driver.process_next()["error"]["code"] == "source_unbound"
        fx.control("discard", [row for row in fx.rows() if row["state"] == "blocked"][0]["id"])
        assert fx.driver.process_next()["status"] == "discarded"
    assert fx.context.snapshot().files[NOTES + "orphan.md"] == b"Existing" and fx.runner.calls == []


def test_an_action_edit_after_a_discarded_add_is_never_reinterpreted_as_an_add(fx):
    action = fx.action("Shipped the release")
    fx.correct(action, 1, text="Shipped the release, corrected")
    add = fx.rows()[0]
    fx.runner.fail = RuntimeError("model unavailable")
    assert fx.driver.process_next()["status"] == "input_blocked"
    fx.control("discard", add["id"])
    assert fx.driver.process_next()["status"] == "discarded"
    fx.runner.fail, calls = None, len(fx.runner.calls)
    edit = fx.driver.process_next()
    assert edit["status"] == "input_blocked" and edit["error"]["code"] == "context_conflict"
    assert "requires a current accepted action" in edit["error"]["message"]
    assert action["action_id"] not in fx.context.snapshot().actions and fx.context.pending() is None
    assert len(fx.runner.calls) == calls and fx.rows()[1]["state"] == "blocked"


# -- Context cannot be checked before an attempt ---------------------------------------------------------------------------

def unreadable(fx, monkeypatch):
    def snapshot(*arguments, **keywords):
        raise OSError("repository unreadable")

    monkeypatch.setattr(fx.context, "snapshot", snapshot)


def test_a_context_that_cannot_be_checked_blocks_a_first_attempt_with_no_event_then_retries_and_discards(fx, monkeypatch):
    item = fx.submit("preflight.md")
    with monkeypatch.context() as patch:
        unreadable(fx, patch)
        refused = fx.driver.process_next()
    assert refused["status"] == "input_blocked" and refused["error"]["code"] == "storage_error"
    assert refused["error"]["phase"] == "preflight" and refused["context_event_id"] is None and refused["attempt"] == 1
    row = fx.row(item["id"])
    assert row["state"] == "blocked" and row["context_event_id"] is None and fx.runner.calls == []
    assert [n["event_type"] for n in fx.notices()] == ["context.input_blocked"] and "(event" not in fx.notices()[0]["summary"]
    assert fx.bindings() == {}                                                          # no source binding came of it
    assert fx.driver.process_next()["status"] == "blocked" and fx.runner.calls == []

    fx.control("retry", item["id"])
    with monkeypatch.context() as patch:
        unreadable(fx, patch)
        assert fx.driver.process_next()["attempt"] == 2
    assert fx.row(item["id"])["context_event_id"] is None and fx.runner.calls == [] and len(fx.notices()) == 2

    fx.control("retry", item["id"])
    assert fx.driver.process_next()["status"] == "published"                            # a readable Context: now the first event ID
    assert fx.row(item["id"])["attempt"] == 3 and fx.row(item["id"])["context_event_id"] is not None

    other = fx.submit("discarded.md")
    with monkeypatch.context() as patch:
        unreadable(fx, patch)
        assert fx.driver.process_next()["status"] == "input_blocked"
    fx.control("discard", other["id"])
    assert fx.driver.process_next() == {"status": "discarded", "input_id": other["id"], "context": "no_event"}
    assert fx.runner.propagations == 1


def test_a_retry_that_cannot_check_context_keeps_the_prior_event_id(fx, monkeypatch):
    fx.runner.fail = RuntimeError("model unavailable")
    item = fx.submit("keeps.md")
    event_id = fx.driver.process_next()["context_event_id"]
    fx.control("retry", item["id"])
    with monkeypatch.context() as patch:
        unreadable(fx, patch)
        refused = fx.driver.process_next()
    assert refused["status"] == "input_blocked" and refused["attempt"] == 2 and refused["context_event_id"] == event_id
    assert refused["error"]["phase"] == "preflight" and fx.row(item["id"])["context_event_id"] == event_id
    assert fx.runner.propagations == 1                                                  # the unreadable retry reached no model
    fx.runner.fail = None
    fx.control("retry", item["id"])
    published = fx.driver.process_next()
    assert published["status"] == "published" and published["context_event_id"] == event_id
    assert fx.row(item["id"])["result_json"]["context_event_id"] == event_id and fx.row(item["id"])["attempt"] == 3


def test_an_unreadable_publication_lookup_after_a_failed_submit_is_not_evidence_of_absence(fx):
    fx.runner.handoff = "Committed note"
    item = fx.submit("lost-response.md")
    submit = fx.driver._submit

    def publish_then_fail(row, event_id):
        submit(row, event_id)                                                           # really publishes
        raise RuntimeError("the response was lost")

    lookup, failures = fx.driver._published, []

    def flaky(event_id):
        if len(failures) < 3:                                                           # the failing submit's check and two ticks
            failures.append(event_id)
            raise OSError("repository temporarily unreadable")
        return lookup(event_id)

    fx.driver._submit, fx.driver._published = publish_then_fail, flaky
    first = fx.driver.process_next()
    event_id = first["context_event_id"]
    assert first["status"] == "unrecorded" and first["reason"] == "context_unavailable" and first["attempt"] == 1
    assert lookup(event_id) is not None                                                 # the publication really exists
    for _ in range(2):
        assert fx.driver.process_next()["status"] == "busy"                             # still unreadable: nothing is concluded
    row = fx.row(item["id"])
    assert row["state"] == "processing" and row["attempt"] == 1 and row["context_event_id"] == event_id
    assert fx.notices() == [] and fx.runner.propagations == 1

    recovered = fx.driver.process_next()                                                # Context is readable again
    assert recovered["status"] == "published" and recovered["recovered"] is True and recovered["context_event_id"] == event_id
    done = fx.row(item["id"])
    assert done["state"] == "published" and done["attempt"] == 1 and fx.runner.propagations == 1
    assert [n["event_type"] for n in fx.notices()] == ["context.handoff_published"]       # no false failure notice, one real handoff


def test_a_lookup_that_succeeds_and_finds_nothing_still_blocks_a_failed_submit(fx):
    item = fx.submit("really-failed.md")
    fx.runner.fail = RuntimeError("model unavailable")
    result = fx.driver.process_next()
    assert result["status"] == "input_blocked" and fx.row(item["id"])["state"] == "blocked"
    assert [n["event_type"] for n in fx.notices()] == ["context.input_blocked"]


# -- event identity ----------------------------------------------------------------------------------------------------------

def test_an_event_id_already_used_in_context_or_the_queue_is_never_reissued(tmp_path):
    fx = Fixture(tmp_path, ids=["aaaaa", "aaaaa", "AAAA!", "bbbbb"])
    first, second = fx.submit("one.md"), fx.submit("two.md")
    assert [r["status"] for r in fx.drain()] == ["published", "published", "idle"]
    assert [fx.row(item["id"])["context_event_id"] for item in (first, second)] == ["aaaaa", "bbbbb"]


def test_a_published_event_under_the_same_id_with_different_evidence_is_a_conflict_not_a_record(fx):
    fx.submit("mine.md", "Mine")
    original = fx.driver._submit

    def crash(row, event_id):
        raise ProcessStopped                                                           # the process dies right after the ID was saved

    fx.driver._submit = crash
    with pytest.raises(ProcessStopped):
        fx.driver.process_next()
    fx.driver._submit = original
    head = fx.rows()[0]
    event_id = head["context_event_id"]
    assert head["state"] == "processing" and fx.context.pending() is None
    foreign = fx.context.begin("add", NOTES + "foreign.md", content=b"Foreign", metadata=META, submitted_by="outside",
                               event_id=event_id)
    fx.context.stage_handoff_note(foreign.event_id, "")
    fx.context.publish(event_id)

    recovered = fx.driver.recover()
    assert recovered["status"] == "input_blocked" and recovered["error"]["code"] == "event_conflict"
    assert fx.row(head["id"])["state"] == "blocked" and fx.row(head["id"])["result_json"] is None
    assert fx.event(event_id)["source"]["path"] == NOTES + "foreign.md"                 # the foreign event is untouched
    fx.control("retry", head["id"])
    retried = fx.driver.process_next()
    assert retried["status"] == "published" and retried["context_event_id"] != event_id
    assert fx.context.snapshot().files[NOTES + "mine.md"] == b"Mine" and fx.runner.propagations == 1


# -- crash and recording failures --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seam", ["after_publication_in_pipeline", "after_git_publication_in_context"])
def test_a_crash_after_real_git_publication_before_queue_recording_costs_no_second_model_call(fx, monkeypatch, seam):
    fx.runner.handoff = "A committed handoff note"
    item = fx.submit("crash.md", "Crash content")

    def stop(phase):
        if phase == "after_publication":
            raise ProcessStopped

    target = fx.pipeline if seam == "after_publication_in_pipeline" else fx.context
    with monkeypatch.context() as patch:
        patch.setattr(target, "_checkpoint", stop)
        with pytest.raises(ProcessStopped):
            fx.driver.process_next()
    crashed = fx.row(item["id"])
    event_id = crashed["context_event_id"]
    assert crashed["state"] == "processing" and event_id in fx.context.snapshot().events           # published, not recorded
    assert fx.runner.propagations == 1 and fx.notices() == []

    restarted = Runner()
    driver = fx.build(restarted)                                                       # a fresh process: new driver and runner
    recovered = driver.recover()
    assert recovered["status"] == "published" and recovered["recovered"] is True
    assert restarted.calls == [] and fx.runner.propagations == 1                       # ZERO second model calls
    done = fx.row(item["id"])
    assert done["state"] == "published" and done["result_json"]["published_commit"] == fx.context.snapshot().event_commits[event_id]
    assert done["result_json"]["handoff_note"] == {"disposition": "prepared", "text": "A committed handoff note"}
    assert fx.context.pending() is None and [n["event_type"] for n in fx.notices()] == ["context.handoff_published"]
    assert driver.recover() == {"status": "idle"} and driver.process_next() == {"status": "idle"}
    assert len(fx.notices()) == 1                                                       # one deduplicated notice


def test_a_sqlite_recording_failure_keeps_the_publication_and_reconciles_it_mechanically(fx):
    fx.runner.handoff = "Committed note"
    item = fx.submit("record.md")
    write, failures = fx.driver._write, []

    def flaky(function, **arguments):
        if function is transitions.record_published and not failures:
            failures.append(1)
            raise sqlite3.OperationalError("disk I/O error")
        return write(function, **arguments)

    fx.driver._write = flaky
    first = fx.driver.process_next()
    assert first["status"] == "unrecorded" and first["reason"] == "record_failed" and first["handoff_note"]["text"] == "Committed note"
    row = fx.row(item["id"])
    assert row["state"] == "processing" and row["attempt"] == 1                          # never blocked: it is published
    assert row["context_event_id"] in fx.context.snapshot().events and fx.notices() == []
    second = fx.driver.process_next()
    assert second["status"] == "published" and second["recovered"] is True and fx.runner.propagations == 1
    assert fx.row(item["id"])["state"] == "published" and len(fx.notices("context.handoff_published")) == 1
    assert fx.notices("context.input_blocked") == []


def test_an_interrupted_unpublished_attempt_is_blocked_for_explicit_retry_or_discard(fx):
    item = fx.submit("interrupted.md")

    def crash(row, event_id):
        raise ProcessStopped

    original, fx.driver._submit = fx.driver._submit, crash
    with pytest.raises(ProcessStopped):
        fx.driver.process_next()
    fx.driver._submit = original
    assert fx.row(item["id"])["state"] == "processing"
    recovered = fx.driver.recover()
    assert recovered["status"] == "input_blocked" and recovered["error"]["code"] == "interrupted" and fx.runner.calls == []
    assert fx.row(item["id"])["state"] == "blocked" and [n["event_type"] for n in fx.notices()] == ["context.input_blocked"]
    fx.control("retry", item["id"])
    assert fx.driver.process_next()["status"] == "published" and fx.runner.propagations == 1


# -- the handoff note --------------------------------------------------------------------------------------------------------

def test_only_a_committed_handoff_notifies_once_and_drafts_and_empty_notes_never_do(fx):
    fx.runner.fail, fx.runner.draft = RuntimeError("model unavailable"), "DRAFT NOTE: never to be sent"
    drafted = fx.submit("drafted.md")
    assert fx.driver.process_next()["status"] == "input_blocked"
    assert fx.context.pending()["handoff_note"]["text"] == "DRAFT NOTE: never to be sent"
    assert [n["event_type"] for n in fx.notices()] == ["context.input_blocked"]
    assert "DRAFT NOTE" not in fx.notices()[0]["summary"] and fx.notices("context.handoff_published") == []

    fx.runner.fail, fx.runner.draft, fx.runner.handoff = None, None, "Committed: the owner now prefers short briefs."
    fx.control("retry", drafted["id"])
    assert fx.driver.process_next()["status"] == "published"
    published = fx.row(drafted["id"])["result_json"]
    assert published["handoff_note"] == {"disposition": "prepared", "text": "Committed: the owner now prefers short briefs."}
    assert isinstance(published["handoff_event_id"], int)
    delivered = fx.notices("context.handoff_published")
    assert len(delivered) == 1 and "Committed" not in delivered[0]["summary"]            # the text is read from the input
    assert fx.host.deliveries(COORDINATOR, source_type="context_inputs")[-1]["required"] == 1

    fx.runner.handoff = ""
    empty = fx.submit("empty.md")
    assert fx.driver.process_next()["status"] == "published"
    result = fx.row(empty["id"])["result_json"]
    assert result["handoff_note"] == {"disposition": "empty", "text": ""} and result["handoff_event_id"] is None
    assert len(fx.notices("context.handoff_published")) == 1 and len(fx.notices("context.input_blocked")) == 1


def test_a_delivery_is_not_an_operational_action(fx):
    fx.runner.handoff = "Committed note"
    fx.submit()
    fx.drain()
    assert fx.host.count("action_revisions") == 0 and fx.host.count("context_inputs", "input_kind='action'") == 0
    assert len(fx.notices("context.handoff_published")) == 1


# -- the coordinator ---------------------------------------------------------------------------------------------------------

def test_without_a_coordinator_nothing_starts_and_no_model_runs(fx):
    fx.coordinator(None)
    item = fx.submit()
    result = fx.driver.process_next()
    assert result["status"] == "not_ready" and result["reason"] == "no_coordinator"
    row = fx.row(item["id"])
    assert row["state"] == "queued" and row["attempt"] == 0 and row["context_event_id"] is None
    assert fx.runner.calls == [] and fx.context.pending() is None and fx.notices() == []
    assert fx.driver.diagnostics()["coordinator_ready"] is False
    fx.coordinator(COORDINATOR)
    assert fx.driver.process_next()["status"] == "published"


def test_a_coordinator_that_resolves_without_a_codex_runtime_is_still_ready(fx):
    with fx.host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_remove(profile_json,'$.runtime') WHERE agent_id=?",
                               (COORDINATOR,))
    assert fx.driver.coordinator() == COORDINATOR


def test_a_coordinator_lost_after_publication_keeps_the_exact_recovery_and_never_reruns_propagation(fx):
    fx.runner.handoff = "A committed handoff note"
    item = fx.submit("lost.md")
    fx.runner.during = lambda tools, payload: fx.coordinator(None)                     # the coordinator disappears mid-run
    lost = fx.driver.process_next()
    assert lost["status"] == "not_ready" and lost["reason"] == "no_coordinator" and lost["pending_recovery"] is True
    event_id = lost["context_event_id"]
    assert event_id in fx.context.snapshot().events and lost["published_commit"] == fx.context.snapshot().event_commits[event_id]
    assert lost["handoff_note"] == {"disposition": "prepared", "text": "A committed handoff note"}
    assert fx.row(item["id"])["state"] == "processing" and fx.notices() == []           # nothing fabricated
    assert fx.context.pending() is None                                                  # Context's own completion is done
    for _ in range(2):
        assert fx.driver.process_next()["pending_recovery"] is True
        assert fx.driver.recover()["pending_recovery"] is True
    assert fx.runner.propagations == 1 and fx.row(item["id"])["state"] == "processing"
    fx.coordinator(COORDINATOR)
    done = fx.driver.process_next()
    assert done["status"] == "published" and fx.runner.propagations == 1
    assert fx.row(item["id"])["result_json"]["context_event_id"] == event_id and len(fx.notices("context.handoff_published")) == 1


def test_an_empty_handoff_records_without_a_coordinator_because_nobody_is_notified(fx):
    item = fx.submit("quiet.md")
    fx.runner.during = lambda tools, payload: fx.coordinator(None)
    result = fx.driver.process_next()
    assert result["status"] == "published" and fx.row(item["id"])["state"] == "published" and fx.notices() == []


def test_a_blocked_attempt_with_no_coordinator_stays_processing_until_one_can_receive_the_notice(fx):
    fx.runner.fail = RuntimeError("model unavailable")
    item = fx.submit("blocked.md")
    fx.runner.during = lambda tools, payload: fx.coordinator(None)
    result = fx.driver.process_next()
    assert result["status"] == "not_ready" and fx.row(item["id"])["state"] == "processing" and fx.notices() == []
    fx.coordinator(COORDINATOR)
    assert fx.driver.process_next()["status"] == "input_blocked" and fx.row(item["id"])["state"] == "blocked"
    assert len(fx.notices("context.input_blocked")) == 1


# -- the loop ----------------------------------------------------------------------------------------------------------------

def wait_for(condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_the_loop_recovers_first_processes_in_order_wakes_on_new_work_and_stops(fx):
    fx.runner.handoff = "Committed note"
    interrupted = fx.submit("interrupted.md")
    original, fx.driver._submit = fx.driver._submit, lambda row, event_id: (_ for _ in ()).throw(ProcessStopped())
    with pytest.raises(ProcessStopped):
        fx.driver.process_next()
    fx.driver._submit = original
    queued = [fx.submit("one.md"), fx.submit("two.md")]
    loop = ContextDriverLoop(fx.driver, poll_seconds=0.05)
    recovered = loop.start()
    assert recovered["status"] == "input_blocked" and recovered["error"]["code"] == "interrupted"     # mechanical, no model
    assert loop.start() == recovered and loop.running                                  # idempotent while running
    assert wait_for(lambda: fx.row(interrupted["id"])["state"] == "blocked") and fx.runner.calls == []
    fx.control("retry", interrupted["id"])
    loop.wake()
    assert wait_for(lambda: all(fx.row(i["id"])["state"] == "published" for i in (interrupted, *queued)))
    late = fx.submit("late.md")
    loop.wake()
    assert wait_for(lambda: fx.row(late["id"])["state"] == "published")
    rows = fx.rows()
    assert [row["payload_json"]["path"] for row in rows] == [NOTES + name for name in ("interrupted.md", "one.md", "two.md", "late.md")]
    commits = [row["result_json"]["published_commit"] for row in rows]
    chain = [fx.event(row["result_json"]["context_event_id"])["base_revision"] for row in rows[1:]]
    assert chain == commits[:-1]                                                        # published strictly in queue order
    stopped = loop.stop()
    assert stopped == {"stopped": True, "in_flight": False} and not loop.running
    assert loop.diagnostics()["last_error"] is None and fx.runner.propagations == 4


class StalledDriver:
    """A driver whose every step reports the same non-progressing outcome (or raises), counting its calls."""

    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def recover(self):
        return {"status": "idle"}

    def process_next(self):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {"status": self.outcome, "input_id": "stuck"}


@pytest.mark.parametrize("outcome", ["discard_failed", "unrecorded", RuntimeError("unexpected")])
def test_a_persistently_failing_step_backs_off_instead_of_spinning_and_can_still_stop(outcome):
    driver = StalledDriver(outcome)
    loop = ContextDriverLoop(driver, poll_seconds=0.05, max_backoff_seconds=0.2)
    loop.start()
    time.sleep(0.8)
    started = time.monotonic()
    assert loop.stop(timeout=2) == {"stopped": True, "in_flight": False} and time.monotonic() - started < 1
    assert 2 <= driver.calls <= 10                                                       # a spin would be thousands of writes
    assert 1 <= loop.diagnostics()["consecutive_failures"] <= loop._ceiling


def test_the_backoff_exponent_stops_growing_at_the_ceiling():
    loop = ContextDriverLoop(StalledDriver("discard_failed"), poll_seconds=1.0, max_backoff_seconds=60.0)
    delays = [loop._delay(count) for count in (0, 1, 2, 3, 5, 6, 7, 10, 1000, 10 ** 9)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 32.0, 60.0, 60.0, 60.0, 60.0, 60.0]
    assert loop._ceiling == 6


def test_a_long_backoff_wait_is_interrupted_by_stop_and_by_wake():
    driver = StalledDriver("discard_failed")
    loop = ContextDriverLoop(driver, poll_seconds=30.0, max_backoff_seconds=300.0)
    loop.start()
    assert wait_for(lambda: driver.calls == 1)                                           # now waiting out a 60 second delay
    loop.wake()
    assert wait_for(lambda: driver.calls == 2)
    started = time.monotonic()
    assert loop.stop(timeout=5) == {"stopped": True, "in_flight": False} and time.monotonic() - started < 2


def test_the_loop_reports_an_in_flight_model_run_honestly_when_stop_times_out(fx):
    release, entered = threading.Event(), threading.Event()
    fx.runner.during = lambda tools, payload: (entered.set(), release.wait(20))
    fx.submit("slow.md")
    loop = ContextDriverLoop(fx.driver, poll_seconds=0.05)
    loop.start()
    assert entered.wait(20)
    assert loop.stop(timeout=0.05) == {"stopped": False, "in_flight": True}
    release.set()
    assert loop.stop(timeout=20) == {"stopped": True, "in_flight": False}
    assert fx.drain()[0]["status"] in ("idle", "published")


def test_a_second_driver_for_the_same_context_is_busy_not_concurrent(fx):
    entered, release = threading.Event(), threading.Event()
    fx.runner.during = lambda tools, payload: (entered.set(), release.wait(20))
    fx.submit("busy.md")
    worker = threading.Thread(target=fx.driver.process_next, daemon=True)
    worker.start()
    assert entered.wait(20)
    rival = fx.build(Runner())
    assert rival.process_next() == {"status": "busy", "reason": "driver_locked"}
    assert rival.recover() == {"status": "busy", "reason": "driver_locked"}
    release.set()
    worker.join(20)
    assert fx.rows()[0]["state"] == "published"
