import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from qorqut_browser.config import HandoffConfig
from wire_fixture import BRIDGE_TOKEN, TOKEN_B, BrowserServer, private_file


class ApplicationFixture:
    """A saved-reply fixture, not core's future durable request/control domain."""

    def __init__(self, root):
        self.modes = queue.SimpleQueue()
        self.received, self.saved = [], {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(raw)
                fixture.received.append((raw, self.headers["Authorization"], self.path))
                if self.headers["Authorization"] != "Bearer " + BRIDGE_TOKEN:
                    self.send_error(401)
                    return
                key = (payload["actor_id"], payload["idempotency_key"])
                if key not in fixture.saved:
                    receipt = {"protocol": 1, "actor_id": payload["actor_id"],
                        "target_agent_id": payload["target_agent_id"], "idempotency_key": payload["idempotency_key"],
                        "request_id": str(uuid4()), "conversation_id": str(uuid4()), "surface_id": str(uuid4()),
                        "state": "requested", "browser_binding": None}
                    fixture.saved[key] = receipt
                    # A response may be lost only after this fixture's save.
                    private_file(root / (receipt["request_id"] + ".json"), receipt)
                reply = copy.deepcopy(fixture.saved[key])
                mode = fixture.modes.get_nowait() if not fixture.modes.empty() else "ok"
                if mode == "loss":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status = 200
                if mode in {"actor_id", "target_agent_id"}:
                    reply[mode] = str(uuid4())
                elif mode == "idempotency_key":
                    reply[mode] = "different-key"
                elif mode == "ready_without_binding":
                    reply["state"] = "ready"
                elif mode == "extra":
                    reply["secret"] = BRIDGE_TOKEN
                elif mode == "wrong_binding":
                    reply["browser_binding"] = payload["observed_tab"]["binding"]
                    reply["browser_binding"]["identity"]["agent_id"] = str(uuid4())
                    reply["state"] = "ready"
                elif mode == "server_failure":
                    status = 503
                    reply = {"error": {"code": "unavailable", "message": BRIDGE_TOKEN}}
                elif mode == "rejection":
                    status = 409
                    reply = {"error": {"code": "idempotency_conflict", "message": BRIDGE_TOKEN}}
                elif mode == "redirect":
                    status = 307
                body = json.dumps(reply).encode()
                if mode == "oversize":
                    body = b" " * 65537
                if mode == "malformed":
                    body = b"{ " + BRIDGE_TOKEN.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + (100 if mode == "truncated" else 0)))
                if mode == "redirect":
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/handoff?version=1"

    def close(self):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()


class HandoffWireTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-handoff-r2-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.application = ApplicationFixture(self.root)
        self.addCleanup(self.application.close)
        token = private_file(self.root / "bridge.token", BRIDGE_TOKEN + "\n")
        self.server = BrowserServer(self.root, HandoffConfig(url=self.application.url, token_file=token))
        self.addCleanup(self.server.close)
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
                                     "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            await self.server.start()

    def intent(self):
        return {"idempotency_key": "same-key", "prompt": "  Please enter the synthetic test value.\n",
                "target_agent_id": str(self.server.config.browser.binding.identity.agent_id),
                "observed_tab": {"binding": self.server.store.status().binding.model_dump(mode="json"),
                                 "tab_id": "opaque-tab:17"}}

    async def test_response_loss_retry_preserves_original_actor_target_key_and_observation(self):
        server, fixture = self.server, self.application
        intent = self.intent()
        async with server.session(TOKEN_B) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "browser_request_input")
            self.assertEqual(set(tool.inputSchema["properties"]),
                             {"idempotency_key", "prompt", "target_agent_id", "observed_tab"})
            self.assertNotIn("iframe", tool.description)
            self.assertNotIn("actor_id", tool.inputSchema["properties"])
            fixture.modes.put("loss")
            lost = await session.call_tool("browser_request_input", intent)
            self.assertTrue(lost.isError)
            self.assertEqual(lost.structuredContent["error"]["code"], "uncertain_effect")
            self.assertEqual(len(fixture.received), 1)  # no automatic retry
            self.assertEqual(len(fixture.saved), 1)
            self.assertFalse(server.store.status().ready)  # forwarding did not reconcile
            async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                await server.reconcile(client)
            recovered = await session.call_tool("browser_request_input", intent)
            self.assertFalse(recovered.isError)
            receipt = recovered.structuredContent
            self.assertEqual(receipt["actor_id"], str(server.actor_b))
            self.assertEqual(receipt["target_agent_id"], intent["target_agent_id"])
            self.assertEqual(receipt["idempotency_key"], intent["idempotency_key"])
            self.assertEqual(len(fixture.saved), 1)
            self.assertEqual(fixture.received[0], fixture.received[1])
            transmitted = json.loads(fixture.received[0][0])
            self.assertEqual(transmitted, {**intent, "protocol": 1, "actor_id": str(server.actor_b)})
            self.assertEqual(fixture.received[0][1:], ("Bearer " + BRIDGE_TOKEN, "/handoff?version=1"))
            self.assertEqual(json.loads((self.root / (receipt["request_id"] + ".json")).read_text()), receipt)
            defaulted = await session.call_tool("browser_request_input", {"idempotency_key": "default-target", "prompt": "Act"})
            self.assertEqual(defaulted.structuredContent["target_agent_id"], str(server.agent_b))
            self.assertIsNone(json.loads(fixture.received[-1][0])["observed_tab"])
            bad = await session.call_tool("browser_request_input", {**intent, "actor_id": str(server.actor_a)})
            self.assertTrue(bad.isError)
            self.assertEqual(len(fixture.received), 3)

    async def test_malformed_mismatched_and_ambiguous_replies_remain_uncertain(self):
        fixture = self.application
        async with self.server.session() as session:
            for mode in ("malformed", "actor_id", "target_agent_id", "idempotency_key", "ready_without_binding",
                         "wrong_binding", "extra", "oversize", "truncated", "redirect", "server_failure"):
                with self.subTest(mode=mode):
                    fixture.modes.put(mode)
                    result = await session.call_tool("browser_request_input", self.intent())
                    self.assertTrue(result.isError)
                    self.assertEqual(result.structuredContent["error"]["code"], "uncertain_effect")
                    self.assertNotIn(BRIDGE_TOKEN, result.model_dump_json())
            self.assertEqual(len(fixture.received), 11)
            self.assertTrue(all(request[2] == "/handoff?version=1" for request in fixture.received))
            fixture.modes.put("rejection")
            result = await session.call_tool("browser_request_input", self.intent())
            self.assertEqual(result.structuredContent["error"]["code"], "idempotency_conflict")
            self.assertNotIn(BRIDGE_TOKEN, self.server.log.read_text())

    async def test_definite_connect_failure_is_unavailable_without_local_request(self):
        self.application.close()
        # Keep the exact destination port bound but unlistening for a definite refusal.
        refused = socket.socket()
        refused.bind(("127.0.0.1", self.application.server.server_port))
        self.addCleanup(refused.close)
        async with self.server.session() as session:
            result = await session.call_tool("browser_request_input", self.intent())
            self.assertEqual(result.structuredContent["error"]["code"], "unavailable")
        self.assertEqual(self.application.saved, {})
        with self.server.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM commands").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
