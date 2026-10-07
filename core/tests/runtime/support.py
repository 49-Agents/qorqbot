"""Owned children and synthetic process observations for the host boundary."""

import subprocess
import sys
from uuid import uuid4

from agents.support import AgentCase
from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.contracts import canonical
from qorqut_core.runner.models import ProcessFingerprint
from qorqut_core.runner.process import fingerprint
from qorqut_core.runtime import OwnerCandidate, ProcessObservation, SessionOwners
from qorqut_core.storage.database import assert_external_io_allowed


class RuntimeCase(AgentCase):
    def setUp(self):
        super().setUp()
        self.host = next(row for row in self.rows("agents") if row["kind"] == "host")
        self.states = {}
        self.observed = []
        self.owners = SessionOwners(self.app, observe_process=self.observe)

    def observe(self, process):
        assert_external_io_allowed()
        self.observed.append(process)
        state = self.states.get(canonical(process), "live")
        if isinstance(state, Exception):
            raise state
        return ProcessObservation(state=state, observed_at=self.clock(), reason="fixture_" + state)

    def state(self, candidate, state):
        self.states[canonical(candidate.process)] = state

    def candidate(self, process=None, **changes):
        return OwnerCandidate(agent_id=self.host["id"], recovery_epoch=self.config()["recovery_epoch"],
            terminal_id=uuid4(), environment_generation=1, provider="codex", cli_version=VERSIONS["codex"],
            native_session_id=uuid4(), process=process or ProcessFingerprint(
                boot_id=uuid4(), pid=123, start_ticks=123, uid=1000, pid_namespace=123), **changes)

    def runtime(self):
        result = self.app.invoke("runtime_get", self.context, {"agent_id": self.host["id"]})
        self.assertTrue(result.ok, result)
        return result.data

    def session_events(self):
        return [row for row in self.rows("events") if row["kind"].startswith("session.")]

    def child(self):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.exit_child, child)
        return child, fingerprint(child.pid)

    @staticmethod
    def exit_child(child):
        if child.stdin and not child.stdin.closed:
            child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            raise
