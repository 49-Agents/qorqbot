"""Native child model/MCP/hook contract against loopback fixtures only."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

import pytest
import tomli_w

from bazarlyk.bootstrap.codex_config import add_activity_hooks, configuration
from qorqut_operations.contracts import new_id


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_CODEX_SUBAGENTS") != "1",
                    reason="explicit isolated native subagent contract only")
@pytest.mark.parametrize(("expected_effort", "explicit_override"), [("low", False), ("medium", True)])
def test_native_luna_child_inherits_mcp_without_claiming_inbox(tmp_path, expected_effort, explicit_override):
    calls, routes, errors = [], [], []
    token = "synthetic-test-credential-with-no-authority"
    task_id = new_id()
    agent_id, registration_id = new_id(), new_id()
    current_work = [{"business_id": None, "summary": "Preserve the parent review", "blocked": False}]
    activity_reads, work_updates, parent_resume_inputs = [], [], []
    native_parent_tools = []
    parent_tool_calls, lifecycle_order, parent_activity_states = [], [], []
    parent_finished = threading.Event()

    def tool_named(items, name, namespace=None):
        for tool in items:
            if tool.get("type") == "namespace":
                found = tool_named(tool.get("tools", []), name, tool["name"])
                if found:
                    return found
            elif tool.get("name") == name:
                return namespace, tool

    def request_tools(request):
        tools = list(request.get("tools", ()))
        for item in request.get("input", ()):
            if item.get("type") == "additional_tools":
                tools.extend(item.get("tools", ()))
        return tools

    def function(namespace, name, arguments):
        return {"id": "fc_" + new_id(), "type": "function_call", "call_id": "call_" + new_id(),
                **({"namespace": namespace} if namespace else {}), "name": name,
                "arguments": json.dumps(arguments), "status": "completed"}

    def custom_tool_call(namespace, name, source):
        return {"id": "ctc_" + new_id(), "type": "custom_tool_call", "call_id": "call_" + new_id(),
                **({"namespace": namespace} if namespace else {}), "name": name,
                "input": source, "status": "completed"}

    def spawn(value, *, name, effort=None):
        found = tool_named(request_tools(value), "spawn_agent")
        assert found, json.dumps(value, indent=2)
        namespace, tool = found
        schema = tool.get("parameters", tool.get("input_schema", {}))
        fields = schema.get("properties", {})
        args = {"message": "Read only synthetic task " + task_id + " using tasks_get, then return its revision."}
        if "task_name" in fields:
            args["task_name"] = name
        if "fork_context" in fields:
            args["fork_context"] = False
        if "fork_turns" in fields:
            args["fork_turns"] = "none"
        if effort is not None:
            assert "model" in fields and "reasoning_effort" in fields, tool
            args.update(model="gpt-6-luna", reasoning_effort=effort)
        return function(namespace, tool["name"], args)

    def message(text):
        return {"id": "msg_" + new_id(), "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith(("/api/", "/mcp")):
                routes.append((self.path, value))
                assert self.headers.get("Authorization") == "Bearer " + token
                if self.path == "/mcp":
                    if "id" not in value:
                        self.send_response(202)
                        self.end_headers()
                        return
                    if value["method"] == "initialize":
                        result = {"protocolVersion": value["params"]["protocolVersion"], "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "synthetic-qorqut", "version": "1"}}
                    elif value["method"] == "tools/list":
                        result = {"tools": [
                            {"name": "tasks_get", "description": "Read the synthetic task.",
                             "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                                             "required": ["task_id"], "additionalProperties": False}},
                            {"name": "agents_activity_list", "description": "Read synthetic parent activity and work.",
                             "inputSchema": {"type": "object", "properties": {"agent_id": {"type": "string"}},
                                             "required": ["agent_id"], "additionalProperties": False}},
                            {"name": "agent_work_update", "description": "Replace synthetic session work.",
                             "inputSchema": {"type": "object", "properties": {
                                 "session_id": {"type": "string"}, "items": {"type": "array"},
                                 "expected_work_revision": {"type": "integer"}},
                                 "required": ["session_id", "items", "expected_work_revision"],
                                 "additionalProperties": False}},
                        ]}
                    else:
                        name = value["params"]["name"]
                        arguments = value["params"].get("arguments", {})
                        if name == "tasks_get":
                            lifecycle_order.append("helper_tasks_get")
                            data = {"id": task_id, "revision": 7}
                        elif name == "agents_activity_list":
                            parents = [route for path, route in routes
                                       if path == "/api/agent/bootstrap"]
                            parent_session = parents[0]["session_id"] if parents else None
                            state_path = workspace / ".bazarlyk/activity" / (parent_session + ".json") if parent_session else None
                            try:
                                session_activity = json.loads(state_path.read_text()) if state_path else {}
                            except (OSError, ValueError):
                                session_activity = {}
                            session = {"session_id": parent_session, "state": session_activity.get("state", "unknown"),
                                       "items": list(current_work), "work_revision": 4}
                            data = {"agents": [{"agent_id": agent_id, "state": session["state"],
                                                "sessions": [session]}], "next_cursor": None}
                            activity_reads.append(data)
                        elif name == "agent_work_update":
                            work_updates.append(arguments)
                            current_work[:] = arguments.get("items", [])
                            data = {"work_revision": 5}
                        else:
                            raise AssertionError("Unexpected synthetic MCP tool: " + name)
                        result = {"content": [{"type": "text", "text": json.dumps(data)}]}
                    reply = {"jsonrpc": "2.0", "id": value["id"], "result": result}
                elif self.path == "/api/agent/bootstrap":
                    reply = {"ready": True, "effective_config_id": "synthetic", "inbox_available": True,
                             "inbox_consumer_id": value["session_id"], "mandatory_context": "SYNTHETIC_PARENT_BOOTSTRAP"}
                else:
                    reply = {}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())
                return

            # The client initializes project MCP servers concurrently with its
            # first model request.  Observe that handshake before delegation.
            if not calls:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if any(path == "/mcp" and route.get("method") == "tools/list"
                           for path, route in routes):
                        break
                    time.sleep(.05)
            calls.append(value)
            child = value.get("model") == "gpt-6-luna"
            count = sum((call.get("model") == "gpt-6-luna") == child for call in calls)
            item = message("SYNTHETIC_CHILD_DONE" if child else "SYNTHETIC_PARENT_DONE")
            try:
                if count == 1 and not child:
                    native_parent_tools[:] = request_tools(value)
                    item = spawn(value, name="read_task",
                                 effort=expected_effort if explicit_override else None)
                    parent_tool_calls.append(item["name"])
                elif count == 1 and child:
                    exec_tool = tool_named(request_tools(value), "exec")
                    assert exec_tool, request_tools(value)
                    namespace, tool = exec_tool
                    item = custom_tool_call(namespace, tool["name"],
                                            "const result = await tools.mcp__bazarlyk__tasks_get({task_id: "
                                            + json.dumps(task_id) + "}); text(JSON.stringify(result));")
                elif child:
                    item = message("SYNTHETIC_CHILD_DONE")
                    lifecycle_order.append("helper_completed")
                elif not child and count >= 2:
                    helper_completed = "helper_completed" in lifecycle_order
                    if not helper_completed:
                        wait_tool = tool_named(native_parent_tools, "wait_agent")
                        assert wait_tool, "Codex did not expose wait_agent after spawning the child"
                        namespace, tool = wait_tool
                        schema = tool.get("parameters", tool.get("input_schema", {}))
                        fields = schema.get("properties", {})
                        args = {"timeout_ms": 1000} if "timeout_ms" in fields else {}
                        missing = [key for key in schema.get("required", ()) if key not in args]
                        assert not missing, json.dumps({"wait_agent": tool, "missing": missing}, indent=2)
                        item = function(namespace, tool["name"], args)
                        parent_tool_calls.append(item["name"])
                    elif not parent_resume_inputs:
                        parent_resume_inputs.append(value)
                        lifecycle_order.append("parent_resumed")
                        exec_tool = tool_named(native_parent_tools, "exec")
                        assert exec_tool, native_parent_tools
                        namespace, tool = exec_tool
                        item = custom_tool_call(namespace, tool["name"],
                                                "const result = await tools.mcp__bazarlyk__agents_activity_list({agent_id: "
                                                + json.dumps(agent_id) + "}); text(JSON.stringify(result));")
                    else:
                        parent_finished.set()
            except Exception as exc:
                errors.append(str(exc))
            response = {"id": "resp_" + new_id(), "object": "response", "created_at": 1,
                        "model": value["model"], "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
            if item["type"] == "function_call":
                events.extend([
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**item, "arguments": "", "status": "in_progress"}},
                    {"type": "response.function_call_arguments.delta", "output_index": 0,
                     "item_id": item["id"], "delta": item["arguments"]},
                ])
            elif item["type"] == "custom_tool_call":
                events.extend([
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {**item, "input": "", "status": "in_progress"}},
                    {"type": "response.custom_tool_call_input.delta", "output_index": 0,
                     "item_id": item["id"], "delta": item["input"]},
                ])
            else:
                events.append({"type": "response.output_item.added", "output_index": 0, "item": item})
            events.extend([
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ])
            for event in events:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".bazarlyk").mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (home / "config.toml").write_text(tomli_w.dumps({
        "model": "gpt-6-astra", "model_reasoning_effort": "xhigh", "model_provider": "probe",
        "check_for_update_on_startup": False,
        # Custom fixture providers cannot refresh the authenticated model catalog.
        # Supply a discovered catalog when testing a newly released model.
        **({"model_catalog_json": os.environ["QORQUT_TEST_CODEX_MODEL_CATALOG"]}
           if os.environ.get("QORQUT_TEST_CODEX_MODEL_CATALOG") else {}),
        "model_providers": {"probe": {"name": "Probe", "base_url": endpoint + "/v1",
                                      "wire_api": "responses", "requires_openai_auth": False}},
        "projects": {str(workspace): {"trust_level": "trusted"}},
        "features": {"apps": False, "plugins": False, "code_mode": False},
    }))
    registration_path = workspace / ".bazarlyk/registration.json"
    registration_path.write_text(json.dumps({"schema_version": 1, "registration_id": registration_id, "agent_id": agent_id,
        "workspace": str(workspace), "service_url": endpoint, "credential_file": ".bazarlyk/credential",
        "capture_transcripts": False}))
    credential = workspace / ".bazarlyk/credential"
    credential.write_text(token)
    credential.chmod(0o600)

    def command(module, *args):
        return shlex.join([sys.executable, "-I", "-m", module, "--registration", str(registration_path), *args])

    config, hooks = configuration(workspace=workspace, endpoint=endpoint,
        header_command=command("bazarlyk.bootstrap.mcp_headers"),
        command=command("bazarlyk.bootstrap.claude_session", "--runtime", "codex"),
        snapshot_command=command("bazarlyk.bootstrap.codex_session"),
        protected=[registration_path.parent], read_denied=[credential], delegate_operations=True)
    from bazarlyk.bootstrap import activity_hook, technical_reporting
    activity_script = workspace / ".bazarlyk/activity-hook.py"
    activity_script.write_bytes(Path(activity_hook.__file__).read_bytes())
    (workspace / ".bazarlyk/technical_reporting.py").write_bytes(Path(technical_reporting.__file__).read_bytes())
    activity_command = shlex.join([sys.executable, "-I", str(activity_script), "--registration",
                                   str(registration_path), "--runtime", "codex"])
    add_activity_hooks(hooks, activity_command, "codex")
    (workspace / ".codex/config.toml").write_bytes(config)
    (workspace / ".codex/hooks.json").write_text(json.dumps({"hooks": hooks}))
    (workspace / "AGENTS.md").write_text("Delegate the synthetic task to a native subagent.\n")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    # Trust bypass applies only to this disposable fixture, never the real installation.
    process = subprocess.Popen(["codex", "--strict-config", "--dangerously-bypass-hook-trust",
        "--no-alt-screen", "Delegate the isolated synthetic task"], cwd=workspace,
        env={"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = b""
    mcp_approved = set()
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], .2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output += chunk
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
                for name in ("tasks_get", "agents_activity_list"):
                    prompt = f'Allow the bazarlyk MCP server to run tool "{name}"?'.encode()
                    if name not in mcp_approved and prompt in output:
                        # Approve only reads from this isolated synthetic MCP fixture.
                        os.write(master, b"1\r")
                        mcp_approved.add(name)
            bootstraps = [value for path, value in routes if path == "/api/agent/bootstrap"]
            parent_activity = workspace / ".bazarlyk/activity" / (bootstraps[0]["session_id"] + ".json") if bootstraps else None
            try:
                parent_state = json.loads(parent_activity.read_text()).get("state") if parent_activity else None
            except (OSError, ValueError):
                parent_state = None
            if parent_state:
                parent_activity_states.append(parent_state)
            if errors or (parent_finished.is_set() and parent_state in {"idle", "offline"}):
                break
        assert not errors, errors
        children = [call for call in calls if call.get("model") == "gpt-6-luna"]
        parents = [call for call in calls if call.get("model") != "gpt-6-luna"]
        assert len(children) >= 2 and len(parents) >= 4, json.dumps({"models": [c.get("model") for c in calls], "tool_outputs": [i for c in calls[-3:] for i in c.get("input", []) if i.get("type") in {"function_call_output", "custom_tool_call_output"}]}, default=str)
        assert all(call["reasoning"]["effort"] == expected_effort for call in children)
        assert calls[0]["model"] == "gpt-6-astra" and calls[0]["reasoning"]["effort"] == "xhigh"
        mcp_calls = [value for path, value in routes if path == "/mcp" and value.get("method") == "tools/call"]
        assert any(call.get("params", {}).get("name") == "tasks_get"
                   and call["params"].get("arguments", {}).get("task_id") == task_id
                   for call in mcp_calls), json.dumps(mcp_calls, indent=2)
        bootstraps = [value for path, value in routes if path == "/api/agent/bootstrap"]
        assert bootstraps
        assert len({value["session_id"] for value in bootstraps}) == 1
        # Neither the parent nor its helper starts an agent-side inbox poller or claims the inbox.
        assert not [path for path, _ in routes if path.startswith("/api/agent/inbox/")]
        assert len(parent_resume_inputs) == 1
        assert task_id in json.dumps(parent_resume_inputs[0]), "parent did not receive the completed helper result"
        assert "SYNTHETIC_CHILD_DONE" in json.dumps(parent_resume_inputs[0]), "parent resumed before receiving helper completion"
        assert parent_tool_calls[0] == "spawn_agent" and set(parent_tool_calls[1:]) == {"wait_agent"}
        assert lifecycle_order.index("helper_tasks_get") < lifecycle_order.index("helper_completed")
        assert lifecycle_order.index("helper_completed") < lifecycle_order.index("parent_resumed")
        assert activity_reads and activity_reads[-1]["agents"][0]["state"] in {"running", "idle"}
        assert activity_reads[-1]["agents"][0]["sessions"][0]["session_id"] == bootstraps[0]["session_id"]
        assert activity_reads[-1]["agents"][0]["sessions"][0]["items"] == current_work
        assert not work_updates, "completed child changed the parent's current-work declaration"
        assert "running" in parent_activity_states and parent_activity_states[-1] in {"idle", "offline"}
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        os.close(master)
        server.shutdown()
        server.server_close()
