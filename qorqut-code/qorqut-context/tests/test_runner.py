from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
from unittest import TestCase
from unittest.mock import patch

from qorqut_context.runner import AppServerRunner, RunnerConfig, RunnerError
from qorqut_context.usage import AttemptCalls, Usage


class Probe:
    definitions = [{"type": "function", "name": "probe", "description": "A synthetic mutation counter",
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}},
                                    "required": ["value"], "additionalProperties": False}}]

    def __init__(self):
        self.calls = 0

    def dispatch(self, name, arguments):
        self.calls += 1
        return {"ok": True, "result": {"count": self.calls}}


def counters(thread="thread", turn="turn", **changes):
    """A synthetic ``thread/tokenUsage/updated`` payload; ``last`` is deliberately different from ``total``."""
    total = {"inputTokens": 100, "cachedInputTokens": 40, "outputTokens": 20, "reasoningOutputTokens": 5,
             "totalTokens": 120, **changes}
    return {"threadId": thread, "turnId": turn,
            "tokenUsage": {"total": total, "last": {**total, "inputTokens": 1, "totalTokens": 2}, "modelContextWindow": None}}


class AppServer(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        server = self.root / "codex"
        server.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name("fake_app_server.py").read_text())
        server.chmod(0o700)
        self.config = RunnerConfig(executable=str(server), propagation_timeout=5, stderr_bytes=1000)

    def test_dynamic_call_replay_identity_and_bounded_stderr(self):
        probe, processes = Probe(), []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        directory = self.root / "run"
        with patch("qorqut_context.runner.subprocess.Popen", side_effect=launch):
            result = AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check",
                payload={}, directory=directory, tools=probe)
        self.assertEqual(result.status, "completed")
        self.assertEqual(probe.calls, 1)
        self.assertEqual(result.output, "Complete")
        self.assertIn("truncated", (directory / "stderr.txt").read_text())
        self.assertLess((directory / "stderr.txt").stat().st_size, 1100)
        self.assertNotIn("must-not-be-logged", (directory / "protocol.jsonl").read_text())
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(processes[0].stdout.closed)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_echoed_but_unrecognized_restriction_fails_before_turn(self):
        with patch.dict(os.environ, {"QORQUT_TEST_SERVER_MODE": "ignored-restriction"}), self.assertRaisesRegex(RunnerError, "recognize/enforce"):
            AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                             directory=self.root / "ignored", tools=Probe())

    def test_shell_gate_is_required_despite_enabled_exec_implementation(self):
        directory = self.root / "exec-implementation"
        AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                         directory=directory, tools=Probe())
        identity = json.loads((directory / "identity.json").read_bytes())
        self.assertIs(identity["observed_unified_exec"], True)
        self.assertIs(identity["verified_feature_state"]["shell_tool"], False)
        self.assertNotIn("unified_exec", identity["disabled_features"])
        self.assertNotIn("unified_exec", identity["verified_feature_state"])
        probe = Probe()
        with patch.dict(os.environ, {"QORQUT_TEST_SERVER_MODE": "exposed-shell"}), self.assertRaisesRegex(RunnerError, "shell_tool"):
            AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                             directory=self.root / "exposed-shell", tools=probe)
        self.assertEqual(probe.calls, 0)
        identity = json.loads((self.root / "exposed-shell" / "identity.json").read_bytes())
        self.assertNotIn("turn_id", identity)

    def test_transport_failures_and_unexpected_interactions_reap_child(self):
        # Distinct risks: deadline, framing/EOF, scope binding, and denied approvals.
        for mode in ("timeout", "malformed", "eof", "wrong-identity", "approval"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"QORQUT_TEST_SERVER_MODE": mode}):
                processes, probe = [], Probe()
                original = subprocess.Popen
                def launch(*args, **kwargs):
                    process = original(*args, **kwargs)
                    processes.append(process)
                    return process
                with patch("qorqut_context.runner.subprocess.Popen", side_effect=launch), self.assertRaises(RunnerError):
                    AppServerRunner(replace(self.config, propagation_timeout=0.3)).run(
                        stage="propagation", instructions="Synthetic check", payload={},
                        directory=self.root / mode, tools=probe)
                self.assertEqual(probe.calls, 0)
                self.assertIsNotNone(processes[0].returncode)
                self.assertTrue(processes[0].stdout.closed)

    def test_global_preference_provenance_and_standard_tier_boundary(self):
        home = self.root / "configuration"
        home.mkdir()
        instruction = home / "AGENTS.md"
        instruction.write_text("Generic owner preferences.")
        with patch.dict(os.environ, {"CODEX_HOME": str(home), "QORQUT_TEST_INSTRUCTION_PATH": str(instruction)}):
            directory = self.root / "global"
            AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                             directory=directory, tools=Probe())
            identity = json.loads((directory / "identity.json").read_bytes())
            self.assertEqual(identity["requested_service_tier"], "default")
            self.assertIsNone(identity["observed_service_tier"])
            self.assertEqual(identity["instruction_sources"], [{"path": str(instruction),
                "sha256": hashlib.sha256(instruction.read_bytes()).hexdigest()}])
            with patch.dict(os.environ, {"QORQUT_TEST_RETURNED_TIER": '"priority"'}), self.assertRaisesRegex(RunnerError, "model or permissions"):
                AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                                 directory=self.root / "priority", tools=Probe())
            with patch.dict(os.environ, {"QORQUT_TEST_INSTRUCTION_PATH": str(self.root / "unrelated.md")}), self.assertRaisesRegex(RunnerError, "outside"):
                AppServerRunner(self.config).run(stage="propagation", instructions="Synthetic check", payload={},
                                                 directory=self.root / "unrelated", tools=Probe())

    def test_host_termination_reaps_app_server(self):
        program = """
import sys
from qorqut_context.runner import AppServerRunner, RunnerConfig, RunnerError
from qorqut_context.usage import AttemptCalls, Usage
try:
    AppServerRunner(RunnerConfig(executable=sys.argv[1])).run(
        stage='propagation', instructions='Synthetic check', payload={}, directory=sys.argv[2],
        on_identity=lambda value: print('ready', flush=True))
except RunnerError as error:
    print(str(error), flush=True)
"""
        pid_file = self.root / "child.pid"
        environment = {**os.environ, "QORQUT_TEST_SERVER_MODE": "timeout", "QORQUT_TEST_CHILD_PID_FILE": str(pid_file)}
        host = subprocess.Popen([sys.executable, "-c", program, self.config.executable, str(self.root / "cancelled")],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(host.stdout.readline().strip(), "ready")
            child_pid = int(pid_file.read_text())
            host.send_signal(signal.SIGTERM)
            output, errors = host.communicate(timeout=10)
            self.assertEqual(host.returncode, 0, errors)
            self.assertIn("cancelled", output)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if host.poll() is None:
                host.kill()
                host.wait()
            host.stdout.close()
            host.stderr.close()

    def usage_run(self, environment, tools=True, **overrides):
        """One synthetic propagation call; returns (callback snapshots, directory, result-or-error)."""
        snapshots, directory = [], self.root / ("usage-%d" % len(list(self.root.glob("usage-*"))))
        identities = []
        with patch.dict(os.environ, {key: json.dumps(value) if not isinstance(value, str) else value
                                     for key, value in environment.items()}):
            runner = AppServerRunner(replace(self.config, **overrides))
            try:
                outcome = runner.run(stage="propagation", instructions="Synthetic check", payload={},
                                     directory=directory, tools=Probe() if tools else None,
                                     on_identity=identities.append, on_usage=snapshots.append)
            except RunnerError as error:
                outcome = error
        return snapshots, directory, outcome, identities

    def test_cumulative_usage_replaces_and_unrelated_or_malformed_updates_are_ignored(self):
        early = counters(inputTokens=10, totalTokens=12)          # arrives before the turn/start response
        snapshots, directory, result, identities = self.usage_run({
            "QORQUT_TEST_USAGE_EARLY": [early],
            "QORQUT_TEST_USAGE": [counters(inputTokens=60, outputTokens=10, totalTokens=70),
                                  counters(thread="foreign", inputTokens=999999), counters(turn="other-turn", inputTokens=888),
                                  counters(outputTokens=-1), counters(totalTokens=True), counters(inputTokens="7")],
            "QORQUT_TEST_USAGE_LATE": [counters()]})
        self.assertEqual([item.input_tokens for item in snapshots], [10, 60, 100])
        self.assertEqual(snapshots[-1], Usage(input_tokens=100, cached_input_tokens=40, output_tokens=20,
                                              reasoning_output_tokens=5, total_tokens=120, cost_usd=None,
                                              thread_id="thread", turn_id="turn", source="thread/tokenUsage/updated"))
        self.assertEqual(result.usage, snapshots[-1])              # the last snapshot, never a sum
        self.assertIsNone(result.usage.cost_usd)                   # the turn events carry no dollar field
        self.assertEqual(identities[-1]["usage_updates_ignored"], 5)
        diagnostics = [json.loads(line) for line in (directory / "protocol.jsonl").read_text().splitlines()]
        self.assertEqual([entry["reason"] for entry in diagnostics if entry["type"] == "usage_ignored"],
                         ["foreign_identity", "foreign_identity", "malformed", "malformed", "malformed"])
        self.assertEqual(sum(entry["type"] == "token_usage" for entry in diagnostics), 3)

    def test_usage_survives_a_failed_or_stalled_turn_and_absent_evidence_stays_unknown(self):
        snapshots, _, error, _ = self.usage_run({"QORQUT_TEST_USAGE": [counters()], "QORQUT_TEST_TURN_STATUS": "failed"})
        self.assertRegex(str(error), "status failed")
        self.assertEqual([item.total_tokens for item in snapshots], [120])
        with patch.dict(os.environ, {"QORQUT_TEST_SERVER_MODE": "usage-stall"}):
            snapshots, _, error, _ = self.usage_run({"QORQUT_TEST_USAGE": [counters(totalTokens=77)]}, propagation_timeout=0.5)
        self.assertRegex(str(error), "timed out")
        self.assertEqual([item.total_tokens for item in snapshots], [77])
        snapshots, _, result, _ = self.usage_run({})
        self.assertEqual(snapshots, [])
        self.assertIsNone(result.usage)

    def test_a_reroute_for_this_turn_changes_only_the_observed_model(self):
        _, _, result, identities = self.usage_run({"QORQUT_TEST_REROUTE": [
            {"threadId": "foreign", "turnId": "turn", "fromModel": "a", "toModel": "foreign-model", "reason": "highRiskCyberActivity"},
            {"threadId": "thread", "turnId": "turn", "fromModel": self.config.model, "toModel": "rerouted-model",
             "reason": "highRiskCyberActivity"}]})
        self.assertEqual(next(item for item in identities if "observed_model" in item)["observed_model"],
                         self.config.model)                                         # from thread/start
        self.assertEqual(identities[-1]["observed_model"], "rerouted-model")
        self.assertEqual(result.model, self.config.model)                          # configured identity is unchanged

    def test_the_native_runner_and_receipt_agree_on_the_verified_pair(self):
        attempt = {"attempt_id": "a" * 32, "event_id": "abcde", "sequence": 1}
        directory = self.root / "attempt"
        directory.mkdir()
        environment = {"QORQUT_TEST_USAGE_EARLY": json.dumps([counters(inputTokens=10, totalTokens=12)]),
                       "QORQUT_TEST_USAGE": json.dumps([counters(thread="foreign"), counters()])}
        with patch.dict(os.environ, environment):
            AttemptCalls(AppServerRunner(self.config), directory, attempt, self.config.identity()).run(
                stage="propagation", instructions="Synthetic check", payload={}, directory=directory / "propagation", tools=Probe())
        receipt = json.loads((directory / "calls" / "propagation.json").read_bytes())
        self.assertEqual(receipt["observed"], {"provider": "fixture", "model": self.config.model, "thread_id": "thread", "turn_id": "turn"})
        self.assertEqual((receipt["state"], receipt["usage"]["input_tokens"], receipt["usage"]["total_tokens"]), ("completed", 100, 120))
        self.assertEqual(receipt["evidence"], {
            "usage_source": "thread/tokenUsage/updated", "usage_scope": "native", "usage_updates": 2, "ignored_updates": 1,
            "usage_observed": True, "counters_complete": True, "usage_complete": False, "cost_observed": False,
            "limitations": ["final_totals_unproven", "cost_unreported", "ignored_updates"]})
