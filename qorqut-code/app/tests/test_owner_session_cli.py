"""Native CLI configuration checks; no model calls or real user configuration."""

import json
import os
import select
import shutil
import subprocess
import time

import pytest
import tomli_w

from bazarlyk.bootstrap import owner_prepare
from test_owner_session_mcp import enable
from test_service_transports import ServiceFixture


def codex_config(binary, root, env):
    """Read the real app-server's effective config without starting a thread."""
    process = subprocess.Popen([binary, "app-server", "--stdio"], cwd=root, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        requests = [
            {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "owner-session-test", "version": "1"}}},
            {"method": "initialized"},
            {"id": 2, "method": "config/read", "params": {"cwd": str(root), "includeLayers": True}},
        ]
        process.stdin.write(b"".join((json.dumps(row) + "\n").encode() for row in requests))
        process.stdin.flush()
        pending = b""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") == 2:
                    assert "error" not in response, response
                    return response["result"]["config"]
        pytest.fail("Native Codex did not return its effective configuration")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(not shutil.which("codex"), reason="Codex CLI is not installed")
def test_native_codex_keeps_user_settings_and_loads_each_prepared_root(tmp_path):
    service = ServiceFixture(tmp_path)
    enable(service.fixture)
    top = tmp_path / "remote code"
    nested = top / "nested checkout"
    nested.mkdir(parents=True)
    for root in (top, nested):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    user_home = tmp_path / "user"
    codex_home = user_home / ".codex"
    codex_home.mkdir(parents=True)
    user_config = {
        "model": "owner-model", "approval_policy": "on-request", "sandbox_mode": "workspace-write",
        "mcp_servers": {"personal-tool": {"command": "/bin/false", "enabled": False}},
        "projects": {str(root): {"trust_level": "trusted"} for root in (top, nested)},
    }
    config_path = codex_home / "config.toml"
    config_path.write_text(tomli_w.dumps(user_config))
    before = config_path.read_bytes()
    owner_prepare.prepare(service.fixture.db, business=service.fixture.business_id, code_root=top,
                          nested=[nested.name], endpoint="http://testserver", label="test-machine")
    # This temporary HOME represents the user's existing home. No generated
    # launch command selects a different CODEX_HOME.
    env = {**os.environ, "HOME": str(user_home)}
    env.pop("CODEX_HOME", None)
    binary = shutil.which("codex")
    for root in (top, nested):
        config = codex_config(binary, root, env)
        for key in ("model", "approval_policy", "sandbox_mode"):
            assert config[key] == user_config[key]
        assert "personal-tool" in config["mcp_servers"]
        owner = config["mcp_servers"][owner_prepare.SERVER]
        assert owner["url"] == "http://testserver/mcp/owner"
        assert str(root / owner_prepare.HEADERS) in owner["http_headers_helper"]
        result = subprocess.run([binary, "mcp", "get", owner_prepare.SERVER, "--json"],
                                cwd=root, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["name"] == owner_prepare.SERVER
    assert config_path.read_bytes() == before


@pytest.mark.skipif(not shutil.which("claude"), reason="Claude Code CLI is not installed")
def test_native_claude_discovers_prepared_nested_project_without_user_changes(tmp_path):
    service = ServiceFixture(tmp_path)
    enable(service.fixture)
    top = tmp_path / "code"
    nested = top / "repo"
    nested.mkdir(parents=True)
    user_home = tmp_path / "claude-user"
    user_home.mkdir()
    owner_prepare.prepare(service.fixture.db, business=service.fixture.business_id, code_root=top,
                          nested=["repo"], endpoint="http://127.0.0.1:9", label="test-machine")
    env = {**os.environ, "HOME": str(user_home), "CLAUDE_CONFIG_DIR": str(user_home / ".claude"),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    for root in (top, nested):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        result = subprocess.run([shutil.which("claude"), "mcp", "get", owner_prepare.SERVER],
                                cwd=root, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert owner_prepare.SERVER in result.stdout
        assert "http://127.0.0.1:9/mcp/owner" in result.stdout
        # This read-only discovery leaves initial project trust/approval to the
        # real owner. It never invokes a model or connects to the live service.
