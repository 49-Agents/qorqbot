import sys
import asyncio
import json
from pathlib import Path
import shlex
import socket
import subprocess
from threading import Thread
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

from qorqut_operations.contracts import new_id
from bazarlyk.service.main import load_terminal_broker
from bazarlyk.service.web import create_app
from bazarlyk.transport.terminals import TerminalService
from test_service_transports import ORIGIN, ServiceFixture
from test_agent_configuration import REPO, PATHS


def test_real_sdk_client_uses_single_http_service_and_json_text_results(tmp_path):
    fixture = ServiceFixture(tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
    app = create_app(fixture.runtime, origins=[origin], authentication=fixture.authentication)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="on"))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.started:
                break
            time.sleep(0.05)
        assert server.started

        async def client():
            headers = {"Authorization": "Bearer " + fixture.tokens[fixture.agent.id]}
            async with httpx.AsyncClient(headers=headers, trust_env=False, timeout=10) as http:
                async with streamable_http_client(origin + "/mcp", http_client=http) as (read, write, session_id):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        assert initialized.serverInfo.name == "bazarlyk"
                        assert session_id()
                        listed = await session.list_tools()
                        assert "tasks_create" in {tool.name for tool in listed.tools}
                        assert "operations_batch" in {tool.name for tool in listed.tools}
                        batch = await session.call_tool("operations_batch", {"operations": [
                            {"id": "a", "operation": "tasks_create", "arguments": {"title": "SDK batch A", "idempotency_key": new_id()}},
                            {"id": "b", "operation": "tasks_create", "arguments": {"title": "SDK batch B", "idempotency_key": new_id()}}]})
                        assert not batch.isError and batch.structuredContent is None
                        assert len(batch.content) == 1
                        assert json.loads(batch.content[0].text)["data"]["all_ok"]
                        created = await session.call_tool("tasks_create", {"title": "SDK integration", "idempotency_key": new_id()})
                        assert not created.isError
                        assert created.structuredContent is None
                        assert json.loads(created.content[0].text)["status"] == "ok"
                        assert json.loads(created.content[0].text)["data"]["task"]["created_by"] == fixture.agent.id
                        refused = await session.call_tool("tasks_create", {"title": "No identity override", "job_id": new_id(), "idempotency_key": new_id()})
                        assert refused.isError
                        assert refused.structuredContent is None
                        assert json.loads(refused.content[0].text)["error"]["code"] == "validation_error"
        asyncio.run(client())
        from bazarlyk.bootstrap.preparation import prepare_claude_agent
        prepared = prepare_claude_agent(fixture.runtime.resolver, fixture.agent, endpoint=origin,
            claude_version="2.1.263 (Claude Code)")
        workspace = Path(prepared["workspace"])
        settings = json.loads((workspace / ".claude/settings.local.json").read_text())
        defaults = json.loads((workspace / ".claude/settings.json").read_text())
        assert "mcp__bazarlyk__operations_batch" in defaults["permissions"]["allow"]
        connection = json.loads((workspace / ".mcp.json").read_text())["mcpServers"]["bazarlyk"]
        command = shlex.split(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        assert command[:2] == [sys.executable, "-I"]
        environment = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONPATH": "/untrusted-caller-path"}
        helper = subprocess.run(shlex.split(connection["headersHelper"]), cwd=workspace, env=environment,
            capture_output=True, text=True, check=True, timeout=10)
        assert json.loads(helper.stdout)["Authorization"] == "Bearer " + fixture.tokens[fixture.agent.id]
        event = {"hook_event_name": "SessionStart", "source": "startup", "session_id": "installed-hook-test"}
        def hook(event):
            result = subprocess.run(command, input=json.dumps(event), cwd=workspace, env=environment,
                capture_output=True, text=True, check=True, timeout=10)
            assert fixture.tokens[fixture.agent.id] not in result.stdout + result.stderr
            return json.loads(result.stdout)
        emitted = hook(event)
        from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
        assert len(emitted["hookSpecificOutput"]["additionalContext"]) <= MAX_CONTEXT_CHARACTERS
        assert "Qorqut Configuration" in emitted["hookSpecificOutput"]["additionalContext"]
        assert hook({**event, "hook_event_name": "UserPromptSubmit"}) == {}
        with fixture.fixture.db.transaction() as uow:
            credential = uow.repository("credentials").list(filters={"principal_id": fixture.agent.id})[0]
            uow.repository("credentials").update(credential["id"], {"revoked_at": "2026-09-08T00:00:00Z"}, expected_revision=credential["revision"])
        blocked = hook({**event, "hook_event_name": "UserPromptSubmit"})
        assert blocked["decision"] == "block"
    finally:
        server.should_exit = True
        thread.join(15)
        listener.close()
        assert not thread.is_alive()


def test_isolated_tmux_mirror_survives_service_shutdown_without_claude(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path)
    broker = load_terminal_broker(PATHS)
    if not broker.tmux_available():
        pytest.skip("tmux is not installed")
    broker.TMUX_SOCK = "bazarlyk-test-" + new_id()
    config = tmp_path / "tmux.conf"
    config.write_text(broker.TMUX_CONF)
    monkeypatch.setattr(broker, "_conf_path", lambda: str(config))
    from bazarlyk.service.local import LocalFiles
    local = LocalFiles(service.runtime)
    terminal = TerminalService(service.runtime, broker, service_url="http://127.0.0.1:9988",
        version_provider=lambda: "2.1.263 (Claude Code)", environment=local.environment)
    with service.fixture.db.read() as uow:
        business = uow.repository("businesses").get(service.fixture.business_id)
    context = service.fixture.ctx
    session = broker.session_name("widget", "business-agent")
    mirror = None
    try:
        mirror, profile, row = terminal.open(context, business, service.agent.id, cols=100, rows=30)
        assert broker.session_alive(session)
        mirror.write(b"printf 'BAZARLYK_MIRROR_OK\\n'\n")
        for _ in range(100):
            snapshot, _ = mirror.snapshot()
            if b"BAZARLYK_MIRROR_OK\r\n" in snapshot:
                break
            time.sleep(0.02)
        assert b"BAZARLYK_MIRROR_OK" in snapshot
        service.runtime.stop()
        assert broker.session_alive(session)
        assert terminal.listing(context, business)[0]["agent_id"] == service.agent.id
        key, _, _ = terminal.binding(business, row)
        broker.stop(key)
        attached, _, _ = terminal.open(context, business, service.agent.id)
        assert attached is not mirror
        assert broker.session_alive(session)
    finally:
        for key in broker.keys():
            broker.stop(key)
        broker.tmux("kill-server")
