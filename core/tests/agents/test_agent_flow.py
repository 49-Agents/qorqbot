"""One shared real HTTP/official SDK flow for the five agent operations."""

from contextlib import AsyncExitStack
import unittest
from uuid import uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.contracts import OperationResult
from transports.support import Installation
from transports.test_transports import session, structured


class AgentTransportFlow(unittest.IsolatedAsyncioTestCase):
    async def test_shared_agent_http_mcp_flow_and_explicit_workspace_step(self):
        fixture = Installation()
        self.addCleanup(fixture.close)
        fixture.start()
        async with AsyncExitStack() as stack:
            http = await stack.enter_async_context(httpx.AsyncClient(base_url=fixture.url,
                headers=fixture.headers(), trust_env=False, timeout=5))
            owner = await session(stack, fixture)
            actor = await session(stack, fixture, fixture.agent)
            schemas = (await http.get("/api/v1/schema")).json()
            tools = {t.name: t for t in (await owner.list_tools()).tools}
            operations = {op["name"]: op for op in schemas["operations"]}
            self.assertEqual(set(tools), set(fixture.registry))
            self.assertEqual(len(tools), 14)
            for name in ("agents_create", "agents_get", "agents_list", "agents_update", "agents_status_set"):
                operation = fixture.registry[name]
                self.assertEqual(tools[name].inputSchema, operation.arguments.model_json_schema())
                self.assertEqual(tools[name].inputSchema, operations[name]["input_schema"])
                self.assertEqual(operations[name]["output_data_schema"], operation.output.model_json_schema())
                self.assertEqual(tools[name].outputSchema, OperationResult.model_json_schema())
            directory = (await http.post("/api/v1/operations/agents_list", json={})).json()
            self.assertEqual(structured(await actor.call_tool("agents_list", {})), directory)
            host = directory["data"]["items"][0]
            self.assertEqual(directory["data"]["onbash_agent_id"], host["id"])
            self.assertEqual((host["kind"], host["start_requested"], host["workspace_initialized_at"]), ("host", False, None))
            self.assertFalse((fixture.settings.data_root / "agents").exists())
            args = {"idempotency_key": "shared-create", "display_name": "\t Shared agent \n",
                "cli": fixture.defaults.cli.model_dump(mode="json"), "persona": "Exact synthetic seed\n", "start": True}
            response = await http.post("/api/v1/operations/agents_create", json=args)
            first = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(first["outcome"], "accepted")
            self.assertEqual(structured(await owner.call_tool("agents_create", args)), first)
            agent_id = first["data"]["agent"]["id"]
            get_args = {"agent_id": agent_id}
            get = (await http.post("/api/v1/operations/agents_get", json=get_args)).json()
            self.assertEqual(structured(await actor.call_tool("agents_get", get_args)), get)
            self.assertEqual(get["data"], first["data"]["agent"])
            conflict = {**args, "display_name": "Different"}
            response = await http.post("/api/v1/operations/agents_create", json=conflict)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
            self.assertEqual(structured(await owner.call_tool("agents_create", conflict)), response.json())
            status_args = {**get_args, "expected_revision": 1, "text": "\u2003Reviewing\n evidence", "idempotency_key": "status"}
            status = structured(await actor.call_tool("agents_status_set", status_args))
            self.assertEqual(status["outcome"], "completed")
            self.assertEqual(status["data"]["status"]["text"], "Reviewing evidence")
            self.assertEqual(status["data"]["status"]["actor_id"], str(fixture.agent.actor_id))
            self.assertEqual((await http.post("/api/v1/operations/agents_status_set", json=status_args,
                headers=fixture.headers(fixture.agent))).json(), status)
            update_args = {**get_args, "expected_revision": 1, "display_name": "Renamed", "icon_key": "book", "idempotency_key": "rename"}
            update = structured(await actor.call_tool("agents_update", update_args))
            self.assertEqual(update["outcome"], "accepted")
            self.assertEqual(update["data"]["revision"], 2)
            self.assertEqual(update["data"]["status"], status["data"]["status"])
            self.assertEqual((await http.post("/api/v1/operations/agents_update", json=update_args,
                headers=fixture.headers(fixture.agent))).json(), update)
            for name, invalid in (("agents_update", {**update_args, "expected_revision": True, "idempotency_key": "bad"}),
                                  ("agents_status_set", {**status_args, "expected_revision": "2", "idempotency_key": "bad"}),
                                  ("agents_create", {**args, "start": "true", "idempotency_key": "bad"})):
                bad = await http.post("/api/v1/operations/" + name, json=invalid)
                self.assertEqual(bad.status_code, 400)
                self.assertEqual(bad.json()["error"]["code"], "invalid_argument")
                self.assertEqual(structured(await owner.call_tool(name, invalid)), bad.json())
            stale = {**update_args, "idempotency_key": "stale"}
            response = await http.post("/api/v1/operations/agents_update", json=stale)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "revision_conflict")
            self.assertEqual(structured(await owner.call_tool("agents_update", stale)), response.json())
            wrong_epoch = {"recovery_epoch": str(uuid4())}
            response = await http.post("/api/v1/operations/agents_list", json=wrong_epoch)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(structured(await actor.call_tool("agents_list", wrong_epoch)), response.json())
            events = structured(await actor.call_tool("events_list", {}))["data"]["items"]
            target = [e for e in events if e["agent_id"] == agent_id]
            self.assertEqual([e["actor_id"] for e in target], [str(fixture.owner.actor_id)] * 2 + [str(fixture.agent.actor_id)] * 2)
            self.assertFalse((fixture.settings.data_root / "agents").exists())
        fixture.stop()
        # Only explicit in-process fixture work runs these real fixed jobs.
        application = Application.open(fixture.settings)
        try:
            executed = []
            while (job_id := application.jobs.run_next(worker_id=uuid4())) is not None:
                executed.append(application.jobs.get(job_id))
            self.assertEqual(len(executed), 3)
            self.assertEqual(sorted(j["wait_reason"] for j in executed if j["kind"] == "agent_provision"), ["dependency_unavailable"] * 2)
            self.assertEqual([j["state"] for j in executed if j["kind"] == "agent_project"], ["completed"])
        finally:
            application.close()
        fixture.start()
        async with AsyncExitStack() as stack:
            sdk = await session(stack, fixture)
            lookup_args = {"operation_id": first["operation_id"]}
            lookup = structured(await sdk.call_tool("operations_get", lookup_args))
            self.assertEqual(fixture.post("operations_get", lookup_args).json(), lookup)
            self.assertEqual(lookup["data"]["original_result"], first)
            self.assertEqual(lookup["data"]["jobs"][0]["state"], "waiting")
            self.assertEqual(lookup["data"]["jobs"][0]["wait_reason"], "dependency_unavailable")
            self.assertEqual(lookup["data"]["jobs"][0]["last_error"]["code"], "unsupported_capability")
            latest = structured(await sdk.call_tool("agents_get", get_args))["data"]
            self.assertEqual((latest["revision"], latest["projection_revision"]), (2, 2))
            self.assertIsNotNone(latest["workspace_initialized_at"])
            self.assertEqual((latest["lifecycle"], latest["activity"], latest["capture_health"]), ("provisioning", "unknown", "unknown"))
            self.assertTrue(latest["start_requested"])
            self.assertEqual(structured(await sdk.call_tool("agents_create", args)), first)
            directory = structured(await sdk.call_tool("agents_list", {}))["data"]
            self.assertEqual(directory["onbash_agent_id"], host["id"])
            self.assertEqual(len(directory["items"]), 2)
