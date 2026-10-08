import asyncio
import json
import time
import unittest
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.contracts import Actor, Invocation
from transports.support import Installation, write_private
from transports.test_transports import session, structured


class ChatTransports(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_and_shared_http_mcp_send_list_ack_flow(self):
        fixture = Installation()
        self.addCleanup(fixture.close)
        app = Application.open(fixture.settings)
        try:
            created = app.invoke("agents_create", Invocation(actor_id=fixture.owner.actor_id), {
                "idempotency_key": "chat-agent", "display_name": "Chat fixture", "cli": fixture.defaults.cli.model_dump(mode="json")})
            self.assertTrue(created.ok)
            agent = created.data["agent"]
            while app.jobs.run_next(worker_id=uuid4()) is not None:
                pass
            source = fixture.settings.data_root / agent["workspace_path"] / "files/reference 文.txt"
            source.write_text("Synthetic artifact", encoding="utf-8")
            actor = Actor(actor_id=agent["actor_id"], kind="agent", agent_id=agent["id"])
            fixture.tokens[actor.actor_id] = "synthetic-" + uuid4().hex + uuid4().hex
            write_private(fixture.credentials_file, json.dumps({"actors": [
                {"actor_id": str(key), "token": value} for key, value in fixture.tokens.items()]}))
        finally:
            app.close()
        fixture.start()
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(base_url=fixture.url, headers=fixture.headers(), trust_env=False, timeout=10))
            owner_sdk = await session(stack, fixture)
            agent_sdk = await session(stack, fixture, actor)
            schemas = {operation["name"]: operation for operation in (await http.get("/api/v1/schema")).json()["operations"]}
            tools = {tool.name: tool for tool in (await agent_sdk.list_tools()).tools}
            expected = {"messages_send", "messages_list", "messages_acknowledge", "deliveries_get", "conversation_list", "files_reference"}
            self.assertTrue(expected <= set(tools))
            self.assertFalse({"assistant_write", "deliveries_resolve", "history_list"} & set(tools))
            for name in expected:
                self.assertEqual(tools[name].inputSchema, schemas[name]["input_schema"])
                self.assertEqual(schemas[name]["output_data_schema"], fixture.registry[name].output.model_json_schema())
            self.assertEqual(tools["files_reference"].description, fixture.registry["files_reference"].description)
            self.assertIn("metadata", tools["files_reference"].description)
            self.assertEqual(tools["messages_send"].description, "messages send")
            arguments = {"idempotency_key": "cross-transport", "recipient_agent_id": agent["id"], "body": "  Exact 文\n\t\x00\r\x1b\x7f\x85  "}
            responses = await asyncio.gather(
                http.post("/api/v1/operations/messages_send", json=arguments), owner_sdk.call_tool("messages_send", arguments))
            saved = responses[0].json()
            self.assertEqual(saved, structured(responses[1]))
            self.assertEqual(saved["outcome"], "accepted")
            self.assertEqual(saved["data"]["message"]["sender_actor_id"], str(fixture.owner.actor_id))
            self.assertIsNone(saved["data"]["message"]["sender_agent_id"])
            filters = {"agent_id": agent["id"]}
            # The running server publishes the saved entry concurrently. Compare
            # complete views only after its publication observation has settled.
            deadline = time.monotonic() + 20
            while (await http.post("/api/v1/operations/conversation_list", json=filters)).json()["data"]["publication"]["state"] != "current":
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.05)
            for name in ("messages_list", "conversation_list"):
                http_result = (await http.post("/api/v1/operations/" + name, json=filters)).json()
                self.assertEqual(http_result, structured(await agent_sdk.call_tool(name, filters)))
                self.assertEqual(len(http_result["data"]["items"]), 1)
                item = http_result["data"]["items"][0]
                self.assertEqual(item["message"]["body"] if name == "messages_list" else item["text"], arguments["body"])
            lookup = {"message_id": saved["data"]["message"]["message_id"]}
            exact = structured(await owner_sdk.call_tool("messages_list", lookup))
            self.assertEqual(exact, (await http.post("/api/v1/operations/messages_list", json=lookup)).json())
            self.assertEqual(exact["data"]["items"][0]["message"]["body"], arguments["body"])
            missing = {"message_id": str(uuid4())}
            absent = structured(await agent_sdk.call_tool("messages_list", missing))
            self.assertEqual(absent, (await http.post("/api/v1/operations/messages_list", json=missing)).json())
            self.assertEqual(absent["error"]["code"], "not_found")
            ack = {"idempotency_key": "ack", "message_ids": [saved["data"]["message"]["message_id"]], "note": "Handled by the actual actor"}
            handled = structured(await agent_sdk.call_tool("messages_acknowledge", ack))
            replay = (await http.post("/api/v1/operations/messages_acknowledge", json=ack, headers=fixture.headers(actor))).json()
            self.assertEqual(handled, replay)
            self.assertEqual(handled["data"]["items"][0]["handled_by"], str(actor.actor_id))
            delivery_args = {"message_id": saved["data"]["message"]["message_id"]}
            delivery = structured(await agent_sdk.call_tool("deliveries_get", delivery_args))
            self.assertEqual(delivery, (await http.post("/api/v1/operations/deliveries_get", json=delivery_args)).json())
            self.assertEqual(delivery["data"]["delivery"]["state"], "pending")
            self.assertEqual(delivery["data"]["attempts"], [])
            # Agent authorship comes from its authenticated registration, and a
            # self-send creates one entry and one message in its list.
            self_sent = structured(await agent_sdk.call_tool("messages_send", {**arguments, "idempotency_key": "agent-self"}))
            self.assertEqual(self_sent["data"]["message"]["sender_agent_id"], agent["id"])
            self.assertEqual(len(self_sent["data"]["entries"]), 1)
            self.assertEqual(len(structured(await agent_sdk.call_tool("messages_list", {}))["data"]["items"]), 2)
            reference_args = {"idempotency_key": "file-cross-transport", "agent_id": agent["id"],
                "path": "./files//reference 文.txt", "label": "  Artifact  ", "note": "metadata only\n"}
            reference_responses = await asyncio.gather(http.post("/api/v1/operations/files_reference", json=reference_args),
                owner_sdk.call_tool("files_reference", reference_args))
            referenced = reference_responses[0].json()
            self.assertEqual(referenced, structured(reference_responses[1]))
            self.assertTrue(referenced["ok"], referenced)
            self.assertEqual(referenced["outcome"], "completed")
            reference = referenced["data"]["reference"]
            self.assertEqual(reference["actor_id"], str(fixture.owner.actor_id))
            self.assertEqual(reference["path"], "files/reference 文.txt")
            self.assertEqual(reference["requested_path"], reference_args["path"])
            self.assertIsInstance(reference["mtime_ns"], str)
            self.assertIsNone(reference["sha256"])
            source.unlink()
            self.assertEqual(structured(await owner_sdk.call_tool("files_reference", reference_args)), referenced)
            conflict_args = {**reference_args, "label": "Different"}
            conflict = (await http.post("/api/v1/operations/files_reference", json=conflict_args)).json()
            self.assertEqual(conflict["error"]["code"], "idempotency_conflict")
            self.assertEqual(structured(await owner_sdk.call_tool("files_reference", conflict_args)), conflict)
        fixture.stop()
        fixture.start()
        restarted = fixture.post("conversation_list", {"agent_id": agent["id"]}).json()
        self.assertEqual(len(restarted["data"]["items"]), 3)
        self.assertEqual(restarted["data"]["items"][0]["handling"]["handled_by"], str(actor.actor_id))
        self.assertEqual(restarted["data"]["items"][-1]["source"], {"type": "file_reference", "reference": reference})
        self.assertEqual(fixture.post("files_reference", reference_args).json(), referenced)
        deadline = time.monotonic() + 20
        while restarted["data"]["publication"]["state"] != "current":
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.05)
            restarted = fixture.post("conversation_list", {"agent_id": agent["id"]}).json()
        path = fixture.settings.data_root / "agents" / agent["id"] / "transcripts" / (reference["recorded_at"][:10] + ".jsonl")
        published = [json.loads(line) for line in path.read_bytes().splitlines()]
        self.assertEqual(len(published), 3)
        self.assertEqual(published[-1]["format_version"], 1)
        self.assertEqual(published[-1]["source"], {"type": "file_reference", "reference": reference})
