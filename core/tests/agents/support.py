"""Reuse the foundation's temporary installation, clock and fault boundaries."""

import unittest
from uuid import UUID

from test_foundation import FoundationCase


class AgentCase(unittest.TestCase):
    setUp = FoundationCase.setUp
    tearDown = FoundationCase.tearDown
    restart = FoundationCase.restart
    config = FoundationCase.config
    counts = FoundationCase.counts
    recovery = FoundationCase.recovery
    update_defaults = FoundationCase.update

    def create(self, key="create", **changes):
        args = {"idempotency_key": key, "display_name": "Research", "cli": self.defaults.cli.model_dump(mode="json"), **changes}
        result = self.app.invoke("agents_create", self.context, args)
        self.assertTrue(result.ok, result)
        return result

    def get(self, agent_id):
        result = self.app.invoke("agents_get", self.context, {"agent_id": str(agent_id)})
        self.assertTrue(result.ok, result)
        return result.data

    def rows(self, table):
        with self.app.db.read() as tx:
            return [dict(row) for row in tx.connection.execute(f"SELECT * FROM {table}")]

    def events(self, agent_id, kind=None):
        return [row for row in self.rows("events") if row["agent_id"] == str(agent_id) and (kind is None or row["kind"] == kind)]

    def coordinates(self, result):
        agent_id = result.data["agent"]["id"]
        job_id = UUID(result.data["provisioning_job_id"])
        parent = self.settings.data_root / "agents"
        return agent_id, job_id, parent / agent_id, parent / (".qorqut-agent-stage-" + str(job_id))
