"""Small protocol peer for focused subprocess/lifecycle checks; never a model."""
import json
import os
import sys
import time


def send(value):
    print(json.dumps(value), flush=True)


def receive():
    return json.loads(sys.stdin.readline())


def notifications(method, variable):
    """Synthetic notifications from a JSON list of params in the environment."""
    for params in json.loads(os.environ.get(variable, "[]")):
        send({"method": method, "params": params})


def main():
    if os.environ.get("QORQUT_TEST_CHILD_PID_FILE"):
        with open(os.environ["QORQUT_TEST_CHILD_PID_FILE"], "w") as handle:
            handle.write(str(os.getpid()))
    mode = os.environ.get("QORQUT_TEST_SERVER_MODE", "complete")
    config = {}
    for index, arg in enumerate(sys.argv):
        if arg == "-c":
            key, value = sys.argv[index + 1].split("=", 1)
            target = config
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = json.loads(value)
    while True:
        request = receive()
        method = request.get("method")
        if method == "initialize":
            # Exceeds pipe capacity and the diagnostic cap; the reader must keep draining.
            sys.stderr.write("x" * 150000)
            sys.stderr.flush()
            send({"id": request["id"], "result": {"userAgent": "focused-fixture"}})
        elif method == "initialized":
            continue
        elif method == "config/read":
            config["mcp_servers"] = {"fixture-server": {"command": "unused", "env": {"SECRET": "must-not-be-logged"}}}
            send({"id": request["id"], "result": {"config": config}})
        elif method == "thread/start":
            supplied = request["params"]
            assert supplied["config"]["mcp_servers.fixture-server.enabled"] is False
            assert supplied["config"]["mcp_servers.fixture-server.required"] is False
            assert supplied["allowProviderModelFallback"] is False
            assert supplied["environments"] == []
            send({"id": request["id"], "result": {"thread": {"id": "thread", "cliVersion": "0.160.0", "ephemeral": True},
                "model": supplied["model"], "modelProvider": "fixture", "reasoningEffort": config["model_reasoning_effort"],
                "serviceTier": json.loads(os.environ.get("QORQUT_TEST_RETURNED_TIER", "null")),
                "approvalPolicy": "never", "sandbox": {"type": "readOnly", "networkAccess": False},
                "instructionSources": [os.environ["QORQUT_TEST_INSTRUCTION_PATH"]] if "QORQUT_TEST_INSTRUCTION_PATH" in os.environ else []}})
        elif method == "experimentalFeature/list":
            items = [{"name": name, "enabled": enabled, "stage": "stable"} for name, enabled in config["features"].items()]
            items.append({"name": "unified_exec", "enabled": True, "stage": "stable"})
            if mode == "ignored-restriction":
                items = [item for item in items if item["name"] != "shell_tool"]
            if mode == "exposed-shell":
                for item in items:
                    if item["name"] == "shell_tool":
                        item["enabled"] = True
            send({"id": request["id"], "result": {"data": items, "nextCursor": None}})
        elif method == "mcpServerStatus/list":
            send({"id": request["id"], "result": {"data": [], "nextCursor": None}})
        elif method == "turn/start":
            assert request["params"]["serviceTier"] == "default"
            assert request["params"]["serviceTierForTurn"] == "default"
            if mode == "timeout":
                time.sleep(30)
                return
            if mode == "malformed":
                print("not json", flush=True)
                return
            if mode == "eof":
                return
            params = {"threadId": "thread", "turnId": "turn", "item": {"type": "userMessage", "id": "input", "content": []}}
            send({"method": "item/started", "params": params})
            notifications("thread/tokenUsage/updated", "QORQUT_TEST_USAGE_EARLY")   # before the turn/start response
            send({"id": request["id"], "result": {"turn": {"id": "turn", "status": "inProgress", "items": []}}})
            notifications("model/rerouted", "QORQUT_TEST_REROUTE")
            notifications("thread/tokenUsage/updated", "QORQUT_TEST_USAGE")
            if mode == "usage-stall":
                time.sleep(30)
                return
            if mode == "approval":
                send({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {"threadId": "thread", "turnId": "turn"}})
                response = receive()
                assert "error" in response
                return
            arguments = {"value": "one mutation"}
            params = {"threadId": "foreign" if mode == "wrong-identity" else "thread", "turnId": "turn",
                      "callId": "stable-call", "tool": "probe", "arguments": arguments}
            send({"id": "tool-1", "method": "item/tool/call", "params": params})
            first = receive()
            send({"id": "tool-2", "method": "item/tool/call", "params": params})
            second = receive()
            assert first["result"] == second["result"]
            send({"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn",
                "item": {"type": "agentMessage", "id": "answer", "text": "Complete", "phase": "final_answer"}}})
            notifications("thread/tokenUsage/updated", "QORQUT_TEST_USAGE_LATE")
            send({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
                "id": "turn", "status": os.environ.get("QORQUT_TEST_TURN_STATUS", "completed"), "items": []}}})
            time.sleep(30)  # Host must terminate even a successful child.
            return


if __name__ == "__main__":
    main()
