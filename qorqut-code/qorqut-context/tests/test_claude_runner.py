"""The Claude runner against a synthetic ``claude`` CLI, without a provider.

The fixture speaks the CLI's stream-json, records its argument vector, and drives the real MCP relay
(``qorqut_context.claude_tools``) from the private MCP configuration it is given, so tool calls take the same path as in
production: CLI -> relay -> runner socket -> ``tools.dispatch``.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from qorqut_context.claude_runner import ClaudeRunner, runner_for
from qorqut_context.runner import AppServerRunner, RunnerConfig, RunnerError
from qorqut_context.usage import AttemptCalls

FIXTURE = r'''
import json, os, subprocess, sys
argv = sys.argv[1:]
open(ARGS, "w").write(json.dumps(argv))
open(ARGS + ".memory", "w").write(os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "unset"))
value = lambda flag: argv[argv.index(flag) + 1] if flag in argv else None
prompt = sys.stdin.read()
tools, results = [], []
config = value("--mcp-config")
if config:
    server = json.load(open(config))["mcpServers"]["context"]
    relay = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def rpc(identity, method, params):
        relay.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}) + "\n")
        relay.stdin.flush()
        return json.loads(relay.stdout.readline())
    rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
    tools = ["mcp__context__" + t["name"] for t in rpc(2, "tools/list", {})["result"]["tools"]]
    for n, call in enumerate(CALLS):
        results.append(rpc(10 + n, "tools/call", call)["result"])
    relay.stdin.close(); relay.wait()
offered = tools + (["StructuredOutput"] if value("--json-schema") else []) + EXTRA_TOOLS
print(json.dumps({"type": "system", "subtype": "init", "session_id": "session-1", "model": "claude-fixture",
                  "tools": offered, "mcp_servers": [{"name": "context", "status": "connected"}] if config else [],
                  "claude_code_version": "fixture"}), flush=True)
print(json.dumps({"type": "user", "message": {"content": results}}), flush=True)
final = {"type": "result", "subtype": "success", "is_error": IS_ERROR, "result": "plain answer",
         "session_id": "session-1", "total_cost_usd": 0.01,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 0, "output_tokens": 5}}
if value("--json-schema"):
    final["structured_output"] = {"echo": json.loads(prompt)}
print(json.dumps(final), flush=True)
'''


class Tools:
    definitions = [{"type": "function", "name": "lookup", "description": "Look up a word.",
                    "inputSchema": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}]

    def __init__(self):
        self.calls = []

    def dispatch(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True, "result": {"word": arguments["word"].upper()}}


class ClaudeRunnerTests(unittest.TestCase):
    def run_fixture(self, *, stage="propagation", tools=None, schema=None, calls=(), extra_tools=(), is_error=False,
                    tool_calls=200):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executable = root / "bin" / "claude"
        executable.parent.mkdir()
        executable.write_text(f"#!{sys.executable}\nARGS = {str(root / 'args.json')!r}\nCALLS = {list(calls)!r}\n"
                              f"EXTRA_TOOLS = {list(extra_tools)!r}\nIS_ERROR = {is_error!r}\n" + FIXTURE)
        executable.chmod(0o700)
        runner = ClaudeRunner(RunnerConfig(executable=str(executable), model="sonnet", effort="max",
                                           propagation_timeout=20, bootstrap_timeout=20, tool_calls=tool_calls))
        usage = []
        result = lambda: runner.run(stage=stage, instructions="Instructions", payload={"q": 1}, directory=root / "run",
                                    tools=tools, output_schema=schema, on_usage=usage.append)
        return result, root, usage

    def test_tool_calls_reach_dispatch_through_the_relay(self):
        tools = Tools()
        run, root, usage = self.run_fixture(tools=tools, calls=[{"name": "lookup", "arguments": {"word": "kale"}}],
                                            schema={"type": "object"})
        result = run()
        self.assertEqual(tools.calls, [("lookup", {"word": "kale"})])
        self.assertEqual(json.loads(result.output), {"echo": {"q": 1}})
        self.assertEqual((result.status, result.thread_id, result.model), ("completed", "session-1", "claude-fixture"))
        self.assertEqual((usage[-1].input_tokens, usage[-1].cached_input_tokens, usage[-1].output_tokens,
                          usage[-1].total_tokens, usage[-1].cost_usd), (100, 90, 5, 105, 0.01))
        log = (root / "run" / "protocol.jsonl").read_text()
        self.assertIn('"tool_call"', log)

    def test_usage_reaches_the_call_receipt_as_native_evidence(self):
        _, root, _ = self.run_fixture(tools=Tools(), schema={"type": "object"})
        runner = ClaudeRunner(RunnerConfig(executable=str(root / "bin" / "claude"), model="sonnet", effort="max",
                                           propagation_timeout=20, bootstrap_timeout=20))
        (root / "attempt").mkdir()
        attempt = {"attempt_id": "attempt-1", "event_id": "event-1", "sequence": 1}
        AttemptCalls(runner, root / "attempt", attempt, {}).run(
            stage="propagation", instructions="Instructions", payload={"q": 1}, directory=root / "run",
            tools=Tools(), output_schema={"type": "object"})
        receipt = json.loads((root / "attempt" / "calls" / "propagation.json").read_text())
        usage = receipt["usage"]
        self.assertEqual((usage["input_tokens"], usage["cached_input_tokens"], usage["output_tokens"],
                          usage["total_tokens"], usage["cost_usd"]), (100, 90, 5, 105, 0.01))
        self.assertEqual((receipt["evidence"]["usage_scope"], receipt["evidence"]["usage_observed"],
                          receipt["evidence"]["ignored_updates"]), ("native", True, 0))
        self.assertEqual((receipt["observed"]["thread_id"], receipt["observed"]["turn_id"]), ("session-1", "session-1"))

    def test_cli_gets_no_builtin_tools_settings_or_sessions(self):
        run, root, _ = self.run_fixture(tools=Tools(), schema={"type": "object"})
        run()
        argv = json.loads((root / "args.json").read_text())
        flag = lambda name: argv[argv.index(name) + 1]
        self.assertEqual(flag("--tools"), "")
        self.assertEqual(flag("--setting-sources"), "")
        self.assertEqual(flag("--permission-mode"), "dontAsk")
        self.assertEqual(flag("--model"), "sonnet")
        self.assertEqual(flag("--effort"), "max")
        self.assertEqual(flag("--allowedTools"), "mcp__context__lookup")
        for name in ("--strict-mcp-config", "--no-session-persistence", "-p"):
            self.assertIn(name, argv)
        self.assertEqual((root / "args.json.memory").read_text(), "1", "Claude Code's own memory is off in a filing run")

    def test_bootstrap_without_tools_returns_plain_text(self):
        run, root, _ = self.run_fixture(stage="bootstrap")
        self.assertEqual(run().output, "plain answer")
        self.assertNotIn("--mcp-config", json.loads((root / "args.json").read_text()))

    def test_an_unexpected_tool_is_refused(self):
        run, _, _ = self.run_fixture(tools=Tools(), extra_tools=["Bash"])
        with self.assertRaisesRegex(RunnerError, "other than the event"):
            run()

    def test_a_model_error_is_reported_with_usage_kept(self):
        run, _, usage = self.run_fixture(tools=Tools(), is_error=True)
        with self.assertRaisesRegex(RunnerError, "model error"):
            run()
        self.assertEqual(usage[-1].output_tokens, 5)

    def test_tool_call_limit_fails_the_turn(self):
        calls = [{"name": "lookup", "arguments": {"word": w}} for w in ("a", "b")]
        run, _, _ = self.run_fixture(tools=Tools(), calls=calls, tool_calls=1)
        with self.assertRaisesRegex(RunnerError, "tool-call limit"):
            run()

    def test_runner_is_chosen_by_executable_name(self):
        self.assertIsInstance(runner_for(RunnerConfig(executable="/opt/bin/claude", model="sonnet")), ClaudeRunner)
        self.assertIsInstance(runner_for(RunnerConfig(executable="codex", model="gpt-6-luna")), AppServerRunner)
        with self.assertRaises(RunnerError):
            ClaudeRunner(RunnerConfig(executable="claude", model="sonnet", service_tier="priority"))


if __name__ == "__main__":
    unittest.main()
