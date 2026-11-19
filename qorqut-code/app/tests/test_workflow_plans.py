"""Plan runs of the workflow engine: an inline step list, added and skipped steps, step metadata and review."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from qorqut_agentic.errors import InvalidRequestError, NotFoundError, OperationConflictError, StateConflictError
from qorqut_agentic.layer import AgenticLayer

import plan_template_package

HOST = {"actor_id": "task-plans", "actor_kind": "host"}
WORKER = {"actor_id": "worker-1", "actor_kind": "agent"}
LEAD = {"actor_id": "lead-1", "actor_kind": "agent"}
NOTIFY = {"notify": "owner", "check": {"kind": "command", "run": "make test"}}


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.layer = AgenticLayer.initialize(base / "tree", base / "state")
        self.workflows = self.layer.workflows

    def start(self, steps=("Find the file", "Change it", "Check the result"), operation_id="plan-1"):
        return self.workflows.start_plan(list(steps), HOST, "task:t1", operation_id)["run"]

    def states(self, run):
        return [(step["id"], step["state"]) for step in run["steps"]]

    def ready(self, run):
        return [step["step_id"] for step in run["ready_steps"]]


class WorkflowPlanTests(PlanCase):
    def test_steps_run_one_after_another(self):
        run = self.start()
        self.assertEqual(run["workflow_ref"], "task-plan")
        self.assertEqual(run["state"], "running")
        self.assertEqual(self.ready(run), ["s1"])
        with self.assertRaises(StateConflictError):
            self.workflows.complete_step(run["run_id"], "s2", WORKER, "op-early")
        run = self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1", "found it")["run"]
        self.assertEqual(self.ready(run), ["s2"])
        run = self.workflows.complete_step(run["run_id"], "s2", WORKER, "op-2")["run"]
        run = self.workflows.complete_step(run["run_id"], "s3", WORKER, "op-3")["run"]
        self.assertEqual(run["state"], "completed")
        self.assertEqual(run["steps"][0]["completion_note"], "found it")
        self.assertEqual(run["steps"][0]["completed_by"], WORKER)

    def test_a_repeated_start_returns_the_same_run(self):
        first = self.start()
        again = self.start()
        self.assertEqual(first["run_id"], again["run_id"])
        with self.assertRaises(OperationConflictError):
            self.start(steps=("Something else",))
        self.assertEqual(self.workflows.plan_run("plan-1")["run"]["run_id"], first["run_id"])
        self.assertIsNone(self.workflows.plan_run("plan-unknown"))

    def test_the_lookup_shows_current_progress(self):
        run = self.start()
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        current = self.workflows.plan_run("plan-1")["run"]
        self.assertEqual(self.states(current)[0], ("s1", "completed"))

    def test_a_plan_needs_real_steps(self):
        for steps in ([], ["  "], ["x"] * 51, "not a list", [3]):
            with self.assertRaises(InvalidRequestError):
                self.workflows.start_plan(steps, HOST, "task:t1", "plan-bad")

    def test_a_step_is_added_after_the_named_step(self):
        run = self.start()
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        run = self.workflows.add_step(run["run_id"], "Back up first", WORKER, "op-add", after="s1")["run"]
        self.assertEqual([step["id"] for step in run["steps"]], ["s1", "s4", "s2", "s3"])
        self.assertEqual(self.ready(run), ["s4"])
        run = self.workflows.complete_step(run["run_id"], "s4", WORKER, "op-4")["run"]
        self.assertEqual(self.ready(run), ["s2"])
        # The same request again changes nothing.
        again = self.workflows.add_step(run["run_id"], "Back up first", WORKER, "op-add", after="s1")["run"]
        self.assertEqual(len(again["steps"]), 4)

    def test_a_step_is_added_at_the_end_by_default(self):
        run = self.start()
        run = self.workflows.add_step(run["run_id"], "Tell the lead", WORKER, "op-add")["run"]
        self.assertEqual([step["id"] for step in run["steps"]], ["s1", "s2", "s3", "s4"])
        self.assertEqual(run["steps"][3]["depends_on"], ["s3"])

    def test_a_step_cannot_be_added_before_started_work(self):
        run = self.start()
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        self.workflows.start_step(run["run_id"], "s2", WORKER, "op-start")
        with self.assertRaises(StateConflictError):
            self.workflows.add_step(run["run_id"], "Too late", WORKER, "op-add", after="s1")
        with self.assertRaises(NotFoundError):
            self.workflows.add_step(run["run_id"], "Nowhere", WORKER, "op-add-2", after="s9")

    def test_a_skipped_step_counts_as_finished(self):
        run = self.start()
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        run = self.workflows.skip_step(run["run_id"], "s2", LEAD, "op-skip", "Already done last week")["run"]
        self.assertEqual(self.states(run)[1], ("s2", "skipped"))
        self.assertEqual(run["steps"][1]["completion_note"], "Already done last week")
        self.assertEqual(run["steps"][1]["completed_by"], LEAD)
        self.assertEqual(self.ready(run), ["s3"])
        with self.assertRaises(StateConflictError):
            self.workflows.skip_step(run["run_id"], "s2", LEAD, "op-skip-2", "Twice")
        with self.assertRaises(StateConflictError):
            self.workflows.complete_step(run["run_id"], "s2", WORKER, "op-2")
        run = self.workflows.complete_step(run["run_id"], "s3", WORKER, "op-3")["run"]
        self.assertEqual(run["state"], "completed")

    def test_skipping_the_last_open_step_finishes_the_run(self):
        run = self.start(steps=("Only step",))
        run = self.workflows.skip_step(run["run_id"], "s1", LEAD, "op-skip", "Not needed")["run"]
        self.assertEqual(run["state"], "completed")
        with self.assertRaises(StateConflictError):
            self.workflows.add_step(run["run_id"], "After the end", WORKER, "op-add")

    def test_a_skip_needs_a_reason(self):
        run = self.start()
        with self.assertRaises(InvalidRequestError):
            self.workflows.skip_step(run["run_id"], "s1", LEAD, "op-skip", " ")

    def test_a_paused_plan_takes_no_changes(self):
        run = self.start()
        self.workflows.pause(run["run_id"], LEAD, "op-pause")
        with self.assertRaises(StateConflictError):
            self.workflows.add_step(run["run_id"], "Later", WORKER, "op-add")
        with self.assertRaises(StateConflictError):
            self.workflows.skip_step(run["run_id"], "s1", LEAD, "op-skip", "Paused")


class PlanStepMetadataTests(PlanCase):
    def snapshot(self, run_id):
        with self.layer.workflows.ctx.store.reader() as conn:
            row = conn.execute("SELECT workflow_revision, snapshot FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row["workflow_revision"], json.loads(row["snapshot"])

    def test_a_plan_of_plain_text_is_stored_as_before(self):
        run = self.start()
        revision, snapshot = self.snapshot(run["run_id"])
        # The stored definition and both digests, written out as they were before steps could carry metadata.
        expected = {"plan": True, "origin": "task:t1", "steps": [
            {"id": "s1", "instructions": "Find the file"},
            {"id": "s2", "instructions": "Change it", "depends_on": ["s1"]},
            {"id": "s3", "instructions": "Check the result", "depends_on": ["s2"]}]}
        self.assertEqual(snapshot, expected)

        def digest(value):
            text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(revision, digest(expected))
        self.assertEqual(run["workflow_revision"], revision)
        with self.layer.workflows.ctx.store.reader() as conn:
            fingerprint = conn.execute("SELECT fingerprint FROM workflow_ops WHERE scope = 'start' AND operation_id = 'plan-1'"
                                       ).fetchone()["fingerprint"]
        self.assertEqual(fingerprint, digest({"operation": "start_plan", "actor": HOST, "origin": "task:t1",
                                              "steps": ["Find the file", "Change it", "Check the result"]}))
        self.assertTrue(all("meta" not in step for step in run["steps"]))
        # Text, a bare record and a record with empty metadata are one and the same request.
        again = self.workflows.start_plan([{"instructions": "Find the file"}, {"instructions": "Change it", "meta": {}},
                                           "Check the result"], HOST, "task:t1", "plan-1")["run"]
        self.assertEqual(again["run_id"], run["run_id"])

    def test_an_added_step_without_metadata_is_stored_as_before(self):
        run = self.start()
        self.workflows.add_step(run["run_id"], "Tell the lead", WORKER, "op-add")
        again = self.workflows.add_step(run["run_id"], "Tell the lead", WORKER, "op-add", meta={})["run"]
        self.assertEqual(len(again["steps"]), 4)
        self.assertNotIn("meta", again["steps"][3])
        _, snapshot = self.snapshot(run["run_id"])
        self.assertEqual(snapshot["steps"][3], {"id": "s4", "instructions": "Tell the lead", "depends_on": ["s3"],
                                                "added": True})
        with self.layer.workflows.ctx.store.reader() as conn:
            fingerprint = conn.execute("SELECT fingerprint FROM workflow_ops WHERE scope = ? AND operation_id = 'op-add'",
                                       (run["run_id"],)).fetchone()["fingerprint"]
        text = json.dumps({"operation": "add_step", "actor": WORKER, "run_id": run["run_id"],
                           "instructions": "Tell the lead", "after": None}, sort_keys=True, separators=(",", ":"))
        self.assertEqual(fingerprint, hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_metadata_is_stored_and_returned_untouched(self):
        steps = ["Find the file", {"instructions": "Change it", "meta": NOTIFY}]
        run = self.start(steps=steps)
        self.assertNotIn("meta", run["steps"][0])
        self.assertEqual(run["steps"][1]["meta"], NOTIFY)
        self.assertEqual(self.start(steps=steps)["run_id"], run["run_id"])
        run = self.workflows.add_step(run["run_id"], "Tell the owner", WORKER, "op-add", meta={"notify": "owner"})["run"]
        self.assertEqual(run["steps"][2]["meta"], {"notify": "owner"})
        self.assertEqual(self.workflows.plan_run("plan-1")["run"]["steps"][1]["meta"], NOTIFY)
        self.assertEqual(self.workflows.get(run["run_id"])["run"]["steps"][2]["meta"], {"notify": "owner"})
        _, snapshot = self.snapshot(run["run_id"])
        self.assertEqual([step.get("meta") for step in snapshot["steps"]], [None, NOTIFY, {"notify": "owner"}])

    def test_metadata_is_part_of_the_request(self):
        self.start(steps=["Find the file", {"instructions": "Change it", "meta": NOTIFY}])
        with self.assertRaises(OperationConflictError):
            self.start(steps=["Find the file", "Change it"])
        with self.assertRaises(OperationConflictError):
            self.start(steps=["Find the file", {"instructions": "Change it", "meta": {"notify": "lead"}}])
        run = self.start(operation_id="plan-2")
        self.workflows.add_step(run["run_id"], "Tell the owner", WORKER, "op-add", meta={"notify": "owner"})
        with self.assertRaises(OperationConflictError):
            self.workflows.add_step(run["run_id"], "Tell the owner", WORKER, "op-add")

    def test_metadata_must_be_a_small_json_object(self):
        for meta in ("owner", ["owner"], 3, {"when": object()}, {"text": "x" * 2000}):
            with self.assertRaises(InvalidRequestError):
                self.workflows.start_plan([{"instructions": "Change it", "meta": meta}], HOST, "task:t1", "plan-bad")
        for item in ({"meta": NOTIFY}, {"instructions": "Change it", "check": "x"}, {"instructions": " "}):
            with self.assertRaises(InvalidRequestError):
                self.workflows.start_plan([item], HOST, "task:t1", "plan-bad")
        self.assertIsNone(self.workflows.plan_run("plan-bad"))
        run = self.start()
        with self.assertRaises(InvalidRequestError):
            self.workflows.add_step(run["run_id"], "Tell the owner", WORKER, "op-add", meta="owner")
        self.assertEqual(len(self.workflows.get(run["run_id"])["run"]["steps"]), 3)


class PlanStepReviewTests(PlanCase):
    def submit(self, run, step_id="s1", operation_id="op-submit", note="found it", output=None):
        return self.workflows.submit_step(run["run_id"], step_id, WORKER, operation_id, note, output)["run"]

    def events(self, run):
        return [event["type"] for event in run["events"]]

    def test_a_submitted_step_waits_for_its_review(self):
        run = self.submit(self.start(), output={"path": "a.txt"})
        step = run["steps"][0]
        self.assertEqual((step["state"], step["verification"], step["ready"]), ("active", "awaiting_review", False))
        self.assertEqual((step["started_by"], step["completed_by"]), (WORKER, None))
        self.assertEqual(self.ready(run), [])
        self.assertEqual(len(step["attempts"]), 1)
        attempt = step["attempts"][0]
        self.assertEqual((attempt["status"], attempt["actor"], attempt["note"], attempt["output"], attempt["operation_id"]),
                         ("submitted", WORKER, "found it", {"path": "a.txt"}, "op-submit"))
        self.assertEqual(run["events"][-1]["type"], "step_submitted")
        self.assertEqual(run["events"][-1]["payload"], {"operation_id": "op-submit", "step_id": "s1"})
        self.assertEqual(run["events"][-1]["actor"], WORKER)

    def test_a_passed_review_completes_the_step(self):
        run = self.submit(self.start(), output={"path": "a.txt"})
        run = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)["run"]
        step = run["steps"][0]
        self.assertEqual((step["state"], step["verification"]), ("completed", "reviewed"))
        self.assertEqual((step["completion_note"], step["output"], step["completed_by"]),
                         ("found it", {"path": "a.txt"}, LEAD))
        attempt = step["attempts"][0]
        self.assertEqual((attempt["status"], attempt["actor"], attempt["feedback"]), ("approved", WORKER, None))
        self.assertEqual(attempt["details"]["reviewed_by"], LEAD)
        self.assertEqual(attempt["details"]["review_operation_id"], "op-review")
        self.assertEqual(self.ready(run), ["s2"])
        self.assertEqual(self.events(run)[-2:], ["step_reviewed", "step_completed"])
        reviewed = run["events"][-2]
        self.assertEqual(reviewed["payload"], {"operation_id": "op-review", "step_id": "s1", "passed": True,
                                               "submission_operation_id": "op-submit"})
        self.assertEqual(run["events"][-1]["payload"]["verification"], "reviewed")

    def test_a_step_sent_back_is_submitted_again(self):
        run = self.submit(self.start())
        run = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, " Wrong file ")["run"]
        step = run["steps"][0]
        self.assertEqual((step["state"], step["verification"], step["ready"]), ("active", "rejected", True))
        self.assertIsNone(step["completed_at"])
        self.assertEqual(self.ready(run), ["s1"])
        self.assertEqual((step["attempts"][0]["status"], step["attempts"][0]["feedback"]), ("rejected", "Wrong file"))
        self.assertEqual(step["attempts"][0]["details"]["reviewed_by"], LEAD)
        self.assertEqual(run["events"][-1]["type"], "step_reviewed")
        self.assertIs(run["events"][-1]["payload"]["passed"], False)
        run = self.submit(run, operation_id="op-submit-2", note="the right file")
        step = run["steps"][0]
        self.assertEqual(step["verification"], "awaiting_review")
        self.assertEqual([attempt["status"] for attempt in step["attempts"]], ["rejected", "submitted"])
        run = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-2", True, "Good")["run"]
        step = run["steps"][0]
        self.assertEqual((step["state"], step["verification"], step["completion_note"]),
                         ("completed", "reviewed", "the right file"))
        self.assertEqual([(attempt["status"], attempt["feedback"]) for attempt in step["attempts"]],
                         [("rejected", "Wrong file"), ("approved", "Good")])

    def test_a_step_sent_back_can_also_be_completed_outright(self):
        run = self.submit(self.start())
        self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Wrong file")
        run = self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1", "fixed")["run"]
        self.assertEqual((run["steps"][0]["state"], run["steps"][0]["verification"]), ("completed", "declared"))

    def test_a_step_awaiting_review_is_not_completed_directly(self):
        run = self.submit(self.start())
        with self.assertRaises(StateConflictError) as raised:
            self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        self.assertIn("awaits review", raised.exception.message)
        with self.assertRaises(StateConflictError) as raised:
            self.submit(run, operation_id="op-submit-2")
        self.assertIn("awaits review", raised.exception.message)
        with self.assertRaises(StateConflictError):
            self.workflows.start_step(run["run_id"], "s1", WORKER, "op-start")
        current = self.workflows.get(run["run_id"])["run"]
        self.assertEqual(current["steps"][0]["verification"], "awaiting_review")
        self.assertEqual(len(current["steps"][0]["attempts"]), 1)

    def test_only_an_open_step_in_order_is_submitted(self):
        run = self.start()
        with self.assertRaises(StateConflictError):
            self.submit(run, step_id="s2")
        with self.assertRaises(NotFoundError):
            self.submit(run, step_id="s9")
        with self.assertRaises(InvalidRequestError):
            self.workflows.submit_step(run["run_id"], "s1", WORKER, "op-submit", 3)
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        with self.assertRaises(StateConflictError):
            self.submit(run)
        self.workflows.pause(run["run_id"], LEAD, "op-pause")
        with self.assertRaises(StateConflictError):
            self.submit(run, step_id="s2")

    def test_a_review_needs_a_submitted_step(self):
        run = self.start()
        with self.assertRaises(StateConflictError) as raised:
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)
        self.assertIn("does not await review", raised.exception.message)
        self.workflows.start_step(run["run_id"], "s1", WORKER, "op-start")
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)
        with self.assertRaises(NotFoundError):
            self.workflows.review_step(run["run_id"], "s9", LEAD, "op-review", True)
        run = self.submit(run)
        self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Wrong file")
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-2", True)
        self.submit(run, operation_id="op-submit-2")
        self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-3", True)
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-4", True)

    def test_a_review_says_plainly_what_it_decides(self):
        run = self.submit(self.start())
        for passed, feedback in ((1, ""), ("yes", ""), (None, ""), (False, ""), (False, "  "), (True, 3),
                                 (False, "x" * 4001)):
            with self.assertRaises(InvalidRequestError):
                self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", passed, feedback)
        self.assertEqual(self.workflows.get(run["run_id"])["run"]["steps"][0]["verification"], "awaiting_review")

    def test_a_paused_plan_takes_no_review(self):
        run = self.submit(self.start())
        self.workflows.pause(run["run_id"], LEAD, "op-pause")
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)
        self.workflows.resume(run["run_id"], LEAD, "op-resume")
        run = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)["run"]
        self.assertEqual(run["steps"][0]["state"], "completed")

    def test_a_step_awaiting_review_can_be_skipped(self):
        run = self.submit(self.start())
        run = self.workflows.skip_step(run["run_id"], "s1", LEAD, "op-skip", "Not needed after all")["run"]
        step = run["steps"][0]
        self.assertEqual((step["state"], step["verification"], step["completion_note"]),
                         ("skipped", "none", "Not needed after all"))
        self.assertEqual(step["attempts"][0]["status"], "withdrawn")
        self.assertEqual(self.ready(run), ["s2"])
        # The event says the step waited, so the host can tell whoever handed it in; a plain skip's event is as before.
        self.assertEqual(run["events"][-1]["payload"], {"operation_id": "op-skip", "step_id": "s1",
                                                        "reason": "Not needed after all", "withdrawn": True})
        run = self.workflows.skip_step(run["run_id"], "s2", LEAD, "op-skip-2", "Nor this")["run"]
        self.assertEqual(run["events"][-1]["payload"], {"operation_id": "op-skip-2", "step_id": "s2", "reason": "Nor this"})
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)

    def test_the_run_completes_when_its_last_step_passes_review(self):
        run = self.start(steps=("Find the file", "Change it"))
        self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-1")
        run = self.submit(run, step_id="s2")
        self.assertEqual(run["state"], "running")
        run = self.workflows.review_step(run["run_id"], "s2", LEAD, "op-review", True)["run"]
        self.assertEqual(run["state"], "completed")
        self.assertIsNotNone(run["completed_at"])
        self.assertEqual(self.events(run)[-3:], ["step_reviewed", "step_completed", "completed"])
        with self.assertRaises(StateConflictError):
            self.submit(run, step_id="s2", operation_id="op-submit-2")

    def test_a_repeated_submit_or_review_changes_nothing(self):
        run = self.start()
        first = self.submit(run)
        again = self.submit(run)
        self.assertEqual(first, again)
        self.assertEqual(len(self.workflows.get(run["run_id"])["run"]["steps"][0]["attempts"]), 1)
        first = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Wrong file")
        again = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Wrong file")
        self.assertEqual(first, again)
        self.assertEqual(first["operation_id"], "op-review")
        self.submit(run, operation_id="op-submit-2")
        # The first submit, repeated after the step moved on, still answers with what it did then.
        self.assertEqual(self.submit(run)["steps"][0]["attempts"][0]["status"], "submitted")
        first = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-2", True)
        again = self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review-2", True)
        self.assertEqual(first, again)
        current = self.workflows.get(run["run_id"])["run"]
        self.assertEqual(self.events(current).count("step_submitted"), 2)
        self.assertEqual(self.events(current).count("step_reviewed"), 2)
        self.assertEqual(self.events(current).count("step_completed"), 1)

    def test_an_operation_id_stands_for_one_request(self):
        run = self.submit(self.start())
        with self.assertRaises(OperationConflictError):
            self.submit(run, note="something else")
        with self.assertRaises(OperationConflictError):
            self.workflows.submit_step(run["run_id"], "s1", LEAD, "op-submit", "found it")
        with self.assertRaises(OperationConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-submit", True)
        self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Wrong file")
        with self.assertRaises(OperationConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", True)
        with self.assertRaises(OperationConflictError):
            self.workflows.review_step(run["run_id"], "s1", LEAD, "op-review", False, "Another reason")
        with self.assertRaises(OperationConflictError):
            self.workflows.complete_step(run["run_id"], "s1", WORKER, "op-review")


class PlanTemplateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.layer = AgenticLayer.initialize(base / "tree", base / "state")
        plan_template_package.install(self.layer)
        self.workflows = self.layer.workflows

    def test_a_saved_workflow_reads_as_ordered_steps(self):
        found = self.workflows.plan_template("house-plans/publish-post")
        self.assertEqual(found["ref"], "house-plans/publish-post")
        self.assertEqual(found["steps"], plan_template_package.PUBLISH_STEPS)
        self.assertTrue(found["revision"])
        self.assertEqual(self.workflows.list()["runs"], [])

    def test_a_saved_workflow_takes_no_review(self):
        run = self.workflows.start("house-plans/publish-post", WORKER, operation_id="op-start")["run"]
        step_id = run["ready_steps"][0]["step_id"]
        with self.assertRaises(StateConflictError):
            self.workflows.submit_step(run["run_id"], step_id, WORKER, "op-submit")
        with self.assertRaises(StateConflictError):
            self.workflows.review_step(run["run_id"], step_id, LEAD, "op-review", True)
        run = self.workflows.complete_step(run["run_id"], step_id, WORKER, "op-1")["run"]
        done = next(step for step in run["steps"] if step["id"] == step_id)
        self.assertEqual((done["state"], done["verification"]), ("completed", "declared"))
        self.assertNotIn("meta", done)

    def test_a_workflow_with_arguments_is_not_a_plan(self):
        with self.assertRaises(InvalidRequestError) as raised:
            self.workflows.plan_template("house-plans/with-inputs")
        self.assertIn("arguments", raised.exception.message)

    def test_an_unknown_workflow_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.workflows.plan_template("house-plans/nothing")
        with self.assertRaises(InvalidRequestError):
            self.workflows.plan_template("not a reference")


if __name__ == "__main__":
    unittest.main()
