"""Recorded platform messages reach dated files through the real running headless server."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
import unittest
from contextlib import AsyncExitStack, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.contracts import Actor, Invocation
from transports.support import Installation, write_private
from transports.test_transports import session, structured

CURRENT = {"pending_entries": 0, "state": "current", "error": None}
EQUAL_TEXT = "  Same owner text — 文字 🙂\nsecond line\t end\n"
RELAY_TEXT = "Agent to agent\n\t“exact” ünïcode 🙂\n"
ONBASH_TEXT = "Owner to onbash\n"
CRASH_TEXT = "Saved before the publisher crash\n文 🙂\t"
FAULT_POINT = "conversation_after_attach_commit"


@contextmanager
def application(fixture):
    app = Application.open(fixture.settings)
    try:
        yield app
    finally:
        app.close()


def register(fixture, app, *names):
    """Real application operations, as used during synthetic setup."""
    owner = Invocation(actor_id=fixture.owner.actor_id)
    created = [app.invoke("agents_create", owner, {"idempotency_key": "create-" + name, "display_name": name,
                                                   "cli": fixture.defaults.cli.model_dump(mode="json")}) for name in names]
    assert all(result.ok for result in created), created
    return [result.data["agent"] for result in created]


def bind(fixture, agents):
    """Credentials for the actors actually registered above."""
    actors = [Actor(actor_id=agent["actor_id"], kind="agent", agent_id=agent["id"]) for agent in agents]
    for actor in actors:
        fixture.tokens[actor.actor_id] = "synthetic-" + uuid4().hex + uuid4().hex
    write_private(fixture.credentials_file, json.dumps({"actors": [
        {"actor_id": str(key), "token": value} for key, value in fixture.tokens.items()]}))
    return actors


async def settled(observe, wanted, timeout=60):
    """Bounded polling; callers still assert the exact final observation."""
    deadline = time.monotonic() + timeout
    while True:
        observed = observe()
        if inspect.isawaitable(observed):
            observed = await observed
        if observed == wanted or time.monotonic() >= deadline:
            return observed
        await asyncio.sleep(0.1)


async def sign_in(stack, fixture):
    client = await stack.enter_async_context(httpx.AsyncClient(
        base_url=fixture.url, trust_env=False, timeout=10, headers={"Origin": fixture.url}))
    signed = await client.post("/api/v1/session", json={"token": fixture.tokens[fixture.owner.actor_id]})
    assert signed.status_code == 200 and signed.json()["actor_id"] == str(fixture.owner.actor_id)
    return client


async def conversations(client, agent_ids):
    return {agent_id: (await client.post("/api/v1/operations/conversation_list", json={"agent_id": agent_id})).json()["data"]
            for agent_id in agent_ids}


async def publications(client, agent_ids):
    return {agent_id: page["publication"] for agent_id, page in (await conversations(client, agent_ids)).items()}


def with_current(pages):
    return {agent_id: {**page, "publication": CURRENT} for agent_id, page in pages.items()}


def expected_files(pages):
    """Recording-date partitions of the saved immutable entries, as transcript lines."""
    days = {}
    for agent_id, page in pages.items():
        for item in page["items"]:
            entry = {key: value for key, value in item.items() if key not in ("delivery", "handling")}
            days.setdefault((agent_id, entry["recorded_at"][:10]), []).append({"format_version": 1, **entry})
    return days


def transcript_path(fixture, agent_id, date=None):
    directory = fixture.settings.data_root / "agents" / agent_id / "transcripts"
    return directory if date is None else directory / f"{date}.jsonl"


def read_files(fixture, keys):
    observed = {}
    for agent_id, date in keys:
        try:
            data = transcript_path(fixture, agent_id, date).read_bytes()
        except FileNotFoundError:
            observed[agent_id, date] = None
        else:
            observed[agent_id, date] = [json.loads(line) for line in data.split(b"\n")[:-1]]
    return observed


class ChatPublication(unittest.IsolatedAsyncioTestCase):
    def check_files(self, fixture, expected):
        """Exact LF framing and no stray names beside the recording-date files."""
        for (agent_id, date), lines in expected.items():
            data = transcript_path(fixture, agent_id, date).read_bytes()
            self.assertTrue(data.endswith(b"\n"))
            self.assertEqual(data.count(b"\n"), len(lines))
        for agent_id in {agent_id for agent_id, _ in expected}:
            self.assertEqual(sorted(os.listdir(transcript_path(fixture, agent_id))),
                             sorted(f"{date}.jsonl" for owner, date in expected if owner == agent_id))

    async def test_running_server_publishes_recorded_messages_and_restarts_unchanged(self):
        f = Installation()
        self.addCleanup(f.close)
        with application(f) as app:
            alpha, beta = register(f, app, "alpha", "beta")
            onbash = next(a for a in app.invoke("agents_list", Invocation(actor_id=f.owner.actor_id), {}).data["items"]
                          if a["kind"] == "host")
        alpha_actor, _ = bind(f, [alpha, beta])
        ids = [alpha["id"], beta["id"], onbash["id"]]
        f.start()  # Normal entry point, no UI root; server startup and its worker provision the folders.
        for agent_id in ids:
            def provisioned():
                view = f.post("agents_get", {"agent_id": agent_id}).json()["data"]
                return view["workspace_initialized_at"] is not None, view["lifecycle"]
            self.assertEqual(await settled(provisioned, (True, "provisioning"), 30), (True, "provisioning"))
            self.assertTrue((f.settings.data_root / "agents" / agent_id / "workspace/persona.md").is_file())

        owner_id, alpha_actor_id = str(f.owner.actor_id), str(alpha_actor.actor_id)
        async with AsyncExitStack() as stack:
            browser = await sign_in(stack, f)

            async def send(key, recipient, body):
                response = await browser.post("/api/v1/operations/messages_send", json={
                    "idempotency_key": key, "recipient_agent_id": recipient, "body": body})
                self.assertEqual(response.status_code, 200)
                return response.json()

            first, second = await send("owner-1", alpha["id"], EQUAL_TEXT), await send("owner-2", alpha["id"], EQUAL_TEXT)
            self.assertEqual(await send("owner-1", alpha["id"], EQUAL_TEXT), first)
            agent = await session(stack, f, alpha_actor)
            relay = structured(await agent.call_tool("messages_send", {
                "idempotency_key": "agent-relay", "recipient_agent_id": beta["id"], "body": RELAY_TEXT}))
            direct = await send("owner-onbash", onbash["id"], ONBASH_TEXT)

            for result, sender, sender_agent, recipient, body, targets in (
                    (first, owner_id, None, alpha["id"], EQUAL_TEXT, [alpha["id"]]),
                    (second, owner_id, None, alpha["id"], EQUAL_TEXT, [alpha["id"]]),
                    (relay, alpha_actor_id, alpha["id"], beta["id"], RELAY_TEXT, [alpha["id"], beta["id"]]),
                    (direct, owner_id, None, onbash["id"], ONBASH_TEXT, [onbash["id"]])):
                self.assertEqual(result["outcome"], "accepted")
                message = result["data"]["message"]
                self.assertEqual((message["sender_actor_id"], message["sender_agent_id"],
                                  message["recipient_agent_id"], message["body"]), (sender, sender_agent, recipient, body))
                self.assertEqual([e["agent_id"] for e in result["data"]["entries"]], targets)
            sent = [r["data"]["message"]["message_id"] for r in (first, second, relay, direct)]
            self.assertEqual(len(set(sent)), 4)

            pages = await conversations(browser, ids)
            shape = lambda page: [(i["conversation_seq"], i["role"], i["direction"], i["sender_actor_id"],
                                   i["source"]["message_id"], i["text"]) for i in page["items"]]
            self.assertEqual(shape(pages[alpha["id"]]), [
                (1, "user", "incoming", owner_id, sent[0], EQUAL_TEXT),
                (2, "user", "incoming", owner_id, sent[1], EQUAL_TEXT),
                (3, "agent", "outgoing", alpha_actor_id, sent[2], RELAY_TEXT)])
            self.assertEqual(shape(pages[beta["id"]]), [(1, "agent", "incoming", alpha_actor_id, sent[2], RELAY_TEXT)])
            self.assertEqual(shape(pages[onbash["id"]]), [(1, "user", "incoming", owner_id, sent[3], ONBASH_TEXT)])
            items = [item for page in pages.values() for item in page["items"]]
            self.assertEqual(len({i["entry_id"] for i in items}), 5)
            self.assertEqual({(i["delivery"]["state"], i["delivery"]["reason"], i["handling"]) for i in items},
                             {("pending", "runtime_unavailable", None)})

        # All clients are closed. Only read-only filesystem observation can see the files appear.
        expected = expected_files(pages)
        self.assertEqual(await settled(lambda: read_files(f, expected), expected), expected)
        self.check_files(f, expected)
        saved = {key: transcript_path(f, *key).read_bytes() for key in expected}

        async with AsyncExitStack() as stack:
            browser = await sign_in(stack, f)
            current = {agent_id: CURRENT for agent_id in ids}
            self.assertEqual(await settled(lambda: publications(browser, ids), current), current)
            self.assertEqual(await conversations(browser, ids), with_current(pages))

        f.stop()
        f.start()
        async with AsyncExitStack() as stack:
            browser = await sign_in(stack, f)
            self.assertEqual(await conversations(browser, ids), with_current(pages))
            replay = await browser.post("/api/v1/operations/messages_send", json={
                "idempotency_key": "owner-1", "recipient_agent_id": alpha["id"], "body": EQUAL_TEXT})
            self.assertEqual(replay.json(), first)
            self.assertEqual(await conversations(browser, ids), with_current(pages))
        self.assertEqual(read_files(f, expected), expected)
        self.assertEqual({key: transcript_path(f, *key).read_bytes() for key in expected}, saved)
        self.check_files(f, expected)
        for agent_id in ids:  # Published files do not make a VM ready.
            self.assertEqual(f.post("agents_get", {"agent_id": agent_id}).json()["data"]["lifecycle"], "provisioning")

    async def test_restart_after_receipt_attachment_finalizes_without_new_message(self):
        f = Installation()
        self.addCleanup(f.close)
        owner = Invocation(actor_id=f.owner.actor_id)
        with application(f) as app:
            (agent,) = register(f, app, "crash")
            sent = app.invoke("messages_send", owner, {"idempotency_key": "setup-message",
                                                        "recipient_agent_id": agent["id"], "body": CRASH_TEXT})
            self.assertTrue(sent.ok, sent)
        message, (entry,) = sent.data["message"], sent.data["entries"]

        f.start(crash_point=FAULT_POINT, claim_lease_seconds=1)
        f.crashed(FAULT_POINT)
        f.stop()

        def inspect_durable_state(receipt_id=None):
            with application(f) as app:
                with app.db.read() as tx:
                    publication = dict(tx.connection.execute(
                        "SELECT * FROM conversation_publications WHERE agent_id=?", (agent["id"],)).fetchone())
                    receipts = [r[0] for r in tx.connection.execute(
                        "SELECT receipt_id FROM file_receipts WHERE target_path LIKE ?", (f"agents/{agent['id']}/transcripts/%",))]
                jobs = app.invoke("operations_get", owner, {"operation_id": str(sent.operation_id)}).data["jobs"]
                receipt = app.files.get(UUID(receipt_id or publication["pending_receipt_id"]))
            return publication, receipts, {job["kind"]: job for job in jobs}, receipt

        # The child exited after durably attaching one receipt and before any install or finalization.
        publication, receipts, jobs, receipt = inspect_durable_state()
        receipt_id = publication["pending_receipt_id"]
        date = Path(receipt["target_path"]).stem
        self.assertEqual(receipts, [receipt_id])
        self.assertEqual((publication["through_seq"], publication["pending_target_seq"], publication["error_code"]), (0, 1, None))
        self.assertEqual((receipt["state"], receipt["target_path"]), ("staged", f"agents/{agent['id']}/transcripts/{date}.jsonl"))
        self.assertEqual((jobs["conversation_publish"]["state"], jobs["conversation_publish"]["attempt_count"]), ("running", 1))
        self.assertEqual((jobs["file_install"]["state"], jobs["file_install"]["args"]["receipt_id"]), ("queued", receipt_id))
        self.assertFalse(transcript_path(f, agent["id"], date).exists())

        f.start()  # Unmodified entry point and real clock; nothing below sends, runs or reconciles work.
        async with AsyncExitStack() as stack:
            browser = await sign_in(stack, f)
            # Staging progress renews the crashed claim with the production 30-second lease, so its expiry
            # may follow the crash by that long; the bound covers it plus the finite retry delays.
            current = {agent["id"]: CURRENT}
            self.assertEqual(await settled(lambda: publications(browser, [agent["id"]]), current, 90), current)
            pages = await conversations(browser, [agent["id"]])
        (item,) = pages[agent["id"]]["items"]
        self.assertEqual((item["entry_id"], item["conversation_seq"], item["source"]["message_id"], item["text"]),
                         (entry["entry_id"], 1, message["message_id"], CRASH_TEXT))
        self.assertEqual(item["recorded_at"][:10], date)
        expected = expected_files(pages)
        self.assertEqual(read_files(f, expected), expected)
        self.check_files(f, expected)
        self.assertEqual(httpx.get(f.url + "/health/ready", trust_env=False).json(), {"ready": True})
        self.assertEqual(len(f.post("messages_list", {"agent_id": agent["id"]}).json()["data"]["items"]), 1)
        installed = transcript_path(f, agent["id"], date).read_bytes()
        f.stop()

        final, receipts, finished, bound = inspect_durable_state(receipt_id)
        self.assertEqual(receipts, [receipt_id])
        self.assertEqual((final["through_seq"], final["pending_receipt_id"], final["pending_target_seq"], final["error_code"]),
                         (1, None, None, None))
        self.assertEqual((final["sha256"], final["length"]), (hashlib.sha256(installed).hexdigest(), len(installed)))
        self.assertEqual((bound["state"], bound["sha256"], bound["length"]), ("installed", final["sha256"], final["length"]))
        epoch = f.initial["data"]["recovery_epoch"]  # Normal restart: the original-epoch intent, not a restore.
        self.assertEqual({kind: (job["job_id"], job["state"], job["recovery_epoch"]) for kind, job in finished.items()
                          if kind != "message_delivery"},
                         {kind: (job["job_id"], "completed", epoch) for kind, job in jobs.items() if kind != "message_delivery"})
