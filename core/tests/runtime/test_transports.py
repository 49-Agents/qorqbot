import asyncio
import json
import subprocess
import sys
import time
import unittest
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx

from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.app import Application
from qorqut_core.contracts import Invocation
from qorqut_core.runner.process import fingerprint
from qorqut_core.runtime import OwnerCandidate
from runtime.support import RuntimeCase
from transports.support import Installation
from transports.test_transports import session, structured


class RuntimeTransports(unittest.IsolatedAsyncioTestCase):
    async def test_shared_http_mcp_operator_read_and_monitor_observed_child_exit(self):
        fixture = Installation()
        self.addCleanup(fixture.close)
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(RuntimeCase.exit_child, child)
        application = Application.open(fixture.settings)
        try:
            context = Invocation(actor_id=fixture.owner.actor_id)
            agent_id = application.invoke("agents_list", context).data["onbash_agent_id"]
            epoch = application.invoke("config_get", context).data["recovery_epoch"]
            candidate = OwnerCandidate(agent_id=agent_id, recovery_epoch=epoch, environment_generation=1,
                terminal_id=uuid4(), provider="codex", cli_version=VERSIONS["codex"],
                native_session_id=uuid4(), process=fingerprint(child.pid))
            connection = application.session_owners.connect(candidate)
            saved_owner = application.invoke("runtime_get", context, {"agent_id": agent_id}).data["current_owner"]
        finally:
            application.close()
        fixture.start()
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(base_url=fixture.url,
                headers=fixture.headers(), trust_env=False, timeout=5))
            sdk = await session(stack, fixture, fixture.agent)
            schema = (await http.get("/api/v1/schema")).json()
            tools = {tool.name: tool for tool in (await sdk.list_tools()).tools}
            operation = next(op for op in schema["operations"] if op["name"] == "runtime_get")
            self.assertFalse(operation["mutation"])
            self.assertEqual(operation["input_schema"], tools["runtime_get"].inputSchema)
            self.assertEqual(len(tools), 15)
            self.assertFalse({"session_connect", "session_release", "runtime_start"} & tools.keys())
            args = {"agent_id": agent_id}
            direct = (await http.post("/api/v1/operations/runtime_get", json=args)).json()
            mcp = structured(await sdk.call_tool("runtime_get", args))
            # The periodic observer may advance observed_at between calls.
            for result in (direct, mcp):
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"]["current_owner"], saved_owner)
                self.assertEqual(result["data"]["status"], "connected")
                self.assertFalse(result["data"]["input_ready"])
                self.assertIsNotNone(result["data"]["observed_at"])
                self.assertIsNone(result["operation_id"])
            arguments = fixture.root / "runtime-arguments.json"
            arguments.write_text(json.dumps(args))
            operator = fixture.command("call", "runtime_get", "--url", fixture.url,
                "--token-file", fixture.token_files[fixture.owner.actor_id], "--arguments-file", arguments, "--json")
            self.assertEqual(operator.returncode, 0, operator.stderr)
            self.assertEqual(json.loads(operator.stdout)["data"]["current_owner"], saved_owner)
            for invalid in ({}, {"agent_id": "bad"}, {"agent_id": str(uuid4())}, {**args, "owner_id": str(connection.owner_id)}):
                http_error = (await http.post("/api/v1/operations/runtime_get", json=invalid)).json()
                self.assertEqual(http_error, structured(await sdk.call_tool("runtime_get", invalid)))
                self.assertFalse(http_error["ok"])
            RuntimeCase.exit_child(child)
            deadline = time.monotonic() + 5
            while True:
                disconnected = (await http.post("/api/v1/operations/runtime_get", json=args)).json()
                if disconnected["data"]["status"] == "disconnected":
                    break
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.03)
            self.assertEqual(disconnected, structured(await sdk.call_tool("runtime_get", args)))
            self.assertIsNone(disconnected["data"]["current_owner"])
        fixture.stop()
        application = Application.open(fixture.settings)
        try:
            with application.db.read() as tx:
                row = tx.connection.execute("SELECT state,observation FROM session_owners WHERE owner_id=?",
                                            (str(connection.owner_id),)).fetchone()
                self.assertEqual(tuple(row), ("closed", "exited"))
        finally:
            application.close()
