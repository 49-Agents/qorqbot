"""Terminals mode: the hook's ``attest`` accepts an interactive Claude Code client only for a folder prepared for a terminal, the
service's ``NativeBindingAdapter.verify`` takes the tmux pane path, and ``Selection`` tells the transports apart."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap import native_binding
from qorqut.bootstrap.native_binding import NativeBindingAdapter, PROBE
from qorqut.managed_runtime import mode
from qorqut.native_chat.probe import Observation
from qorqut.native_chat.selection import Selection

AGENT = "11111111-1111-4111-8111-111111111111"
CONVERSATION = "33333333-3333-4333-8333-333333333333"


class AttestTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}
        self.hook = {"session_id": CONVERSATION, "cwd": str(self.workspace), "hook_event_name": "SessionStart"}
        self.args = [b"claude", b"--settings", b"x"]
        patches = (mock.patch.object(native_binding, "load_registration", side_effect=lambda path: dict(self.registration)),
                   mock.patch.object(native_binding, "write_json_private"),
                   mock.patch("qorqut.bootstrap.terminal_chat.native_process", return_value={"pid": 4343, "started": "777"}),
                   mock.patch.object(native_binding, "local_instance", return_value={}),
                   mock.patch.object(Path, "read_bytes", lambda path: b"\0".join(self.args) + b"\0"))
        self.written = patches[1].start()
        for patcher in patches[:1] + patches[2:]:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def attest(self, runtime="claude-code"):
        return native_binding.attest(self.hook, "registration.json", runtime)

    def test_an_interactive_client_is_accepted_for_a_terminal_folder_and_records_no_host(self):
        self.registration["runtime_mode"] = "terminal"
        value = self.attest()
        self.assertEqual((value["owner"], value["pid"]), ("4343:777", 4343))
        self.assertNotIn("managed_host", value)
        self.assertNotIn("managed_host", self.written.call_args.args[1])

    def test_a_managed_folder_still_refuses_an_interactive_client(self):
        for registration in ({}, {"runtime_mode": "managed"}, {"runtime_mode": "other"}):
            self.registration.update(registration)
            with self.subTest(registration=registration), self.assertRaises(DomainError) as refused:
                self.attest()
            self.assertEqual(refused.exception.code, "inbox_binding_required")
        self.written.assert_not_called()

    def test_diagnostic_and_subagent_clients_are_refused_even_in_a_terminal_folder(self):
        self.registration["runtime_mode"] = "terminal"
        with mock.patch.object(native_binding, "managed_host", return_value=None):
            for args in ([b"claude", b"-p"], [b"claude", b"--print", b"x"], [b"claude", b"--agent-id", b"sub"]):
                self.args = args
                with self.subTest(args=args), self.assertRaises(DomainError):
                    self.attest()
        self.written.assert_not_called()

    def test_a_hook_with_an_agent_id_is_refused(self):
        self.registration["runtime_mode"] = "terminal"
        self.hook["agent_id"] = "sub"
        with self.assertRaises(DomainError):
            self.attest()

    def test_codex_is_not_attested_for_a_terminal_folder(self):
        self.registration["runtime_mode"] = "terminal"
        with mock.patch("qorqut.bootstrap.codex_session.hosted_session_process", return_value=None):
            with self.assertRaises(DomainError):
                self.attest("codex")


class VerifyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = SimpleNamespace(runtime=Path(directory.name))
        self.panes = (0, "4300\n")
        self.tmux = mock.Mock(side_effect=lambda *args: self.panes)
        terminals = SimpleNamespace(execution=lambda business: SimpleNamespace(broker=SimpleNamespace(tmux=self.tmux)),
                                    binding=lambda business, row, broker: ("key", "agent-session", "/w"))
        database = SimpleNamespace(read=mock.MagicMock())
        database.read.return_value.__enter__.return_value.repository.return_value.get.return_value = None
        self.runtime = SimpleNamespace(paths=self.paths, terminals=terminals, database=database)
        self.profile = {"agent_id": AGENT, "workspace": "/w", "home_business_id": None,
                        "profile_json": json.dumps({"runtime": "claude-code"})}
        self.ran = []
        self.observation = {"agent_id": AGENT, "session_id": CONVERSATION, "pid": 4343, "started": "777", "owner": "4343:777"}

        def run(script, arguments):
            self.ran.append((script, list(arguments)))
            return json.dumps(self.observation) if script is PROBE else "dead"
        patcher = mock.patch.object(NativeBindingAdapter, "run", staticmethod(run))
        patcher.start()
        self.addCleanup(patcher.stop)

    def verify(self, **kwargs):
        adapter = NativeBindingAdapter(self.runtime)
        with mock.patch.object(NativeBindingAdapter, "selected", return_value=self.profile):
            return adapter.verify(AGENT, "reg-1", CONVERSATION, **kwargs)

    def test_terminals_mode_probes_the_one_pane_and_never_the_managed_runtime(self):
        mode.commit(self.paths, mode.TERMINAL)
        with mock.patch.object(NativeBindingAdapter, "managed_identity") as identity:
            value, state = self.verify()
        identity.assert_not_called()
        self.assertEqual((value["execution_target"], value["target"], state), ("local", "local", "unknown"))
        self.assertEqual(self.tmux.call_args.args, ("list-panes", "-t", "=agent-session", "-F", "#{pane_pid}"))
        self.assertEqual(self.ran[0][1], ["/w", CONVERSATION, "4300", AGENT, "reg-1"])

    def test_two_panes_or_a_tmux_error_pass_an_empty_pane_so_the_probe_refuses(self):
        mode.commit(self.paths, mode.TERMINAL)
        for panes in ((0, "1\n2\n"), (1, "")):
            self.panes = panes
            self.verify()
            self.assertEqual(self.ran[-1][1][2], "")

    def test_a_failing_probe_is_an_ambiguous_binding(self):
        mode.commit(self.paths, mode.TERMINAL)
        self.run_error = True
        with mock.patch.object(NativeBindingAdapter, "run", staticmethod(lambda s, a: (_ for _ in ()).throw(OSError()))):
            with self.assertRaises(DomainError) as refused:
                self.verify()
        self.assertEqual(refused.exception.code, "inbox_binding_ambiguous")

    def test_a_previous_binding_is_judged_by_the_dead_probe(self):
        mode.commit(self.paths, mode.TERMINAL)
        _, state = self.verify(previous={"execution_target": "local", "pid": 1, "started": "2", "owner": "1:2"})
        self.assertEqual(state, "dead")

    def test_the_pane_decides_not_the_profile(self):
        """An agent whose profile names Codex but whom the owner runs on Claude Code in Settings is verified like any other:
        the probe refuses whatever in the pane is not the interactive Claude Code client the marker names."""
        mode.commit(self.paths, mode.TERMINAL)
        self.profile["profile_json"] = json.dumps({"runtime": "codex"})
        value, _ = self.verify()
        self.assertEqual(value["execution_target"], "local")
        self.assertEqual(self.ran[0][1], ["/w", CONVERSATION, "4300", AGENT, "reg-1"])
        self.assertIn("assert v['runtime']=='claude-code'", native_binding.PROBE)
        self.assertIn("assert name in {'claude','claude.exe'}", native_binding.PROBE)

    def test_managed_mode_has_no_pane_path(self):
        with mock.patch.object(NativeBindingAdapter, "managed_identity", return_value=None):
            with self.assertRaises(DomainError):
                self.verify()
        self.tmux.assert_not_called()


class SelectionTests(unittest.TestCase):
    def selection(self, owner):
        return Selection(AGENT, CONVERSATION, "claude", 1, "running",
                         {"owner": owner, "pid": 4343, "started": "777", "runtime": "claude-code"})

    def observation(self, transport, owner):
        return Observation(agent_id=AGENT, runtime="claude-code", provider="claude", conversation_id=CONVERSATION,
                           transcript_path=None, pid=4343, started="777", transport=transport, owner=owner)

    def test_a_terminal_observation_is_held_only_by_its_own_process_owner(self):
        terminal = self.observation("terminal", None)
        self.assertTrue(self.selection("4343:777").holds(terminal))
        self.assertTrue(self.selection(None).holds(terminal))
        self.assertFalse(self.selection("host:1:2").holds(terminal))

    def test_a_managed_observation_keeps_its_rule(self):
        managed = self.observation("managed", "host:1:2")
        self.assertTrue(self.selection("host:1:2").holds(managed))
        self.assertTrue(self.selection("4343:777").holds(managed))
        self.assertFalse(self.selection("host:9:9").holds(managed))

    def test_public_names_the_transport_by_mode(self):
        self.assertEqual(self.selection(None).public()["transport"], "managed")
        self.assertEqual(self.selection(None).public(terminal=True)["transport"], "terminal")


if __name__ == "__main__":
    unittest.main()
