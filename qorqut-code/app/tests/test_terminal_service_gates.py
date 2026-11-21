"""Terminals mode is the installation's setting: every terminal entry point refuses on a managed installation and while a switch
runs, before the broker or the database is touched, and constructing the service starts nothing. ``start_agent`` (the call the
installation-wide switch makes per agent) resumes the selected conversation or does the first launch, with fake brokers only."""

import asyncio
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.managed_runtime import mode, options
from qorqut.transport.agent_terminal_control import AgentTerminalBackend
from qorqut.transport.terminals import TerminalService

OFF = "Agents run as managed sessions on this installation."
SHELL = {"state": "present", "server_pid": "10", "session_id": "$1", "pane_id": "%1", "pane_pid": "4242", "command": "bash",
         "foreground_pid": 4242, "foreground_started": "1000", "client": "claude", "selection_revision": None}
CLIENT = {**SHELL, "command": "claude"}
ABSENT = {"state": "absent", "client": "claude", "selection_revision": None}


class Tripwire:
    """A broker that fails the test when anything at all is asked of it."""

    def __getattr__(self, name):
        raise AssertionError("the broker was touched: " + name)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="terminal gates ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.paths = SimpleNamespace(runtime=self.base / "runtime")
        self.paths.runtime.mkdir()

    def terminals(self):
        patched = mock.patch.object(mode, "current", return_value="terminal")
        patched.start()
        self.addCleanup(patched.stop)


class ServiceGateTest(Fixture):
    def service(self, broker=None):
        runtime = SimpleNamespace(paths=self.paths, workspace=self.base)
        return TerminalService(runtime, broker or Tripwire(), service_url="http://127.0.0.1:1")

    def refused(self, call, code):
        with self.assertRaises(DomainError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def entry_points(self, service):
        context = SimpleNamespace(project_id=None)
        request = SimpleNamespace(method="GET", headers={}, query_params={})
        return {
            "open": lambda: service.open(context, None, "ordubashy"),
            "open_workspace": lambda: service.open_workspace(context),
            "reconnect_selected": lambda: service.reconnect_selected(context, None, "ordubashy", None, "1:claude:x"),
            "route": lambda: asyncio.run(service.route(request, context, None, ["terminals"])),
            "route_agent": lambda: asyncio.run(service.route(request, context, None, ["terminal", "ordubashy", "stream"])),
        }

    def test_every_entry_point_refuses_on_a_managed_installation(self):
        service = self.service()
        for name, call in self.entry_points(service).items():
            with self.subTest(name):
                error = self.refused(call, "terminal_mode_off")
                self.assertEqual(error.message, OFF)

    def test_every_entry_point_refuses_while_a_switch_runs_and_it_is_retryable(self):
        self.terminals()
        service = self.service()
        with mock.patch.object(mode, "switching", return_value=True):
            for name, call in self.entry_points(service).items():
                with self.subTest(name):
                    error = self.refused(call, "runtime_switching")
                    self.assertTrue(error.retryable)
                    self.assertEqual(error.message, "Agents are being switched. Try again in a minute.")

    def test_a_managed_installation_has_nothing_to_reset_and_receives_no_broadcast(self):
        service = self.service()
        self.assertIsNone(service.reset_project_for_archive({"slug": "x"}, [{"agent_id": "a"}]))
        self.assertEqual(service.input_existing(None, "ordubashy", "hello", key="k")["state"], "unavailable")

    def test_the_reset_is_silent_without_tmux_too(self):
        self.terminals()
        broker = SimpleNamespace(tmux_available=lambda: False)
        self.assertIsNone(self.service(broker).reset_project_for_archive({"slug": "x"}, [{"agent_id": "a"}]))

    def test_constructing_the_service_starts_no_process_and_writes_no_file(self):
        before = sorted(self.base.rglob("*"))
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a process was started")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("a process was run")):
            self.service()
        self.assertEqual(sorted(self.base.rglob("*")), before)


class Broker:
    def __init__(self):
        self.tmux_calls, self.typed = [], []

    def tmux(self, *args):
        self.tmux_calls.append(args)
        return 0, ""

    def submit_input(self, session, text):
        self.typed.append(text)


class StartAgentTest(Fixture):
    def backend(self, snapshots, *, profile=None, resume=None, ready=None):
        self.workspace = self.base / "agents" / "ordubashy"
        self.workspace.mkdir(parents=True)
        profile = profile or {"agent_id": "ordubashy", "home_project_id": None, "status": "active",
                              "profile_json": json.dumps({"runtime": "claude-code", "role": {"id": "ordubashy"}})}
        self.broker, self.opened, self.resumed = Broker(), [], []

        class Rows:
            def fetchone(self_inner):
                return profile

        @contextmanager
        def read():
            yield SimpleNamespace(connection=SimpleNamespace(execute=lambda *args: Rows()),
                                  repository=lambda name: SimpleNamespace(get=lambda key: {"owner_principal_id": "owner"}))

        def resume_selected(agent, authorized):
            self.resumed.append((agent, authorized))
            return resume
        runtime = SimpleNamespace(
            database=SimpleNamespace(read=read), paths=self.paths, policy=SimpleNamespace(context_for=lambda *a, **k: "context"),
            native_chat=SimpleNamespace(arbiter=SimpleNamespace(locks=lambda agent: nullcontext()),
                                        attachment=SimpleNamespace(resume_selected=resume_selected)))
        terminals = SimpleNamespace(runtime=runtime, open=lambda context, project, agent: self.opened.append(agent))
        backend = AgentTerminalBackend(terminals, timeout=1, sleep=lambda seconds: None, ready=ready or (lambda client: True))
        backend.selected = lambda row: (self.broker, "key", "session", self.workspace, None)
        seen = list(snapshots)
        backend.snapshot = lambda row, client=None: seen.pop(0) if len(seen) > 1 else seen[0]
        backend.opened_snapshot = lambda row, attempts=50: SHELL
        backend.wait_ready = lambda row, client="claude": None
        for target, value in (("qorqut.process_info.cwd", lambda pid: self.workspace), ("qorqut.agent_trust.ensure", mock.Mock())):
            patched = mock.patch(target, value)
            patched.start()
            self.addCleanup(patched.stop)
        return backend

    def test_a_managed_installation_starts_nothing(self):
        backend = self.backend([ABSENT])
        with self.assertRaises(DomainError) as raised:
            backend.start_agent("ordubashy")
        self.assertEqual(raised.exception.code, "terminal_mode_off")
        self.assertEqual((self.opened, self.broker.tmux_calls, self.broker.typed), ([], [], []))

    def test_the_selected_conversation_is_resumed_and_the_role_is_not_sent(self):
        self.terminals()
        backend = self.backend([ABSENT, SHELL], resume="Claude Code resume of the selected conversation submitted")
        self.assertEqual(backend.start_agent("ordubashy"), {"agent_id": "ordubashy", "resumed": True})
        self.assertEqual((self.opened, self.resumed), (["ordubashy"], [("ordubashy", None)]))
        self.assertEqual(self.broker.typed, [])

    def test_nothing_selected_launches_afresh_with_the_stored_model_and_effort_and_the_role(self):
        self.terminals()
        options.write(self.paths, "ordubashy", model="sonnet", effort="high")
        backend = self.backend([ABSENT, SHELL, CLIENT])
        self.assertEqual(backend.start_agent("ordubashy"), {"agent_id": "ordubashy", "resumed": False})
        self.assertEqual(self.broker.typed, ["QORQBOT_ROLE_SENT=1 claude --model sonnet --effort high", "/role"])
        self.assertIn(("send-keys", "-t", "%1", "C-u"), self.broker.tmux_calls)

    def test_a_first_launch_without_options_types_the_bare_client(self):
        self.terminals()
        backend = self.backend([ABSENT, SHELL, CLIENT])
        backend.start_agent("ordubashy")
        self.assertEqual(self.broker.typed, ["QORQBOT_ROLE_SENT=1 claude", "/role"])

    def test_a_terminal_already_running_the_client_is_refused_in_plain_words(self):
        self.terminals()
        backend = self.backend([CLIENT])
        with self.assertRaises(DomainError) as raised:
            backend.start_agent("ordubashy")
        self.assertEqual(raised.exception.code, "invalid_transition")
        self.assertEqual((self.opened, self.broker.typed), ([], []))

    CODEX_PROFILE = {"agent_id": "ordubashy", "home_project_id": None, "status": "active",
                     "profile_json": json.dumps({"runtime": "codex", "role": {"id": "ordubashy"}})}

    def test_a_profile_that_says_codex_starts_claude_when_settings_say_claude_code(self):
        self.terminals()
        options.write(self.paths, "ordubashy", client="claude")
        backend = self.backend([ABSENT, SHELL, CLIENT], profile=self.CODEX_PROFILE, ready=lambda client: False)
        self.assertEqual(backend.start_agent("ordubashy"), {"agent_id": "ordubashy", "resumed": False})
        self.assertEqual(self.broker.typed, ["QORQBOT_ROLE_SENT=1 claude", "/role"])

    def test_with_no_settings_choice_the_effective_client_function_decides(self):
        # Profile says Codex, nothing stored, only Claude Code ready: options.effective_client answers claude.
        self.terminals()
        ready = lambda client: client == "claude"
        self.assertEqual(options.effective_client(self.paths, "ordubashy", self.CODEX_PROFILE, ready), "claude")
        backend = self.backend([ABSENT, SHELL, CLIENT], profile=self.CODEX_PROFILE, ready=ready)
        self.assertEqual(backend.terminal_client(self.CODEX_PROFILE), "claude")
        self.assertEqual(backend.start_agent("ordubashy")["resumed"], False)
        self.assertEqual(self.broker.typed[0], "QORQBOT_ROLE_SENT=1 claude")

    def test_with_no_settings_choice_and_codex_ready_the_agent_is_refused(self):
        self.terminals()
        ready = lambda client: client == "codex"
        self.assertEqual(options.effective_client(self.paths, "ordubashy", self.CODEX_PROFILE, ready), "codex")
        backend = self.backend([ABSENT, SHELL], profile=self.CODEX_PROFILE, ready=ready)
        with self.assertRaises(DomainError) as raised:
            backend.start_agent("ordubashy")
        self.assertEqual((raised.exception.code, raised.exception.message),
                         ("unsupported_runtime", "Only Claude Code agents can run in terminals."))
        self.assertEqual((self.opened, self.resumed, self.broker.typed), ([], [], []))

    def test_an_effective_client_of_codex_is_refused_even_when_the_profile_says_claude(self):
        self.terminals()
        options.write(self.paths, "ordubashy", client="codex")
        backend = self.backend([ABSENT, SHELL], ready=lambda client: True)
        with self.assertRaises(DomainError) as raised:
            backend.start_agent("ordubashy")
        self.assertEqual(raised.exception.message, "Only Claude Code agents can run in terminals.")
        self.assertEqual((self.opened, self.broker.typed), ([], []))

    def test_the_selected_client_follows_the_same_rule(self):
        self.terminals()
        options.write(self.paths, "ordubashy", client="codex")
        backend = self.backend([ABSENT], ready=lambda client: True)
        self.assertIsNone(backend.terminal_client({"agent_id": "ordubashy", "profile_json": "{}"}))
        self.assertIsNone(backend.selected_client({"agent_id": "ordubashy", "profile_json": "{}"}))

    def test_the_readiness_answer_is_kept_for_a_short_time(self):
        self.terminals()
        asked = []
        backend = self.backend([ABSENT], ready=lambda client: asked.append(client) or True)
        for _ in range(3):
            backend.terminal_client(self.CODEX_PROFILE)
        self.assertEqual(len(asked), 2)   # one status read: both clients, once

    def test_an_unknown_agent_is_refused(self):
        self.terminals()
        backend = self.backend([ABSENT], profile=None)
        backend.runtime.database.read = contextmanager(lambda: iter([SimpleNamespace(
            connection=SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: None)))]))
        with self.assertRaises(DomainError) as raised:
            backend.start_agent("nobody")
        self.assertEqual(raised.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
