"""Focused real HTTP/official-SDK acceptance plus bounded transport failure checks."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from qorqut_core.app import Application, RecoveryActivation
from qorqut_core.contracts import CoreError, Invocation, OperationResult
from qorqut_core.runtime.native_hooks import NativeHookResult
from qorqut_core.transports.configuration import (
    MAX_INPUT_BYTES, InputError, read_configuration, read_credentials, read_file, read_token,
)
from qorqut_core.transports.operator import MAX_RESPONSE_BYTES, call
from qorqut_core.transports.boundary import Boundary
from qorqut_core.transports.mcp import install_logging_boundary
from qorqut_core.transports.server import ADMISSION_TIMEOUT, Dispatcher, MAX_CALLS, MAX_WAITERS, create_app

from transports.support import Installation, write_private


async def session(stack, fixture, actor=None, client=None):
    if client is None:
        client = await stack.enter_async_context(httpx.AsyncClient(headers=fixture.headers(actor), trust_env=False, timeout=5))
    reader, writer, session_id = await stack.enter_async_context(streamable_http_client(fixture.url + "/mcp", http_client=client))
    sdk = await stack.enter_async_context(ClientSession(reader, writer))
    initialized = await sdk.initialize()
    assert initialized.capabilities.tools is not None
    assert initialized.capabilities.resources is None
    assert session_id() is None
    return sdk


def structured(result):
    assert len(result.content) == 1 and result.content[0].type == "text"
    assert json.loads(result.content[0].text) == result.structuredContent
    assert result.isError == (not result.structuredContent["ok"])
    return result.structuredContent


class SharedFlow(unittest.IsolatedAsyncioTestCase):
    async def test_http_sdk_parity_identity_replay_revisions_events_recovery(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start()
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(base_url=f.url, headers=f.headers(), trust_env=False, timeout=5))
            owner, agent = await session(stack, f), await session(stack, f, f.agent)
            schema = (await http.get("/api/v1/schema")).json()
            self.assertEqual(schema["api_version"], 1)
            self.assertEqual(schema["result_schema"], OperationResult.model_json_schema())
            owner_tools, agent_tools = await owner.list_tools(), await agent.list_tools()
            self.assertEqual(owner_tools, agent_tools)
            tools = {t.name: t for t in owner_tools.tools}
            self.assertEqual(set(tools), set(f.registry))
            for operation in schema["operations"]:
                descriptor = f.registry[operation["name"]]
                self.assertEqual(operation["input_schema"], descriptor.arguments.model_json_schema())
                self.assertEqual(operation["output_data_schema"], descriptor.output.model_json_schema())
                self.assertEqual(operation["mutation"], descriptor.mutation)
                self.assertEqual(tools[descriptor.name].inputSchema, operation["input_schema"])
                self.assertEqual(tools[descriptor.name].outputSchema, schema["result_schema"])
            http_get, mcp_get = await asyncio.gather(http.post("/api/v1/operations/config_get", json={}), agent.call_tool("config_get", {}))
            self.assertEqual(http_get.json(), f.initial)
            self.assertEqual(structured(mcp_get), f.initial)

            owner_args = f.update(model="  first  ")
            response = await http.post("/api/v1/operations/config_update", json=owner_args)
            self.assertEqual(response.status_code, 200)
            first = response.json()
            self.assertEqual(first["data"]["revision"], 2)
            self.assertEqual(first["outcome"], "completed")
            owner_args["defaults"]["cli"]["model"] = "first"
            self.assertEqual(structured(await owner.call_tool("config_update", owner_args)), first)
            conflict = f.update(model="different")
            hconflict = await http.post("/api/v1/operations/config_update", json=conflict)
            self.assertEqual(hconflict.status_code, 409)
            self.assertEqual(hconflict.json()["error"]["code"], "idempotency_conflict")
            self.assertEqual(structured(await owner.call_tool("config_update", conflict)), hconflict.json())
            other_actor = structured(await agent.call_tool("config_update", owner_args))
            self.assertEqual(other_actor["error"]["code"], "revision_conflict")
            agent_args = f.update(revision=2, model="second")
            second = structured(await agent.call_tool("config_update", agent_args))
            self.assertTrue(second["ok"])
            self.assertNotEqual(first["operation_id"], second["operation_id"])

            # Four concurrent requests, two actors and both transports. Identity is
            # carried by each request, including reused SDK client connections.
            replay = await asyncio.gather(
                owner.call_tool("config_update", owner_args), agent.call_tool("config_update", agent_args),
                http.post("/api/v1/operations/config_update", json=owner_args),
                http.post("/api/v1/operations/config_update", json=agent_args, headers=f.headers(f.agent)))
            self.assertEqual([structured(replay[0]), structured(replay[1]), replay[2].json(), replay[3].json()],
                             [first, second, first, second])
            shared_client = await stack.enter_async_context(httpx.AsyncClient(headers=f.headers(), trust_env=False, timeout=5))
            shared_sdk = await session(stack, f, client=shared_client)
            # Even a client initialized under the owner must attribute its next
            # request to the newly supplied token, not its initialization identity.
            shared_client.headers.update(f.headers(f.agent))
            self.assertEqual(structured(await shared_sdk.call_tool("config_update", agent_args)), second)
            shared_client.headers.update(f.headers())
            self.assertEqual(structured(await shared_sdk.call_tool("config_update", owner_args)), first)
            for expected, actor in ((first, f.owner), (second, f.agent)):
                args = {"operation_id": expected["operation_id"]}
                lookup = structured(await agent.call_tool("operations_get", args))
                self.assertEqual((await http.post("/api/v1/operations/operations_get", json=args)).json(), lookup)
                self.assertEqual(lookup["data"]["actor_id"], str(actor.actor_id))
                self.assertEqual(lookup["data"]["original_result"], expected)
                self.assertEqual(lookup["data"]["jobs"], [])

            invalid = f.update(key="invalid")
            invalid["actor_id"] = str(f.agent.actor_id)
            invalid["defaults"]["resources"]["cpu"] = True
            hinvalid = await http.post("/api/v1/operations/config_update", json=invalid)
            self.assertEqual(hinvalid.status_code, 400)
            self.assertEqual(hinvalid.json()["error"]["code"], "invalid_argument")
            self.assertEqual(structured(await owner.call_tool("config_update", invalid)), hinvalid.json())
            stale = f.update(key="stale", revision=1)
            hstale = await http.post("/api/v1/operations/config_update", json=stale)
            self.assertEqual(hstale.status_code, 409)
            self.assertEqual(structured(await owner.call_tool("config_update", stale)), hstale.json())
            unknown = await http.post("/api/v1/operations/not_implemented", json={})
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(structured(await agent.call_tool("not_implemented", {})), unknown.json())

            first_page = structured(await owner.call_tool("events_list", {"limit": 1}))["data"]
            self.assertTrue(first_page["has_more"])
            events = first_page["items"][:]
            cursor = {"after_seq": first_page["next_after_seq"], "recovery_epoch": first_page["recovery_epoch"], "limit": 1}
            saved_cursor = dict(cursor)
            while True:
                hpage = (await http.post("/api/v1/operations/events_list", json=cursor)).json()
                self.assertEqual(structured(await agent.call_tool("events_list", cursor)), hpage)
                page = hpage["data"]
                events += page["items"]
                cursor["after_seq"] = page["next_after_seq"]
                if not page["has_more"]:
                    break
            self.assertEqual(events[:len(f.initial_events)], f.initial_events)
            changes = events[len(f.initial_events):]
            self.assertEqual([event["kind"] for event in changes], ["config.updated", "config.updated"])
            self.assertEqual([event["actor_id"] for event in changes], [str(f.owner.actor_id), str(f.agent.actor_id)])
            missing_epoch = await http.post("/api/v1/operations/events_list", json={"after_seq": 1})
            self.assertEqual(missing_epoch.status_code, 400)

        f.stop()
        maintenance = Application.open(f.settings)
        try:
            activation = maintenance.activate_recovery(Invocation(actor_id=f.owner.actor_id), RecoveryActivation(
                idempotency_key="test-recovery", expected_installation_id=f.settings.installation_id,
                expected_recovery_epoch=f.initial["data"]["recovery_epoch"]))
            self.assertTrue(activation.ok)
        finally:
            maintenance.close()
        f.start()
        async with AsyncExitStack() as stack:
            sdk = await session(stack, f)
            wrong_epoch = f.post("events_list", saved_cursor)
            self.assertEqual(wrong_epoch.status_code, 409)
            self.assertEqual(wrong_epoch.json()["error"]["code"], "target_changed")
            self.assertEqual(structured(await sdk.call_tool("events_list", saved_cursor)), wrong_epoch.json())
            self.assertEqual(structured(await sdk.call_tool("config_update", owner_args)), first)
            self.assertEqual(f.post("config_get").json()["data"]["revision"], 3)


class HTTPBoundary(unittest.TestCase):
    def setUp(self):
        self.f = Installation()
        self.addCleanup(self.f.close)
        self.f.start()

    def test_auth_host_origin_override_unknown_versions_and_private_protocol_errors(self):
        f = self.f
        paths = ["/api/v1/schema", "/api/v1/operations/config_get", "/mcp"]
        with httpx.Client(base_url=f.url, trust_env=False, timeout=5) as client:
            for path in paths:
                for headers in ({}, {"Authorization": "Bearer invalid"}, {"Cookie": "token=" + f.tokens[f.owner.actor_id]}):
                    response = client.request("GET" if path.endswith("schema") else "POST", path, headers=headers, json={})
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
                    self.assertNotIn("operation_id", response.json())
                for extra, status in (({"Host": "malicious.invalid"}, 421), ({"Origin": "http://malicious.invalid"}, 403),
                                      ({"Origin": f.url + "/"}, 403), ({"X-Qorqut-Actor": str(f.agent.actor_id)}, 400)):
                    response = client.request("GET" if path.endswith("schema") else "POST", path,
                                              headers={**f.headers(), **extra}, json={})
                    self.assertEqual(response.status_code, status)
                    self.assertNotIn("operation_id", response.json())
            for origin in (f.url, f.url.replace("127.0.0.1", "localhost")):
                response = client.get("/api/v1/schema", headers={**f.headers(), "Origin": origin})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("access-control-allow-origin", response.headers)
            response = client.get("/api/v2/schema", headers=f.headers())
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "unsupported_api_version")
            self.assertEqual(client.get("/api/v1/events", headers=f.headers()).status_code, 404)
            self.assertEqual(client.get("/api/v1/schema/", headers=f.headers()).status_code, 404)
            self.assertEqual(client.get("/api/v1/schema?actor_id=ignored", headers=f.headers()).status_code, 400)
            self.assertEqual(client.get("/mcp", headers=f.headers()).status_code, 405)
            self.assertEqual(client.get("/health/live").json(), {"live": True})
            self.assertEqual(client.get("/health/ready").json(), {"ready": True})
            # Official SDK still determines protocol failures. Its raw Pydantic
            # diagnostics must not echo submitted values to responses or logs.
            sentinel = "PRIVATE-SUBMITTED-CONTENT-" + f.tokens[f.owner.actor_id]
            headers = {**f.headers(), "Accept": "application/json, text/event-stream"}
            malformed = [
                {"jsonrpc": sentinel, "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "config_get", "arguments": [sentinel]}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "config_get", "arguments": sentinel}},
                {"jsonrpc": "2.0", "id": 3, "method": sentinel, "params": {}},
            ]
            for message in malformed:
                response = client.post("/mcp", json=message, headers=headers)
                self.assertIn("error", response.json())
                self.assertIn(response.json()["error"]["code"], {-32602, -32601})
                self.assertNotIn(sentinel, response.text)
                self.assertLess(len(response.content), 300)
            notification = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                "params": {"requestId": {}, "reason": sentinel}}, headers=headers)
            self.assertEqual(notification.status_code, 202)
            self.assertNotIn(sentinel, notification.text)
            protocol = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                   headers={**headers, "MCP-Protocol-Version": sentinel})
            self.assertEqual(protocol.status_code, 400)
            self.assertNotIn(sentinel, protocol.text)
            bad_accept = client.post("/mcp", json=malformed[0], headers={**headers, "Accept": "text/plain"})
            self.assertEqual(bad_accept.status_code, 406)
            # The notification response precedes SDK validation. A following
            # ordinary call and shutdown settle its task before inspecting the
            # complete child stdout/stderr capture below.
        self.assertEqual(f.post("config_get").json(), f.initial)
        self.assertEqual(f.post("events_list").json()["data"]["items"], f.initial_events)
        f.stop()
        captured = f.log_file.read_text()
        self.assertNotIn(sentinel, captured)
        self.assertGreaterEqual(captured.count("MCP transport diagnostic"), 4)
        self.assertIn("Application startup complete", captured)
        self.assertIn("Application shutdown complete", captured)
        for token in f.tokens.values():
            self.assertNotIn(token, captured)
        # Assembly installs once, preserving custom factory behavior and normal
        # unrelated diagnostics rather than globally disabling logging.
        prior = logging.getLogRecordFactory()
        def custom_factory(*args, **kwargs):
            record = logging.LogRecord(*args, **kwargs)
            record.custom_marker = "preserved"
            return record
        try:
            logging.setLogRecordFactory(custom_factory)
            create_app(*read_configuration(f.installation_file, f.transport_file))
            installed = logging.getLogRecordFactory()
            install_logging_boundary()
            self.assertIs(installed, logging.getLogRecordFactory())
            with self.assertLogs("qorqut.test.unrelated", level="WARNING") as logged:
                logging.getLogger("qorqut.test.unrelated").warning("Unrelated diagnostic %s", "preserved")
            self.assertEqual(logged.records[0].getMessage(), "Unrelated diagnostic preserved")
            self.assertEqual(logged.records[0].custom_marker, "preserved")
        finally:
            logging.setLogRecordFactory(prior)

    def test_declared_chunked_and_json_body_bounds_before_dispatch(self):
        f = self.f
        with httpx.Client(base_url=f.url, trust_env=False, timeout=5) as client:
            for path in ("/api/v1/operations/config_update", "/mcp"):
                headers = {**f.headers(), "Content-Type": "application/json", "Accept": "application/json"}
                over = client.post(path, content=b"x" * (MAX_INPUT_BYTES + 1), headers=headers)
                self.assertEqual(over.status_code, 413)
                chunks = (b"x" * 65536 for _ in range(17))
                over = client.post(path, content=chunks, headers=headers)
                self.assertEqual(over.status_code, 413)
                self.assertNotIn("operation_id", over.text)
            for content in (b"[]", b"null", b"{", b'{"a":1,"a":2}', b'{"value":NaN}'):
                response = client.post("/api/v1/operations/config_get", content=content,
                                       headers={**f.headers(), "Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("operation_id", response.json())
            exact = b"{}" + b" " * (MAX_INPUT_BYTES - 2)
            self.assertEqual(client.post("/api/v1/operations/config_get", content=exact,
                headers={**f.headers(), "Content-Type": "application/json"}).json(), f.initial)
        self.assertEqual(f.post("events_list").json()["data"]["items"], f.initial_events)

    def test_cli_http_path_exit_codes_proxy_isolation_and_restart_lock(self):
        f = self.f
        params = ["--url", f.url, "--token-file", f.token_files[f.owner.actor_id]]
        result = f.command("call", "config_get", *params, "--json", env={
            "HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), f.initial)
        arguments = write_private(f.root / "arguments.json", json.dumps(f.update()))
        result = f.command("call", "config_update", *params, "--arguments-file", arguments, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)
        self.assertEqual(saved, f.post("config_update", f.update()).json())
        result = f.command("call", "operations_get", *params, "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_argument")
        for invalid_url in (f.url + "?credential=" + f.tokens[f.owner.actor_id], "http://\u2603.invalid"):
            with self.subTest(invalid_url=invalid_url.split("?")[0]):
                result = f.command("call", "config_get", "--url", invalid_url,
                                   "--token-file", f.token_files[f.owner.actor_id])
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "An explicit HTTP/HTTPS origin and operation name are required\n")
                for private in ("Traceback", "\u2603.invalid", f.tokens[f.owner.actor_id], "may have occurred"):
                    self.assertNotIn(private, result.stderr)
                # Refusal must precede even opening a client, not just fail DNS.
                with patch("qorqut_core.transports.operator.httpx.Client",
                           side_effect=AssertionError("Local URL validation opened a client")) as client:
                    with self.assertRaises(InputError):
                        call(invalid_url, "config_get", read_token(f.token_files[f.owner.actor_id]), {})
                    client.assert_not_called()
        result = f.command("call", "config_get", *params, "--unexpected", f.tokens[f.owner.actor_id])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(f.tokens[f.owner.actor_id], result.stderr)
        wrong = write_private(f.root / "wrong.token", "x" * 40)
        result = f.command("call", "config_get", "--url", f.url, "--token-file", wrong)
        self.assertEqual(result.returncode, 1)
        self.assertIn("may have occurred", result.stderr)
        second = f.command("serve", "--installation", f.installation_file, "--transport-config", f.transport_file)
        self.assertEqual(second.returncode, 1)
        with self.assertRaises(CoreError):
            Application.open(f.settings)
        f.stop()
        after = Application.open(f.settings)
        try:
            self.assertEqual(after.invoke("config_update", Invocation(actor_id=f.owner.actor_id), f.update()).model_dump(mode="json"), saved)
        finally:
            after.close()
        f.start()
        self.assertEqual(f.post("config_get").json()["data"]["recovery_epoch"], f.initial["data"]["recovery_epoch"])
        for token in f.tokens.values():
            self.assertNotIn(token, f.log_file.read_text())


class Disconnect(unittest.IsolatedAsyncioTestCase):
    async def test_real_disconnect_before_commit_retries_historical_result_over_sdk(self):
        for protocol in ("http", "mcp"):
            with self.subTest(protocol=protocol):
                await self.disconnected_call(protocol)

    async def disconnected_call(self, protocol):
        f = Installation()
        self.addCleanup(f.close)
        f.start(gated=True)
        args = f.update(key="disconnect")
        if protocol == "mcp":
            data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "config_update", "arguments": args}}).encode()
            path = "/mcp"
        else:
            data, path = json.dumps(args).encode(), "/api/v1/operations/config_update"
        sock = socket.create_connection(("127.0.0.1", f.settings.listen_port), timeout=3)
        self.addCleanup(sock.close)
        head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{f.settings.listen_port}\r\n"
                f"Authorization: Bearer {f.tokens[f.owner.actor_id]}\r\nContent-Type: application/json\r\n"
                f"Accept: application/json, text/event-stream\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        sock.sendall(head + data)
        deadline = time.monotonic() + 5
        while not (f.root / "entered").exists():
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.01)
        sock.close()  # Outcome unknown: deliberately disconnect before commit.
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            self.assertEqual((await client.get(f.url + "/health/live")).status_code, 200)
        with self.assertRaises(CoreError):
            Application.open(f.settings)
        (f.root / "released").touch()
        async with AsyncExitStack() as stack:
            sdk = await session(stack, f)
            result = structured(await sdk.call_tool("config_update", args))
            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["revision"], 2)
            self.assertEqual(f.post("config_update", args).json(), result)
            self.assertEqual(len(f.post("events_list").json()["data"]["items"]), len(f.initial_events) + 1)
        f.stop()
        reopened = Application.open(f.settings)
        reopened.close()


class ExplicitInputs(unittest.TestCase):
    def setUp(self):
        self.f = Installation()
        self.addCleanup(self.f.close)

    def test_strict_private_bounded_credentials_and_configuration(self):
        f = self.f
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        self.assertEqual(settings, f.settings)
        self.assertEqual(credentials.actor(f.headers()["Authorization"].encode()), f.owner.actor_id)
        self.assertNotIn(f.tokens[f.owner.actor_id], repr(credentials))
        self.assertNotIn(f.tokens[f.owner.actor_id], repr(read_token(f.token_files[f.owner.actor_id])))
        original = json.loads(f.credentials_file.read_text())
        bad = [
            {**original, "unknown": True}, {"actors": [original["actors"][0]] * 2},
            {"actors": [original["actors"][0], {**original["actors"][1], "token": original["actors"][0]["token"]}]},
            {"actors": [original["actors"][0], {**original["actors"][1], "actor_id": original["actors"][0]["actor_id"]}]},
            {"actors": [{**original["actors"][0], "extra": "secret"}]},
            {"actors": [{**original["actors"][0], "token": "short"}]},
            {"actors": [{**original["actors"][0], "actor_id": "invalid"}]}, {"actors": []},
        ]
        for value in bad:
            write_private(f.credentials_file, json.dumps(value))
            with self.assertRaises(InputError) as raised:
                read_credentials(f.credentials_file, f.owner.actor_id)
            self.assertNotIn(f.tokens[f.owner.actor_id], str(raised.exception))
        write_private(f.credentials_file, json.dumps(original))
        with self.assertRaises(InputError):
            read_credentials(f.credentials_file, uuid4())
        f.credentials_file.chmod(0o640)
        with self.assertRaises(InputError):
            read_credentials(f.credentials_file, f.owner.actor_id)
        f.credentials_file.chmod(0o600)
        link = f.root / "credentials-link"
        link.symlink_to(f.credentials_file)
        linked_dir = f.root / "linked-directory"
        linked_dir.symlink_to(f.root, target_is_directory=True)
        fifo = f.root / "credentials-fifo"
        os.mkfifo(fifo, mode=0o600)
        for path in (link, linked_dir / "credentials.json", linked_dir / ".." / f.root.name / "credentials.json", f.root, fifo):
            with self.assertRaises(InputError):
                read_credentials(path, f.owner.actor_id)
        large = write_private(f.root / "large.json", " " * (MAX_INPUT_BYTES + 1))
        with self.assertRaises(InputError):
            read_file(large)
        for value in ({"credentials_file": "relative", "readiness_actor_id": str(f.owner.actor_id)},
                      {"credentials_file": str(f.credentials_file), "readiness_actor_id": str(f.owner.actor_id), "extra": True}):
            write_private(f.transport_file, json.dumps(value))
            with self.assertRaises(InputError):
                read_configuration(f.installation_file, f.transport_file)
        write_private(f.transport_file, json.dumps({"credentials_file": str(f.credentials_file), "readiness_actor_id": str(f.owner.actor_id)}))
        for address in ("0.0.0.0", "::1"):
            configured = json.loads(f.installation_file.read_text())
            configured["listen_address"] = address
            alternative = write_private(f.root / "alternative.json", json.dumps(configured))
            with self.assertRaises(InputError):
                read_configuration(alternative, f.transport_file)

    def test_startup_refuses_unregistered_bindings_and_does_not_initialize(self):
        f = self.f
        supplied = json.loads(f.credentials_file.read_text())
        supplied["actors"][1]["actor_id"] = str(uuid4())
        write_private(f.credentials_file, json.dumps(supplied))
        result = f.command("serve", "--installation", f.installation_file, "--transport-config", f.transport_file)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        reopened = Application.open(f.settings)
        try:
            self.assertEqual(reopened.invoke("config_get", Invocation(actor_id=f.owner.actor_id)).model_dump(mode="json"), f.initial)
        finally:
            reopened.close()
        supplied["actors"][1]["actor_id"] = str(f.agent.actor_id)
        write_private(f.credentials_file, json.dumps(supplied))
        configured = json.loads(f.installation_file.read_text())
        configured["data_root"] = str(f.root / "absent-data")
        absent = write_private(f.root / "uninitialized.json", json.dumps(configured))
        result = f.command("serve", "--installation", absent, "--transport-config", f.transport_file)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((f.root / "absent-data").exists())

    def test_imports_are_passive_and_packaged_notice_remains(self):
        f = self.f
        probe = r'''
import sys
sys.dont_write_bytecode = True
def audit(event, args):
    if event in {"sqlite3.connect", "subprocess.Popen", "os.system", "socket.__new__", "socket.bind", "os.mkdir"}:
        raise AssertionError("Unexpected import effect: " + event)
    if event == "open":
        path, mode, flags = args
        if str(path).startswith("/home/alp/qorqut/"):
            raise AssertionError("Upstream runtime read")
        if (mode and any(c in mode for c in "wax+")) or flags & 0x243:
            raise AssertionError("Import write")
sys.addaudithook(audit)
import qorqut_core
import qorqut_core.__main__
import qorqut_core.transports.server
import qorqut_core.transports.operator
import qorqut_core.agents.models
import qorqut_core.agents.registry
import qorqut_core.agents.workspace
import qorqut_core.adapters.capabilities
import qorqut_core.adapters.clients
import qorqut_core.adapters.composer
import qorqut_core.adapters.discovery
import qorqut_core.adapters.hooks
import qorqut_core.adapters.managed_io
import qorqut_core.adapters.preparation
import qorqut_core.adapters.types
import qorqut_core.runner.models
import qorqut_core.runner.process
import qorqut_core.runner.storage
import qorqut_core.runner.terminal
import qorqut_core.runner.tmux
import qorqut_core.transcripts.spool
import qorqut_core.transcripts.recorder
from importlib.resources import files
assert "MIT" in files("qorqut_core.storage").joinpath("NOTICE").read_text()
print("passive imports and NOTICE verified")
'''
        result = subprocess.run([sys.executable, "-c", probe], cwd=f.root, env=f.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verified", result.stdout)


class AdmissionAndLifetime(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.001)

    @asynccontextmanager
    async def blocked_dispatcher(self):
        """Synthetic call boundaries; no native hook or provider execution."""
        release, entered, tasks = threading.Event(), [], []

        def blocked(context, result):
            entered.append(context)
            if not release.wait(5):
                raise RuntimeError("Test gate timed out")
            return result

        application = SimpleNamespace(
            invoke=lambda name, context, arguments: blocked(context, OperationResult(ok=True, outcome="completed", data=arguments)),
            native_hooks=SimpleNamespace(receive=lambda context, request: blocked(context, request)))
        dispatcher = Dispatcher(application)
        try:
            yield dispatcher, release, entered, tasks
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await dispatcher.close()

    async def test_short_burst_waits_then_executes_each_invocation_and_hook_once(self):
        async with self.blocked_dispatcher() as (dispatcher, release, entered, tasks):
            hook = NativeHookResult.result("observed", uuid4(), 1)
            with patch.object(dispatcher.pool, "submit", wraps=dispatcher.pool.submit) as submitted:
                for number in range(MAX_CALLS + MAX_WAITERS):
                    call = (dispatcher.native_hook(number, hook) if number % 2 else
                            dispatcher.invoke("config_get", number, {"number": number}))
                    tasks.append(asyncio.create_task(call))
                await self.wait_until(lambda: len(entered) == MAX_CALLS and dispatcher.waiters == MAX_WAITERS)
                self.assertEqual(submitted.call_count, 4)
                self.assertEqual(dispatcher.waiters, 12)
                self.assertTrue(all(not task.done() for task in tasks))
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
                for number, result in enumerate(results):
                    if number % 2:
                        self.assertIs(result, hook)
                    else:
                        self.assertTrue(result.ok)
                        self.assertEqual(result.data, {"number": number})
                self.assertEqual(sorted(entered), list(range(MAX_CALLS + MAX_WAITERS)))
                self.assertEqual(submitted.call_count, len(tasks))
                self.assertEqual(dispatcher.waiters, 0)

    async def test_waiting_overflow_timeout_and_cancellation_never_execute(self):
        async with self.blocked_dispatcher() as (dispatcher, release, entered, tasks):
            hook = NativeHookResult.result("conflict")
            tasks.extend(asyncio.create_task(dispatcher.invoke("config_get", number, {}))
                         for number in range(MAX_CALLS))
            await self.wait_until(lambda: len(entered) == MAX_CALLS)
            started = time.monotonic()
            waiting = [asyncio.create_task(dispatcher.native_hook(number, hook) if number % 2 else
                       dispatcher.invoke("config_get", number, {}))
                       for number in range(MAX_CALLS, MAX_CALLS + MAX_WAITERS)]
            tasks.extend(waiting)
            await self.wait_until(lambda: dispatcher.waiters == MAX_WAITERS)
            # Overflow must resolve immediately, not consume another wait budget.
            rejected = await asyncio.wait_for(dispatcher.invoke("config_get", "overflow", {}), 0.2)
            self.assertFalse(rejected.ok)
            self.assertTrue(rejected.error.retryable)
            self.assertIn("not admitted", rejected.error.message)
            self.assertIsNone(rejected.operation_id)
            rejected_hook = await asyncio.wait_for(dispatcher.native_hook("overflow-hook", hook), 0.2)
            self.assertEqual(rejected_hook, NativeHookResult.result("unavailable"))
            for task in waiting[:2]:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(dispatcher.waiters, MAX_WAITERS - 2)
            replacements = [asyncio.create_task(dispatcher.invoke("config_get", "replacement", {})),
                            asyncio.create_task(dispatcher.native_hook("replacement-hook", hook))]
            tasks.extend(replacements)
            await self.wait_until(lambda: dispatcher.waiters == MAX_WAITERS)
            self.assertTrue(all(not task.done() for task in replacements))
            refused = await asyncio.wait_for(asyncio.gather(*waiting[2:], *replacements), 2)
            self.assertGreaterEqual(time.monotonic() - started, ADMISSION_TIMEOUT * 0.9)
            for result in refused:
                if isinstance(result, OperationResult):
                    self.assertFalse(result.ok)
                    self.assertTrue(result.error.retryable)
                    self.assertIsNone(result.operation_id)
                else:
                    self.assertEqual(result, NativeHookResult.result("unavailable"))
            self.assertEqual(dispatcher.waiters, 0)
            self.assertCountEqual(entered, range(MAX_CALLS))
            release.set()
            self.assertTrue(all(result.ok for result in await asyncio.gather(*tasks[:MAX_CALLS])))
            self.assertTrue((await dispatcher.invoke("config_get", "after-timeout", {})).ok)
            self.assertIs(await dispatcher.native_hook("after-timeout-hook", hook), hook)
            self.assertCountEqual(entered, [*range(MAX_CALLS), "after-timeout", "after-timeout-hook"])

    async def test_cancelled_startup_cannot_lose_application_ownership(self):
        f = Installation()
        self.addCleanup(f.close)
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        web = create_app(settings, credentials)
        opened = Application.open
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(settings):
            application = opened(settings)
            entered.set()
            release.wait(5)
            return application

        async def start():
            async with web.router.lifespan_context(web):
                self.fail("Cancelled startup must not begin serving")

        with patch.object(Application, "open", side_effect=blocked):
            task = asyncio.create_task(start())
            try:
                deadline = time.monotonic() + 3
                while not entered.is_set():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        application = opened(settings)
        application.close()

    async def test_cancelled_waiter_retains_slot_and_shutdown_drains_running_calls(self):
        f = Installation()
        self.addCleanup(f.close)
        application = Application.open(f.settings)
        self.addCleanup(application.close)
        dispatcher = Dispatcher(application)
        original = application.invoke
        release = threading.Event()
        self.addCleanup(release.set)
        entered = []
        hook = NativeHookResult.result("conflict")

        def enter():
            entered.append(True)
            if not release.wait(5):
                raise RuntimeError("Test gate timed out")

        def blocked(*args):
            enter()
            return original(*args)

        def blocked_hook(*args):
            enter()
            return hook

        tasks = []
        waiting = []
        closing = None
        try:
            with (patch.object(application, "invoke", side_effect=blocked),
                  patch.object(application.native_hooks, "receive", side_effect=blocked_hook) as receive):
                context = Invocation(actor_id=f.owner.actor_id)
                tasks = [asyncio.create_task(dispatcher.native_hook(context, None) if number % 2 else
                         dispatcher.invoke("config_get", context, {})) for number in range(MAX_CALLS)]
                deadline = time.monotonic() + 3
                while len(entered) < MAX_CALLS:
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.01)
                for task in tasks[:2]:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                waiting = [asyncio.create_task(dispatcher.invoke("config_get", context, {})),
                           asyncio.create_task(dispatcher.native_hook(context, None))]
                await self.wait_until(lambda: dispatcher.waiters == 2)
                self.assertTrue(all(not task.done() for task in waiting))
                closing = asyncio.create_task(dispatcher.close())
                rejected, rejected_hook = await asyncio.wait_for(asyncio.gather(*waiting), 0.5)
                self.assertFalse(rejected.ok)
                self.assertTrue(rejected.error.retryable)
                self.assertIn("not admitted", rejected.error.message)
                self.assertIsNone(rejected.operation_id)
                self.assertEqual(rejected_hook, NativeHookResult.result("unavailable"))
                self.assertFalse((await dispatcher.invoke("config_get", context, {})).ok)
                self.assertEqual(await dispatcher.native_hook(context, None), rejected_hook)
                self.assertEqual(receive.call_count, 2)
                await asyncio.sleep(0.02)
                self.assertFalse(closing.done())
                closing.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(closing.done())
                release.set()
                completed, completed_hook = await asyncio.gather(*tasks[2:])
                with self.assertRaises(asyncio.CancelledError):
                    await closing
                self.assertTrue(completed.ok)
                self.assertIs(completed_hook, hook)
                self.assertEqual(len(entered), MAX_CALLS)
                self.assertEqual(dispatcher.waiters, 0)
                self.assertEqual(len(dispatcher.pending), 0)
        finally:
            release.set()
            await asyncio.gather(*tasks, *waiting, *([closing] if closing else []), return_exceptions=True)
            await dispatcher.close()

    async def test_readiness_failure_is_minimal_and_lifespan_releases_lock(self):
        f = Installation()
        self.addCleanup(f.close)
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        web = create_app(settings, credentials)
        # Composition must not open the installation before explicit lifespan.
        application = Application.open(settings)
        application.close()
        async with web.router.lifespan_context(web):
            with self.assertRaises(CoreError):
                Application.open(settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as client:
                with patch.object(web.state.application, "invoke", side_effect=RuntimeError("SECRET-UNEXPECTED-ERROR")) as invoke:
                    response = await client.get("/health/ready")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"ready": False})
                    response = await client.post("/api/v1/operations/config_get", json={}, headers=f.headers())
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn("SECRET", response.text)
                    self.assertIn("may have occurred", response.text)
                    self.assertFalse(response.json()["error"]["retryable"])
                    self.assertEqual(invoke.call_count, 2)
                    self.assertEqual((await client.get("/health/live")).json(), {"live": True})
        application = Application.open(settings)
        application.close()

    async def test_framing_duplicates_slow_body_and_disconnect_never_dispatch(self):
        f = Installation()
        self.addCleanup(f.close)
        _, credentials = read_configuration(f.installation_file, f.transport_file)
        calls = []

        async def probe(scope, receive, send):
            calls.append(True)

        boundary = Boundary(probe, credentials=credentials, port=f.settings.listen_port)
        base_headers = [(b"host", f"127.0.0.1:{f.settings.listen_port}".encode()),
                        (b"authorization", f.headers()["Authorization"].encode())]

        async def exercise(headers, receive):
            sent = []
            async def send(message):
                sent.append(message)
            await boundary({"type": "http", "path": "/mcp", "headers": headers}, receive, send)
            return sent

        async def empty():
            return {"type": "http.request", "body": b""}

        for extra, status in (([(b"authorization", b"Bearer duplicate")], 401),
                              ([(b"content-length", b"0"), (b"content-length", b"0")], 400),
                              ([(b"content-length", b"0"), (b"transfer-encoding", b"chunked")], 400),
                              ([(b"content-length", b"-1")], 400)):
            self.assertEqual((await exercise(base_headers + extra, empty))[0]["status"], status)
        async def slow():
            await asyncio.sleep(1)
        with patch("qorqut_core.transports.boundary.BODY_TIMEOUT", 0.02):
            self.assertEqual((await exercise(base_headers, slow))[0]["status"], 408)
        async def disconnected():
            return {"type": "http.disconnect"}
        self.assertEqual(await exercise(base_headers, disconnected), [])
        self.assertEqual(calls, [])


@contextmanager
def operator_peer(mode, *, tls=None, body_gate=None):
    """Owned loopback failure peer, used only to test the operator's HTTP boundary."""
    calls = []
    accepted = OperationResult(ok=True, outcome="accepted", operation_id=uuid4(), data={}).model_dump(mode="json")
    if mode in {"declared_large", "streamed_large", "streamed_below"}:
        size = MAX_RESPONSE_BYTES + (-1 if mode == "streamed_below" else 1)
        accepted["data"]["padding"] = ""
        accepted["data"]["padding"] = "x" * (size - len(json.dumps(accepted).encode()))
    body = json.dumps(accepted).encode()
    if mode in {"declared_large", "streamed_large", "streamed_below"}:
        assert len(body) == size
    if mode == "declared_large":
        assert body_gate is not None
    gate_expired = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(5)
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(307 if mode == "redirect" else 200)
            if mode == "redirect":
                self.send_header("Location", "/api/v1/operations/config_update")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_header("Content-Type", "application/json")
            if mode in {"accepted", "declared_large"}:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if mode == "declared_large":
                    self.wfile.flush()
                    # Longer than command()'s 15-second failure guard: without
                    # the header check the command must time out, not see EOF.
                    if not body_gate.wait(30):
                        gate_expired.set()
                        return
                for offset in range(0, len(body), 65536):
                    self.wfile.write(body[offset:offset + 65536])
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    if tls is not None:
        server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_port}", calls, accepted
    finally:
        if body_gate is not None:
            body_gate.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not gate_expired.is_set(), "Declared-length peer released its body without client refusal"


class OperatorBoundary(unittest.TestCase):
    def setUp(self):
        self.f = Installation()
        self.addCleanup(self.f.close)

    def test_redirect_disconnect_and_declared_streamed_response_ceiling_no_retry(self):
        f = self.f
        arguments = write_private(f.root / "arguments.json", json.dumps(f.update()))
        for mode in ("redirect", "drop", "declared_large", "streamed_below", "streamed_large"):
            body_gate = threading.Event()
            with self.subTest(mode=mode), operator_peer(mode, body_gate=body_gate) as (url, calls, accepted):
                result = f.command("call", "config_update", "--url", url, "--token-file", f.token_files[f.owner.actor_id],
                                   "--arguments-file", arguments, "--json")
                self.assertEqual(len(calls), 1)
                if mode == "streamed_below":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), accepted)
                    self.assertEqual(result.stderr, "")
                    continue
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "No valid operation result received; execution may have occurred. "
                                 "Preserve the original idempotency key.\n")
                self.assertNotIn(f.tokens[f.owner.actor_id], result.stderr)
                self.assertEqual(result.stdout, "")
                if mode == "declared_large":
                    self.assertFalse(body_gate.is_set())

    def test_accepted_output_does_not_claim_completion(self):
        f = self.f
        with operator_peer("accepted") as (url, calls, accepted):
            params = ["call", "config_update", "--url", url, "--token-file", f.token_files[f.owner.actor_id]]
            result = f.command(*params)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, f"Accepted ({accepted['operation_id']})\n")
            result = f.command(*params, "--json")
            self.assertEqual(json.loads(result.stdout), accepted)
            self.assertEqual(len(calls), 2)

    def test_untrusted_https_certificate_and_environment_ca_are_not_accepted(self):
        import ssl
        from datetime import datetime, timedelta, timezone
        from ipaddress import ip_address
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        f = self.f
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "synthetic-loopback")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256()))
        cert_path = write_private(f.root / "test-cert.pem", cert.public_bytes(serialization.Encoding.PEM).decode())
        key_path = write_private(f.root / "test-key.pem", key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        with operator_peer("accepted", tls=context) as (url, calls, _):
            result = f.command("call", "config_get", "--url", url, "--token-file", f.token_files[f.owner.actor_id],
                               env={"SSL_CERT_FILE": str(cert_path)})
            self.assertEqual(result.returncode, 1)
            self.assertEqual(calls, [])
            self.assertNotIn(f.tokens[f.owner.actor_id], result.stderr)
