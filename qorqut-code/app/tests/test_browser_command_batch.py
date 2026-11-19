"""``browser_turn`` ``commands``: several commands in one call, in order, under one use lock and one target read.

The runner is a stand-in, so each test scripts what every command does (its exit code, output, time or failure)."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE
from qorqut.bootstrap import browser, mcp_reconnect
from qorqut.browser import access as access_module, agent_browsers, operations, setup
from qorqut.browser.access import BrowserAccess
from qorqut.browser.handles import BrowserHandles, _compact
from qorqut.transport import mcp_presentation

ORDUBASHY = "0a6f3c52-9d1e-4b7a-8c35-2f4e6d8b1a90"
ATLAS = "8fdca155-2f3b-4735-800f-41624c6d26cc"
ASSIGNMENT = "5d2e7c1a-6b30-4f9e-8a41-0c7d9e3b2f18"
FOLDER = "work/browser/qorqut-test"
REAL_VALIDATE = browser.validate_arguments


def actor(principal_id, *, assignment=None):
    return SimpleNamespace(principal=SimpleNamespace(id=principal_id), registration_id="registration", credential_id="credential",
                           delegation_assignment_id=assignment, delegation_generation=1 if assignment else None,
                           delegation_binding_id=None)


def step(code=0, stdout="ok", stderr="", took=0.0, raises=None, writes=None):
    """What one command does: its result, how long it takes, an exception it raises, a file it writes."""
    return {"result": (code, stdout, stderr), "took": took, "raises": raises, "writes": writes}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    earlier = path.stat().st_mtime_ns if path.exists() else time.time_ns()
    path.write_text(text)
    os.utime(path, ns=(earlier + 1_000_000_000,) * 2)


class BatchCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE browser_handles (id TEXT, turn_id TEXT, binding_json TEXT, arguments_json TEXT, superseded INT)")
        self.addCleanup(connection.close)

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        runtime = SimpleNamespace(database=SimpleNamespace(read=read), paths=SimpleNamespace(runtime=self.root, instance_root=self.root))
        self.access = BrowserAccess(runtime)
        self.now = 0.0
        self.access.clock = lambda: self.now
        self.script, self.ran, self.validated, self.resolved, self.locked = [], [], [], [], []
        self.holding = False
        self.validated_before_first_run = None
        workers = [{"agent_id": ORDUBASHY, "name": "Ordubashy", "role": ORDUBASHY_ROLE},
                   {"agent_id": ATLAS, "name": "Atlas", "role": GLOBAL_WORKER_ROLE}]

        def target(browser_key, owner):
            self.resolved.append((browser_key, owner))
            return {"cdp_endpoint": "http://127.0.0.1:20400", "generation": "configured", "instance": browser_key,
                    "runtime_dir": "/runtime", "browser_key": browser_key}

        @contextmanager
        def use(_paths, agent_id):
            self.locked.append(agent_id)
            self.holding = True
            try:
                yield
            finally:
                self.holding = False

        def validate(command):
            self.validated.append(command)
            if command[0] == "close":
                raise browser.BrowserError("'close' is not a supported browser command.")

        def run(workspace, command, *, target, instance, capture, pinned=False):
            if not self.ran:
                self.validated_before_first_run = len(self.validated)
            self.ran.append({"command": command, "target": target[0], "instance": instance, "capture": capture,
                             "holding_the_lock": self.holding})
            scripted = self.script.pop(0) if self.script else step()
            self.now += scripted["took"]
            if scripted["writes"]:
                scripted["writes"](workspace / FOLDER)
            if scripted["raises"]:
                raise scripted["raises"]
            return scripted["result"]
        for owner, name, value in ((self.access, "_worker_target", target), (self.access, "authorize_ordubashy", lambda _caller: None),
                                   (self.access.targets, "registry", lambda: None),
                                   (setup, "targets", lambda _runtime: ("owner", workers)),
                                   (agent_browsers, "command_use", use), (browser, "run", run),
                                   (browser, "validate_arguments", validate),
                                   (browser, "runtime_executable", lambda directory: directory)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.handle = self.access.tool(actor(ATLAS), "acquire", {"action": "acquire", "protocol": "handles"})["handle"]
        self.resolved.clear()               # acquiring reads the target once itself; only the commands' reads are counted

    def batch(self, commands, **extra):
        return self.access.tool(actor(ATLAS), "command", {"action": "command", "handle": self.handle, "commands": commands, **extra})

    def refused(self, body):
        with self.assertRaises(DomainError) as caught:
            self.access.tool(actor(ATLAS), "command", {"action": "command", "handle": self.handle, **body})
        self.assertEqual(caught.exception.code, "validation_error")
        self.assertEqual(self.ran, [])
        return caught.exception.message


FILL_THEN_ENTER = [["fill", "e1", "Ada"], ["fill", "e2", "Lovelace"], ["press", "Enter"]]


class BatchRunTest(BatchCase):
    def test_the_commands_run_in_order_and_the_reply_has_one_entry_each(self):
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual([call["command"] for call in self.ran], FILL_THEN_ENTER)
        self.assertEqual(reply, {"results": [{"command": command, "exit_code": 0, "stdout": "ok"} for command in FILL_THEN_ENTER],
                                 "exit_code": 0})

    def test_they_share_one_use_lock_and_one_target_read(self):
        self.batch(FILL_THEN_ENTER)
        self.assertEqual(self.locked, [ATLAS])
        self.assertEqual(self.resolved, [("configured:" + ATLAS, ATLAS)])
        self.assertTrue(all(call["holding_the_lock"] and call["capture"] for call in self.ran))
        self.assertEqual({(call["target"], call["instance"]) for call in self.ran},
                         {("http://127.0.0.1:20400", "configured:" + ATLAS)})

    def test_every_command_is_checked_before_the_first_one_runs(self):
        self.assertIn("commands[2]", self.refused({"commands": [["fill", "e1", "x"], ["press", "Enter"], ["close"]]}))
        self.assertEqual(self.validated, [["fill", "e1", "x"], ["press", "Enter"], ["close"]])
        self.assertEqual(self.resolved, [])
        self.validated.clear()
        self.batch([["fill", "e1", "x"], ["press", "Enter"]])
        self.assertEqual(self.validated_before_first_run, 2)

    def test_the_real_validator_still_refuses_a_session_override_in_a_later_command(self):
        with mock.patch.object(browser, "validate_arguments", REAL_VALIDATE):
            self.assertIn("commands[1]", self.refused({"commands": [["observe", "overview"], ["click", "e1", "--cdp=ws://other"]]}))

    def test_a_list_of_one_runs_like_a_single_command(self):
        reply = self.batch([["click", "e1"]])
        self.assertEqual(reply["results"], [{"command": ["click", "e1"], "exit_code": 0, "stdout": "ok"}])
        self.assertEqual((self.locked, len(self.resolved)), ([ATLAS], 1))

    def test_a_single_command_keeps_its_reply(self):
        reply = self.access.tool(actor(ATLAS), "command", {"action": "command", "handle": self.handle, "command": ["click", "e1"]})
        self.assertEqual(reply, {"exit_code": 0, "stdout": "ok"})

    def test_a_null_command_beside_commands_counts_as_not_given(self):
        self.assertEqual(self.batch([["click", "e1"]], command=None)["exit_code"], 0)

    def test_a_worker_browser_selected_by_ordubashy_takes_batches_too(self):
        handle = self.access.tool(actor(ORDUBASHY), "acquire", {"action": "acquire", "protocol": "handles", "browser_id": ATLAS})["handle"]
        self.resolved.clear(), self.locked.clear()
        reply = self.access.tool(actor(ORDUBASHY), "command", {"action": "command", "handle": handle, "commands": FILL_THEN_ENTER})
        self.assertEqual(len(reply["results"]), 3)
        self.assertEqual((self.resolved, self.locked), ([("configured:" + ATLAS, ATLAS)], [ATLAS]))


class BatchRefusalTest(BatchCase):
    def test_exactly_one_of_command_and_commands(self):
        self.assertIn("not both", self.refused({"command": ["click", "e1"], "commands": [["click", "e2"]]}))
        self.assertIn("commands", self.refused({}))
        self.assertIn("commands", self.refused({"command": None, "commands": None}))

    def test_a_list_of_one_to_ten_commands_each_a_non_empty_list_of_strings(self):
        for label, commands in (("none", []), ("eleven", [["click", "e1"]] * 11), ("a string", ["click e1"]),
                                ("an empty command", [["click", "e1"], []]), ("a number", [["click", 1]]),
                                ("not a list", {"click": "e1"})):
            with self.subTest(label):
                self.assertIn("commands must be a list of 1 to 10", self.refused({"commands": commands}))
        self.assertEqual(len(self.batch([["click", "e1"]] * 10)["results"]), 10)

    def test_a_browser_that_is_not_ready_raises_before_anything_runs(self):
        @contextmanager
        def stopped(_paths, _agent_id):
            raise DomainError("browser_not_ready", "Your dedicated browser is not running. Tell Ordubashy.")
            yield
        with mock.patch.object(agent_browsers, "command_use", stopped), self.assertRaises(DomainError) as caught:
            self.batch(FILL_THEN_ENTER)
        self.assertEqual(caught.exception.code, "browser_not_ready")
        self.assertEqual(self.ran, [])

    def test_a_first_command_the_runner_cannot_start_raises_as_a_single_command_does(self):
        self.script = [step(raises=browser.BrowserError("Another browser command is running in this workspace; wait for it to finish."))]
        with self.assertRaises(DomainError) as caught:
            self.batch(FILL_THEN_ENTER)
        self.assertEqual((caught.exception.code, len(self.ran)), ("browser_failed", 1))
        self.assertIn("Another browser command", caught.exception.message)

    def test_a_delegated_assignment_keeps_one_command_per_call(self):
        class Turns:
            def begin_action(self, *_arguments, **_keywords):
                raise AssertionError("an intent was written for a refused batch")
        self.access.turns = Turns()
        with self.assertRaises(DomainError) as caught:
            self.access._turn_command(actor(ATLAS, assignment=ASSIGNMENT), {"commands": [["click", "e1"]]}, turn_id="turn",
                                      lease_id="lease", requester=ASSIGNMENT, generation=1, browser_key="configured:" + ATLAS,
                                      owner_id=ASSIGNMENT)
        self.assertEqual(caught.exception.code, "validation_error")
        self.assertIn("one command per call", caught.exception.message)
        self.assertEqual(self.ran, [])


class BatchStopTest(BatchCase):
    def test_the_run_stops_at_the_first_non_zero_exit(self):
        self.script = [step(), step(code=1, stdout="no such element", stderr="e1 not found"), step()]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(reply["results"][1], {"command": ["fill", "e2", "Lovelace"], "exit_code": 1, "stdout": "no such element",
                                               "stderr": "e1 not found"})
        self.assertEqual(reply["exit_code"], 1)
        self.assertEqual({key: reply["stopped"][key] for key in ("index", "reason", "not_run")},
                         {"index": 1, "reason": "exit_code", "not_run": 1})
        self.assertIn("exited with code 1", reply["stopped"]["message"])

    def test_a_failing_last_command_is_reported_with_nothing_left(self):
        self.script = [step(), step(), step(code=2)]
        stopped = self.batch(FILL_THEN_ENTER)["stopped"]
        self.assertEqual((stopped["index"], stopped["reason"], stopped["not_run"]), (2, "exit_code", 0))
        self.assertNotIn("later commands", stopped["message"])

    def test_a_timeout_is_reported_not_raised_and_the_earlier_results_are_kept(self):
        self.script = [step(), step(raises=browser.BrowserTimeout("slow")), step()]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual(len(self.ran), 2)
        self.assertEqual(reply["results"], [{"command": ["fill", "e1", "Ada"], "exit_code": 0, "stdout": "ok"}])
        self.assertEqual(reply["exit_code"], 0)          # that of the last command that finished
        self.assertEqual({key: reply["stopped"][key] for key in ("index", "reason", "not_run")},
                         {"index": 1, "reason": "browser_timeout", "not_run": 1})
        self.assertIn("inspect the page", reply["stopped"]["message"])

    def test_a_timeout_of_the_first_command_leaves_an_empty_list_and_no_exit_code(self):
        self.script = [step(raises=browser.BrowserTimeout("slow"))]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual(reply["results"], [])
        self.assertNotIn("exit_code", reply)
        self.assertEqual((reply["stopped"]["index"], reply["stopped"]["not_run"]), (0, 2))

    def test_a_later_command_the_runner_cannot_start_is_reported_as_failed(self):
        self.script = [step(), step(raises=browser.BrowserError("Another browser command is running in this workspace."))]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual(len(reply["results"]), 1)
        self.assertEqual(reply["stopped"], {"index": 1, "reason": "browser_failed", "not_run": 2,
                                            "message": "Another browser command is running in this workspace."})

    def test_a_clean_run_has_no_stopped_entry(self):
        self.assertNotIn("stopped", self.batch(FILL_THEN_ENTER))


class BatchTimeBudgetTest(BatchCase):
    def test_the_budget_is_the_request_window_of_the_agents_bridge(self):
        self.assertEqual(access_module._REQUEST_SECONDS, min(mcp_reconnect.RETRY_WINDOW, mcp_reconnect.HTTP_REQUEST_TIMEOUT))
        # A first command, which is never refused, must still fit with the reply.
        self.assertLess(access_module._COMMAND_SECONDS + access_module._REPLY_SECONDS, access_module._REQUEST_SECONDS)

    def test_the_next_command_runs_while_its_45_seconds_still_end_inside_the_budget(self):
        self.script = [step(took=10), step(took=0), step(took=0)]
        self.assertNotIn("stopped", self.batch(FILL_THEN_ENTER))
        self.assertEqual(len(self.ran), 3)

    def test_the_run_stops_before_a_command_that_could_pass_the_budget(self):
        self.script = [step(took=10.5), step()]
        reply = self.batch(FILL_THEN_ENTER, action_id="batch-1")
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(len(reply["results"]), 1)
        self.assertEqual({key: reply["stopped"][key] for key in ("index", "reason", "not_run")},
                         {"index": 1, "reason": "time_budget", "not_run": 2})
        self.assertIn("new call", reply["stopped"]["message"])
        self.assertEqual(reply["action"], {"action_id": "batch-1", "state": "failed"})

    def test_a_slow_first_command_is_not_refused_but_ends_the_batch(self):
        self.script = [step(took=44)]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual((len(reply["results"]), reply["stopped"]["reason"]), (1, "time_budget"))


class BatchOutputTest(BatchCase):
    def test_stdout_keeps_its_first_24576_characters_per_command_and_says_when_it_cut(self):
        self.script = [step(stdout="a" * 24_576 + "b" * 5000)]
        entry = self.batch([["eval", "f"]])["results"][0]
        self.assertEqual(entry["stdout"], "a" * 24_576)
        self.assertIs(entry["truncated"], True)

    def test_a_read_at_the_largest_budget_arrives_whole_with_its_address_first(self):
        page = "Address: https://example.com/\nTitle: Example\n" + "t" * 23_900
        self.script = [step(), step(stdout=page)]
        entry = self.batch([["click", "e1"], ["read", "--budget=24000"]])["results"][1]
        self.assertEqual(entry["stdout"], page)
        self.assertNotIn("truncated", entry)

    def test_an_unexpected_failure_after_the_first_command_keeps_the_earlier_results(self):
        self.script = [step(stdout="done"), step(raises=OSError("pipe"))]
        reply = self.batch([["fill", "e1", "a"], ["click", "e2"], ["click", "e3"]])
        self.assertEqual([entry["stdout"] for entry in reply["results"]], ["done"])
        self.assertEqual((reply["stopped"]["index"], reply["stopped"]["reason"], reply["stopped"]["not_run"]),
                         (1, "browser_failed", 1))
        self.assertIn("may not have reached the page", reply["stopped"]["message"])

    def test_captcha_is_refused_in_a_batch_before_anything_runs(self):
        with self.assertRaises(DomainError) as refused:
            self.batch([["click", "e1"], ["captcha"]])
        self.assertIn("commands[1]", str(refused.exception))
        self.assertEqual(self.ran, [])

    def test_an_unexpected_failure_of_the_first_command_raises(self):
        self.script = [step(raises=OSError("pipe"))]
        with self.assertRaises(OSError):
            self.batch(FILL_THEN_ENTER)

    def test_stderr_is_only_there_when_not_empty_and_is_cut_per_command(self):
        self.script = [step(), step(stderr="e" * 20_000)]
        first, second = self.batch([["click", "e1"], ["click", "e2"]])["results"]
        self.assertNotIn("stderr", first)
        self.assertEqual(second["stderr"], "e" * 16_384)

    def test_the_batch_keeps_to_65536_characters_of_stdout_and_marks_the_entries_it_cut(self):
        self.script = [step(stdout="x" * 15_000 + "END") for _ in range(6)]
        results = self.batch([["eval", "f"]] * 6)["results"]
        self.assertEqual([len(entry["stdout"]) for entry in results], [15_003] * 4 + [65_536 - 4 * 15_003, 0])
        self.assertEqual([entry.get("truncated") for entry in results], [None] * 4 + [True, True])
        self.assertTrue(results[4]["stdout"].startswith("x") and "END" not in results[4]["stdout"])  # a cut keeps the start


class BatchArtifactsTest(BatchCase):
    def test_artifacts_cover_the_whole_run_and_leave_out_old_and_bookkeeping_files(self):
        write(self.root / "browser-turns" / ATLAS / self.handle / FOLDER / "older.txt", "from long ago")
        self.script = [step(writes=lambda directory: write(directory / "a.txt", "1")), step(writes=lambda directory: (
            write(directory / "b.txt", "22"), write(directory / "observation-0123456789abcdef.meta.json", "{}"),
            write(directory / "command.lock", ""))), step()]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual([item["artifact_path"] for item in reply["artifacts"]], [FOLDER + "/a.txt", FOLDER + "/b.txt"])
        self.assertEqual(reply["artifacts"][1]["artifact_handle"], f"browser-artifact:{ATLAS}:{self.handle}:{FOLDER}/b.txt")

    def test_a_batch_that_writes_nothing_has_no_artifacts_key(self):
        write(self.root / "browser-turns" / ATLAS / self.handle / FOLDER / "older.txt", "from long ago")
        self.assertNotIn("artifacts", self.batch(FILL_THEN_ENTER))

    def test_files_written_before_a_stop_are_still_listed(self):
        self.script = [step(writes=lambda directory: write(directory / "a.txt", "1")), step(code=1)]
        reply = self.batch(FILL_THEN_ENTER)
        self.assertEqual([item["artifact_path"] for item in reply["artifacts"]], [FOLDER + "/a.txt"])


class BatchActionTest(BatchCase):
    def test_the_action_id_names_the_whole_batch(self):
        self.assertEqual(self.batch(FILL_THEN_ENTER, action_id="batch-1")["action"], {"action_id": "batch-1", "state": "succeeded"})
        self.assertEqual(self.batch(FILL_THEN_ENTER, idempotency_key="key-1")["action"], {"action_id": "key-1", "state": "succeeded"})
        self.assertEqual(self.batch(FILL_THEN_ENTER, action_id="a", idempotency_key="b")["action"]["action_id"], "a")
        self.assertNotIn("action", self.batch(FILL_THEN_ENTER))

    def test_the_state_says_failed_for_a_stop_and_uncertain_for_a_timeout(self):
        self.script = [step(code=1)]
        self.assertEqual(self.batch(FILL_THEN_ENTER, action_id="a")["action"]["state"], "failed")
        self.script = [step(raises=browser.BrowserTimeout("slow"))]
        self.assertEqual(self.batch(FILL_THEN_ENTER, action_id="b")["action"]["state"], "uncertain")


class BatchReplyTravelTest(BatchCase):
    """The reply must reach the agent whole: through the handle layer and the MCP envelope."""

    def stopped_reply(self):
        self.script = [step(), step(code=1, stdout="no such element", stderr="e2 not found")]
        return self.batch(FILL_THEN_ENTER, action_id="batch-1")

    def test_the_handle_layer_hands_results_and_stopped_through(self):
        self.script = [step(), step(code=1, stdout="no such element", stderr="e2 not found")]
        called = BrowserHandles(self.access).call(actor(ATLAS), "command", {"action": "command", "handle": self.handle,
                                                                           "commands": FILL_THEN_ENTER})
        self.assertEqual(set(called), {"results", "exit_code", "stopped"})
        self.assertEqual((len(called["results"]), called["stopped"]["reason"]), (2, "exit_code"))

    def test_the_compact_reply_keeps_results_and_stopped_and_still_drops_an_empty_artifacts(self):
        reply = {**self.stopped_reply(), "artifacts": [], "stderr": ""}
        compact = _compact(reply, "b0123456789ab", operation="command")
        self.assertEqual(compact["results"], reply["results"])
        self.assertEqual(compact["stopped"], reply["stopped"])
        self.assertEqual(compact["exit_code"], 1)
        self.assertNotIn("artifacts", compact)
        self.assertNotIn("stderr", compact)
        self.assertEqual(compact["results"][1]["stderr"], "e2 not found")     # a nested stderr is not an empty top-level one

    def test_the_mcp_envelope_does_not_reshape_it(self):
        reply = self.stopped_reply()
        envelope = {"status": "ok", "data": reply, "request_id": "r"}
        self.assertEqual(mcp_presentation.finalize(envelope)["data"], reply)
        self.assertEqual(mcp_presentation.present("browser_turn", envelope), envelope)
        self.assertEqual(json.loads(mcp_presentation.serialize(mcp_presentation.finalize(envelope)))["data"], reply)


class BatchSchemaTest(unittest.TestCase):
    def setUp(self):
        registered = []
        operations.register_browser_operation(SimpleNamespace(registry=SimpleNamespace(register=registered.append)))
        self.descriptor = registered[0]

    def accepts(self, **arguments):
        return self.descriptor.validate({"action": "command", "handle": "b0123456789ab", **arguments})

    def test_commands_takes_one_to_ten_commands_each_a_list_of_strings(self):
        self.accepts(commands=FILL_THEN_ENTER)
        self.accepts(commands=[["click", "e1"]] * 10)
        self.accepts(commands=None, command=["click", "e1"])
        for label, commands in (("none", []), ("eleven", [["click", "e1"]] * 11), ("a string item", ["click e1"]),
                                ("an empty command", [[]]), ("a number", [["click", 1]]), ("a long string", [["fill", "e1", "x" * 2001]]),
                                ("a long command", [["click"] + ["x"] * 32])):
            with self.subTest(label), self.assertRaises(DomainError) as caught:
                self.accepts(commands=commands)
            self.assertEqual(caught.exception.code, "validation_error")

    def test_commands_is_described_and_command_is_unchanged(self):
        properties = self.descriptor.input_schema["properties"]
        self.assertIn("1 to 10", properties["commands"]["description"])
        self.assertEqual(properties["command"]["maxItems"], 32)

    def test_the_other_fields_still_reject_unknown_names(self):
        with self.assertRaises(DomainError):
            self.accepts(batch=[["click", "e1"]])


if __name__ == "__main__":
    unittest.main()
