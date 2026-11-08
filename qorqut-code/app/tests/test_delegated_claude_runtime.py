"""Delegated workers run on Claude Code when the parent Ordubashy session does."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import delegated_runtime as runtime_module
from qorqut.bootstrap.delegated_runtime import (DelegatedRuntime, MissingRuntimeThread, claude_usage,
                                                  claude_worker_command, interpret_event, worker_command)

SESSION = "7d1e7c52-9a0b-4c55-8f0e-2f4d3b1c9a10"
ASSIGNMENT = {"id": str(uuid.uuid4()), "allowed_operations_json": ["tasks_get"], "selected_model": "gpt-6-luna",
              "reasoning_effort": "medium", "state": "running"}
REGISTRATION = {"service_url": "http://127.0.0.1:1049", "workspace": "/tmp/worker"}

# A stand-in for `claude -p --output-format stream-json`: it records its argv and emits the events the real CLI emits.
FAKE_CLAUDE = r'''#!{python}
import json, sys
args = sys.argv[1:]
open({log!r}, "a").write(json.dumps(args) + "\n")
sys.stdin.read()
mode = open({mode!r}).read().strip()
def emit(event): print(json.dumps(event), flush=True)
if mode == "missing":
    emit({{"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": "x"}})
    print("No conversation found with session ID", file=sys.stderr); sys.exit(1)
session = args[args.index("--resume") + 1] if "--resume" in args else "{session}"
emit({{"type": "system", "subtype": "init", "session_id": session, "tools": [], "mcp_servers": []}})
emit({{"type": "assistant", "message": {{"content": []}}}})
if mode == "error":
    emit({{"type": "result", "subtype": "error_max_turns", "is_error": True, "session_id": session}}); sys.exit(1)
emit({{"type": "result", "subtype": "success", "is_error": False, "session_id": session,
       "usage": {{"input_tokens": 2, "cache_read_input_tokens": 600, "cache_creation_input_tokens": 3000,
                  "output_tokens": 7, "output_tokens_details": {{"thinking_tokens": 3}}}}}})
'''


class CommandTests(unittest.TestCase):
    def test_new_session_is_isolated_headless_and_scoped(self):
        command = claude_worker_command(ASSIGNMENT, REGISTRATION, Path("/tmp/worker/.qorqut/registration.json"))
        self.assertEqual(command[:2], ["claude", "-p"])
        self.assertIn("stream-json", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "project,local")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--strict-mcp-config", command)
        servers = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]
        self.assertEqual(list(servers), ["qorqut"])
        self.assertEqual(servers["qorqut"]["type"], "http")
        self.assertEqual(servers["qorqut"]["url"], "http://127.0.0.1:1049/mcp")
        self.assertIn("qorqut.bootstrap.mcp_headers", servers["qorqut"]["headersHelper"])
        allowed = command[command.index("--allowedTools") + 1:]
        allowed = allowed[:next((i for i, value in enumerate(allowed) if value.startswith("--")), len(allowed))]
        self.assertIn("mcp__qorqut__tasks_get", allowed)
        self.assertTrue(all(name.startswith("mcp__qorqut__") for name in allowed))
        self.assertNotIn("--resume", command)

    def test_codex_model_is_not_passed_to_claude_but_effort_is(self):
        command = claude_worker_command(ASSIGNMENT, REGISTRATION, Path("/r.json"))
        self.assertNotIn("--model", command)
        self.assertEqual(command[command.index("--effort") + 1], "medium")
        claude_model = claude_worker_command({**ASSIGNMENT, "selected_model": "claude-sonnet-5-5"}, REGISTRATION, Path("/r.json"))
        self.assertEqual(claude_model[claude_model.index("--model") + 1], "claude-sonnet-5-5")

    def test_resume_requires_a_canonical_session_id(self):
        command = claude_worker_command(ASSIGNMENT, REGISTRATION, Path("/r.json"), thread_id=SESSION)
        self.assertEqual(command[command.index("--resume") + 1], SESSION)
        with self.assertRaises(DomainError):
            claude_worker_command(ASSIGNMENT, REGISTRATION, Path("/r.json"), thread_id="not-a-session")

    def test_runtime_selects_its_command_builder(self):
        for runtime, expected in (("codex", worker_command), ("claude-code", claude_worker_command)):
            delegated = DelegatedRuntime({"registration_id": "r"}, str(uuid.uuid4()), alive=lambda: True,
                                         runtime=runtime, client=object(), executable="/bin/true")
            self.assertEqual(delegated.command(ASSIGNMENT, REGISTRATION, Path("/r.json"), None),
                             expected(ASSIGNMENT, REGISTRATION, Path("/r.json"), executable="/bin/true"))
        with self.assertRaises(DomainError):
            DelegatedRuntime({}, str(uuid.uuid4()), alive=lambda: True, runtime="gemini", client=object())

    def test_claude_worker_keeps_the_real_home_and_codex_keeps_its_private_one(self):
        claude = DelegatedRuntime({}, str(uuid.uuid4()), alive=lambda: True, runtime="claude-code", client=object(),
                                  executable="/bin/true").environment("/tmp/worker")
        self.assertEqual(claude["HOME"], os.environ.get("HOME", str(Path.home())))
        self.assertNotIn("CODEX_HOME", claude)
        codex = DelegatedRuntime({}, str(uuid.uuid4()), alive=lambda: True, runtime="codex", client=object(),
                                 executable="/bin/true").environment("/tmp/worker")
        self.assertEqual(codex["HOME"], "/tmp/worker")
        self.assertIn("CODEX_HOME", codex)


class EventTests(unittest.TestCase):
    def test_claude_events(self):
        self.assertEqual(interpret_event("claude-code", {"type": "system", "subtype": "init", "session_id": SESSION}),
                         ("thread", SESSION))
        self.assertEqual(interpret_event("claude-code", {"type": "system", "subtype": "hook_response"}), (None, None))
        self.assertEqual(interpret_event("claude-code", {"type": "result", "subtype": "success", "is_error": True})[0], "failed")
        self.assertEqual(interpret_event("claude-code", {"type": "result", "subtype": "error_max_turns"})[0], "failed")
        kind, usage = interpret_event("claude-code", {"type": "result", "subtype": "success", "is_error": False, "usage": {}})
        self.assertEqual(kind, "completed")
        self.assertEqual(usage, {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0})

    def test_codex_events_are_unchanged(self):
        self.assertEqual(interpret_event("codex", {"type": "thread.started", "thread_id": SESSION}), ("thread", SESSION))
        self.assertEqual(interpret_event("codex", {"type": "turn.completed", "usage": {"input_tokens": 5, "extra": 1}}),
                         ("completed", {"input_tokens": 5}))
        self.assertEqual(interpret_event("codex", {"type": "turn.failed"})[0], "failed")
        self.assertEqual(interpret_event("codex", {"type": "system", "subtype": "init"}), (None, None))

    def test_usage_counts_all_input_and_never_more_cached_than_input(self):
        usage = claude_usage({"input_tokens": 2, "cache_read_input_tokens": 649, "cache_creation_input_tokens": 2975,
                              "output_tokens": 7, "output_tokens_details": {"thinking_tokens": 4}})
        self.assertEqual(usage, {"input_tokens": 3626, "cached_input_tokens": 649, "output_tokens": 7,
                                 "reasoning_output_tokens": 4})
        self.assertEqual(claude_usage({"input_tokens": -1, "output_tokens": "x"})["input_tokens"], 0)


class FakeClient:
    def __init__(self):
        self.posts = []

    def post(self, route, body):
        self.posts.append((route, body))
        return {"assignment": ASSIGNMENT, "events": []}


@unittest.skipUnless(sys.platform != "win32", "POSIX process groups")
class TurnTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.log, self.mode = self.base / "argv.jsonl", self.base / "mode"
        self.executable = self.base / "claude"
        self.executable.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(self.log), mode=str(self.mode),
                                                      session=SESSION))
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IEXEC)
        self.client = FakeClient()
        self.runtime = DelegatedRuntime({"registration_id": "r"}, str(uuid.uuid4()), alive=lambda: True,
                                        runtime="claude-code", client=self.client, executable=str(self.executable))

    def turn(self, mode, prior=None):
        self.mode.write_text(mode)
        command = self.runtime.command(ASSIGNMENT, REGISTRATION, self.base / "registration.json", prior)
        return self.runtime._run_turn(command, "prompt", ASSIGNMENT, 1, time.monotonic() + 30, str(self.base), prior, 1)

    def test_new_turn_reports_the_session_and_usage(self):
        thread, usage, _status, _events = self.turn("ok")
        self.assertEqual(thread, SESSION)
        self.assertEqual(usage["input_tokens"], 3602)
        self.assertEqual(usage["cached_input_tokens"], 600)
        routes = [body for route, body in self.client.posts if route == "delegation/poll" or route.endswith("/poll")]
        self.assertEqual(routes[0]["runtime_thread_id"], SESSION)
        self.assertEqual(routes[-1]["turn_usage"]["output_tokens"], 7)

    def test_resume_passes_the_saved_session(self):
        thread, *_ = self.turn("ok", prior=SESSION)
        self.assertEqual(thread, SESSION)
        argv = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)

    def test_missing_session_on_resume_is_a_missing_thread(self):
        with self.assertRaises(MissingRuntimeThread):
            self.turn("missing", prior=SESSION)

    def test_failed_turn_is_a_worker_failure(self):
        with self.assertRaises(DomainError) as caught:
            self.turn("error")
        self.assertEqual(caught.exception.code, "worker_turn_failed")
        self.assertIn("Claude Code", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
