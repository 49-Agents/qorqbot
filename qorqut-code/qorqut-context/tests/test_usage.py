"""Durable per-call usage receipts and their read-only page API: synthetic Git stores and injected runners only."""
import json
import os
from pathlib import Path
import re
import tempfile
from unittest import TestCase, skipIf
from unittest.mock import patch

from qorqut_context import Conflict, ContextError, Pipeline, PropagationError, RunResult, Usage
from qorqut_context import usage as usage_module
from qorqut_context.runner import RunnerConfig
from qorqut_context.schema import encode
from .helpers import ContextCase
from .test_pipeline import ScriptRunner, finish, new_node
from .test_recovery import ProcessStopped

TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")


def snap(index, input_tokens=10, **changes):
    """A cumulative snapshot attributed to the ScriptRunner's call number ``index``."""
    return Usage(**{"input_tokens": input_tokens, "cached_input_tokens": 4, "output_tokens": 2,
                    "reasoning_output_tokens": 1, "total_tokens": input_tokens + 2,
                    "thread_id": "script", "turn_id": str(index), "source": "synthetic", **changes})


class CallReceipts(ContextCase):
    def setUp(self):
        super().setUp()
        source = self.source()
        self.node(source)
        self.context.publish(source.event_id)       # a graph node the next source mentions, so bootstrap calls the model

    def submit(self, pipeline, name="two.md"):
        return pipeline.submit("add", "foundation/" + name, content=b"Jesse appears in this note.",
                               metadata={"kind": "note", "title": name, "origin": "focused-test"}, submitted_by="test")

    def read(self, pipeline, event_id, **kwargs):
        result = pipeline.usage_receipts(event_id, **kwargs)
        self.assertEqual(json.loads(json.dumps(result)), result)           # primitive JSON values only
        return result

    def receipt_files(self, event_id):
        return sorted((self.context.runtime / "runs" / event_id).glob("*/calls/*.json"))

    def test_a_successful_pair_is_reserved_before_launch_and_recorded_with_its_counters(self):
        observed = []

        def usage(stage, index, emit):
            (path,) = self.context.runtime.glob(f"runs/*/*/calls/{stage}.json")
            reserved = json.loads(path.read_bytes())
            observed.append((stage, reserved["state"], reserved["finished_at"], reserved["usage"]))
            emit(snap(index, 10 * index))
            final = snap(index, 20 * index)
            emit(final)
            return final

        runner = ScriptRunner(lambda tools, payload: (new_node(tools, "A new person"), finish(tools)), usage)
        pipeline = Pipeline(self.context, runner=runner)
        event_id = self.submit(pipeline)["event_id"]
        empty = {name: None for name in ("input_tokens", "cached_input_tokens", "output_tokens",
                                         "reasoning_output_tokens", "total_tokens", "cost_usd")}
        self.assertEqual(observed, [("bootstrap", "started", None, empty), ("propagation", "started", None, empty)])

        result = self.read(pipeline, event_id)
        self.assertEqual((result["status"], result["event_id"], result["next_cursor"], result["error"]),
                         ("ready", event_id, None, None))
        bootstrap, propagation = result["items"]
        attempt_id = bootstrap["attempt_id"]
        self.assertEqual([item["call_id"] for item in result["items"]], [f"{attempt_id}:bootstrap", f"{attempt_id}:propagation"])
        self.assertEqual([(item["stage"], item["state"], item["attempt_sequence"]) for item in result["items"]],
                         [("bootstrap", "completed", 1), ("propagation", "completed", 1)])
        self.assertEqual(bootstrap["usage"], {"input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 2,
                                              "reasoning_output_tokens": 1, "total_tokens": 22, "cost_usd": None})
        self.assertEqual(propagation["usage"]["input_tokens"], 40)              # replaced, never summed
        self.assertEqual(bootstrap["observed"], {"provider": None, "model": None, "thread_id": "script", "turn_id": "1"})
        self.assertEqual(propagation["observed"]["turn_id"], "2")
        self.assertEqual(propagation["configured"], {"model": runner.config.model, "effort": "max", "service_tier": "default"})
        # A finished call with all five counters is still not *verified final* usage.
        self.assertEqual(bootstrap["evidence"], {
            "usage_source": "synthetic", "usage_scope": "native", "usage_updates": 2, "ignored_updates": 0,
            "usage_observed": True, "counters_complete": True, "usage_complete": False, "cost_observed": False,
            "limitations": ["final_totals_unproven", "cost_unreported"]})
        for item in result["items"]:
            self.assertRegex(item["started_at"], TIME)
            self.assertRegex(item["finished_at"], TIME)
            self.assertLessEqual(item["started_at"], item["finished_at"])
            self.assertIsNone(item["error"])
        self.assertIsNone(bootstrap["bootstrap_call_id"])
        self.assertEqual(propagation["bootstrap_call_id"], bootstrap["call_id"])
        attempt = json.loads(next((self.context.runtime / "runs" / event_id).glob("*/attempt.json")).read_bytes())
        self.assertEqual(attempt["bootstrap_call_id"], bootstrap["call_id"])
        for path in self.receipt_files(event_id):
            self.assertEqual((path.stat().st_mode & 0o777, path.parent.stat().st_mode & 0o777), (0o600, 0o700))
        for text in json.dumps(result).split('"'):                      # no prompts, payloads or tool arguments
            self.assertNotIn("Jesse", text)

    def test_repeated_counters_replace_and_foreign_or_malformed_updates_are_counted_not_summed(self):
        def usage(stage, index, emit):
            if stage == "propagation":
                emit(snap(index, 10))
                emit(snap(index, 999, turn_id="other-turn"))             # another native turn
                emit(snap(index, 999, thread_id="other-thread"))
                emit(snap(index, -1))                                    # malformed counters
                emit(snap(index, True))
                emit(snap(index, 5, cost_usd=float("nan")))
                emit(snap(index, 5, cost_usd=-0.5))
                emit({"input_tokens": 5})                                # not a Usage
                emit(Usage())                                            # carries no evidence at all
                emit(snap(index, 40, cost_usd=0.25))
                emit(snap(index, 40, cost_usd=0.25))                     # a repeated cumulative value
            return None

        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools, "X"), finish(tools)), usage))
        propagation = self.read(pipeline, self.submit(pipeline)["event_id"])["items"][1]
        self.assertEqual(propagation["usage"]["input_tokens"], 40)
        self.assertEqual(propagation["usage"]["cost_usd"], 0.25)
        self.assertEqual((propagation["evidence"]["usage_updates"], propagation["evidence"]["ignored_updates"]), (3, 7))
        self.assertIs(propagation["evidence"]["counters_complete"], True)
        self.assertIs(propagation["evidence"]["usage_complete"], False)           # ignored evidence stays visible
        self.assertEqual(propagation["evidence"]["limitations"], ["final_totals_unproven", "ignored_updates"])
        self.assertIs(propagation["evidence"]["cost_observed"], True)

    def test_a_failed_call_keeps_the_usage_observed_before_it_failed(self):
        def usage(stage, index, emit):
            if stage == "propagation":
                emit(snap(index, 30))

        def fail(tools, payload):
            raise RuntimeError("transport disappeared")

        pipeline = Pipeline(self.context, runner=ScriptRunner(fail, usage))
        with self.assertRaises(PropagationError) as caught:
            self.submit(pipeline)
        bootstrap, propagation = self.read(pipeline, caught.exception.event_id)["items"]
        self.assertEqual((bootstrap["state"], bootstrap["usage"]["input_tokens"]), ("completed", None))
        self.assertEqual((propagation["state"], propagation["usage"]["input_tokens"]), ("failed", 30))
        self.assertEqual(propagation["error"], {"type": "RuntimeError", "message": "transport disappeared"})
        self.assertRegex(propagation["finished_at"], TIME)

    def test_crash_after_usage_is_unfinished_not_free_and_an_explicit_retry_is_charged_separately(self):
        stopped = []

        def action(tools, payload):
            if not stopped:
                stopped.append(True)
                raise ProcessStopped()
            new_node(tools, "A new person")
            finish(tools)

        def usage(stage, index, emit):
            final = snap(index, 10 * index)
            emit(final)
            return final

        runner = ScriptRunner(action, usage)
        pipeline = Pipeline(self.context, runner=runner)
        with self.assertRaises(ProcessStopped):
            self.submit(pipeline)
        event_id = self.context.pending()["id"]
        before = self.read(pipeline, event_id)["items"]
        self.assertEqual([(item["stage"], item["state"]) for item in before], [("bootstrap", "completed"), ("propagation", "started")])
        unfinished = before[1]
        self.assertIsNone(unfinished["finished_at"])
        self.assertEqual(unfinished["usage"]["input_tokens"], 20)               # persisted before the crash
        self.assertEqual((unfinished["evidence"]["counters_complete"], unfinished["evidence"]["usage_complete"]), (True, False))
        self.assertEqual(unfinished["evidence"]["limitations"], ["unfinished_call", "final_totals_unproven", "cost_unreported"])

        pipeline.retry(event_id)
        self.assertEqual(runner.calls, ["bootstrap", "propagation", "propagation"])      # the bootstrap sample is reused
        after = self.read(pipeline, event_id)["items"]
        self.assertEqual(after[:2], before)                                      # earlier receipts are never rewritten
        self.assertEqual([(item["attempt_sequence"], item["stage"], item["state"]) for item in after],
                         [(1, "bootstrap", "completed"), (1, "propagation", "started"), (2, "propagation", "completed")])
        self.assertEqual(len({item["call_id"] for item in after}), 3)
        retried = after[2]
        self.assertEqual(retried["bootstrap_call_id"], before[0]["call_id"])    # references the original call, no second bootstrap
        self.assertEqual(retried["usage"]["input_tokens"], 30)                   # its own call, not added to the first
        attempt = json.loads(next((self.context.runtime / "runs" / event_id).glob("*/attempt.json")).read_bytes())
        self.assertEqual(attempt["bootstrap_call_id"], before[0]["call_id"])

    def test_publication_recovery_and_receipt_replay_create_no_call(self):
        runner = ScriptRunner(lambda tools, payload: (new_node(tools, "A new person"), finish(tools)),
                              lambda stage, index, emit: snap(index))
        pipeline = Pipeline(self.context, runner=runner)

        def stop(phase):
            if phase == "after_ready":
                raise ProcessStopped()

        with patch.object(pipeline, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.submit(pipeline)
        event_id = self.context.pending()["id"]
        before = self.read(pipeline, event_id)
        self.assertEqual(len(before["items"]), 2)
        pipeline.retry(event_id)                                   # publishes from the ready record
        pipeline.retry(event_id)                                   # replays the published receipt
        pipeline.recover()
        self.assertEqual(runner.calls, ["bootstrap", "propagation"])
        self.assertEqual(self.read(pipeline, event_id), before)

    def test_unavailable_usage_and_cost_stay_unknown_never_zero(self):
        pipeline = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools, "X"), finish(tools))))
        bootstrap, propagation = self.read(pipeline, self.submit(pipeline)["event_id"])["items"]
        for item in (bootstrap, propagation):
            self.assertEqual(item["state"], "completed")
            self.assertEqual(set(item["usage"].values()), {None})
            self.assertEqual(item["evidence"], {"usage_source": None, "usage_scope": None, "usage_updates": 0,
                                                "ignored_updates": 0, "usage_observed": False, "counters_complete": False,
                                                "usage_complete": False, "cost_observed": False,
                                                "limitations": ["usage_unreported", "cost_unreported"]})

        def partial(stage, index, emit):
            emit(Usage(total_tokens=7, thread_id="script", turn_id=str(index)))
            return None

        second = Pipeline(self.context, runner=ScriptRunner(lambda tools, payload: (new_node(tools, "Y"), finish(tools)), partial))
        usage = self.read(second, self.submit(second, "three.md")["event_id"])["items"][1]
        self.assertEqual(usage["usage"], {"input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
                                          "reasoning_output_tokens": None, "total_tokens": 7, "cost_usd": None})
        self.assertEqual((usage["evidence"]["usage_observed"], usage["evidence"]["counters_complete"],
                          usage["evidence"]["usage_complete"]), (True, False, False))
        self.assertEqual(usage["evidence"]["limitations"], ["partial_counters", "final_totals_unproven", "cost_unreported"])

    def stopped_event(self):
        """An event with three receipts across two attempts (the first crashed)."""
        stopped = []

        def action(tools, payload):
            if not stopped:
                stopped.append(True)
                raise ProcessStopped()
            new_node(tools, "A new person")
            finish(tools)

        pipeline = Pipeline(self.context, runner=ScriptRunner(action, lambda stage, index, emit: snap(index)))
        with self.assertRaises(ProcessStopped):
            self.submit(pipeline)
        event_id = self.context.pending()["id"]
        pipeline.retry(event_id)
        return pipeline, event_id

    def test_pages_are_explicit_ordered_and_bound_to_the_event_and_limit(self):
        pipeline, event_id = self.stopped_event()
        everything = self.read(pipeline, event_id)["items"]
        self.assertEqual(len(everything), 3)
        collected, cursor, pages = [], None, 0
        while True:
            page = self.read(pipeline, event_id, limit=1, cursor=cursor)
            collected += page["items"]
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
            self.assertEqual(len(page["items"]), 1)                              # a further page is explicit
        self.assertEqual((collected, pages), (everything, 3))
        first = self.read(pipeline, event_id, limit=2)
        self.assertEqual((len(first["items"]), first["next_cursor"] is not None), (2, True))
        self.assertEqual(self.read(pipeline, event_id, limit=2, cursor=first["next_cursor"])["items"], everything[2:])
        self.assertIsNone(self.read(pipeline, event_id, limit=3)["next_cursor"])
        self.assertEqual(len(self.read(pipeline, event_id, limit=500)["items"]), 3)

        refused = [("limit", {"limit": value}) for value in (0, 501, -1, True, 2.0, "5", None)]
        refused += [("cursor", {"limit": 2, "cursor": value}) for value in (
            "", "garbage", "a.b", 7, first["next_cursor"][:-1] + ("0" if first["next_cursor"][-1] != "0" else "1"),
            first["next_cursor"].split(".")[0], first["next_cursor"] + ".00", "x" * 600)]
        refused += [("binding", {"limit": 1, "cursor": first["next_cursor"]})]            # issued for limit 2
        for label, arguments in refused:
            with self.subTest(label=label, arguments=arguments), self.assertRaises(ContextError):
                pipeline.usage_receipts(event_id, **arguments)
        with self.assertRaises(ContextError):                                              # issued for another event
            pipeline.usage_receipts("zzzzz", limit=2, cursor=first["next_cursor"])
        for bad in ("abc", "UPPER", 5, None, "../x/abc"):
            with self.subTest(event=bad), self.assertRaises(ContextError):
                pipeline.usage_receipts(bad)

    def test_reading_takes_no_lock_creates_nothing_and_an_unrun_event_has_no_items(self):
        runtime = self.context.runtime
        self.assertFalse((runtime / "runs").exists())
        self.assertEqual(Pipeline(self.context, runner=ScriptRunner(None)).usage_receipts("zzzzz"),
                         {"status": "ready", "event_id": "zzzzz", "items": [], "next_cursor": None, "error": None})
        self.assertFalse((runtime / "runs").exists())
        pipeline, event_id = self.stopped_event()

        def tree():
            return sorted((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in runtime.rglob("*"))

        before = tree()
        with pipeline._lock():                                     # the propagation lock is held for a whole model call
            self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)
            self.assertEqual(self.read(pipeline, "zzzzz")["items"], [])
        self.assertEqual(tree(), before)

    def test_corrupt_or_inconsistent_evidence_is_unavailable_never_zero_cost(self):
        pipeline, event_id = self.stopped_event()
        paths = {(item["attempt_sequence"], item["stage"]): self.context.runtime / "runs" / event_id /
                 item["attempt_id"] / "calls" / (item["stage"] + ".json")
                 for item in self.read(pipeline, event_id)["items"]}
        target, other = paths[(1, "propagation")], paths[(1, "bootstrap")]      # the crashed call, and a settled one
        originals = {path: path.read_bytes() for path in paths.values()}
        original = originals[target]

        def rewrite(change, path=target):
            changed = json.loads(originals[path])
            change(changed)
            path.write_bytes(json.dumps(changed).encode())

        def not_a_number():
            damaged = originals[other].replace(b'"input_tokens": 10', b'"input_tokens": NaN')
            self.assertNotEqual(damaged, originals[other])
            return damaged

        def flip(changed):
            changed["evidence"]["usage_complete"] = not changed["evidence"]["usage_complete"]

        cases = {
            "not json": lambda: target.write_bytes(b"{"),
            "not an object": lambda: target.write_bytes(b"[]"),
            "nan": lambda: other.write_bytes(not_a_number()),
            "oversized": lambda: target.write_bytes(original + b" " * (64 * 1024)),
            "other event": lambda: rewrite(lambda c: c.update(event_id="zzzzz")),
            "other call": lambda: rewrite(lambda c: c.update(call_id="0" * 32 + ":propagation")),
            "other stage": lambda: rewrite(lambda c: c.update(stage="bootstrap")),
            "other sequence": lambda: rewrite(lambda c: c.update(attempt_sequence=9)),
            "bad state": lambda: rewrite(lambda c: c.update(state="done")),
            "finished without end": lambda: rewrite(lambda c: c.update(state="started", finished_at=c["started_at"])),
            "negative tokens": lambda: rewrite(lambda c: c["usage"].update(output_tokens=-1)),
            "boolean tokens": lambda: rewrite(lambda c: c["usage"].update(output_tokens=True)),
            "negative cost": lambda: rewrite(lambda c: c["usage"].update(cost_usd=-1.0)),
            "extra field": lambda: rewrite(lambda c: c.update(cost_estimate=0)),
            "missing field": lambda: rewrite(lambda c: c.pop("observed")),
            "edited flags": lambda: rewrite(flip),
            "edited limitations": lambda: rewrite(lambda c: c["evidence"].update(limitations=[])),
            "claimed final usage": lambda: rewrite(lambda c: c["evidence"].update(usage_complete=True), other),
            "scope without usage": lambda: rewrite(lambda c: c["evidence"].update(usage_scope="native")),
            "native scope without a verified pair": lambda: rewrite(lambda c: c["observed"].update(turn_id=None), other),
            "reference on a bootstrap call": lambda: rewrite(lambda c: c.update(bootstrap_call_id="0" * 32 + ":bootstrap"), other),
            "malformed reference": lambda: rewrite(lambda c: c.update(bootstrap_call_id="elsewhere")),
            "symlink": lambda: (target.unlink(), target.symlink_to(other)),
        }
        for label, damage in cases.items():
            with self.subTest(label):
                damage()
                result = self.read(pipeline, event_id)
                self.assertEqual((result["status"], result["items"], result["next_cursor"]), ("unavailable", [], None))
                self.assertEqual(result["error"]["code"], "corrupt_evidence")
                self.assertLess(len(result["error"]["message"]), 300)
                self.assertNotIn(str(self.context.runtime), result["error"]["message"])
                for path, data in originals.items():
                    path.unlink(missing_ok=True)
                    path.write_bytes(data)
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)

        attempt_path = next((self.context.runtime / "runs" / event_id).glob("*/attempt.json"))
        record = attempt_path.read_bytes()
        for label, change in {"other event": {"event_id": "zzzzz"}, "other attempt": {"attempt_id": "0" * 32},
                              "no sequence": {"sequence": "1"}}.items():
            with self.subTest(attempt=label):
                attempt_path.write_bytes(json.dumps({**json.loads(record), **change}).encode())
                self.assertEqual(self.read(pipeline, event_id)["error"]["code"], "corrupt_evidence")
        attempt_path.write_bytes(record)
        with patch("qorqut_context.usage.os.open", side_effect=PermissionError):
            result = self.read(pipeline, event_id)
        self.assertEqual((result["status"], result["error"]["code"]), ("unavailable", "unreadable_evidence"))
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)


    def evidence_paths(self, event_id):
        runs = self.context.runtime / "runs"
        return runs, runs / event_id, sorted((runs / event_id).iterdir())

    def assertUnavailable(self, pipeline, event_id, code):
        result = self.read(pipeline, event_id)
        self.assertEqual((result["status"], result["items"], result["next_cursor"], result["error"]["code"]),
                         ("unavailable", [], None, code))
        self.assertLess(len(result["error"]["message"]), 300)
        self.assertNotIn(str(self.context.runtime), result["error"]["message"])

    def test_a_redirected_evidence_directory_is_never_followed(self):
        pipeline, event_id = self.stopped_event()
        runs, event_directory, attempts = self.evidence_paths(event_id)
        calls = attempts[1] / "calls"
        moved = attempts[1] / "calls.real"
        calls.rename(moved)
        calls.symlink_to(attempts[0] / "calls")                    # another attempt's receipts, readable through the link
        self.assertUnavailable(pipeline, event_id, "corrupt_evidence")
        calls.unlink()
        moved.rename(calls)
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)

        real = runs / "real-event"
        event_directory.rename(real)
        event_directory.symlink_to(real)                           # the event directory itself
        self.assertUnavailable(pipeline, event_id, "corrupt_evidence")
        event_directory.unlink()
        real.rename(event_directory)

        moved_runs = self.context.runtime / "runs.real"
        runs.rename(moved_runs)
        runs.write_bytes(b"not a directory")                       # present but not a directory is not "absent"
        self.assertUnavailable(pipeline, event_id, "corrupt_evidence")
        runs.unlink()
        moved_runs.rename(runs)
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)

    def test_failures_to_inspect_list_or_open_evidence_are_unavailable_not_empty(self):
        pipeline, event_id = self.stopped_event()
        for target, label in (("os.lstat", "inspect"), ("os.listdir", "list"), ("os.open", "open")):
            with self.subTest(label), patch("qorqut_context.usage." + target, side_effect=PermissionError):
                self.assertUnavailable(pipeline, event_id, "unreadable_evidence")
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)
        with patch("qorqut_context.usage.os.lstat", side_effect=FileNotFoundError):    # only a missing path is absent
            self.assertEqual(self.read(pipeline, event_id)["items"], [])

    @skipIf(os.geteuid() == 0, "permission bits do not restrict root")
    def test_unreadable_evidence_directories_are_unavailable_not_absent(self):
        pipeline, event_id = self.stopped_event()
        runs, event_directory, attempts = self.evidence_paths(event_id)
        for label, path in (("run directory", runs), ("event directory", event_directory),
                            ("attempt directory", attempts[0]), ("calls directory", attempts[0] / "calls")):
            with self.subTest(label):
                path.chmod(0)
                try:
                    self.assertUnavailable(pipeline, event_id, "unreadable_evidence")
                finally:
                    path.chmod(0o700)
        self.assertEqual(len(self.read(pipeline, event_id)["items"]), 3)
        self.assertEqual(self.read(pipeline, "zzzzz")["items"], [])                    # a missing event stays empty


class Stub:
    config = RunnerConfig()

    def __init__(self, behavior):
        self.behavior, self.calls = behavior, 0

    def run(self, *, stage, instructions, payload, directory, tools=None, output_schema=None,
            on_identity=None, on_usage=None):
        self.calls += 1
        return self.behavior(stage, on_identity, on_usage)


class CallSettlement(TestCase):
    """The reserve -> launch -> settle contract of one attempt, without a Context."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = self.directory = Path(temporary.name)
        self.attempt = {"attempt_id": "a" * 32, "event_id": "abcde", "sequence": 1}

    def fresh(self, name):
        self.directory = self.root / name
        self.directory.mkdir()

    def calls(self, behavior):
        self.runner = Stub(behavior)
        return usage_module.AttemptCalls(self.runner, self.directory, self.attempt, RunnerConfig().identity())

    def launch(self, calls, stage="propagation"):
        return calls.run(stage=stage, instructions="x", payload={}, directory=self.directory / stage)

    def stored(self, stage="propagation"):
        return json.loads((self.directory / "calls" / (stage + ".json")).read_bytes())

    def test_the_receipt_is_durable_before_the_runner_launches_and_a_stage_cannot_be_charged_twice(self):
        seen = []

        def behavior(stage, on_identity, on_usage):
            seen.append(self.stored(stage)["state"])
            on_identity({"stage": stage, "thread_id": "thread", "turn_id": "turn", "model_provider": "fixture",
                         "observed_model": "observed", "disabled_features": ["x"], "usage_updates_ignored": 2})
            return RunResult("completed", "", "thread", "turn", "m")

        calls = self.calls(behavior)
        self.launch(calls)
        self.assertEqual(seen, ["started"])
        receipt = self.stored()
        self.assertEqual(receipt["observed"], {"provider": "fixture", "model": "observed", "thread_id": "thread", "turn_id": "turn"})
        self.assertEqual(receipt["evidence"]["ignored_updates"], 2)             # runner-reported discards stay visible
        self.assertNotIn("disabled_features", json.dumps(receipt))             # only verified identities are copied
        with self.assertRaisesRegex(Conflict, "already reserved"):
            self.launch(calls)
        self.assertEqual(self.runner.calls, 1)
        with self.assertRaises(ContextError):
            self.launch(calls, "other")

    def test_a_receipt_that_cannot_be_reserved_never_launches_the_runner(self):
        calls = self.calls(lambda *args: self.fail("launched"))
        with patch("qorqut_context.usage.atomic_write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.launch(calls)
        self.assertEqual(self.runner.calls, 0)

    def test_failure_and_interruption_are_settled_truthfully(self):
        failing = self.calls(lambda *args: RunResult("failed", "", "t", "u", "m", Usage(total_tokens=3)))
        self.assertEqual(self.launch(failing).status, "failed")
        receipt = self.stored()
        self.assertEqual((receipt["state"], receipt["error"]), ("failed", {"type": "TurnStatus", "message": "Model turn ended with status failed"}))
        self.assertEqual(receipt["usage"]["total_tokens"], 3)                    # the result's own snapshot is observed
        self.assertEqual(receipt["evidence"]["usage_updates"], 1)

        self.fresh("interrupted")

        def interrupted(stage, on_identity, on_usage):
            on_usage(Usage(total_tokens=9))
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.launch(self.calls(interrupted))
        receipt = self.stored()
        self.assertEqual((receipt["state"], receipt["finished_at"], receipt["usage"]["total_tokens"]), ("started", None, 9))

        self.fresh("unwritable-failure")

        def broken(stage, on_identity, on_usage):
            raise RuntimeError("boom")
        calls = self.calls(broken)
        with patch.object(usage_module.CallMeter, "fail", side_effect=OSError("disk full")), self.assertRaisesRegex(RuntimeError, "boom"):
            self.launch(calls)
        self.assertEqual(self.stored()["state"], "started")                      # unknown, and the original error survives

    def test_a_native_usage_claim_needs_the_verified_pair_and_identity_never_changes(self):
        stored = []

        def behavior(stage, on_identity, on_usage):
            on_usage(Usage(total_tokens=5, thread_id="foreign", turn_id="foreign-turn"))    # nothing to verify it against
            on_identity({"stage": stage, "thread_id": "actual", "turn_id": "actual-turn"})
            stored.append(self.stored())                       # the early claim was neither kept nor relabelled
            on_usage(Usage(total_tokens=7, thread_id="actual", turn_id="actual-turn"))
            stored.append(self.stored())
            on_identity({"stage": stage, "thread_id": "other", "turn_id": "other-turn",   # an attempted replacement
                         "observed_model": "rerouted", "model_provider": "fixture"})
            on_usage(Usage(total_tokens=9, thread_id="other", turn_id="other-turn"))      # claims the replacement
            on_usage(Usage(total_tokens=8, thread_id="actual"))                           # half a claim
            on_usage(Usage(total_tokens=6, turn_id="actual-turn"))
            on_usage(Usage(total_tokens=10, thread_id="actual", turn_id="actual-turn"))
            return RunResult("completed", "", "actual", "actual-turn", "m")

        self.launch(self.calls(behavior))
        early, valid = stored
        self.assertEqual(early["observed"], {"provider": None, "model": None, "thread_id": "actual", "turn_id": "actual-turn"})
        self.assertEqual(set(early["usage"].values()), {None})
        self.assertEqual((early["evidence"]["usage_scope"], early["evidence"]["usage_updates"],
                          early["evidence"]["ignored_updates"], early["evidence"]["usage_observed"]), (None, 0, 1, False))
        self.assertEqual((valid["usage"]["total_tokens"], valid["evidence"]["usage_scope"]), (7, "native"))
        final = self.stored()
        self.assertEqual((final["observed"]["thread_id"], final["observed"]["turn_id"]), ("actual", "actual-turn"))
        self.assertEqual((final["observed"]["model"], final["observed"]["provider"]), ("rerouted", "fixture"))   # may change
        self.assertEqual(final["usage"]["total_tokens"], 10)
        self.assertEqual((final["evidence"]["usage_scope"], final["evidence"]["usage_updates"],
                          final["evidence"]["ignored_updates"]), ("native", 2, 4))

    def test_call_scoped_evidence_is_not_relabelled_native_by_a_later_identity(self):
        stored = []

        def behavior(stage, on_identity, on_usage):
            on_usage(Usage(total_tokens=4))                    # a trusted injected runner: neither native ID
            on_identity({"stage": stage, "thread_id": "late", "turn_id": "late-turn"})
            stored.append(self.stored())
            on_usage(Usage(total_tokens=6))
            return RunResult("completed", "", "late", "late-turn", "m", Usage(total_tokens=6))

        self.launch(self.calls(behavior))
        (midway,) = stored
        self.assertEqual((midway["usage"]["total_tokens"], midway["evidence"]["usage_scope"]), (4, "call"))
        self.assertEqual((midway["observed"]["thread_id"], midway["observed"]["turn_id"]), ("late", "late-turn"))
        final = self.stored()
        self.assertEqual((final["usage"]["total_tokens"], final["evidence"]["usage_scope"], final["evidence"]["usage_updates"]),
                         (6, "call", 2))
        self.assertEqual((final["state"], final["evidence"]["counters_complete"], final["evidence"]["usage_complete"]),
                         ("completed", False, False))

    def test_a_completed_call_with_all_five_counters_is_never_verified_final(self):
        full = Usage(input_tokens=1, cached_input_tokens=0, output_tokens=2, reasoning_output_tokens=0, total_tokens=3)

        def behavior(stage, on_identity, on_usage):
            on_usage(full)
            return RunResult("completed", "", "t", "u", "m", full)

        self.launch(self.calls(behavior))
        evidence = self.stored()["evidence"]
        self.assertEqual((self.stored()["state"], evidence["counters_complete"], evidence["usage_complete"]), ("completed", True, False))
        self.assertEqual(evidence["limitations"], ["final_totals_unproven", "cost_unreported"])


class StrictReads(TestCase):
    """Strict reads of one genuine synthetic receipt tree (no Context, no model)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs, self.event = Path(temporary.name) / "runs", "abcde"
        self.directory = self.runs / self.event / ("a" * 32)
        self.directory.mkdir(parents=True)
        attempt = {"attempt_id": "a" * 32, "event_id": self.event, "sequence": 1}
        (self.directory / "attempt.json").write_bytes(encode(attempt))

        def behavior(stage, on_identity, on_usage):
            on_usage(Usage(total_tokens=3))                     # partial: one counter, no cost
            return RunResult("completed", "", "t", "u", "m")

        usage_module.AttemptCalls(Stub(behavior), self.directory, attempt, RunnerConfig().identity()).run(
            stage="propagation", instructions="x", payload={}, directory=self.directory / "propagation")
        self.receipt = self.directory / "calls" / "propagation.json"

    def read(self):
        return usage_module.page(self.runs, self.event, limit=10, cursor=None)

    def test_flags_and_counters_must_be_their_exact_json_types(self):
        original = json.loads(self.receipt.read_bytes())
        evidence = original["evidence"]
        flags = ("usage_observed", "counters_complete", "usage_complete", "cost_observed")
        self.assertEqual([evidence[name] for name in flags], [True, False, False, False])
        self.assertEqual(self.read()["status"], "ready")
        # 1 == True and 0 == False, so these all equal the derived values yet are not booleans.
        for change in ({"usage_observed": 1}, {"counters_complete": 0}, {"usage_complete": 0}, {"cost_observed": 0},
                       {"usage_observed": 1, "counters_complete": 0, "usage_complete": 0, "cost_observed": 0}):
            with self.subTest(change):
                self.receipt.write_bytes(json.dumps({**original, "evidence": {**evidence, **change}}).encode())
                result = self.read()
                self.assertEqual((result["status"], result["items"], result["error"]["code"]), ("unavailable", [], "corrupt_evidence"))
        with self.subTest("attempt_sequence"):
            self.receipt.write_bytes(json.dumps({**original, "attempt_sequence": True}).encode())
            self.assertEqual(self.read()["error"]["code"], "corrupt_evidence")      # True == 1
        self.receipt.write_bytes(json.dumps(original).encode())
        self.assertEqual(self.read()["items"][0]["evidence"]["usage_observed"], True)

    def test_a_symlinked_attempt_entry_is_corrupt_before_any_child_is_inspected(self):
        event = self.runs / self.event
        (event / "stray.txt").write_bytes(b"an unrelated file is not an attempt")
        self.assertEqual(len(self.read()["items"]), 1)                            # unrelated files are still ignored
        link = event / ("b" * 32)
        real = self.runs / "childless"
        real.mkdir()
        for label, target in (("dangling", self.runs / "missing"), ("childless", real), ("the genuine attempt", self.directory)):
            with self.subTest(label):
                link.symlink_to(target)
                with patch.object(usage_module, "probe", wraps=usage_module.probe) as spy:
                    result = self.read()
                self.assertEqual((result["status"], result["items"], result["error"]["code"]), ("unavailable", [], "corrupt_evidence"))
                inspected = [Path(call.args[0]) for call in spy.call_args_list]
                self.assertIn(link, inspected)                                      # its own entry was inspected...
                self.assertFalse([path for path in inspected if path.is_relative_to(link) and path != link])   # ...nothing beneath it
                link.unlink()
        self.assertEqual(len(self.read()["items"]), 1)
