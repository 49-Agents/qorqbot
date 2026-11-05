import json
from unittest.mock import patch

from qorqut_context import Conflict, ContextError
from qorqut_context.pipeline import Pipeline, PropagationError
from qorqut_context.runner import RunResult, RunnerConfig
from .helpers import ContextCase
from .test_recovery import ProcessStopped


def call(tools, name, **arguments):
    result = tools.dispatch(name, arguments)
    if not result["ok"]:
        raise AssertionError(result)
    return result["result"]


def finish(tools):
    call(tools, "stage_handoff_note", text="")
    return call(tools, "finish_propagation", status="complete", summary="Processed the supplied source.",
                reassessed=tools.required_reassessment)


def new_node(tools, summary="Jesse"):
    return call(tools, "put_node", record={"summary": summary, "attributes": {
        "basic_entity_type": ["person"], "name": [summary]}},
        explanation="Supported by this source.", citations=["self"], support_after=["self"])["id"]


class ScriptRunner:
    """Injected runner honoring the explicit ``Runner`` protocol.

    ``usage(stage, index, emit)`` may report cumulative snapshots through ``emit`` (the ``on_usage`` callback)
    and returns the final snapshot for ``RunResult.usage`` (or ``None``); without it no usage is supplied.
    """
    config = RunnerConfig()

    def __init__(self, action, usage=None):
        self.action, self.calls, self.usage = action, [], usage

    def run(self, *, stage, instructions, payload, directory, tools=None, output_schema=None,
            on_identity=None, on_usage=None):
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


class Propagation(ContextCase):
    def submit(self, pipeline, name="one.md", content=b"Jesse appears in this note.", **kwargs):
        return pipeline.submit("add", "foundation/" + name, content=content,
            metadata={"kind": "note", "title": name, "origin": "focused-test"}, submitted_by="test", **kwargs)

    def test_source_lifecycle_support_only_and_corrections(self):
        ids = []
        def create(tools, payload):
            self.assertEqual(payload["bootstrap"]["X"], [])
            ids.append(new_node(tools))
            finish(tools)
        runner = ScriptRunner(create)
        pipeline = Pipeline(self.context, runner=runner)
        first = self.submit(pipeline)
        first_commit = first["published_commit"]
        key = ids[0]
        def support(tools, payload):
            self.assertEqual(payload["bootstrap"]["X"], [key])
            record = call(tools, "read_object", kind="node", id=key)["record"]
            call(tools, "put_node", record=record, explanation="Additional independent support.",
                 citations=["self"], support_after=[first_commit, "self"])
            finish(tools)
        runner.action = support
        second = self.submit(pipeline, "two.md")
        self.assertEqual(second["result_counts"]["node"]["edit"], 1)
        def correct(tools, payload):
            self.assertEqual(payload["source_change"]["before"], "Jesse appears in this note.")
            self.assertEqual(tools.required_reassessment, [{"kind": "node", "id": key}])
            record = call(tools, "read_object", kind="node", id=key)["record"]
            call(tools, "put_node", record=record, explanation="Withdrawn text; independent source survives.",
                 citations=["self", first_commit], support_after=[first_commit])
            finish(tools)
        runner.action = correct
        pipeline.submit("edit", "foundation/two.md", content=b"---\ntitle: Note\n---\n", submitted_by="test")
        runner.action = lambda tools, payload: finish(tools)
        last = pipeline.submit("remove", "foundation/two.md", submitted_by="test")
        self.assertEqual(self.context.snapshot().read("node", key)["support"], [first_commit])
        self.assertEqual(len(self.context.snapshot().history(key)), 3)
        self.assertEqual(self.context.snapshot(first_commit).read("node", key)["support"], [first_commit])
        self.assertEqual(self.context.snapshot().source_change(second["published_commit"])["after"], b"Jesse appears in this note.")
        for receipt in (first, second, last):
            self.assertTrue((self.context.runtime / "runs" / receipt["event_id"]).exists())
            self.assertFalse((self.context.runtime / "events" / receipt["event_id"]).exists())
        count = len(runner.calls)
        self.assertEqual(pipeline.retry(second["event_id"])["published_commit"], second["published_commit"])
        self.assertEqual(len(runner.calls), count)

    def test_failed_partial_draft_retry_reuses_bootstrap_and_excludes_competitor(self):
        source = self.source()
        self.node(source)
        self.context.publish(source.event_id)
        staged = []
        def fail(tools, payload):
            with self.assertRaises(Conflict):
                Pipeline(self.context, runner=runner).retry(tools.event_id)
            staged.append(new_node(tools, "A new person"))
            raise RuntimeError("transport disappeared after staging")
        runner = ScriptRunner(fail)
        pipeline = Pipeline(self.context, runner=runner)
        before = self.context.git.head()
        with self.assertRaises(PropagationError) as caught:
            self.submit(pipeline, "two.md")
        event_id = caught.exception.event_id
        self.assertEqual(self.context.git.head(), before)
        self.assertEqual(runner.calls, ["bootstrap", "propagation"])
        def resume(tools, payload):
            self.assertEqual(payload["existing_draft"][0]["id"], staged[0])
            self.assertIsNotNone(call(tools, "read_object", kind="node", id=staged[0])["record"])
            finish(tools)
        runner.action = resume
        receipt = pipeline.retry(event_id)
        self.assertEqual(runner.calls, ["bootstrap", "propagation", "propagation"])
        self.assertEqual(receipt["event_id"], event_id)
        self.assertEqual(len(self.context.snapshot().nodes), 2)

    def test_ready_and_published_recovery_never_reruns_model(self):
        runner = ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools)))
        pipeline = Pipeline(self.context, runner=runner)
        def stop(phase):
            if phase == "after_ready":
                raise ProcessStopped()
        with patch.object(pipeline, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.submit(pipeline)
        event_id = self.context.pending()["id"]
        receipt = pipeline.retry(event_id)
        self.assertEqual(runner.calls, ["propagation"])
        def published_stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=published_stop), self.assertRaises(ProcessStopped):
            self.submit(pipeline, "two.md", b"A different person")
        second = self.context.pending()["id"]
        calls = list(runner.calls)
        commit = self.context.git.head()
        self.assertEqual(pipeline.retry(second)["published_commit"], commit)
        self.assertEqual(runner.calls, calls)
        self.assertNotEqual(commit, receipt["published_commit"])

    def test_ready_mismatch_and_input_limits_preserve_state(self):
        runner = ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools)))
        pipeline = Pipeline(self.context, runner=runner)
        with self.assertRaises(ContextError):
            self.submit(pipeline, content=b"\xff")
        self.assertIsNone(self.context.pending())
        with patch.object(pipeline, "_checkpoint", side_effect=ProcessStopped()), self.assertRaises(ProcessStopped):
            self.submit(pipeline)
        pending = self.context.pending()
        self.context.put_node(pending["id"], {"summary": "Late write", "attributes": {"basic_entity_type": ["entity"]}},
            explanation="Late independent write", citations=["self"], support_after=["self"])
        with self.assertRaisesRegex(PropagationError, "does not match"):
            pipeline.retry(pending["id"])
        self.assertEqual(runner.calls, ["propagation"])
        self.assertEqual(self.context.git.head(), pending["base_revision"])

    def test_blocked_missing_and_invalid_completion_cannot_publish(self):
        for mode in ("blocked", "missing", "invalid"):
            with self.subTest(mode=mode):
                def action(tools, payload):
                    key = new_node(tools)
                    if mode == "blocked":
                        call(tools, "stage_handoff_note", text="Which person does this ambiguous source describe?")
                        call(tools, "finish_propagation", status="blocked", summary="Ambiguous source.", reassessed=[])
                    elif mode == "invalid":
                        call(tools, "put_edge", record={"from_id": key, "to_id": "xxxxx", "summary": "Invalid endpoint", "attributes": {}},
                             explanation="Bad model output", citations=["self"], support_after=["self"])
                        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Invalid", "reassessed": []})["ok"])
                pipeline = Pipeline(self.context, runner=ScriptRunner(action))
                before = self.context.git.head()
                with self.assertRaises(PropagationError) as caught:
                    self.submit(pipeline)
                self.assertEqual(self.context.git.head(), before)
                self.assertIsNotNone(self.context.pending())
                pipeline.discard(caught.exception.event_id)
