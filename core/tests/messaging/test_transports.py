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
            expected = {"messages_send", "messages_list", "messages_acknowledge", "deliveries_get", "conversation_list"}
            self.assertTrue(expected <= set(tools))
            self.assertFalse({"assistant_write", "deliveries_resolve", "history_list"} & set(tools))
            for name in expected:
                self.assertEqual(tools[name].inputSchema, schemas[name]["input_schema"])
                self.assertEqual(schemas[name]["output_data_schema"], fixture.registry[name].output.model_json_schema())
            arguments = {"idempotency_key": "cross-transport", "recipient_agent_id": agent["id"], "body": "  Exact 文\n\t"}
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
        fixture.stop()
        fixture.start()
        restarted = fixture.post("conversation_list", {"agent_id": agent["id"]}).json()
        self.assertEqual(len(restarted["data"]["items"]), 2)
        self.assertEqual(restarted["data"]["items"][0]["handling"]["handled_by"], str(actor.actor_id))
