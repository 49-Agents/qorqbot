"""The terminal side is composed into the service in every mode and costs the managed path nothing: composing starts no process,
loads no broker, writes no file and needs no tmux; the recovery host writers and the managed runtime's openers pick by the
installation's mode at call time. Fakes only; tmux is never run."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import QuiescenceProof
from qorqut.managed_runtime import manager as hosts
from qorqut.managed_runtime import mode
from qorqut.service import main as service_main
from qorqut.storage.host_writers import CommandResult, HostTerminalWriters, ManagedHostWriters, ModeHostWriters
from qorqut.transport.agent_terminal_control import AgentTerminalBackend
from qorqut.transport.terminals import TerminalService

DASHBOARD = Path(__file__).resolve().parents[2] / "biz-dashboard"
AGENT = "11111111-1111-4111-8111-111111111111"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="terminal wiring ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.paths = SimpleNamespace(runtime=self.base / "runtime")
        self.paths.runtime.mkdir()

    def terminals(self):
        patched = mock.patch.object(mode, "current", return_value="terminal")
        patched.start()
        self.addCleanup(patched.stop)

    def forbid_processes(self):
        """Any process, any tmux lookup: the test fails."""
        def fail(*args, **options):
            raise AssertionError("a process was started or tmux was looked for: " + repr(args))
        for target in ("subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.call", "shutil.which"):
            patched = mock.patch(target, side_effect=fail)
            patched.start()
            self.addCleanup(patched.stop)

    def files(self):
        return sorted(str(path) for path in self.base.rglob("*"))


class CompositionTest(Fixture):
    def compose(self, broker=None):
        local = SimpleNamespace(environment=lambda business=None: {}, agent_environment=lambda business, profile: {})
        runtime = SimpleNamespace(paths=self.paths, workspace=self.base, terminal_control=SimpleNamespace(backend=None))
        terminals = service_main.compose_terminals(runtime, paths=self.paths, endpoint="http://127.0.0.1:1/mcp", local=local,
            shell="/bin/bash", broker=broker, capture_transcripts=True, terminal_workspace=None, execution_provider=None)
        return runtime, terminals

    def test_composing_in_managed_mode_sets_both_seams_and_starts_nothing(self):
        self.forbid_processes()
        before, loaded = self.files(), "qorqut_dashboard_pty_broker" in sys.modules
        with mock.patch.object(service_main, "load_terminal_broker", side_effect=AssertionError("the broker was loaded")):
            runtime, terminals = self.compose()
        self.assertIsInstance(runtime.terminals, TerminalService)
        self.assertIs(runtime.terminals, terminals)
        self.assertIsInstance(runtime.terminal_control.backend, AgentTerminalBackend)
        self.assertIs(runtime.terminal_control.backend.terminals, terminals)
        self.assertEqual(self.files(), before)
        self.assertEqual("qorqut_dashboard_pty_broker" in sys.modules, loaded)

    def test_composing_in_terminals_mode_is_just_as_quiet(self):
        self.terminals()
        self.forbid_processes()
        runtime, _ = self.compose()
        self.assertIsNotNone(runtime.terminal_control.backend)
        self.assertEqual(self.files(), [str(self.paths.runtime)])

    def test_the_broker_loads_on_first_use_not_before(self):
        module = SimpleNamespace(TMUX="/usr/bin/tmux", tmux_available=lambda: True)
        loader = mock.Mock(return_value=module)
        broker = service_main.TerminalBroker(self.paths, load=loader)
        loader.assert_not_called()
        self.assertTrue(broker.tmux_available())
        self.assertTrue(broker.tmux_available())
        loader.assert_called_once_with(self.paths)

    def test_a_tmux_installed_after_the_service_started_is_found_without_a_restart(self):
        module = SimpleNamespace(TMUX=None, tmux_available=lambda: module.TMUX is not None)
        broker = service_main.TerminalBroker(self.paths, load=lambda paths: module)
        with mock.patch("qorqut.terminal_support.find_tmux", return_value=None):
            self.assertFalse(broker.tmux_available())
        with mock.patch("qorqut.terminal_support.find_tmux", return_value="/opt/bin/tmux"):
            self.assertTrue(broker.tmux_available())
        self.assertEqual(module.TMUX, "/opt/bin/tmux")

    def test_a_broker_that_cannot_load_is_the_normal_unavailable_error_never_a_crash(self):
        broker = service_main.TerminalBroker(self.paths, load=mock.Mock(side_effect=ImportError("no pty")))
        with self.assertRaises(DomainError) as raised:
            broker.tmux_available()
        self.assertEqual(raised.exception.code, "operation_unavailable")

    def test_the_real_broker_imports_on_a_computer_without_tmux_and_runs_nothing(self):
        self.forbid_processes()
        sys.modules.pop("qorqut_dashboard_pty_broker", None)
        self.addCleanup(sys.modules.pop, "qorqut_dashboard_pty_broker", None)
        with mock.patch("qorqut.installation.component", return_value=DASHBOARD):
            # ``shutil.which`` is the tripwire above, so the module's own lookup is replaced by an empty one.
            with mock.patch("shutil.which", return_value=None):
                module = service_main.load_terminal_broker(self.paths)
        self.assertIsNone(module.TMUX)
        self.assertFalse(module.tmux_available())
        self.assertEqual(self.files(), [str(self.paths.runtime)])

    def test_a_shell_a_terminal_does_not_start_falls_back_to_bash_so_composing_never_raises(self):
        with mock.patch.dict("os.environ", {"SHELL": "/usr/sbin/nologin"}):
            self.assertEqual(service_main.terminal_shell(), "/bin/bash")
        with mock.patch.dict("os.environ", {"SHELL": "/usr/bin/zsh"}):
            self.assertEqual(service_main.terminal_shell(), "/usr/bin/zsh")

    def test_an_interrupted_switch_is_recovered_once_and_a_failure_never_stops_the_service(self):
        recover = mock.Mock()
        service_main.recover_switch(SimpleNamespace(runtime_switch=SimpleNamespace(recover=recover)))
        recover.assert_called_once_with()
        with self.assertLogs("qorqut.service.main", level="ERROR"):
            service_main.recover_switch(SimpleNamespace(runtime_switch=SimpleNamespace(recover=mock.Mock(side_effect=OSError("x")))))
        service_main.recover_switch(SimpleNamespace(runtime_switch=None))
        service_main.recover_switch(SimpleNamespace())


class HostWritersTest(Fixture):
    class Tripwire:
        def inventory_and_freeze(self, *, timeout):
            raise AssertionError("tmux was asked in managed mode")

        def thaw(self):
            raise AssertionError("tmux was touched in managed mode")

    def managed(self, busy=()):
        root = self.paths.runtime / "managed-agents"
        for name in busy:
            (root / name).mkdir(parents=True)
        inspect = lambda directory: SimpleNamespace(status="running" if directory.name in busy else "absent")
        return ManagedHostWriters(root, inspect=inspect)

    def test_managed_mode_gives_the_managed_proof_and_never_asks_tmux(self):
        self.forbid_processes()
        writers = ModeHostWriters(self.managed(), self.Tripwire(), self.paths)
        proof = writers.inventory_and_freeze(timeout=5)
        self.assertEqual(proof, QuiescenceProof("registered_managed_hosts", "frozen"))

    def test_managed_mode_with_a_live_host_refuses_exactly_as_before(self):
        writers = ModeHostWriters(self.managed(busy=(AGENT,)), self.Tripwire(), self.paths)
        with self.assertRaises(DomainError) as raised:
            writers.inventory_and_freeze(timeout=5)
        self.assertEqual(raised.exception.code, "recovery_not_quiescent")

    def test_a_missing_tmux_does_not_block_a_managed_window(self):
        self.forbid_processes()
        writers = ModeHostWriters(self.managed(), HostTerminalWriters(lambda: None), self.paths)
        self.assertEqual(writers.inventory_and_freeze(timeout=5), QuiescenceProof("registered_managed_hosts", "frozen"))

    def terminal_writers(self, output="", returncode=1, executable="/usr/bin/tmux"):
        calls = []

        def runner(arguments, *, timeout):
            calls.append(arguments)
            return CommandResult(returncode, output)
        return HostTerminalWriters(lambda: executable, command_runner=runner), calls

    def test_terminals_mode_reads_the_qorqut_socket_and_passes_when_it_is_empty(self):
        self.terminals()
        terminals, calls = self.terminal_writers("no server running on /tmp/tmux-1/qorqut\n")
        proof = ModeHostWriters(self.managed(), terminals, self.paths).inventory_and_freeze(timeout=5)
        self.assertEqual(proof, QuiescenceProof("registered_host_terminals", "frozen"))
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[2] for call in calls], ["qorqut", "bazarlyk"])

    def test_terminals_mode_refuses_while_a_terminal_session_lives(self):
        self.terminals()
        terminals, _ = self.terminal_writers("$1\tglobal___agent\n", returncode=0)
        with self.assertRaises(DomainError) as raised:
            ModeHostWriters(self.managed(), terminals, self.paths).inventory_and_freeze(timeout=5)
        self.assertEqual(raised.exception.code, "recovery_not_quiescent")

    def test_terminals_mode_still_refuses_a_managed_host_a_switch_left_behind(self):
        self.terminals()
        terminals, _ = self.terminal_writers("no server running on /tmp/tmux-1/qorqut\n")
        with self.assertRaises(DomainError):
            ModeHostWriters(self.managed(busy=(AGENT,)), terminals, self.paths).inventory_and_freeze(timeout=5)

    def test_terminals_mode_without_tmux_has_no_server_to_hold_a_session(self):
        self.terminals()
        terminals, calls = self.terminal_writers(executable=None)
        ModeHostWriters(self.managed(), terminals, self.paths).inventory_and_freeze(timeout=5)
        self.assertEqual(calls, [])

    def test_the_mode_is_asked_at_each_window(self):
        terminals, calls = self.terminal_writers("no server running on /tmp/tmux-1/qorqut\n")
        writers = ModeHostWriters(self.managed(), terminals, self.paths)
        writers.inventory_and_freeze(timeout=5)
        self.assertEqual(calls, [])
        with mock.patch.object(mode, "current", return_value="terminal"):
            writers.inventory_and_freeze(timeout=5)
        self.assertEqual(len(calls), 2)
        calls.clear()
        writers.inventory_and_freeze(timeout=5)
        self.assertEqual(calls, [])


class Launcher:
    def inspect_host(self, state_dir):
        raise AssertionError("a gated open reached the launcher")

    def running_hosts(self, root):
        return {}


class ManagerGateTest(Fixture):
    def manager(self):
        return hosts.ManagedRuntime(SimpleNamespace(paths=self.paths), launcher=Launcher())

    def refused(self, call):
        with self.assertRaises(hosts.ManagedError) as raised:
            call()
        return raised.exception

    def test_managed_mode_does_not_gate(self):
        self.manager()._require_managed_mode()

    def test_terminals_mode_refuses_opening_with_the_quiet_not_managed(self):
        self.terminals()
        manager = self.manager()
        for name, call in (("ensure_open", lambda: manager.ensure_open({"agent_id": AGENT, "workspace": "/w"})),
                           ("_launch", lambda: manager._launch({"agent_id": AGENT, "workspace": "/w"}, 0, False))):
            with self.subTest(name):
                error = self.refused(call)
                self.assertEqual((error.code, error.message, error.retryable), ("not_managed", "This agent runs in its terminal", False))

    def test_a_switch_in_progress_refuses_every_open_and_it_is_retryable(self):
        manager = self.manager()
        for current in ("managed", "terminal"):
            with mock.patch.object(mode, "current", return_value=current), mock.patch.object(mode, "switching", return_value=True):
                for call in (lambda: manager.ensure_open({"agent_id": AGENT, "workspace": "/w"}),
                             lambda: manager._launch({"agent_id": AGENT, "workspace": "/w"}, 0, False)):
                    error = self.refused(call)
                    self.assertEqual((error.code, error.message, error.retryable),
                                     ("runtime_switching", "Agents are being switched. Try again in a minute.", True))

    def test_closing_stopping_and_reading_are_never_gated(self):
        manager = self.manager()
        for current, switching in (("terminal", False), ("managed", True), ("terminal", True)):
            with self.subTest((current, switching)), mock.patch.object(mode, "current", return_value=current), \
                    mock.patch.object(mode, "switching", return_value=switching):
                self.assertEqual(manager.running_hosts(), [])
                manager.close_all("switch")
                manager.stop()
                self.assertFalse(manager.close(AGENT, "now"))

    def test_no_terminal_present_check_came_back_into_the_launch(self):
        source = Path(hosts.__file__).read_text()
        self.assertNotIn("terminal_present", source)
        self.assertNotIn("tmux", source.replace("tmux terminals", ""))


if __name__ == "__main__":
    unittest.main()
