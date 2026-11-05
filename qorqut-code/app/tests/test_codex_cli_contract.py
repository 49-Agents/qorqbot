"""Opt-in real CLI acceptance with isolated HOME and a loopback synthetic model."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
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

from bazarlyk.bootstrap.codex_config import configuration
from qorqut_operations.contracts import new_id


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_CODEX_HOOK_TRUST") != "1", reason="explicit no-model native hook trust check")
def test_installed_codex_release_helper_preserves_hook_trust(tmp_path):
    home, workspace = tmp_path / 'home', tmp_path / 'agent'
    home.mkdir(); (workspace / '.codex').mkdir(parents=True)
    helper = workspace / 'codex-hook'
    helper.write_text('#!/bin/sh\nexit 0 # release one\n')
    hook_file = workspace / '.codex/hooks.json'
    hook_file.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{
        'type': 'command', 'command': shlex.join(['/bin/sh', str(helper), 'bootstrap'])}]}]}}))
    config = {'projects': {str(workspace): {'trust_level': 'trusted'}}}
    config_path = home / 'config.toml'
    config_path.write_text(tomli_w.dumps(config))

    def inspect():
        # No sessions, hooks, model requests or production credentials are used.
        process = subprocess.Popen(['codex', 'app-server', '--stdio'], cwd=workspace,
            env={**os.environ, 'CODEX_HOME': str(home), 'HOME': str(home)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        def call(identifier, method, params):
            process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params})+'\n')
            process.stdin.flush()
            deadline = time.monotonic()+20
            while time.monotonic() < deadline:
                if select.select([process.stdout], [], [], 1)[0]:
                    line = process.stdout.readline()
                    assert line, 'Native configuration inspector exited'
                    result = json.loads(line)
                    if result.get('id') == identifier:
                        assert 'error' not in result, result
                        return result['result']
            raise AssertionError('Native configuration inspector timed out')
        try:
            call(1, 'initialize', {'clientInfo': {'name': 'qorqut_hook_test', 'version': '1'},
                                   'capabilities': {'experimentalApi': True}})
            process.stdin.write('{"method":"initialized","params":{}}\n'); process.stdin.flush()
            result = call(2, 'hooks/list', {'cwds': [str(workspace)]})['data'][0]
            assert not result['errors']
            return result['hooks'][0]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)

    original = inspect()
    assert original['trustStatus'] != 'trusted'
    # Simulate owner review in this disposable test config only.
    config['hooks'] = {'state': {original['key']: {'trusted_hash': original['currentHash']}}}
    config_path.write_text(tomli_w.dumps(config))
    assert inspect()['trustStatus'] == 'trusted'
    helper.write_text('#!/bin/sh\nexit 0 # release two\n')
    updated = inspect()
    assert updated['trustStatus'] == 'trusted' and updated['currentHash'] == original['currentHash']
    changed = json.loads(hook_file.read_text())
    changed['hooks']['SessionStart'][0]['hooks'][0]['command'] += ' changed-definition'
    hook_file.write_text(json.dumps(changed))
    assert inspect()['trustStatus'] == 'modified'


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_CODEX_LIVE") != "1", reason="explicit isolated CLI acceptance only")
def test_installed_codex_bootstrap_mcp_inbox_and_capture(tmp_path):
    requests, routes = [], []
    token = "synthetic-test-credential-with-no-authority"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(405)
            self.end_headers()
        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            value = json.loads(raw)
            if self.path.startswith(("/api/", "/mcp")):
                if self.headers.get("Authorization") != "Bearer " + token:
                    self.send_response(401)
                    self.end_headers()
                    return
                routes.append((self.path, value))
                if self.path == "/mcp":
                    if "id" not in value:
                        self.send_response(202)
                        self.end_headers()
                        return
                    result = ({"protocolVersion": value["params"]["protocolVersion"], "capabilities": {"tools": {}},
                        "serverInfo": {"name": "qorqut-synthetic", "version": "1"}}
                        if value["method"] == "initialize" else {"tools": []})
                    reply = {"jsonrpc": "2.0", "id": value["id"], "result": result}
                elif self.path == "/api/agent/bootstrap":
                    reply = {"ready": True, "effective_config_id": "synthetic", "inbox_available": True,
                        "inbox_session_note": "Synthetic managed inbox guidance.",
                        "mandatory_context": "SYNTHETIC_QORQUT_BOOTSTRAP", "work_briefing": {"rendered": "SYNTHETIC_SAVED_WORK"}}
                else:
                    reply = {}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())
                return
            requests.append(value)
            message = {"id": "msg_" + new_id(), "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Synthetic response complete.", "annotations": []}]}
            response = {"id": "resp_" + new_id(), "object": "response", "created_at": 1, "model": "probe",
                "status": "completed", "output": [message], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "item_id": message["id"], "delta": "Synthetic response complete."},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (home / "config.toml").write_text(tomli_w.dumps({
        "model": "probe", "model_provider": "probe", "check_for_update_on_startup": False,
        "model_providers": {"probe": {"name": "Probe", "base_url": endpoint + "/v1", "wire_api": "responses", "requires_openai_auth": False}},
        "projects": {str(workspace): {"trust_level": "trusted"}}, "features": {"apps": False, "plugins": False},
    }))
    registration_path = workspace / ".bazarlyk/registration.json"
    registration_path.parent.mkdir()
    registration_path.write_text(json.dumps({"schema_version": 1, "registration_id": new_id(), "agent_id": new_id(),
        "workspace": str(workspace), "service_url": endpoint, "credential_file": ".bazarlyk/credential", "capture_transcripts": True}))
    credential = workspace / ".bazarlyk/credential"
    credential.write_text(token)
    credential.chmod(0o600)
    def command(module, *args):
        return shlex.join([sys.executable, "-I", "-m", module, "--registration", str(registration_path), *args])
    config, hooks = configuration(workspace=workspace, endpoint=endpoint,
        header_command=command("bazarlyk.bootstrap.mcp_headers"),
        command=command("bazarlyk.bootstrap.claude_session", "--runtime", "codex"),
        snapshot_command=command("bazarlyk.bootstrap.codex_session"),
        protected=[registration_path.parent], read_denied=[credential])
    (workspace / ".codex").mkdir()
    (workspace / ".codex/config.toml").write_bytes(config)
    (workspace / ".codex/hooks.json").write_text(json.dumps({"hooks": hooks}))
    (workspace / "AGENTS.md").write_text("SYNTHETIC_AGENT_ORIENTATION\n")
    skill = workspace / ".agents/skills/synthetic/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: synthetic\ndescription: SYNTHETIC_SHARED_SKILL\n---\nSynthetic procedure.\n")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    # Trust bypass is confined to this temporary, synthetic test fixture. No real
    # credentials, provider or global CLI configuration enter its environment.
    process = subprocess.Popen(["codex", "--strict-config", "--dangerously-bypass-hook-trust", "--no-alt-screen", "Synthetic acceptance"],
        cwd=workspace, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "CODEX_HOME": str(home), "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    output = b""
    serialized = ""
    try:
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output += chunk
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
            serialized = json.dumps(requests)
            if "SYNTHETIC_QORQUT_BOOTSTRAP" in serialized and any(route == "/api/agent/bootstrap/receipt" for route, _ in routes):
                break
        assert "SYNTHETIC_QORQUT_BOOTSTRAP" in serialized, output[-6000:].decode(errors="replace")
        assert "SYNTHETIC_SAVED_WORK" in serialized
        assert "SYNTHETIC_AGENT_ORIENTATION" in serialized
        assert "SYNTHETIC_SHARED_SKILL" in serialized
        assert "qorqut-agent" in serialized
        assert "nothing needs to be started, polled or kept alive" in serialized, output[-6000:].decode(errors="replace")
        assert any(route == "/mcp" and value.get("method") == "tools/list" for route, value in routes)
        assert any(route == "/api/agent/bootstrap/receipt" for route, _ in routes)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
        os.close(master)
        server.shutdown()
        server.server_close()
    # No agent-side poller runs in the session: the hooks never touch an inbox route.
    assert not any(route.startswith("/api/agent/inbox/") for route, _ in routes)
