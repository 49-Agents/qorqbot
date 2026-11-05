"""Standalone Codex worker-session contract against loopback-only fixtures."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time
import uuid

import pytest
import tomli_w


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_CODEX_DELEGATED_RUNTIME") != "1",
                    reason="explicit isolated Codex delegated-runtime contract only")
def test_saved_worker_session_accepts_idle_followup_with_scoped_mcp(tmp_path):
    worker_token = "synthetic-delegation-token-with-no-authority"
    parent_token = "synthetic-parent-credential-not-for-worker"
    model_requests, mcp_requests, parent_mcp_requests = [], [], []
    offered_tools = []

    def response_item(request_number):
        text = "WORKER_FIRST_TURN_COMPLETE" if request_number == 1 else "WORKER_IDLE_FOLLOWUP_COMPLETE"
        return {"id": "msg_" + str(uuid.uuid4()), "type": "message", "role": "assistant",
                "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/parent-mcp":
                parent_mcp_requests.append((body, self.headers.get("Authorization")))
                self.send_response(404)
                self.end_headers()
                return
            if self.path == "/mcp":
                mcp_requests.append((body, self.headers.get("Authorization")))
                assert self.headers.get("Authorization") == "Bearer " + worker_token
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                method = body["method"]
                if method == "initialize":
                    result = {"protocolVersion": body["params"]["protocolVersion"],
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "synthetic-delegation", "version": "1"}}
                elif method == "tools/list":
                    offered_tools.append("tasks_get")
                    result = {"tools": [{"name": "tasks_get", "description": "Read the assigned task.",
                        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                                        "required": ["task_id"], "additionalProperties": False}}]}
                else:
                    raise AssertionError("Unexpected fixture MCP request: " + json.dumps(body))
                reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())
                return

            if self.path != "/v1/responses":
                self.send_error(404)
                return
            if not model_requests:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if any(request.get("method") == "tools/list"
                           for request, _auth in mcp_requests):
                        break
                    time.sleep(.05)
            model_requests.append(body)
            item = response_item(len(model_requests))
            response = {"id": "resp_" + str(uuid.uuid4()), "object": "response", "created_at": 1,
                "model": body["model"], "status": "completed", "output": [item],
                "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}]
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
    home, workspace = tmp_path / "codex-home", tmp_path / "worker"
    home.mkdir()
    (workspace / ".codex").mkdir(parents=True)
    (home / "config.toml").write_text(tomli_w.dumps({
        "check_for_update_on_startup": False,
        "projects": {str(workspace): {"trust_level": "trusted"}},
        "mcp_servers": {"unrestricted_parent": {"url": endpoint + "/parent-mcp",
            "http_headers": {"Authorization": "Bearer " + parent_token}}},
    }))
    (workspace / ".codex/config.toml").write_text(tomli_w.dumps({
        "model_provider": "probe",
        "model_providers": {"probe": {"name": "Loopback fixture", "base_url": endpoint + "/v1",
            "wire_api": "responses", "requires_openai_auth": False}},
        "features": {"apps": False, "plugins": False, "code_mode": False},
        "approval_policy": "never", "model_reasoning_effort": "low",
        "mcp_servers": {"bazarlyk": {"url": endpoint + "/mcp",
            "http_headers": {"Authorization": "Bearer " + worker_token}, "required": True}},
    }))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CODEX_HOME": str(home), "TERM": "dumb",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
           "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
           "ALL_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
           "https_proxy": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1"}

    runtime_overrides = [
        "-c", 'model_provider="probe"',
        "-c", f'model_providers.probe={{name="Loopback fixture",base_url="{endpoint}/v1",wire_api="responses",requires_openai_auth=false}}',
        "-c", f'mcp_servers.bazarlyk={{url="{endpoint}/mcp",http_headers={{Authorization="Bearer {worker_token}"}},required=true}}',
    ]

    def run(arguments):
        try:
            completed = subprocess.run(["codex", *arguments], cwd=workspace, env=env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("Isolated Codex session did not finish within its bound") from exc
        if completed.returncode:
            raise AssertionError("Isolated Codex session failed: " + completed.stderr[-3000:])
        rows = []
        for line in completed.stdout.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    try:
        first = run(["exec", "--json", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", "read-only",
                     "--model", "gpt-6-luna", *runtime_overrides, "-c", 'model_reasoning_effort="low"',
                     "--cd", str(workspace),
                     "Read the assigned task through the scoped MCP tool and report its revision."])
        thread_id = next((row.get("thread_id") for row in first if row.get("type") == "thread.started"), None)
        assert thread_id and str(uuid.UUID(thread_id)) == thread_id, first
        assert any(row.get("type") == "turn.completed" for row in first), first

        # The initial codex exec process has exited: this is a later notification
        # while Astra is idle, delivered by resuming the saved worker session.
        second = run(["exec", "resume", "--json", "--ignore-user-config", "--skip-git-repo-check",
                      "--model", "gpt-6-luna", *runtime_overrides,
                      "-c", 'model_reasoning_effort="low"', thread_id,
                      "A synthetic follow-up arrived. Continue the saved assignment without Astra."])
        resumed_id = next((row.get("thread_id") for row in second if row.get("type") == "thread.started"), None)
        assert resumed_id == thread_id, second
        assert any(row.get("type") == "turn.completed" for row in second), second
        assert len(model_requests) == 2, [request.get("model") for request in model_requests]
        assert all(request["model"] == "gpt-6-luna" for request in model_requests)
        assert all(request.get("reasoning", {}).get("effort") == "low" for request in model_requests)
        assert "A synthetic follow-up arrived" in json.dumps(model_requests[-1].get("input", []))
        assert offered_tools and set(offered_tools) == {"tasks_get"}
        assert all(auth == "Bearer " + worker_token for _body, auth in mcp_requests)
        assert not parent_mcp_requests, "Worker loaded the parent's unrestricted MCP connection"
    finally:
        server.shutdown()
        server.server_close()
