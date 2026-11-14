"""Plan runs of the workflow engine: an inline step list, added steps and skipped steps."""
import tempfile
import unittest
from pathlib import Path

from qorqut_agentic.errors import InvalidRequestError, NotFoundError, OperationConflictError, StateConflictError
from qorqut_agentic.layer import AgenticLayer

HOST = {"actor_id": "task-plans", "actor_kind": "host"}
WORKER = {"actor_id": "worker-1", "actor_kind": "agent"}
LEAD = {"actor_id": "lead-1", "actor_kind": "agent"}


class WorkflowPlanTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
