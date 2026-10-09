import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx
from mcp.shared.exceptions import McpError

from qorqut_browser.transport import MAX_BODY_BYTES
from wire_fixture import BrowserServer

TOKEN_A = "synthetic-existing-diagnostic"


class ServerWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-mcp-r2-")
        self.addCleanup(self.temporary.cleanup)
        self.server = BrowserServer(Path(self.temporary.name))
        self.addCleanup(self.server.close)
        await self.server.start()
        self.client = httpx.AsyncClient(trust_env=False, timeout=15)
        self.addAsyncCleanup(self.client.aclose)

    async def http(self, method, path, **kwargs):
        kwargs.setdefault("headers", self.server.headers())
        return await self.client.request(method, self.server.url + path, **kwargs)

    async def test_discovery_resource_shared_receipt_and_application_control(self):
        server = self.server
        async with server.session() as session:
            tools = await session.list_tools()
            self.assertEqual([tool.name for tool in tools.tools], ["browser_turn"])
            shape = tools.tools[0].inputSchema
            self.assertEqual(set(shape["properties"]), {"command_id", "binding", "action"})
            self.assertFalse(shape["additionalProperties"])
            self.assertEqual(set(shape["properties"]["action"]["discriminator"]["mapping"]),
                             {"snapshot", "navigate", "click", "fill", "screenshot"})
            self.assertEqual([str(r.uri) for r in (await session.list_resources()).resources], ["browser://identity"])
            value = json.loads((await session.read_resource("browser://identity")).contents[0].text)
            self.assertEqual(value, (await self.http("GET", "/v1/identity")).json())
            self.assertFalse(value["control"]["ready"])
            self.assertFalse(value["handoff_enabled"])
            self.assertNotIn(str(server.root), json.dumps(value))
            absent = await session.call_tool("browser_request_input", {"prompt": "test", "idempotency_key": "missing"})
            self.assertTrue(absent.isError)
            self.assertEqual(absent.structuredContent["error"]["code"], "unsupported_capability")
            closed = await session.call_tool("browser_turn", server.command())
            self.assertEqual(closed.structuredContent["error"]["code"], "not_ready")
            await server.reconcile(self.client)
            command = server.command({"kind": "fill", "selector": "e1", "text": "HOLD"})
            direct = asyncio.create_task(session.call_tool("browser_turn", command))
            directory = await server.wait_effect(command)
            proxy = asyncio.create_task(self.http("POST", "/v1/browser/command", json=command,
                headers=server.headers(server.actor_a)))
            await asyncio.sleep(.05)
            self.assertFalse(proxy.done())
            (directory / "release").touch()
            direct_result, proxy_result = await asyncio.gather(direct, proxy)
            self.assertFalse(direct_result.isError)
            self.assertEqual(proxy_result.status_code, 200)
            self.assertEqual(direct_result.structuredContent, proxy_result.json())
            self.assertEqual(proxy_result.json()["actor_id"], str(server.actor_a))
            self.assertEqual(proxy_result.json()["outcome"], "completed")
            with server.store.connection() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM commands WHERE kind='browser'").fetchone()[0], 1)
            self.assertEqual((await self.http("GET", "/v1/browser/commands/" + command["command_id"])).json(),
                             proxy_result.json())
            self.assertEqual((await self.http("GET", "/v1/browser/commands/" + str(uuid4()))).status_code, 404)
            await server.reconcile(self.client, "human")
            for caller in ("mcp", "http"):
                new = server.command()
                if caller == "mcp":
                    error = (await session.call_tool("browser_turn", new)).structuredContent
                else:
                    response = await self.http("POST", "/v1/browser/command", json=new)
                    self.assertEqual(response.status_code, 503)
                    error = response.json()
                self.assertEqual(error["error"]["code"], "not_ready")
            # Historical retries retain the original binding and success.
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, proxy_result.json())

    async def test_local_default_and_forwarded_attribution_share_control(self):
        server = self.server
        await server.reconcile(self.client)
        self.assertEqual((await self.http("GET", "/v1/identity", headers={})).status_code, 200)
        malformed = await self.http("GET", "/v1/identity", headers=server.headers("bad-uuid"))
        self.assertEqual(malformed.status_code, 400)
        async with server.session() as a, server.session(server.actor_b) as b:
            commands = [server.command(), server.command()]
            results = await asyncio.gather(a.call_tool("browser_turn", commands[0]),
                                           b.call_tool("browser_turn", commands[1]))
            self.assertEqual([r.structuredContent["actor_id"] for r in results],
                             [str(server.actor_a), str(server.actor_b)])
            spoof = {**server.command(), "actor_id": str(server.actor_b)}
            invalid = await a.call_tool("browser_turn", spoof)
            self.assertTrue(invalid.isError)
            self.assertEqual(invalid.structuredContent["error"]["code"], "invalid_argument")
            conflict = await b.call_tool("browser_turn", commands[0])
            self.assertEqual(conflict.structuredContent["error"]["code"], "idempotency_conflict")
            private_text = "secret-input-" + TOKEN_A
            invalid = await a.call_tool("browser_turn", server.command({"kind": "fill", "selector": "e1",
                "text": private_text, "unexpected": private_text}))
            self.assertNotIn(private_text, invalid.model_dump_json())
            bad = await self.http("POST", "/v1/browser/command", json=spoof)
            self.assertEqual(bad.status_code, 400)
            self.assertNotIn(str(server.actor_b), bad.text)

    async def test_existing_sdk_error_logging(self):
        # Envelope and shared-session validation take different SDK paths. The
        # latter uses root logging.warning, including for invalid notifications.
        sentinels = ["synthetic-sdk-secret-" + uuid4().hex for _ in range(3)]
        cases = [
            (400, {"jsonrpc": sentinels[0], "id": 1, "method": "initialize"}),
            (200, {"jsonrpc": "2.0", "id": 201, "method": "tools/call",
                   "params": {"name": "browser_turn", "arguments": sentinels[1]}}),
            (202, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                   "params": {"requestId": {}, "reason": sentinels[2]}}),
        ]
        for status, payload in cases:
            with self.subTest(method=payload["method"]):
                response = await self.http("POST", "/mcp", json=payload,
                    headers={**server.headers(), "Accept": "application/json, text/event-stream"})
                self.assertEqual(response.status_code, status)
                if status == 202:
                    self.assertEqual(response.content, b"")
                else:
                    self.assertEqual(response.json()["error"], {
                        "code": -32602, "message": "Invalid or unavailable MCP request"})
                for sentinel in sentinels:
                    self.assertNotIn(sentinel, response.text)
        self.server.stop()  # flush captured stdout/stderr/logging before checking it
        output = self.server.log.read_text()
        for sentinel in [*sentinels, TOKEN_A]:
            self.assertNotIn(sentinel, output)
        self.assertGreaterEqual(output.count("MCP SDK diagnostic"), 2)

    async def test_http_and_mcp_body_bounds_and_origin_checks(self):
        stream = await self.http("GET", "/mcp", headers={**self.server.headers(), "Accept": "text/event-stream"})
        self.assertEqual(stream.status_code, 405)
        for path in ("/mcp", "/v1/browser/command"):
            headers = {**self.server.headers(), "Content-Type": "application/json", "Accept": "application/json"}
            large = await self.http("POST", path, content=b" " * (MAX_BODY_BYTES + 1), headers=headers)
            self.assertEqual(large.status_code, 400)
            async def chunks():
                for _ in range(17):
                    yield b" " * 65536
            chunked = await self.http("POST", path, content=chunks(), headers=headers)
            self.assertEqual(chunked.status_code, 400)
            for override in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}):
                response = await self.http("POST", path, content=b"{}", headers={**headers, **override})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertFalse(self.server.store.status().ready)
        with self.server.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM commands").fetchone()[0], 0)
        method = await self.http("PUT", "/v1/identity")
        self.assertEqual(method.status_code, 400)
        self.assertEqual(method.json()["error"]["code"], "invalid_argument")

    async def test_incomplete_request_concurrency_is_bounded_without_blocking_event_loop(self):
        connections = []
        try:
            # Hold the documented 32 admission slots with tiny partial bodies.
            for _ in range(32):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
                connections.append((reader, writer))
                writer.write((f"POST /v1/browser/command HTTP/1.1\r\nHost: 127.0.0.1:{self.server.port}\r\n"
                              "Content-Length: 2\r\n"
                              "Content-Type: application/json\r\n\r\n{").encode())
                await writer.drain()
            for _ in range(50):
                response = await self.http("GET", "/v1/identity")
                if response.status_code == 503:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "unavailable")
        finally:
            for _, writer in connections:
                writer.close()
            await asyncio.gather(*(writer.wait_closed() for _, writer in connections))
        for _ in range(50):
            response = await self.http("GET", "/v1/identity")
            if response.status_code == 200:
                break
            await asyncio.sleep(.01)
        self.assertEqual(response.status_code, 200)
        with self.server.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM commands").fetchone()[0], 0)

    async def test_cancelled_mcp_wait_does_not_cancel_effect_or_reopen_control(self):
        server = self.server
        await server.reconcile(self.client)
        async with server.session() as session:
            command = server.command({"kind": "fill", "selector": "e2", "text": "HOLD"})
            call = asyncio.create_task(session.call_tool("browser_turn", command,
                                                       read_timeout_seconds=timedelta(seconds=.3)))
            directory = await server.wait_effect(command)
            with self.assertRaises(McpError):
                await call
            # Identity remains responsive while helper and control wait off-loop.
            self.assertEqual((await self.http("GET", "/v1/identity")).status_code, 200)
            take = asyncio.create_task(server.reconcile(self.client, "human"))
            for _ in range(200):
                if server.store.status().mode == "yielding":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(server.store.status().mode, "yielding")
            self.assertFalse(take.done())
            self.assertFalse(server.store.status().ready)
            (directory / "release").touch()
            await take
            saved = await self.http("POST", "/v1/browser/command", json=command,
                                    headers=server.headers(server.actor_a))
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["outcome"], "completed")
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, saved.json())

    async def test_restart_retains_uncertainty_and_requires_explicit_reconciliation(self):
        server = self.server
        await server.reconcile(self.client)
        command = server.command({"kind": "fill", "selector": "e1", "text": "FAIL"})
        failed = await self.http("POST", "/v1/browser/command", json=command)
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["outcome"], "uncertain")
        witness = (server.config.browser.artifacts / command["command_id"] / "seen.json").read_bytes()
        self.assertFalse(server.store.status().ready)
        server.stop()
        await server.start()
        self.assertFalse(server.store.status().ready)
        observed = await self.http("GET", "/v1/browser/commands/" + command["command_id"])
        self.assertEqual(observed.json(), failed.json())
        async with server.session() as session:
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, failed.json())
            new = await session.call_tool("browser_turn", server.command())
            self.assertEqual(new.structuredContent["error"]["code"], "not_ready")
        self.assertEqual((server.config.browser.artifacts / command["command_id"] / "seen.json").read_bytes(), witness)
        current = server.store.status().binding.model_dump(mode="json")
        pending = await self.http("POST", "/v1/browser/control", json={
            "command_id": str(uuid4()), "expected": current,
            "desired": {**current, "control_epoch": current["control_epoch"] + 1},
            "mode": "automation", "evidence": "Restart must retain unresolved effect"})
        self.assertEqual(pending.json()["outcome"], "pending")
        self.assertEqual(pending.json()["error"], "uncertain_effect")
        self.assertFalse(server.store.status().ready)
        # Only explicit settlement reopens admission; the original receipt remains.
        await server.reconcile(self.client, settled=(command["command_id"],))
        self.assertTrue(server.store.status().ready)
        self.assertEqual((await self.http("GET", "/v1/browser/commands/" + command["command_id"])).json(), failed.json())

    async def test_service_death_leaves_helper_lock_held_and_receipt_observation_inert(self):
        server = self.server
        await server.reconcile(self.client)
        command = server.command({"kind": "fill", "selector": "e1", "text": "HOLD"})
        call = asyncio.create_task(self.http("POST", "/v1/browser/command", json=command))
        directory = await server.wait_effect(command)
        witness = (directory / "seen.json").read_bytes()
        server.process.kill()
        server.process.join(3)
        with self.assertRaises(httpx.TransportError):
            await call
        server.stop()
        await server.start()
        # The surviving owned helper inherited the real lock. Startup could not
        # acquire it; receipt reads neither settle intent nor start a new effect.
        saved = await self.http("GET", "/v1/browser/commands/" + command["command_id"])
        self.assertEqual(saved.json()["outcome"], "intent")
        current = server.store.status().binding.model_dump(mode="json")
        desired = {**current, "control_epoch": current["control_epoch"] + 1}
        request = {"command_id": str(uuid4()), "expected": current, "desired": desired,
                   "mode": "human", "evidence": "Observe surviving synthetic helper"}
        take = asyncio.create_task(self.http("POST", "/v1/browser/control", json=request,
                headers=server.headers(server.actor_a)))
        for _ in range(200):
            if server.store.status().mode == "yielding":
                break
            await asyncio.sleep(.01)
        self.assertEqual(server.store.status().mode, "yielding")
        await asyncio.sleep(.1)
        self.assertFalse(take.done())
        self.assertFalse(server.store.status().ready)
        (directory / "release").touch()
        pending = await take
        self.assertEqual(pending.json()["outcome"], "pending")
        self.assertEqual(pending.json()["error"], "uncertain_effect")
        retried = await self.http("POST", "/v1/browser/command", json=command)
        self.assertEqual(retried.json()["outcome"], "uncertain")
        self.assertEqual((directory / "seen.json").read_bytes(), witness)
        await server.reconcile(self.client, settled=(command["command_id"],))


if __name__ == "__main__":
    unittest.main()
