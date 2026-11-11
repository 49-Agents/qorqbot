"""Exercise tier negotiation through a synthetic app-server, without a provider."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from qorqut_context.__main__ import parser
from qorqut_context.runner import AppServerRunner, RunnerConfig, RunnerError
from qorqut_context.schema import ContextError


SERVER = r'''
import json
from pathlib import Path
import sys

settings = {}
for i, argument in enumerate(sys.argv):
    if argument == "-c":
        key, value = sys.argv[i + 1].split("=", 1)
        settings[key] = json.loads(value)
features = {key[len("features."):]: value for key, value in settings.items() if key.startswith("features.")}

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    with Path(REQUESTS).open("a") as log:
        log.write(json.dumps(request) + "\n")
    if "id" not in request:
        continue
    method, params = request["method"], request["params"]
    if method == "initialize":
        result = {}
    elif method == "config/read":
        result = {"config": {**settings, "features": features, "mcp_servers": {}}}
    elif method == "thread/start":
        result = {"thread": {"id": "thread", "cliVersion": "fixture", "ephemeral": True},
                  "model": params["model"], "modelProvider": "openai",
                  "reasoningEffort": params["config"]["model_reasoning_effort"],
                  "serviceTier": params["serviceTier"] if OBSERVED == "echo" else OBSERVED,
                  "approvalPolicy": "never", "sandbox": {"type": "readOnly"}}
    elif method == "experimentalFeature/list":
        result = {"data": [{"name": key, "enabled": value, "stage": "stable"} for key, value in features.items()],
                  "nextCursor": None}
    elif method == "mcpServerStatus/list":
        result = {"data": [], "nextCursor": None}
    elif method == "turn/start":
        result = {"turn": {"id": "turn"}}
    else:
        raise AssertionError(method)
    send({"id": request["id"], "result": result})
    if method == "turn/start":
        send({"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
              "item": {"id": "answer", "type": "agentMessage", "phase": "final_answer", "text": "OK"}}})
        send({"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}})
'''


class RunnerTierTests(unittest.TestCase):
    def run_server(self, tier, observed="echo", refused=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = root / "requests.jsonl"
            server = root / "codex-fixture"
            server.write_text(f"#!{sys.executable}\nREQUESTS = {str(requests)!r}\nOBSERVED = {observed!r}\n" + SERVER)
            server.chmod(0o700)
            runner = AppServerRunner(RunnerConfig(executable=str(server), model="gpt-6.1-sol", effort="high",
                                                 service_tier=tier, bootstrap_timeout=10))
            arguments = dict(stage="bootstrap", instructions="Return OK", payload={}, directory=root / "run")
            if refused:
                with self.assertRaisesRegex(RunnerError, "did not honor"):
                    runner.run(**arguments)
            else:
                self.assertEqual(runner.run(**arguments).output, "OK")
            calls = [json.loads(line) for line in requests.read_text().splitlines()]
            identity = json.loads((root / "run" / "identity.json").read_text())
            return calls, identity

    def test_explicit_tiers_reach_thread_turn_and_identity(self):
        for tier in ("default", "priority", "ultrafast"):
            with self.subTest(tier=tier):
                calls, identity = self.run_server(tier)
                thread = next(call["params"] for call in calls if call["method"] == "thread/start")
                turn = next(call["params"] for call in calls if call["method"] == "turn/start")
                self.assertEqual(thread["serviceTier"], tier)
                self.assertEqual(thread["config"]["service_tier"], tier)
                self.assertEqual(turn["serviceTier"], tier)
                self.assertEqual(turn["serviceTierForTurn"], tier)
                self.assertEqual(turn["model"], "gpt-6.1-sol")
                self.assertEqual(turn["effort"], "high")
                self.assertEqual(identity["requested_service_tier"], tier)
                self.assertEqual(identity["observed_service_tier"], tier)

    def test_default_accepts_codex_null_tier(self):
        _, identity = self.run_server("default", observed=None)
        self.assertEqual(identity["requested_service_tier"], "default")
        self.assertIsNone(identity["observed_service_tier"])

    def test_ultrafast_does_not_silently_fall_back(self):
        for observed in (None, "default", "priority"):
            with self.subTest(observed=observed):
                calls, _ = self.run_server("ultrafast", observed=observed, refused=True)
                self.assertNotIn("turn/start", [call["method"] for call in calls])

    def test_rejects_unknown_tiers(self):
        for tier in (None, "", "fast", "automatic"):
            with self.subTest(tier=tier), self.assertRaises(ContextError):
                RunnerConfig(service_tier=tier)

    def test_submit_and_retry_accept_explicit_tier(self):
        for command in (("submit", "add", "foundation/source.txt", "--submitted-by", "owner"),
                        ("submit-action", "add", "global/action", "--submitted-by", "owner"), ("retry", "event")):
            with self.subTest(command=command[0]):
                args = parser().parse_args(["--repo", "/unused", *command, "--service-tier", "ultrafast"])
                self.assertEqual(args.service_tier, "ultrafast")


if __name__ == "__main__":
    unittest.main()
