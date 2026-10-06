"""Reuse the owned temporary installation and foundation fault seams."""

from uuid import UUID, uuid4

from agents.support import AgentCase
from qorqut_core.contracts import Invocation


class ChatCase(AgentCase):
    def setUp(self):
        super().setUp()
        self.a = self.create("agent-a").data["agent"]
        self.b = self.create("agent-b").data["agent"]
        self.onbash = next(row for row in self.rows("agents") if row["kind"] == "host")
        self.pump()

    def pump(self):
        for _ in range(1000):
            if self.app.jobs.run_next(worker_id=uuid4()) is not None:
                continue
            retry = [r for r in self.rows("jobs") if r["state"] == "waiting" and r["wait_reason"] == "retry_delay"]
            if not retry:
                return
            self.clock.advance(5)
        self.fail("Owned queue did not settle within its test bound")

    def recover(self):
        self.restart()
        self.clock.advance(61)
        self.app.jobs.expire()
        unresolved = [r for r in self.rows("jobs") if r["state"] == "uncertain"]
        for job in sorted(unresolved, key=lambda row: row["kind"] != "file_install"):
            self.app.jobs.reconcile(UUID(job["job_id"]))
        self.pump()

    def send(self, key="send", body="Exact message\n", *, recipient=None, sender=None, **changes):
        result = self.app.invoke("messages_send", sender or self.context,
            {"idempotency_key": key, "recipient_agent_id": (recipient or self.a)["id"], "body": body, **changes})
        self.assertTrue(result.ok, result)
        return result

    def actor(self, agent):
        return Invocation(actor_id=agent["actor_id"])

    def conversation(self, agent=None, **arguments):
        result = self.app.invoke("conversation_list", self.context, {"agent_id": (agent or self.a)["id"], **arguments})
        self.assertTrue(result.ok, result)
        return result.data

    def messages(self, *, caller=None, **arguments):
        result = self.app.invoke("messages_list", caller or self.context, arguments)
        self.assertTrue(result.ok, result)
        return result.data

    def ack(self, ids, key="ack", caller=None, **changes):
        return self.app.invoke("messages_acknowledge", caller or self.context,
            {"idempotency_key": key, "message_ids": ids, **changes})

    def path(self, agent=None, date=None):
        return self.settings.data_root / "agents" / (agent or self.a)["id"] / "transcripts" / ((date or self.clock()[:10]) + ".jsonl")
