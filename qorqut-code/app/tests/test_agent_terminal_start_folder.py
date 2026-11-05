"""Start types into an agent's shell only when that shell is, physically, in the agent's own workspace root.

The Start flow (``AgentTerminalBackend.execute``) runs against stand-in tmux, terminal and conversation objects that record
what they are asked to do; the folder of the observed shell comes from a stand-in ``process_info.cwd`` or, in the last class,
from real short-lived ``sleep`` processes started in temporary folders. Trust is recorded through the real ``agent_trust`` in a
disposable CLI configuration (isolated_cli_environment). No real terminal, CLI setting or user process is involved.
"""
import isolated_cli_environment

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

from bazarlyk import agent_trust
from bazarlyk.agents import roles
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend

SHELL = {"state": "present", "server_pid": "10", "session_id": "$1", "pane_id": "%1", "pane_pid": "4242", "command": "bash",
         "foreground_pid": 4242, "foreground_started": "1000", "client": "claude", "selection_revision": None}
CLIENT = {**SHELL, "command": "claude"}


class Broker:
    """Records every tmux command and every line typed."""

    def __init__(self):
        self.tmux_calls, self.typed = [], []

    def tmux(self, *args):
        self.tmux_calls.append(args)
        return 0, ""

    def submit_input(self, session, text):
        self.typed.append(text)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.configuration = isolated_cli_environment.isolate(self)
        self.base = Path(tempfile.mkdtemp(prefix="start folder ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.runtime_dir = self.base / "runtime"
        self.runtime_dir.mkdir()
        self.workspace = self.base / "operations" / "agents" / "ordu bashy"
        self.subfolder = self.workspace / "work"
        self.subfolder.mkdir(parents=True)
        (self.base / "operations" / ".git").mkdir()
        self.project = self.base / "other project"
        (self.project / ".git").mkdir(parents=True)
        self.link = self.base / "link to agent"
        self.link.symlink_to(self.workspace)
        # The disposable Claude configuration, with the owner's consent: a trust write would show here.
        self.claude_file = Path(self.configuration["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        self.claude_file.write_text(json.dumps({"projects": {}}))
        agent_trust.consent(self.runtime_dir, {"granted": True})
        self.claude_before = self.claude_file.read_bytes()
        self.broker, self.opened, self.resumed = Broker(), [], []
        self.resume_result = None

    def trust_written(self):
        return self.claude_file.read_bytes() != self.claude_before

    def backend(self, snapshots, cwds, *, row=None, workspace=None):
        """A backend whose successive ``snapshot`` calls return ``snapshots`` (the first is the one the action was authorized
        against, then the check just before dispatch, then the one after the role prompt; the last repeats; an exception
        is raised) and whose just-opened pane is the first of them. The foreground shell is in ``cwds``, one entry per
        ``process_info.cwd`` call (a path, or an exception to raise; the last repeating)."""
        workspace = workspace or self.workspace

        @contextmanager
        def read():
            yield SimpleNamespace(repository=lambda name: SimpleNamespace(get=lambda key: {"owner_principal_id": "owner"}))
        runtime = SimpleNamespace(
            database=SimpleNamespace(read=read), paths=SimpleNamespace(runtime=self.runtime_dir),
            policy=SimpleNamespace(context_for=lambda *args, **options: "context"),
            native_chat=SimpleNamespace(
                arbiter=SimpleNamespace(locks=lambda agent: nullcontext()),
                attachment=SimpleNamespace(resume_selected=lambda agent, authorized: (self.resumed.append((agent, authorized)),
                                                                                       self.resume_result)[1])))
        terminals = SimpleNamespace(runtime=runtime, open=lambda context, business, agent: self.opened.append(agent))
        backend = AgentTerminalBackend(terminals, timeout=1, sleep=lambda seconds: None)
        backend.selected = lambda row: (self.broker, "key", "session", workspace, None)
        seen = list(snapshots)

        def snapshot(row, client=None):
            item = seen.pop(0) if len(seen) > 1 else seen[0]
            if isinstance(item, Exception):
                raise item
            return item
        backend.snapshot = snapshot
        backend.opened_snapshot = lambda row, attempts=50: snapshots[0]
        backend.wait_ready = lambda row, client="codex": None
        self.cwd_calls = []
        remaining = list(cwds)

        def cwd(pid):
            self.cwd_calls.append(pid)
            item = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            if isinstance(item, Exception):
                raise item
            return Path(item)
        self.process_cwd = mock.patch("bazarlyk.process_info.cwd", cwd)
        self.process_cwd.start()
        self.addCleanup(self.process_cwd.stop)
        self.row = row or {"agent_id": "ordubashy", "home_business_id": None,
                           "profile_json": json.dumps({"runtime": "claude-code", "role": {"id": "ordubashy"}})}
        return backend

    def start(self, backend, expected=None):
        return backend.execute(self.row, "start", None, expected or SHELL)

    def nothing_was_done(self):
        self.assertEqual(self.broker.tmux_calls, [])
        self.assertEqual(self.broker.typed, [])
        self.assertEqual(self.resumed, [])


class StartFolderTest(Fixture):
    # -- an agent terminal in its own folder starts as before -----------------------------------------------------------

    def test_a_terminal_in_the_agent_root_starts_and_records_trust(self):
        backend = self.backend([SHELL, SHELL, CLIENT], [self.workspace])
        message = self.start(backend)
        self.assertIn("/role submitted", message)
        self.assertTrue(self.trust_written())
        self.assertIn(("send-keys", "-t", "%1", "C-u"), self.broker.tmux_calls)
        self.assertEqual(self.broker.typed, ["QORQBOT_ROLE_SENT=1 claude", "/role"])
        self.assertEqual(self.resumed, [("ordubashy", None)])
        self.assertEqual(self.opened, ["ordubashy"])
        # Observed before trust is recorded and again at the last moment.
        self.assertEqual(self.cwd_calls, [4242, 4242])

    def test_a_symlink_spelling_of_the_root_is_accepted_either_way(self):
        for name, workspace, here in (("workspace by link", self.link, self.workspace), ("cwd by link", self.workspace, self.link)):
            with self.subTest(name):
                self.broker, self.cwd_calls = Broker(), []
                backend = self.backend([SHELL, SHELL, CLIENT], [here], workspace=workspace)
                self.assertIn("/role submitted", self.start(backend))
                self.assertEqual(self.broker.typed, ["QORQBOT_ROLE_SENT=1 claude", "/role"])

    def test_a_selected_conversation_is_resumed_only_after_the_checks_and_nothing_else_is_typed(self):
        self.resume_result = "Claude resume of the selected conversation submitted"
        expected = {**SHELL, "selection_revision": "rev-1"}
        backend = self.backend([expected, expected], [self.workspace])
        self.assertEqual(self.start(backend, expected), self.resume_result)
        self.assertEqual(self.resumed, [("ordubashy", "rev-1")])
        self.assertEqual(self.broker.typed, [])
        self.assertEqual(self.cwd_calls, [4242, 4242])

    # -- anywhere else is refused before anything is recorded or typed --------------------------------------------------

    def test_another_folder_is_refused_with_advice_and_nothing_is_done(self):
        for name, here in (("an unrelated repository", self.project), ("its parent folder", self.base),
                           ("an agent subfolder", self.subfolder), ("the workspace's parent", self.workspace.parent),
                           ("the operations repository", self.base / "operations")):
            with self.subTest(name):
                self.broker, self.resumed = Broker(), []
                backend = self.backend([SHELL, SHELL, CLIENT], [here])
                with self.assertRaises(DomainError) as raised:
                    self.start(backend)
                self.assertEqual(raised.exception.code, "invalid_transition")
                self.assertIn("Return it to that folder before pressing Start", raised.exception.message)
                self.assertIn("does not change folders or type commands", raised.exception.message)
                self.nothing_was_done()
                self.assertFalse(self.trust_written())

    def test_a_folder_that_cannot_be_verified_is_refused_and_nothing_is_done(self):
        deleted = FileNotFoundError("/proc/4242/cwd")
        for name, cwds, observation in (("the folder was deleted", [deleted], SHELL),
                                        ("not permitted to read it", [PermissionError("Cannot inspect process 4242")], SHELL),
                                        ("a deleted path", [self.base / "gone (deleted)"], SHELL),
                                        ("no foreground process", [self.workspace], {**SHELL, "foreground_pid": None}),
                                        ("a nonsense pid", [self.workspace], {**SHELL, "foreground_pid": 0}),
                                        ("a pid that is not a number", [self.workspace], {**SHELL, "foreground_pid": "4242"})):
            with self.subTest(name):
                self.broker, self.resumed = Broker(), []
                backend = self.backend([observation, observation, CLIENT], cwds)
                with self.assertRaises(DomainError) as raised:
                    self.start(backend, observation)
                self.assertEqual(raised.exception.code, "operation_unavailable")
                self.assertIn("Return the agent terminal to its own folder", raised.exception.message)
                self.nothing_was_done()
                self.assertFalse(self.trust_written())

    def test_a_missing_workspace_is_refused(self):
        backend = self.backend([SHELL, SHELL], [self.workspace], workspace=self.base / "missing")
        with self.assertRaises(DomainError) as raised:
            self.start(backend)
        self.assertEqual(raised.exception.code, "operation_unavailable")
        self.nothing_was_done()
        self.assertFalse(self.trust_written())

    # -- the last-moment check ------------------------------------------------------------------------------------------

    def test_a_different_shell_process_between_the_observations_is_refused_without_typing(self):
        for name, changed in (("another foreground process", {**SHELL, "foreground_pid": 5555}),
                              ("the pid reused by a new process", {**SHELL, "foreground_started": "2000"}),
                              ("another pane", {**SHELL, "pane_id": "%2"}),
                              ("another session", {**SHELL, "session_id": "$2"}),
                              ("a client now in the foreground", {**SHELL, "command": "claude"}),
                              ("an unrecognized foreground program", {**SHELL, "command": "unrecognized"})):
            with self.subTest(name):
                self.broker, self.resumed = Broker(), []
                backend = self.backend([SHELL, changed], [self.workspace])
                with self.assertRaises(DomainError) as raised:
                    self.start(backend)
                self.assertEqual(raised.exception.code, "revision_conflict")
                self.assertEqual(raised.exception.message, "Terminal changed before dispatch")
                self.nothing_was_done()

    def test_a_shell_that_moved_between_the_observations_is_refused_without_typing(self):
        backend = self.backend([SHELL, SHELL], [self.workspace, self.project])
        with self.assertRaises(DomainError) as raised:
            self.start(backend)
        self.assertEqual(raised.exception.code, "invalid_transition")
        self.nothing_was_done()
        backend = self.backend([SHELL, SHELL], [self.workspace, FileNotFoundError("gone")])
        with self.assertRaises(DomainError) as raised:
            self.start(backend)
        self.assertEqual(raised.exception.code, "operation_unavailable")
        self.nothing_was_done()

    def test_a_pane_that_cannot_be_read_at_the_last_moment_is_refused_without_typing(self):
        unreadable = DomainError("operation_unavailable", "Cannot identify the foreground terminal process")
        backend = self.backend([SHELL, unreadable], [self.workspace])
        with self.assertRaises(DomainError):
            self.start(backend)
        self.nothing_was_done()

    def test_the_resume_of_a_selected_conversation_is_not_reached_after_a_refusal(self):
        self.resume_result = "must not be returned"
        backend = self.backend([SHELL, {**SHELL, "foreground_pid": 5555}], [self.workspace])
        with self.assertRaises(DomainError):
            self.start(backend)
        self.assertEqual(self.resumed, [])

    # -- Dev keeps its owner-project launch -----------------------------------------------------------------------------

    def test_dev_still_launches_from_wherever_its_terminal_is(self):
        dev = {"agent_id": "dev", "home_business_id": None,
               "profile_json": json.dumps({"runtime": "claude-code", "role": {"id": roles.DEV_ROLE}})}
        backend = self.backend([SHELL], [self.project], row=dev)
        self.assertEqual(self.start(backend), "Startup command submitted; readiness/work is not confirmed")
        self.assertEqual(self.broker.typed, ["claude"])
        self.assertIn(("send-keys", "-t", "%1", "C-u"), self.broker.tmux_calls)
        self.assertEqual(self.cwd_calls, [])


class RealProcessFolderTest(Fixture):
    """The folder check against real processes: ``process_info.cwd`` of a ``sleep`` started in a temporary folder."""

    def setUp(self):
        super().setUp()
        if not shutil.which("sleep"):
            self.skipTest("sleep is not available")
        self.backend_ = AgentTerminalBackend(SimpleNamespace(runtime=SimpleNamespace()))

    def process_in(self, folder):
        process = subprocess.Popen(["sleep", "30"], cwd=folder, env={"PATH": "/usr/bin:/bin"},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def check(self, folder, workspace=None):
        return self.backend_._require_agent_folder({"foreground_pid": self.process_in(folder).pid}, workspace or self.workspace)

    def test_the_root_and_its_symlink_spellings_are_accepted(self):
        self.check(self.workspace)
        self.check(self.link)
        self.check(self.workspace, workspace=self.link)
        self.check(self.link, workspace=self.link)

    def test_other_folders_are_refused(self):
        for folder in (self.project, self.subfolder, self.workspace.parent, self.base):
            with self.subTest(folder=folder), self.assertRaises(DomainError) as raised:
                self.check(folder)
            self.assertEqual(raised.exception.code, "invalid_transition")

    def test_a_deleted_folder_and_a_vanished_process_cannot_be_verified(self):
        gone = self.base / "gone"
        gone.mkdir()
        process = self.process_in(gone)
        gone.rmdir()
        with self.assertRaises(DomainError) as raised:
            self.backend_._require_agent_folder({"foreground_pid": process.pid}, self.workspace)
        self.assertEqual(raised.exception.code, "operation_unavailable")
        process.kill()
        process.wait()
        with self.assertRaises(DomainError) as raised:
            self.backend_._require_agent_folder({"foreground_pid": process.pid}, self.workspace)
        self.assertEqual(raised.exception.code, "operation_unavailable")


if __name__ == "__main__":
    unittest.main()
