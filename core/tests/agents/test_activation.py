"""D62: an ordinary agent becomes active when its recorded shell is verified, never on a guess."""

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from agents.support import AgentCase
from qorqut_core.agents.models import AgentJob
from qorqut_core.agents.workspace import INITIAL_STATUS, SHELL_READY_STATUS
from qorqut_core.app import Application
from qorqut_core.config import LocalTerminalSettings
from qorqut_core.contracts import CoreError, Invocation
from test_foundation import InjectedCrash


class Activation(AgentCase):
    def setUp(self):
        super().setUp()
        self.app.close()
        self.local = LocalTerminalSettings(runtime_root=Path("/tmp") / uuid4().hex, python_executable=Path(sys.executable),
            tmux_executable=Path("/usr/bin/tmux"), bash_executable=Path("/usr/bin/bash"), path=os.defpath)
        self.settings = self.settings.model_copy(update={"data_root": self.root / "configured", "local_terminal": self.local})
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        self.shell = SimpleNamespace(status="available", reason="verified", terminal_id=uuid4())
        self.calls = []
        self.addCleanup(lambda: shutil.rmtree(self.local.runtime_root, ignore_errors=True))
        # The reserved agent's own provisioning would otherwise race the agent under test for the queue head.
        self.pump()
        self.calls.clear()

    def observe(self, agent_id=None, *, create=True):
        self.calls.append((str(agent_id), create))
        return self.shell

    def pump(self):
        """Drain claimable work with a stubbed observer: no tmux, no process of any kind."""
        with patch.object(self.app.terminals, "observe", self.observe), \
                patch("subprocess.Popen", side_effect=AssertionError("provider/process launch")):
            for _ in range(200):
                if self.app.jobs.run_next(worker_id=uuid4()) is None:
                    return
        self.fail("Owned queue did not settle within its test bound")

    def row(self, agent_id):
        return next(r for r in self.rows("agents") if r["id"] == str(agent_id))

    def jobs(self, agent_id, kind):
        return [r for r in self.rows("jobs") if r["kind"] == kind and r["target_id"] == str(agent_id)]

    def test_verified_shell_activates_once_with_attributed_event_status_and_projection(self):
        agent_id, job_id, final, _ = self.coordinates(self.create(persona="Keep me\n"))
        self.pump()
        job = self.app.jobs.get(job_id)
        self.assertEqual((job["state"], job["progress"]["phase"], job["progress"]["completed"]), ("completed", "shell_ready", 2))
        row = self.row(agent_id)
        self.assertEqual((row["lifecycle"], row["lifecycle_revision"], row["start_requested"], row["kind"]), ("active", 2, 0, "host"))
        self.assertEqual((row["status_text"], row["status_revision"]), (SHELL_READY_STATUS, 2))
        self.assertEqual(row["status_actor_id"], str(self.owner.actor_id))
        lifecycle = self.events(agent_id, "agent.lifecycle_changed")
        self.assertEqual(len(lifecycle), 1)
        self.assertEqual(lifecycle[0]["actor_id"], str(self.owner.actor_id))
        self.assertEqual(lifecycle[0]["operation_id"], row["creation_operation_id"])
        payload = json.loads(lifecycle[0]["payload_json"])
        self.assertEqual((payload["old_lifecycle"], payload["lifecycle"], payload["lifecycle_revision"],
                          payload["provisioning_job_id"], payload["terminal_id"], payload["reason"]),
                         ("provisioning", "active", 2, str(job_id), str(self.shell.terminal_id), "recorded_shell_verified"))
        self.assertEqual([json.loads(e["payload_json"])["new_text"] for e in self.events(agent_id, "agent.status_changed")],
                         [INITIAL_STATUS, SHELL_READY_STATUS])
        # Ready is a recorded shell, not a connected CLI: no start, no run, no owner.
        self.assertEqual(self.get(agent_id)["current_run"], None)
        self.assertEqual(self.rows("session_owners"), [])
        # Prepare, then only observe once more to publish the new coordinates.
        self.assertEqual([c for c in self.calls if c[0] == agent_id], [(agent_id, True), (agent_id, False)])
        # The queued projection follows the new lifecycle revision and keeps authored files.
        (projection,) = self.jobs(agent_id, "agent_project")
        self.assertEqual((projection["state"], projection["expected_generation"]), ("completed", 2))
        self.assertEqual(json.loads((final / "agent.json").read_text())["lifecycle_revision"], 2)
        self.assertEqual((final / "workspace/persona.md").read_bytes(), b"Keep me\n")
        # Reopen: nothing is repeated.
        counts = (len(self.events(agent_id)), len(self.rows("jobs")))
        self.restart()
        self.pump()
        self.assertEqual((len(self.events(agent_id)), len(self.rows("jobs"))), counts)
        self.assertEqual(self.row(agent_id)["lifecycle_revision"], 2)

    def test_authored_status_is_never_replaced(self):
        agent_id, job_id, *_ = self.coordinates(self.create())
        # The agent edits its own status before the shell is ready.
        written = self.app.invoke("agents_status_set", Invocation(actor_id=self.row(agent_id)["actor_id"]),
            {"idempotency_key": "own", "agent_id": agent_id, "expected_revision": 1, "text": "Reading the brief."})
        self.assertTrue(written.ok, written)
        self.pump()
        row = self.row(agent_id)
        self.assertEqual((row["lifecycle"], row["status_text"], row["status_revision"]), ("active", "Reading the brief.", 2))
        self.assertEqual(len(self.events(agent_id, "agent.status_changed")), 2)
        self.assertEqual(len(self.events(agent_id, "agent.lifecycle_changed")), 1)

    def test_unverified_shell_never_activates_and_the_observer_settles_it_later(self):
        agent_id, job_id, *_ = self.coordinates(self.create())
        for status, reason in (("stopped", "shell_exited"), ("unavailable", "binding_unconfirmed"),
                               ("unavailable", "terminal_unavailable"), ("unavailable", "target_changed"),
                               ("waiting_workspace", "workspace_not_initialized")):
            self.shell = SimpleNamespace(status=status, reason=reason, terminal_id=uuid4())
            with patch.object(self.app.terminals, "observe", self.observe):
                if self.app.jobs.get(job_id)["state"] == "queued":
                    self.pump()
                else:
                    self.app.jobs.reconcile(job_id)
            job = self.app.jobs.get(job_id)
            self.assertEqual((job["state"], job["wait_reason"], job["progress"]["phase"]),
                             ("waiting", "dependency_unavailable", "terminal_not_ready"), (status, reason))
            self.assertEqual(self.row(agent_id)["lifecycle"], "provisioning")
            self.assertEqual(self.events(agent_id, "agent.lifecycle_changed"), [])
            self.assertEqual(self.jobs(agent_id, "agent_project"), [])
            self.assertEqual(self.row(agent_id)["status_text"], INITIAL_STATUS)
        # Later the same recorded shell verifies; the typed inspection completes the same job.
        self.shell = SimpleNamespace(status="available", reason="verified", terminal_id=uuid4())
        with patch.object(self.app.terminals, "observe", self.observe):
            self.app.workspaces.settle(UUID(agent_id))
        self.assertEqual(self.app.jobs.get(job_id)["state"], "completed")
        self.assertEqual(self.row(agent_id)["lifecycle"], "active")
        self.assertEqual(len(self.events(agent_id, "agent.lifecycle_changed")), 1)
        # Inspection is observe-only: it never asks for a second shell.
        self.assertEqual(self.calls[-1], (agent_id, False))

    def test_crash_after_activation_commit_recovers_without_a_second_activation_or_shell(self):
        agent_id, job_id, *_ = self.coordinates(self.create())
        self.fault.point = "agent_after_activation_commit"
        with self.assertRaises(InjectedCrash), patch.object(self.app.terminals, "observe", self.observe):
            while self.app.jobs.run_next(worker_id=uuid4()) is not None:
                pass
        self.assertEqual(self.row(agent_id)["lifecycle"], "active")
        self.assertEqual(self.app.jobs.get(job_id)["state"], "running")
        self.restart()
        self.clock.advance(61)
        self.app.jobs.expire()
        self.assertEqual(self.app.jobs.get(job_id)["state"], "uncertain")
        created = [c for c in self.calls if c == (agent_id, True)]
        with patch.object(self.app.terminals, "observe", self.observe):
            self.app.jobs.reconcile(job_id)
        self.assertEqual(self.app.jobs.get(job_id)["state"], "completed")
        self.assertEqual([c for c in self.calls if c == (agent_id, True)], created)
        self.assertEqual((len(self.events(agent_id, "agent.lifecycle_changed")), self.row(agent_id)["lifecycle_revision"]), (1, 2))
        self.assertEqual(len(self.jobs(agent_id, "agent_project")), 1)
        self.pump()
        self.assertEqual(self.jobs(agent_id, "agent_project")[0]["state"], "completed")

    def test_rollback_before_activation_commit_leaves_provisioning(self):
        agent_id, job_id, *_ = self.coordinates(self.create())
        self.fault.point = "agent_before_activation_commit"
        with self.assertRaises(InjectedCrash), patch.object(self.app.terminals, "observe", self.observe):
            while self.app.jobs.run_next(worker_id=uuid4()) is not None:
                pass
        row = self.row(agent_id)
        self.assertEqual((row["lifecycle"], row["lifecycle_revision"], row["status_text"]), ("provisioning", 1, INITIAL_STATUS))
        self.assertEqual(self.events(agent_id, "agent.lifecycle_changed"), [])
        self.assertEqual(self.jobs(agent_id, "agent_project"), [])

    def test_recovery_hold_starts_no_new_effect_and_no_activation(self):
        agent_id, job_id, *_ = self.coordinates(self.create())
        with patch.object(self.app.terminals, "observe", self.observe):
            self.pump()
        self.assertEqual(self.row(agent_id)["lifecycle"], "active")
        held, held_job, *_ = self.coordinates(self.create("second", display_name="Held"))
        self.assertTrue(self.recovery().ok)
        # The real observer, with every process boundary armed: held work must not reach them.
        with patch("qorqut_core.runtime.terminals.ShellManager", side_effect=AssertionError("new shell effect")), \
                patch("subprocess.Popen", side_effect=AssertionError("process launch")):
            self.assertEqual(self.app.jobs.run_next(worker_id=uuid4()), None)
            for job in self.rows("jobs"):
                if job["job_id"] == str(held_job):
                    self.assertIn(job["state"], ("queued", "waiting", "uncertain"))
            self.app.terminals.observe_all()
        self.assertEqual(self.row(held)["lifecycle"], "provisioning")
        self.assertEqual(self.events(held, "agent.lifecycle_changed"), [])

    def test_unavailable_executable_is_honest_and_isolated(self):
        first, first_job, *_ = self.coordinates(self.create("first", display_name="First"))
        second, second_job, *_ = self.coordinates(self.create("second", display_name="Second"))
        broken = self.local.model_copy(update={"tmux_executable": self.root / "no-such-tmux"})
        self.app.terminals.settings = broken
        real = self.app.terminals.observe
        with patch("qorqut_core.runtime.terminals.ShellManager", side_effect=AssertionError("no resource before preflight")):
            for job in (first_job, second_job):
                # The provision handler runs the real observer, which refuses before it creates anything.
                self.app.jobs.run_next(worker_id=uuid4())
            view = real(UUID(first), create=True)
        self.assertEqual((view.status, view.reason), ("unavailable", "terminal_unavailable"))
        for agent_id in (first, second):
            self.assertEqual(self.row(agent_id)["lifecycle"], "provisioning")
            self.assertFalse((self.local.runtime_root / UUID(agent_id).hex).exists())

    def test_without_terminal_configuration_the_honest_dependency_hold_remains(self):
        self.app.close()
        plain = self.settings.model_copy(update={"data_root": self.root / "plain", "local_terminal": None})
        self.settings = plain
        self.app = Application.initialize(plain, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        agent_id, job_id, *_ = self.coordinates(self.create())
        while self.app.jobs.run_next(worker_id=uuid4()) is not None:
            pass
        job = self.app.jobs.get(job_id)
        self.assertEqual((job["state"], job["wait_reason"], job["progress"]["phase"]),
                         ("waiting", "dependency_unavailable", "runtime_integration_required"))
        self.assertEqual(self.row(agent_id)["lifecycle"], "provisioning")

    def test_projection_keeps_working_for_active_agents(self):
        agent_id, job_id, final, _ = self.coordinates(self.create())
        self.pump()
        revision = self.get(agent_id)["revision"]
        renamed = self.app.invoke("agents_update", self.context, {"idempotency_key": "rename", "agent_id": agent_id,
            "expected_revision": revision, "display_name": "Renamed"})
        self.assertTrue(renamed.ok, renamed)
        self.pump()
        projected = json.loads((final / "agent.json").read_text())
        self.assertEqual((projected["display_name"], projected["lifecycle_revision"]), ("Renamed", 2))
        self.assertEqual({j["state"] for j in self.jobs(agent_id, "agent_project")}, {"completed"})

    def test_a_stale_projection_queued_before_activation_still_projects_latest(self):
        agent_id, job_id, final, _ = self.coordinates(self.create())
        self.pump()
        operation = UUID(self.row(agent_id)["creation_operation_id"])
        with self.app.db.transaction() as tx:
            self.app.jobs.enqueue(tx, "agent_project", AgentJob(agent_id=UUID(agent_id), expected_lifecycle_revision=1),
                operation_id=operation, target_id=UUID(agent_id), expected_generation=1)
        self.pump()
        self.assertEqual(json.loads((final / "agent.json").read_text())["lifecycle_revision"], 2)
        self.assertEqual({j["state"] for j in self.jobs(agent_id, "agent_project")}, {"completed"})

    def test_one_agents_observer_failure_never_blocks_another_agents_activation(self):
        first, first_job, *_ = self.coordinates(self.create("first", display_name="First"))
        second, second_job, *_ = self.coordinates(self.create("second", display_name="Second"))
        self.shell = SimpleNamespace(status="unavailable", reason="terminal_unavailable", terminal_id=uuid4())
        self.pump()
        for job in (first_job, second_job):
            self.assertEqual(self.app.jobs.get(job)["wait_reason"], "dependency_unavailable")
        seen = []

        def observe(agent_id=None, *, create=True):
            seen.append(str(agent_id))
            if str(agent_id) == first:
                raise RuntimeError("synthetic failure for exactly one agent")
            return SimpleNamespace(status="available", reason="verified", terminal_id=uuid4())

        with patch.object(self.app.terminals, "observe", observe):
            # An unexpected error is still raised (it stops the observer and readiness) after the pass.
            with self.assertRaisesRegex(RuntimeError, "exactly one agent"):
                self.app.terminals.observe_all()
        # Every eligible local agent was visited (finite snapshot, sorted), including the failing one.
        self.assertTrue({first, second} <= set(seen))
        self.assertEqual(self.row(first)["lifecycle"], "provisioning")
        self.assertEqual(self.row(second)["lifecycle"], "active")
        self.assertEqual(self.app.jobs.get(second_job)["state"], "completed")
        self.assertEqual(self.app.jobs.get(first_job)["state"], "waiting")
