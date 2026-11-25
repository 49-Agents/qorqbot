"""The Context queue driver retries a retryable failure itself, then lets a blocked input step aside for other sources.

Real queue storage, a real Context repository and the real Pipeline; only the model runner is a deterministic stand-in and
the clock is injected, so no test sleeps."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from qorqut_context import Context, Pipeline
from qorqut_context.integration.driver import ContextQueueDriver
from qorqut_context.integration.filing import FilingOutcomes
from qorqut_context.integration.queue import transitions
from qorqut_context.integration.queue.operations import decide
from qorqut_context.integration.queue.queue import encode, general_values
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.contracts import new_id, utc_now
from qorqut_operations.roles import ORDUBASHY_ROLE
from qorqut_operations.storage.database import Database
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

META = {"kind": "note", "title": "A note", "origin": "test"}


class Runner:
    """A deterministic model boundary: a path in ``failing`` fails its propagation turn, everything else files cleanly."""
    config = RunnerConfig()

    def __init__(self):
        self.failing, self.calls = set(), []

    def run(self, *, stage, instructions, payload, directory, tools=None, **kwargs):
        if stage == "bootstrap":
            return RunResult("completed", json.dumps({"node_ids": [item["record"]["id"] for item in payload["candidates"]]}),
                             "thread", "turn", self.config.model)
        path = payload["source_change"]["source"]["path"]
        self.calls.append(path)
        if path in self.failing:
            return RunResult("failed", "", "thread", "turn", self.config.model)
        for name, args in (("stage_handoff_note", {"text": ""}),
                           ("finish_propagation", {"status": "complete", "summary": "Filed.", "reassessed": []})):
            result = tools.dispatch(name, args)
            if not result["ok"]:
                raise AssertionError(result)
        return RunResult("completed", "", "thread", "turn", self.config.model)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class DriverRetryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.db = Database(self.root / "state.sqlite", assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.coordinator, self.driver_id = new_id(), new_id(), new_id()
        profile = {"status": "active", "role": {"id": ORDUBASHY_ROLE},
                   "scope": {"kind": "ecosystem", "global_capabilities": ["read_global_context"]}}
        with self.db.transaction() as uow:
            now = utc_now()
            uow.connection.executemany("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                                       [(self.owner, "human", "Owner", now, now), (self.coordinator, "agent", "Coordinator", now, now),
                                        (self.driver_id, "system", "Context queue driver", now, now)])
            uow.connection.execute(
                "INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,profile_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (self.coordinator, self.coordinator, "Coordinator", "coordinator",
                                           json.dumps(profile), now, now))
        self.context = Context.initialize(self.root / "context", self.root / "runtime")
        self.runner, self.clock = Runner(), Clock()
        self.pipeline = Pipeline(self.context, runner=self.runner, config=self.runner.config)
        self.driver = self.make_driver()

    def make_driver(self, delays=(60, 300, 900)):
        return ContextQueueDriver(self.db, self.context, self.pipeline, driver_id=self.driver_id, outcomes=FilingOutcomes(),
                                  programs=NS(get=lambda _: None), coordinator=lambda uow: self.coordinator,
                                  retry_delays=delays, clock=self.clock)

    # -- helpers ----------------------------------------------------------------------------------------------------

    def submit(self, path, operation="add", content="Some note text.\n"):
        input_id = new_id()
        values = general_values(input_id, {"operation": operation, "path": path,
                                           "content": None if operation == "remove" else content,
                                           "metadata": META if operation == "add" else None}, self.owner, utc_now())
        with self.db.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO context_inputs(id,project_id,input_kind,operation,payload_json,submitted_by,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (input_id, None, "general", values["operation"], encode(values["payload_json"]),
                                           self.owner, values["created_at"]))
        return input_id

    def control(self, input_id, intent):
        """What the registered retry/discard operation applies: the same decision, the same rows."""
        with self.db.transaction() as uow:
            row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (input_id,)).fetchone()
            status, state, _ = decide(uow, row, intent, "blocked")
            uow.connection.execute(
                "INSERT INTO context_input_controls(id,project_id,input_id,intent,expected_state,requested_by,requested_at,"
                "status) VALUES (?,?,?,?,?,?,?,?)", (new_id(), None, input_id, intent, "blocked", self.owner, utc_now(), status))
            if state != row["state"]:
                uow.connection.execute("UPDATE context_inputs SET state=? WHERE id=?", (state, input_id))

    def get(self, input_id):
        with self.db.read() as uow:
            return transitions.get(uow, driver_id=self.driver_id, input_id=input_id)

    def notices(self, input_id):
        with self.db.read() as uow:
            return uow.connection.execute("SELECT COUNT(*) FROM inbox_events WHERE event_type='context.input_blocked' "
                                          "AND source_id=?", (input_id,)).fetchone()[0]

    def step(self, status=None):
        result = self.driver.process_next()
        if status is not None:
            self.assertEqual(status, result["status"], result)
        return result

    # -- automatic retry ---------------------------------------------------------------------------------------------

    def test_a_retryable_failure_is_retried_after_its_wait_and_then_publishes(self):
        first = self.submit("foundation/notes/a.md")
        later = self.submit("foundation/notes/b.md")
        self.runner.failing.add("foundation/notes/a.md")
        blocked = self.step("input_blocked")
        self.assertTrue(blocked["error"]["retryable"])
        self.assertEqual({"retry": 1, "of": 3, "due_at": "2026-11-25T12:01:00.000000Z"}, blocked["automatic_retry"])
        self.assertEqual(0, self.notices(first))
        # While the retry is due later, the input holds the queue: the later input does not start.
        self.assertEqual("waiting", self.step()["status"])
        self.clock.advance(59)
        self.assertEqual("waiting", self.step()["status"])
        self.assertEqual("queued", self.get(later)["state"])
        self.runner.failing.clear()
        self.clock.advance(1)
        self.step("published")
        stored = self.get(first)
        self.assertEqual("published", stored["state"])
        self.assertEqual(["queue", "retry"], [attempt["cause"] for attempt in stored["attempts"]])
        control, = stored["controls"]
        self.assertEqual((self.driver_id, "retry", "applied"), (control["requested_by"], control["intent"], control["status"]))
        self.assertEqual(stored["attempts"][1]["control_id"], control["id"])
        self.assertEqual({"automatic": True, "retry": 1, "of": 3, "after_attempt": 1, "due_at": "2026-11-25T12:01:00.000000Z"},
                         control["result"]["request_evidence"])
        self.assertEqual(0, self.notices(first))
        self.step("published")
        self.assertEqual("published", self.get(later)["state"])

    def test_after_three_automatic_retries_the_input_steps_aside_for_an_unrelated_input(self):
        self.driver = self.make_driver(delays=(0, 0, 0))
        first = self.submit("foundation/notes/a.md")
        later = self.submit("foundation/notes/b.md")
        self.runner.failing.add("foundation/notes/a.md")
        for retry in (1, 2, 3):
            self.assertEqual(retry, self.step("input_blocked")["automatic_retry"]["retry"])
        final = self.step("input_blocked")
        self.assertNotIn("automatic_retry", final)
        self.assertEqual(4, final["attempt"])
        self.assertEqual(1, self.notices(first))
        self.assertEqual(4, self.runner.calls.count("foundation/notes/a.md"))
        # Its draft is still Context's pending event; the next input discards it mechanically and goes on.
        self.assertEqual(self.get(first)["context_event_id"], self.context.pending()["id"])
        self.step("published")
        self.assertEqual("published", self.get(later)["state"])
        self.assertIsNone(self.context.pending())
        self.assertEqual({"status": "blocked", "input_id": first, "attempt": 4, "blocked": 1}, self.step())
        stored = self.get(first)
        self.assertEqual("blocked", stored["state"])
        self.assertNotIn("automatic_retry", stored["error"])
        self.assertEqual(["queue", "retry", "retry", "retry"], [attempt["cause"] for attempt in stored["attempts"]])
        self.assertEqual(1, self.notices(first))

    def test_a_later_input_for_the_same_source_waits_behind_the_blocked_one(self):
        self.driver = self.make_driver(delays=())
        first = self.submit("foundation/notes/a.md")
        edit = self.submit("foundation/notes/a.md", "edit", "Changed note text.\n")
        other = self.submit("foundation/notes/c.md")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        self.step("published")
        self.assertEqual("published", self.get(other)["state"])
        self.assertEqual("blocked", self.step()["status"])
        self.assertEqual(("queued", 0), (self.get(edit)["state"], self.get(edit)["attempt"]))
        self.assertEqual(["foundation/notes/a.md", "foundation/notes/c.md"], self.runner.calls)
        self.assertEqual(1, self.notices(first))

    def test_a_non_retryable_failure_is_not_retried(self):
        first = self.submit("foundation/notes/a.md")
        self.step("published")
        again = self.submit("foundation/notes/a.md")      # Context refuses an add over an existing path
        later = self.submit("foundation/notes/b.md")
        result = self.step("input_blocked")
        self.assertFalse(result["error"]["retryable"])
        self.assertNotIn("automatic_retry", result)
        self.assertEqual(1, self.notices(again))
        self.assertEqual(["foundation/notes/a.md"], self.runner.calls)
        self.clock.advance(3600)
        self.step("published")
        self.assertEqual("published", self.get(later)["state"])
        stored = self.get(again)
        self.assertEqual(("blocked", 1, []), (stored["state"], stored["attempt"], stored["controls"]))
        self.assertEqual("published", self.get(first)["state"])

    def test_the_planned_retry_survives_a_restart(self):
        first = self.submit("foundation/notes/a.md")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        self.driver = self.make_driver()                  # a new process: nothing is kept in memory
        self.assertEqual("waiting", self.driver.recover()["status"])
        self.clock.advance(60)
        # Recovery never starts an attempt, even once the retry is due.
        self.assertEqual("waiting", self.driver.recover()["status"])
        self.runner.failing.clear()
        self.step("published")
        self.assertEqual(["queue", "retry"], [attempt["cause"] for attempt in self.get(first)["attempts"]])

    def test_an_interrupted_attempt_is_blocked_at_startup_with_an_automatic_retry(self):
        first = self.submit("foundation/notes/a.md")
        with self.db.transaction() as uow:      # a driver that started the attempt and then stopped
            transitions.start_attempt(uow, driver_id=self.driver_id, input_id=first, context_event_id="ab123")
        self.driver = self.make_driver()
        recovered = self.driver.recover()
        self.assertEqual(("input_blocked", "interrupted"), (recovered["status"], recovered["error"]["code"]))
        self.assertEqual(1, recovered["automatic_retry"]["retry"])
        self.assertEqual(0, self.notices(first))
        self.clock.advance(60)
        self.step("published")
        stored = self.get(first)
        self.assertEqual(("published", "ab123"), (stored["state"], stored["context_event_id"]))

    def test_a_manual_retry_of_a_stepped_aside_input_runs_next_and_releases_its_source(self):
        self.driver = self.make_driver(delays=())
        first = self.submit("foundation/notes/a.md")
        edit = self.submit("foundation/notes/a.md", "edit", "Changed note text.\n")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        other = self.submit("foundation/notes/c.md")
        unrelated = self.submit("foundation/notes/d.md")
        self.step("published")                           # c.md went past the blocked input
        self.runner.failing.clear()
        self.control(first, "retry")
        self.step("published")                           # the retried input goes before d.md
        stored = self.get(first)
        self.assertEqual(("published", ["queue", "retry"]), (stored["state"], [a["cause"] for a in stored["attempts"]]))
        self.assertEqual(self.owner, stored["controls"][0]["requested_by"])
        self.assertEqual("queued", self.get(unrelated)["state"])
        self.step("published")
        self.assertEqual("published", self.get(edit)["state"])
        self.step("published")
        self.assertEqual(["published"] * 2, [self.get(i)["state"] for i in (other, unrelated)])
        self.assertEqual({"status": "idle"}, self.step())

    def published_on(self, input_id):
        """The base the input's event was published on and the commit it made."""
        result = self.get(input_id)["result"]
        return result["base_revision"], result["published_commit"]

    def test_a_stepped_aside_model_input_retried_after_the_head_moved_publishes_on_the_new_head(self):
        self.driver = self.make_driver(delays=())
        first = self.submit("foundation/notes/a.md")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        other = self.submit("foundation/notes/c.md")
        self.step("published")
        _, moved = self.published_on(other)
        self.runner.failing.clear()
        self.control(first, "retry")
        self.step("published")
        base, commit = self.published_on(first)
        self.assertEqual(moved, base)
        self.assertEqual(commit, self.context.snapshot().revision)

    def test_a_stepped_aside_input_whose_draft_was_ready_retries_afresh_on_the_new_head(self):
        self.driver = self.make_driver(delays=())
        first = self.submit("foundation/notes/a.md")
        checkpoint, failing = self.pipeline._checkpoint, {"after_ready"}

        def fail_once(phase):
            if phase in failing:
                failing.discard(phase)
                raise OSError("publication failed after the draft was ready")
            return checkpoint(phase)

        self.pipeline._checkpoint = fail_once
        blocked = self.step("input_blocked")
        self.assertEqual(("ready", True), (blocked["error"]["phase"], blocked["error"]["retryable"]))
        event_id = self.get(first)["context_event_id"]
        self.assertTrue(list((self.root / "runtime" / "runs" / event_id).glob("*/ready.json")))
        other = self.submit("foundation/notes/c.md")
        self.step("published")                           # the ready draft is discarded and the head moves
        runs = self.root / "runtime" / "runs" / event_id
        self.assertEqual(([], 1), (list(runs.glob("*/ready.json")), len(list(runs.glob("*/ready.discarded.json")))))
        _, moved = self.published_on(other)
        self.control(first, "retry")
        self.step("published")
        base, _ = self.published_on(first)
        self.assertEqual(moved, base)
        self.assertEqual(event_id, self.get(first)["context_event_id"])
        self.assertEqual(2, self.runner.calls.count("foundation/notes/a.md"))

    def test_a_ready_draft_kept_in_place_is_published_by_its_automatic_retry_without_the_model(self):
        self.driver = self.make_driver(delays=(0,))
        first = self.submit("foundation/notes/a.md")
        checkpoint, failing = self.pipeline._checkpoint, {"after_ready"}

        def fail_once(phase):
            if phase in failing:
                failing.discard(phase)
                raise OSError("publication failed after the draft was ready")
            return checkpoint(phase)

        self.pipeline._checkpoint = fail_once
        self.assertEqual(1, self.step("input_blocked")["automatic_retry"]["retry"])
        self.step("published")
        self.assertEqual(1, self.runner.calls.count("foundation/notes/a.md"))
        self.assertEqual("published", self.get(first)["state"])

    def test_a_manual_discard_of_a_stepped_aside_input_still_completes(self):
        self.driver = self.make_driver(delays=())
        first = self.submit("foundation/notes/a.md")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        self.control(first, "discard")
        result = self.step("discarded")
        self.assertEqual("draft_discarded", result["context"])
        self.assertIsNone(self.context.pending())
        self.assertEqual({"status": "idle"}, self.step())

    def test_only_the_next_input_in_order_may_start(self):
        self.driver = self.make_driver(delays=())
        blocked = self.submit("foundation/notes/a.md")
        waiting = self.submit("foundation/notes/a.md", "edit", "Changed note text.\n")
        free = self.submit("foundation/notes/b.md")
        self.runner.failing.add("foundation/notes/a.md")
        self.step("input_blocked")
        with self.db.read() as uow:
            now = transitions.order(uow, driver_id=self.driver_id)
        self.assertEqual((free, None, [blocked]), (now["next"]["id"], now["retry"], [row["id"] for row in now["blocked"]]))
        with self.db.transaction() as uow:
            with self.assertRaisesRegex(Exception, "Only the next input"):
                transitions.start_attempt(uow, driver_id=self.driver_id, input_id=waiting, context_event_id="zz999")
            with self.assertRaisesRegex(Exception, "retry request"):
                transitions.start_attempt(uow, driver_id=self.driver_id, input_id=blocked, context_event_id="zz998")


    def test_the_source_of_an_input_is_its_path_or_its_action(self):
        self.assertEqual(("general", "foundation/notes/a.md"),
                         transitions.source_key({"input_kind": "general", "item_id": None, "path": "foundation/notes/a.md"}))
        self.assertEqual(("general", "foundation/notes/a.md"), transitions.source_key(
            {"input_kind": "general", "item_id": None, "payload_json": json.dumps({"path": "foundation/notes/a.md"})}))
        self.assertEqual(("action", "action-1"), transitions.source_key({"input_kind": "action", "item_id": "action-1", "path": None}))


if __name__ == "__main__":
    unittest.main()
