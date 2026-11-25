"""The host's Context composition tells every filing run the names of the installation's registered active agents."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from qorqut_context import Context
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import PolicyDecision, new_id, utc_now
from qorqut_operations.roles import GLOBAL_WORKER_ROLE, LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE
from qorqut_operations.storage.database import Database
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.shared_context.service import ContextSettings, agent_names_of, compose_context

LIST_START = "The installation's registered agents are named in this quoted list"


class Allow:
    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision("allow", "test")


class Runner:
    config = RunnerConfig()

    def __init__(self):
        self.instructions = []

    def run(self, *, stage, instructions, payload, directory, tools=None, **kwargs):
        if stage == "bootstrap":
            return RunResult("completed", json.dumps({"node_ids": []}), "thread", "turn", self.config.model)
        self.instructions.append(instructions)
        for name, args in (("stage_handoff_note", {"text": ""}),
                           ("finish_propagation", {"status": "complete", "summary": "Filed.", "reassessed": []})):
            result = tools.dispatch(name, args)
            if not result["ok"]:
                raise AssertionError(result)
        return RunResult("completed", "", "thread", "turn", self.config.model)


def profile(role):
    return json.dumps({"status": "active", "role": {"id": role}, "scope": {"kind": "ecosystem"}})


class HostAgentNames(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.db = Database(self.root / "state.sqlite", assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        now = utc_now()
        # (display name, profile name, principal active, profile status, role)
        agents = [("Nimbus", "nimbus", 1, "active", ORDUBASHY_ROLE),
                  ("Brightwell", "brightwell", 1, "active", GLOBAL_WORKER_ROLE),
                  ("Quillan Applier", "quillan", 1, "active", LEARNING_APPLIER_ROLE),
                  ("Dormant Fern", "fern", 0, "inactive", GLOBAL_WORKER_ROLE),
                  ("Retired Opal", "opal", 1, "retired", GLOBAL_WORKER_ROLE)]
        with self.db.transaction() as uow:
            uow.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES (?,?,?,?,?)",
                                   (new_id(), "human", "Synthetic Owner", now, now))
            for display, name, active, status, role in agents:
                identity = new_id()
                uow.connection.execute(
                    "INSERT INTO principals(id,kind,display_name,active,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (identity, "agent", display, active, now, now))
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id,principal_id,name,workspace,status,profile_json,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (identity, identity, name, name, status, profile(role), now, now))

    def test_the_lookup_lists_active_agents_only(self):
        with self.db.read() as uow:
            names = agent_names_of(uow)
        self.assertEqual({"brightwell", "Brightwell", "nimbus", "Nimbus", ORDUBASHY_ROLE,
                          "quillan", "Quillan Applier", LEARNING_APPLIER_ROLE}, set(names))
        self.assertEqual(8, len(names))
        self.assertNotIn(GLOBAL_WORKER_ROLE, names)
        for absent in ("Dormant Fern", "fern", "Retired Opal", "opal", "Synthetic Owner"):
            self.assertNotIn(absent, names)

    def test_a_filing_run_of_the_composed_host_service_receives_the_names(self):
        runner = Runner()
        paths = NS(context=self.root / "context", context_runtime=self.root / "runtime",
                   context_questions=self.root / "questions.sqlite", locks=self.root / "locks", operations=self.root / "operations")
        composed = compose_context(Application(self.db, OperationRegistry(), policy=Allow()), paths, ContextSettings(runner=runner))
        Context.initialize(paths.context, paths.context_runtime)
        receipt = composed.pipeline.submit("add", "foundation/notes/a.md", content=b"Met Dana about the launch.\n",
                                           metadata={"kind": "note", "title": "A note", "origin": "test"}, submitted_by="owner")
        self.assertEqual("published", receipt["status"])
        [instructions] = runner.instructions
        [line] = [line for line in instructions.splitlines() if line.startswith(LIST_START)]
        for name in ("Brightwell", "learning-applier", "Nimbus", "ordubashy", "Quillan Applier", "quillan"):
            self.assertIn(f'"{name}"', line)
        for absent in ("Fern", "fern", "Opal", "opal", "Synthetic Owner", GLOBAL_WORKER_ROLE):
            self.assertNotIn(absent, line)
        # Case-insensitive duplicates collapse to one entry each, in a stable order.
        self.assertEqual(1, line.casefold().count('"nimbus"'))
        self.assertLess(line.index('"learning-applier"'), line.index('"ordubashy"'))
        # A registration that changes is seen by the next run: the list is read at each run, never cached.
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=0 WHERE display_name='Nimbus'")
        composed.pipeline.submit("add", "foundation/notes/b.md", content=b"Another note.\n",
                                 metadata={"kind": "note", "title": "B", "origin": "test"}, submitted_by="owner")
        self.assertNotIn("Nimbus", runner.instructions[-1].split(LIST_START)[1])


if __name__ == "__main__":
    unittest.main()
