"""Terminals mode: ``SessionProbe`` verifies the client in the agent's tmux pane and never asks the managed runtime."""
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.native_chat import probe as probe_module
from qorqut.native_chat.probe import (ProbeResult, REASON_LIVE, REASON_PROCESS_GONE, REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME,
                                      SessionProbe)

AGENT = "11111111-1111-4111-8111-111111111111"
CONVERSATION = "33333333-3333-4333-8333-333333333333"
CLIENT, SHELL, PANE = 4343, 4300, 4300


def profile(runtime="claude-code"):
    return {"agent_id": AGENT, "workspace": "/w", "profile_json": json.dumps({"runtime": runtime})}


def marker(runtime="claude-code", pid=CLIENT):
    return {"agent_id": AGENT, "session_id": CONVERSATION, "runtime": runtime, "pid": pid, "started": "777",
            "transcript_path": None, "capture": False}


def stat(state="S", parent=1, group=10, foreground=10, started="1"):
    fields = [state, str(parent), str(group), "0", "0", str(foreground)] + ["0"] * 14
    fields[19] = started
    return fields


class Broker:
    def __init__(self, panes="4300\n", code=0):
        self.panes, self.code, self.calls = panes, code, []

    def tmux(self, *args):
        self.calls.append(args)
        return self.code, self.panes


class Backend:
    def __init__(self, broker):
        self.broker = broker
        self.selected_calls = 0

    def selected(self, row):
        self.selected_calls += 1
        return self.broker, None, "agent-session", None, None


class TerminalProbeTests(unittest.TestCase):
    def setUp(self):
        self.table = {CLIENT: stat(parent=SHELL, started="777"), SHELL: stat(parent=1, foreground=10)}
        patcher = mock.patch.object(probe_module, "process_fields", lambda pid, terminal=False: self.table[pid])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.managed = mock.Mock()
        self.broker = Broker()
        self.backend = Backend(self.broker)

    def probe(self, *, terminal=True, mark=None, backend=True):
        return SessionProbe(marker_reader=lambda p: dict(mark or marker()), managed=self.managed,
                            terminal_backend=(lambda: self.backend) if backend else None, terminal_mode=lambda: terminal)

    def test_the_client_in_the_one_pane_is_live_as_a_terminal_and_the_host_is_never_asked(self):
        result = self.probe().observe(profile())
        self.assertEqual(result.reason, REASON_LIVE)
        self.assertEqual((result.observation.transport, result.observation.owner), ("terminal", None))
        self.assertEqual((result.observation.phase, result.observation.pending), (None, ()))
        self.assertEqual(self.managed.mock_calls, [])
        self.assertEqual(self.broker.calls, [("list-panes", "-t", "=agent-session", "-F", "#{pane_pid}")])

    def test_a_descendant_of_the_pane_counts_up_to_the_hop_limit(self):
        self.table[CLIENT] = stat(parent=5000, started="777")
        self.table[5000] = stat(parent=SHELL)
        self.assertEqual(self.probe().observe(profile()).reason, REASON_LIVE)

    def test_a_client_outside_the_pane_is_gone(self):
        self.table[CLIENT] = stat(parent=1, started="777")
        self.assertEqual(self.probe().observe(profile()).reason, REASON_PROCESS_GONE)

    def test_another_foreground_group_is_refused(self):
        self.table[CLIENT] = stat(parent=SHELL, group=99, started="777")
        self.assertEqual(self.probe().observe(profile()).reason, REASON_PROCESS_GONE)

    def test_more_than_one_pane_or_a_tmux_error_is_refused(self):
        for broker in (Broker("1\n2\n"), Broker("", 1)):
            self.backend = Backend(broker)
            self.assertEqual(self.probe().observe(profile()).reason, REASON_PROCESS_GONE)

    def test_recycled_process_and_zombie_are_refused(self):
        self.table[CLIENT] = stat(parent=SHELL, started="778")
        self.assertEqual(self.probe().observe(profile()).reason, REASON_PROCESS_GONE)
        self.table[CLIENT] = stat(state="Z", parent=SHELL, started="777")
        self.assertEqual(self.probe().observe(profile()).reason, REASON_PROCESS_GONE)

    def test_no_backend_is_no_session(self):
        self.assertEqual(self.probe(backend=False).observe(profile()).reason, REASON_PROCESS_GONE)

    def test_the_client_that_registered_decides_not_the_profile(self):
        # The owner can run an agent whose profile names Codex on Claude Code (Settings): it is a terminal agent like any other.
        self.assertEqual(self.probe().observe(profile("codex")).reason, REASON_LIVE)
        # A Codex client that registered has no terminal session, whatever the profile says.
        self.assertEqual(self.probe(mark=marker("codex")).observe(profile()).reason, REASON_UNSUPPORTED_RUNTIME)
        self.assertEqual(self.probe(mark=marker("codex")).observe(profile("codex")).reason, REASON_UNSUPPORTED_RUNTIME)
        self.assertEqual(self.backend.selected_calls, 1)      # only the Claude Code client reached the pane check

    def test_a_remote_target_is_native_only(self):
        from qorqut.transport.agent_terminal_control import NATIVE_ONLY
        self.backend.selected = mock.Mock(side_effect=DomainError("operation_unavailable", NATIVE_ONLY))
        self.assertEqual(self.probe().observe(profile()).reason, REASON_UNSUPPORTED)

    def test_managed_mode_never_touches_the_backend(self):
        self.managed.verify.return_value = None
        result = self.probe(terminal=False).observe(profile())
        self.assertEqual(result.reason, REASON_PROCESS_GONE)
        self.assertEqual((self.backend.selected_calls, self.broker.calls), (0, []))

    def test_the_mode_is_asked_on_every_observation(self):
        answers = iter([True, False])
        probe = SessionProbe(marker_reader=lambda p: marker(), managed=self.managed, terminal_backend=lambda: self.backend,
                             terminal_mode=lambda: next(answers))
        self.assertEqual(probe.observe(profile()).reason, REASON_LIVE)
        self.assertEqual(probe.observe(profile()).reason, REASON_PROCESS_GONE)

    def test_default_is_never_terminal(self):
        self.managed.verify.return_value = None
        self.assertEqual(SessionProbe(marker_reader=lambda p: marker(), managed=self.managed).observe(profile()).reason,
                         REASON_PROCESS_GONE)


if __name__ == "__main__":
    unittest.main()
