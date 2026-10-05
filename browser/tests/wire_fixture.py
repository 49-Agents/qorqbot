"""Owned loopback servers and the accepted synthetic readiness/helper seam."""

import asyncio
from contextlib import asynccontextmanager
import json
import multiprocessing
from pathlib import Path
import socket
import sys
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from qorqut_browser.config import ServiceConfig
from qorqut_browser.contracts import Binding, Reconcile
from qorqut_browser.server import create_app
from qorqut_browser.state import Store
from qorqut_browser.tools import AgentBrowser
from test_browser import FAKE_HELPER, configuration, ready

TOKEN_A, TOKEN_B, APPLICATION_TOKEN, BRIDGE_TOKEN = (letter * 40 for letter in "ABCD")


def private_file(path, value):
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(0o600)
    return path


def serve_browser(config, listener, log_path):
    with log_path.open("a") as log:
        sys.stdout = sys.stderr = log
        effect = AgentBrowser(config.browser, config.helper_executable, timeout=8, readiness=ready)
        app = create_app(config, readiness=ready, executor=effect)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.browser.service_port,
                                proxy_headers=False, access_log=False, log_level="warning",
                                timeout_graceful_shutdown=1))
        server.run(sockets=[listener])


class BrowserServer:
    def __init__(self, root: Path, handoff=None):
        self.root = root
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        browser = configuration(root).model_copy(update={"service_port": self.port})
        self.actor_a, self.actor_b, self.agent_b = uuid4(), uuid4(), uuid4()
        self.credentials = private_file(root / "credentials.json", {
            "actors": [{"token": TOKEN_A, "actor_id": str(self.actor_a), "agent_id": str(browser.binding.identity.agent_id)},
                       {"token": TOKEN_B, "actor_id": str(self.actor_b), "agent_id": str(self.agent_b)}],
            "application_token": APPLICATION_TOKEN})
        helper = root / "synthetic-agent-browser"
        helper.write_text(f"#!{sys.executable}\n" + FAKE_HELPER)
        helper.chmod(0o700)
        self.config = ServiceConfig(browser=browser, credentials_file=self.credentials,
                                    helper_executable=helper, handoff=handoff)
        self.store = Store(browser)
        self.store.initialize()
        self.log = private_file(root / "service.log", "")
        self.process = None

    async def start(self):
        self.process = multiprocessing.get_context("spawn").Process(
            target=serve_browser, args=(self.config, self.listener, self.log))
        self.process.start()
        async with httpx.AsyncClient(trust_env=False, timeout=.3) as client:
            for _ in range(150):
                if not self.process.is_alive():
                    raise AssertionError("Fixture server exited: " + self.log.read_text())
                try:
                    response = await client.get(self.url + "/v1/identity", headers=self.headers())
                    if response.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.02)
        raise AssertionError("Fixture service did not become available: " + self.log.read_text())

    def stop(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(3)
            self.process.close()
            self.process = None

    def close(self):
        for directory in self.config.browser.artifacts.iterdir():
            if directory.is_dir():
                (directory / "release").touch()
        self.stop()
        self.listener.close()

    def headers(self, token=TOKEN_A, actor=None):
        value = {"Authorization": "Bearer " + token}
        if actor is not None:
            value["X-Qorqut-Actor"] = str(actor)
        return value

    @asynccontextmanager
    async def session(self, token=TOKEN_A):
        async with httpx.AsyncClient(headers=self.headers(token), trust_env=False, timeout=15) as client:
            async with streamable_http_client(self.url + "/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def reconcile(self, client, mode="automation", settled=()):
        current = self.store.status().binding
        desired = Binding(identity=current.identity, control_epoch=current.control_epoch + 1)
        request = Reconcile(command_id=uuid4(), expected=current, desired=desired, mode=mode,
                            settled_commands=settled, evidence="Synthetic application reconciliation")
        response = await client.post(self.url + "/v1/browser/control", json=request.model_dump(mode="json"),
                                     headers=self.headers(APPLICATION_TOKEN, self.actor_a))
        if response.status_code != 200 or response.json()["outcome"] != "completed":
            raise AssertionError(response.text)
        return desired

    def command(self, action=None):
        return {"command_id": str(uuid4()), "binding": self.store.status().binding.model_dump(mode="json"),
                "action": action or {"kind": "click", "selector": "e1"}}

    async def wait_effect(self, command):
        directory = self.config.browser.artifacts / command["command_id"]
        for _ in range(200):
            if (directory / "seen.json").exists():
                return directory
            await asyncio.sleep(.01)
        raise AssertionError("Synthetic effect was not observed")
