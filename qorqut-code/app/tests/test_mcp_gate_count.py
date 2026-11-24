"""How many times one MCP request passes the per-use authority gate, counted over the real HTTP path (admission in
``MCPTransport.__call__``, then the MCP session manager, then the tool handler), and what a closed gate answers."""
from contextlib import asynccontextmanager
import json
import threading
from types import SimpleNamespace as NS
import unittest

import httpx

from qorqut_operations.contracts import DomainError
from qorqut.transport import mcp
from qorqut.transport.batches import NAME as BATCH

import test_mcp_phase_timing as support

CREDENTIAL = "Bearer test-credential"
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class Caller(support.Caller):
    def authenticate(self, authorization):
        if authorization != CREDENTIAL:
            raise DomainError("unauthenticated", "Unknown credential")
        return NS(principal=NS(id="agent", kind="agent"), delegation_binding_id=None, credential_id="credential")

    def check_batch(self, actor):
        return actor


class Registry(support.Registry):
    def bind_arguments(self, name, arguments):
        return arguments


class GateCountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock, delays = support.Clock(), {"sample_read": {}}
        self.caller = Caller(clock, delays)
        self.application = support.Application(clock, delays, {})
        self.gates, self.closed = [], None

        def require_authority():
            self.gates.append(threading.current_thread())
            if self.closed is not None:
                raise self.closed
        runtime = NS(registry=Registry(clock, delays, {}), application=self.application,
                     require_authority=require_authority)
        self.transport = mcp.MCPTransport(runtime, None, NS(validate=lambda request: None), caller=self.caller)

    @asynccontextmanager
    async def session(self):
        """An initialized MCP session; yields ``post(message, credential=CREDENTIAL)``."""
        async with self.transport.manager.run(), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.transport), base_url="http://test") as client:
            identity = {}

            async def post(message, credential=CREDENTIAL):
                headers = {**HEADERS, "authorization": credential, **identity}
                return await client.post("/mcp", content=json.dumps(message), headers=headers)
            started = await post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            identity["mcp-session-id"] = started.headers["mcp-session-id"]
            await post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.gates.clear()
            yield post

    @staticmethod
    def tool(name, arguments=None):
        return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}

    async def test_a_single_tool_call_passes_the_gate_at_admission_and_on_its_worker(self):
        async with self.session() as post:
            response = await post(self.tool("sample_read"))
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse(response.json()["result"]["isError"], response.text)
        self.assertEqual(["sample_read"], self.application.invoked)
        self.assertEqual(2, len(self.gates))
        self.assertNotIn(threading.main_thread(), self.gates)       # neither blocks the event loop

    async def test_a_batch_passes_it_at_admission_for_the_batch_and_once_per_entry(self):
        entries = [{"id": f"e{index}", "operation": "sample_read", "arguments": {}} for index in range(3)]
        async with self.session() as post:
            response = await post(self.tool(BATCH, {"operations": entries}))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["sample_read"] * 3, self.application.invoked)
        self.assertEqual(1 + 1 + 3, len(self.gates))

    async def test_other_requests_pass_it_once_at_admission(self):
        async with self.session() as post:
            response = await post({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(self.gates))

    async def test_a_closed_gate_refuses_every_request_before_authentication_or_dispatch(self):
        async with self.session() as post:
            self.closed = DomainError("maintenance", "Operational access is paused for maintenance", retryable=True)
            for message, credential in ((self.tool("sample_read"), CREDENTIAL), (self.tool("no_such_tool"), CREDENTIAL),
                                        ({"jsonrpc": "2.0", "id": 3, "method": "ping"}, CREDENTIAL),
                                        (self.tool("sample_read"), "Bearer unknown")):
                response = await post(message, credential)
                self.assertIn("maintenance", response.text)
                self.assertNotIn("unauthenticated", response.text)
        self.assertEqual([], self.application.invoked)
        self.assertEqual([], self.caller.configured)


if __name__ == "__main__":
    unittest.main()
