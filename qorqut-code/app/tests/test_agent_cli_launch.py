"""Every place that starts an agent CLI finds Qorqbot's own downloaded copy at that moment, and only that copy is kept from updating.

``agent_cli_install`` is replaced by a stand-in that says which copy is selected, so nothing is downloaded and no real
Claude Code or Codex runs. The shell wrappers run in real bash (and zsh where installed) against stand-in executables that
record their arguments and the updater variables they were started with.
"""
import isolated_cli_environment

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut import agent_cli_launch
from qorqut.bootstrap import delegated_runtime
from qorqut.bootstrap.preparation import agent_shell, agent_zshrc, installed_versions

CLAUDE_ENV = {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
CODEX_ARGS = ["-c", "check_for_update_on_startup=false"]


def program(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


class Fixture(unittest.TestCase):
    """A Qorqbot folder with (optionally) downloaded copies, and an install stand-in that selects them."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cli launch ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.claude = program(self.base / "tools/claude-1/claude")
        self.codex = program(self.base / "tools/codex-1/bin/codex")
        self.selected = {"claude": self.claude, "codex": self.codex}

    def install(self, **only):
        """The install stand-in: ``managed_launch`` returns the copies in ``self.selected`` (as the real one does for a managed pick)."""
        def managed_launch(provider, environ=None):
            path = self.selected.get(provider)
            return None if path is None else {
                "path": path, "bin_dir": os.path.dirname(path), "source": "managed", "version": "1",
                "env": CLAUDE_ENV if provider == "claude" else {}, "args": CODEX_ARGS if provider == "codex" else []}
        stand_in = SimpleNamespace(managed_launch=managed_launch, **only)
        patch = mock.patch.object(agent_cli_launch, "_install", return_value=stand_in)
        patch.start()
        self.addCleanup(patch.stop)
        return stand_in


class HelperTests(Fixture):
    def test_nothing_is_managed_without_the_install_module_or_a_selected_copy(self):
        with mock.patch.object(agent_cli_launch, "_install", return_value=None):
            self.assertIsNone(agent_cli_launch.managed("claude", {}))
            self.assertEqual(agent_cli_launch.executable("codex", "codex", {}), "codex")
        self.selected.clear()
        self.install()
        self.assertIsNone(agent_cli_launch.managed("claude", {}))
        self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, {}), {"A": "1"})
        self.assertIsNone(agent_cli_launch.managed("gemini", {}))  # nothing here raises

    def test_a_selected_copy_is_returned_with_its_settings(self):
        self.install()
        found = agent_cli_launch.managed("codex", {})
        self.assertEqual(found, {"path": self.codex, "bin_dir": str(Path(self.codex).parent), "env": {}, "args": CODEX_ARGS})
        self.assertEqual(agent_cli_launch.managed("claude", {})["env"], CLAUDE_ENV)
        self.assertEqual(agent_cli_launch.executable("claude", "claude", {}), self.claude)

    def test_an_owner_or_override_pick_is_not_a_managed_copy(self):
        stand_in = self.install()
        stand_in.managed_launch = lambda provider, environ=None: {"path": "/usr/bin/claude", "source": "system", "env": {}, "args": []}
        self.assertIsNone(agent_cli_launch.managed("claude", {}))

    def test_without_managed_launch_the_helper_uses_resolve_and_the_launch_functions(self):
        stand_in = SimpleNamespace(
            resolve=lambda provider, environ=None: {"path": self.claude, "source": "managed"} if provider == "claude"
            else {"path": "/usr/bin/codex", "source": "system"},
            launch_environment=lambda provider: CLAUDE_ENV if provider == "claude" else {},
            launch_arguments=lambda provider: [])
        with mock.patch.object(agent_cli_launch, "_install", return_value=stand_in):
            self.assertEqual(agent_cli_launch.managed("claude", {})["env"], CLAUDE_ENV)
            self.assertIsNone(agent_cli_launch.managed("codex", {}))

    def test_a_session_marker_names_the_copy_where_the_folder_is_unknown(self):
        self.selected.clear()
        stand_in = self.install(launch_environment=lambda provider: CLAUDE_ENV if provider == "claude" else {},
                                launch_arguments=lambda provider: CODEX_ARGS if provider == "codex" else [])
        self.assertEqual(agent_cli_launch.managed("codex", {"QORQBOT_MANAGED_CODEX": self.codex})["args"], CODEX_ARGS)
        for bad in ("relative/codex", str(self.base / "missing"), str(self.base)):
            self.assertIsNone(agent_cli_launch.managed("codex", {"QORQBOT_MANAGED_CODEX": bad}), bad)
        self.assertIsNotNone(stand_in)

    def test_arguments_and_variables_apply_only_to_the_managed_file(self):
        self.install()
        self.assertEqual(agent_cli_launch.for_executable("codex", self.codex, {}), {"args": CODEX_ARGS, "env": {}})
        self.assertEqual(agent_cli_launch.for_executable("claude", self.claude, {})["env"], CLAUDE_ENV)
        through_link = self.base / "link-to-claude"
        through_link.symlink_to(self.claude)
        self.assertEqual(agent_cli_launch.for_executable("claude", str(through_link), {})["env"], CLAUDE_ENV)
        for other in (program(self.base / "own/claude"), "claude", "/missing/claude"):
            self.assertEqual(agent_cli_launch.for_executable("claude", other, {}), {"args": [], "env": {}}, other)

    def test_nothing_raises_whatever_goes_wrong_while_looking(self):
        def boom(*args, **kwargs):
            raise PermissionError("tools folder is unreadable")
        broken = SimpleNamespace(managed_launch=boom, resolve=boom, launch_environment=boom, launch_arguments=boom)
        with mock.patch.object(agent_cli_launch, "_install", return_value=broken):
            self.assertIsNone(agent_cli_launch.managed("claude", {}))
            self.assertIsNone(agent_cli_launch.managed("codex", {"QORQBOT_MANAGED_CODEX": self.codex}))
            self.assertEqual(agent_cli_launch.executable("codex", "codex", {}), "codex")
            self.assertEqual(agent_cli_launch.for_executable("codex", self.codex, {}), {"args": [], "env": {}})
            self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, {}), {"A": "1"})
            self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, {}, plain=True), {"A": "1"})
        # An import that fails for any reason is "no install module".
        with mock.patch("builtins.__import__", side_effect=ImportError("broken")):
            self.assertIsNone(agent_cli_launch._install())

    def test_an_unreadable_tools_folder_with_the_real_install_module_is_just_not_managed(self):
        root = self.base / "folder"
        program(root / "tools/claude-2.1.327/claude")
        (root / "tools").chmod(0)
        self.addCleanup((root / "tools").chmod, 0o755)
        if os.access(root / "tools", os.R_OK | os.X_OK):
            self.skipTest("this user can read a folder with no permissions (root)")
        environ = {"QORQUT_FOLDER_ROOT": str(root), "PATH": ""}
        self.assertIsNone(agent_cli_launch.managed("claude", environ))
        self.assertEqual(agent_cli_launch.executable("claude", "claude", environ), "claude")
        self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, environ), {"A": "1"})

    def test_a_session_gets_our_folders_first_and_the_owners_path_after(self):
        self.install()
        native = {"PATH": "/home/owner/.local/bin:/usr/bin", "HOME": "/home/owner"}
        environ = {"PATH": "/app/venv/bin:/usr/bin", "QORQUT_NATIVE_USER_ENV": json.dumps(native)}
        result = agent_cli_launch.session_environment({"X": "1"}, environ)
        self.assertEqual(result["PATH"], os.pathsep.join([str(Path(self.claude).parent), str(Path(self.codex).parent),
                                                          "/home/owner/.local/bin:/usr/bin"]))
        self.assertEqual((result["X"], result["QORQBOT_MANAGED_CLAUDE"], result["QORQBOT_MANAGED_CODEX"]), ("1", self.claude, self.codex))
        # The wrapper decides about updates per launch, so a session does not carry the updater variables.
        self.assertNotIn("DISABLE_AUTOUPDATER", result)
        self.assertNotIn("DISABLE_UPDATES", result)

    def test_the_owners_plain_shell_gets_launchers_for_our_copy_and_no_updater_variables(self):
        self.install()
        root = self.base / "folder 'q' $x"
        environ = {"PATH": "/service", "QORQUT_FOLDER_ROOT": str(root)}
        result = agent_cli_launch.session_environment({"OWNER": "1"}, environ, plain=True)
        folder = root / "tools/launchers"
        self.assertEqual(result["PATH"], str(folder) + os.pathsep + "/service")
        self.assertEqual(result["OWNER"], "1")
        for name in ("DISABLE_AUTOUPDATER", "DISABLE_UPDATES", "QORQBOT_MANAGED_CLAUDE", "QORQBOT_MANAGED_CODEX"):
            self.assertNotIn(name, result)
        # The launchers start our copy, with the settings and arguments, and pass the owner's arguments and exit status through.
        for name, path in (("claude", self.claude), ("codex", self.codex)):
            program(Path(path), '#!/bin/sh\nprintf "%s\\n" "$@" "u=${DISABLE_AUTOUPDATER:-}${DISABLE_UPDATES:-}"; exit 5\n')
        for name, expected in (("claude", ["two words", "$HOME", "u=11"]), ("codex", [*CODEX_ARGS, "two words", "$HOME", "u="])):
            process = subprocess.run([str(folder / name), "two words", "$HOME"], capture_output=True, text=True,
                                     env={"PATH": "/usr/bin:/bin"})
            self.assertEqual((process.returncode, process.stdout.splitlines()), (5, expected), name)
        # Nothing is written, and the environment is unchanged, where the folder cannot be written or is unknown.
        self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, {"PATH": "/s"}, plain=True), {"A": "1"})
        blocked = self.base / "blocked"
        blocked.write_text("a file, not a folder")
        self.assertEqual(agent_cli_launch.session_environment({"A": "1"}, {"QORQUT_FOLDER_ROOT": str(blocked)}, plain=True), {"A": "1"})
        # A second call leaves unchanged launchers alone.
        before = (folder / "claude").stat().st_mtime_ns
        agent_cli_launch.session_environment({}, environ, plain=True)
        self.assertEqual((folder / "claude").stat().st_mtime_ns, before)

    def test_the_path_a_session_would_have_had_is_kept_after_ours(self):
        self.install()
        self.selected.pop("claude")
        explicit = agent_cli_launch.session_environment({"PATH": "/owner/setting"}, {"PATH": "/service"})
        self.assertEqual(explicit["PATH"], str(Path(self.codex).parent) + os.pathsep + "/owner/setting")
        unset = agent_cli_launch.session_environment({}, {"PATH": "/service", "QORQUT_NATIVE_USER_ENV": "{"})
        self.assertEqual(unset["PATH"], str(Path(self.codex).parent) + os.pathsep + "/service")
        self.assertNotIn("QORQBOT_MANAGED_CLAUDE", unset)


class Wrappers:
    """The agent shell's claude and codex wrappers, run for real against stand-in programs."""
    SHELL = ""

    def setUp(self):
        if not shutil.which(self.SHELL):
            self.skipTest(f"{self.SHELL} is not installed")
        super().setUp()
        self.configuration = isolated_cli_environment.isolate(self)
        self.records = self.base / "records"
        self.records.mkdir()
        self.workspace = self.base / "agent root"
        self.elsewhere = self.base / "elsewhere"
        self.workspace.mkdir()
        self.elsewhere.mkdir()
        self.owner_bin = self.base / "owner bin"
        for name, directory in (("claude", self.base / "tools/claude-1"), ("codex", self.base / "tools/codex-1/bin"),
                                ("claude", self.owner_bin), ("codex", self.owner_bin)):
            program(directory / name, '#!/bin/sh\n{ printf "%s\\n" "$0"; for a in "$@"; do printf "%s\\n" "$a"; done; '
                    'printf "updater=%s,%s\\n" "${DISABLE_AUTOUPDATER:-}" "${DISABLE_UPDATES:-}"; } > "$QORQBOT_TEST_RECORDS/'
                    + name + '.$$"\nexit 3\n')
        self.launch_settings = self.workspace / ".qorqut/claude-launch-settings.json"
        self.shell_file = self.base / "agent-shell"
        self.shell_file.write_text(self.generate(shlex.quote(str(self.launch_settings)), shlex.quote(str(self.base / "no consent")),
                                                 shlex.quote(str(self.workspace))))

    def run_in(self, cwd, command, path, **markers):
        for entry in self.records.iterdir():
            entry.unlink()
        environment = {"PATH": path + ":/usr/bin:/bin", "TERM": "dumb", "PWD": str(cwd), "QORQBOT_TEST_RECORDS": str(self.records),
                       **self.configuration, **markers}
        process = subprocess.run([*self.command, '. "$1"; ' + command, self.SHELL, str(self.shell_file)], cwd=cwd,
                                 env=environment, capture_output=True, text=True, timeout=30)
        calls = [entry.read_text().splitlines() for entry in sorted(self.records.iterdir())]
        return calls, process

    def managed_path(self):
        return str(self.base / "tools/claude-1") + ":" + str(self.base / "tools/codex-1/bin") + ":" + str(self.owner_bin)

    def markers(self):
        return {"QORQBOT_MANAGED_CLAUDE": self.claude, "QORQBOT_MANAGED_CODEX": self.codex}

    def test_our_copy_of_claude_never_updates_itself_in_the_root_or_elsewhere(self):
        for cwd, expected in ((self.workspace, ["--settings", str(self.launch_settings), "hi"]), (self.elsewhere, ["hi"])):
            calls, process = self.run_in(cwd, "claude hi; echo status=$?", self.managed_path(), **self.markers())
            self.assertEqual(len(calls), 1, process.stderr)
            self.assertEqual(calls[0][0], self.claude)
            self.assertEqual(calls[0][1:-1], expected)
            self.assertEqual(calls[0][-1], "updater=1,1")
            self.assertIn("status=3", process.stdout)

    def test_our_copy_of_codex_never_checks_for_updates_in_the_root_or_elsewhere(self):
        for cwd in (self.workspace, self.elsewhere):
            calls, _ = self.run_in(cwd, "QORQBOT_ROLE_SENT=1 codex resume --last", self.managed_path(), **self.markers())
            self.assertEqual(calls[0][0], self.codex)
            self.assertEqual(calls[0][1:-1], [*CODEX_ARGS, "resume", "--last"])

    def test_the_role_prompt_and_launch_settings_still_apply_to_our_copy(self):
        calls, _ = self.run_in(self.workspace, "claude", self.managed_path(), **self.markers())
        self.assertEqual(calls[0][1:-1], ["--settings", str(self.launch_settings), "/role"])
        calls, _ = self.run_in(self.workspace, "codex", self.managed_path(), **self.markers())
        self.assertEqual(calls[0][1:-1], [*CODEX_ARGS, "$role"])

    def test_the_owners_own_cli_is_started_exactly_as_before_when_the_session_has_no_marker(self):
        owner_first = str(self.owner_bin) + ":" + self.managed_path()
        calls, _ = self.run_in(self.elsewhere, "claude hi; codex resume", owner_first)
        self.assertEqual(sorted(calls), sorted([[str(self.owner_bin / "claude"), "hi", "updater=,"],
                                                [str(self.owner_bin / "codex"), "resume", "updater=,"]]))

    def test_our_copy_runs_wherever_the_owners_shell_files_put_their_own_cli_on_path(self):
        # .bashrc / .zshrc run after the session environment and often put Homebrew or nvm first: the marker still wins, so the
        # terminal runs the same copy the queue and the version probe use.
        owner_first = str(self.owner_bin) + ":" + self.managed_path()
        calls, process = self.run_in(self.elsewhere, "claude hi; echo status=$?; QORQBOT_ROLE_SENT=1 codex resume", owner_first,
                                     **self.markers())
        self.assertEqual(sorted(calls), sorted([[self.claude, "hi", "updater=1,1"], [self.codex, *CODEX_ARGS, "resume", "updater=,"]]))
        self.assertIn("status=3", process.stdout)
        # ... and with the owner's directory alone on PATH (ours is not on it at all).
        calls, _ = self.run_in(self.workspace, "QORQBOT_ROLE_SENT=1 claude", str(self.owner_bin), **self.markers())
        self.assertEqual(calls, [[self.claude, "--settings", str(self.launch_settings), "updater=1,1"]])

    def test_a_marker_that_no_longer_names_an_executable_file_falls_back_to_path(self):
        owner_first = str(self.owner_bin) + ":" + self.managed_path()
        notes = self.base / "notes"
        notes.write_text("x")
        for bad in ("", "relative/claude", str(self.base / "gone/claude"), str(self.base), str(notes)):
            with self.subTest(marker=bad):
                calls, _ = self.run_in(self.elsewhere, "claude hi", owner_first, QORQBOT_MANAGED_CLAUDE=bad)
                self.assertEqual(calls, [[str(self.owner_bin / "claude"), "hi", "updater=,"]])

    def test_a_marker_path_with_spaces_quotes_and_dollars_is_run_as_one_word(self):
        awkward = program(self.base / "we ird $HOME 'q' \"d\" `x`/claude",
                          '#!/bin/sh\n{ printf "%s\\n" "$0" "$@"; } > "$QORQBOT_TEST_RECORDS/claude.$$"\n')
        calls, _ = self.run_in(self.elsewhere, "claude 'a b'", str(self.owner_bin), QORQBOT_MANAGED_CLAUDE=awkward)
        self.assertEqual(calls, [[awkward, "a b"]])

    def test_the_wrappers_work_with_unset_variables_treated_as_errors(self):
        owner_first = str(self.owner_bin) + ":" + self.managed_path()
        set_u = "set -u; " if self.SHELL == "bash" else "setopt nounset; "
        calls, process = self.run_in(self.elsewhere, set_u + "claude hi; codex hi", owner_first)
        self.assertEqual(len(calls), 2, process.stderr)
        calls, process = self.run_in(self.elsewhere, set_u + "claude hi; codex hi", owner_first, **self.markers())
        self.assertEqual(sorted(call[0] for call in calls), sorted([self.claude, self.codex]), process.stderr)

    def test_a_session_without_markers_gets_no_updater_settings(self):
        calls, _ = self.run_in(self.workspace, "QORQBOT_ROLE_SENT=1 claude; QORQBOT_ROLE_SENT=1 codex", self.managed_path())
        self.assertEqual(sorted(calls), sorted([[self.claude, "--settings", str(self.launch_settings), "updater=,"],
                                                [self.codex, "updater=,"]]))


class BashWrappers(Wrappers, Fixture):
    SHELL, command = "bash", ["bash", "-c"]
    generate = staticmethod(agent_shell)


class ZshWrappers(Wrappers, Fixture):
    """Skipped where zsh is not installed (it is the macOS default shell)."""
    SHELL, command = "zsh", ["zsh", "-f", "-c"]
    generate = staticmethod(agent_zshrc)


class StartSites(Fixture):
    def test_the_installed_versions_probe_runs_our_copy_when_it_is_selected(self):
        self.install()
        commands = []
        with mock.patch("qorqut.bootstrap.preparation.subprocess.run",
                        side_effect=lambda command, **kw: commands.append(command) or SimpleNamespace(stdout="v\n")):
            self.assertEqual(installed_versions(), {"claude-code": "v", "codex": "v"})
        self.assertEqual(commands, [[self.claude, "--version"], [self.codex, "--version"]])
        self.selected.clear()
        commands.clear()
        with mock.patch("qorqut.bootstrap.preparation.subprocess.run",
                        side_effect=lambda command, **kw: commands.append(command) or SimpleNamespace(stdout="v\n")):
            installed_versions()
        self.assertEqual(commands, [["claude", "--version"], ["codex", "--version"]])

    def delegated(self, runtime, executable):
        return delegated_runtime.DelegatedRuntime({}, "00000000-0000-4000-8000-000000000001", alive=lambda: True, runtime=runtime,
                                                  client=object(), executable=executable)

    def test_delegated_workers_on_our_copy_get_its_settings_and_others_do_not(self):
        self.install()
        codex = self.delegated("codex", self.codex)
        assignment = {"selected_model": "m", "reasoning_effort": "low", "allowed_operations_json": []}
        registration = {"service_url": "http://x", "workspace": str(self.base)}
        command = codex.command(assignment, registration, Path("/r.json"), None)
        self.assertEqual(command[:3], [self.codex, *CODEX_ARGS])
        self.assertEqual(command[3], "exec")
        self.assertEqual(self.delegated("claude-code", self.claude).environment("/tmp/w")["DISABLE_UPDATES"], "1")
        own = program(self.base / "own/codex")
        self.assertEqual(self.delegated("codex", own).command(assignment, registration, Path("/r.json"), None)[0], own)
        self.assertNotIn("-c", self.delegated("codex", own).command(assignment, registration, Path("/r.json"), None)[:2])
        self.assertNotIn("DISABLE_UPDATES", self.delegated("claude-code", own).environment("/tmp/w"))

    def queue(self, executable=None):
        from qorqut.native_chat.adapters import CodexQueueAdapter
        calls = []
        adapter = CodexQueueAdapter(run=lambda command, **kw: calls.append((command, kw)) or SimpleNamespace(returncode=0),
                                    alive=lambda owner: True, executable=executable)
        adapter.submit({"workspace": str(self.base)}, {"owner": "o", "conversation_id": "c"}, "hello", steer=False, echo=False,
                       checkpoint=lambda: None)
        return calls[0]

    def test_codex_queue_runs_the_selected_copy_at_each_queue(self):
        self.install()
        command, _ = self.queue()
        self.assertEqual(command, [self.codex, *CODEX_ARGS, "queue", "--thread", "c", "--message", "hello"])
        self.selected.clear()
        command, _ = self.queue()
        self.assertEqual(command, ["codex", "queue", "--thread", "c", "--message", "hello"])
        # An executable named by the caller is used as given.
        self.assertEqual(self.queue("/opt/codex")[0][0], "/opt/codex")

    def test_the_context_runner_uses_the_selected_copy_unless_the_configuration_names_its_own(self):
        from qorqut_context.runner import AppServerRunner, RunnerConfig
        from qorqut.shared_context.service import ManagedCodexRunner
        seen = []
        ARGS = {"stage": "bootstrap", "instructions": "", "payload": {}, "directory": "."}
        with mock.patch.object(AppServerRunner, "run", autospec=True, side_effect=lambda runner, **kw: seen.append(runner.config.executable)):
            self.install()
            ManagedCodexRunner(RunnerConfig()).run(**ARGS)
            ManagedCodexRunner(RunnerConfig(executable="/opt/codex")).run(**ARGS)
            self.selected.clear()
            ManagedCodexRunner(RunnerConfig()).run(**ARGS)
        self.assertEqual(seen, [self.codex, "/opt/codex", "codex"])


class TerminalSessions(Fixture):
    """Terminal sessions created after a download see our copy first, with nothing relaunched."""

    def service(self, broker):
        from qorqut.transport.terminals import TerminalExecution, TerminalService
        service = TerminalService.__new__(TerminalService)
        service.shell, service.broker = ["/bin/bash", "-i"], broker
        service.global_environment = lambda: {"OWNER_SETTING": "1"}
        service.agent = lambda context, selector: (SimpleNamespace(agent_id="a"), {})
        service.binding = lambda business, row, broker: ("key", "session", str(self.base))
        service._prepare = lambda execution, agent_id: None
        service.workspace_binding = lambda context, slot: ("key", "session", str(self.base))
        return service, TerminalExecution

    def broker(self):
        broker = mock.MagicMock()
        broker.tmux_available.return_value = True
        broker.session_alive.return_value = False
        return broker

    def test_a_new_agent_session_gets_our_copy_first_on_path(self):
        self.install()
        broker = self.broker()
        service, Execution = self.service(broker)
        execution = Execution(broker, "http://x", None, lambda: {}, lambda profile, business: {"A": "1"})
        with mock.patch.dict(os.environ, {"PATH": "/service"}):
            service._open_admitted(None, None, "agent", cols=80, rows=24, execution=execution, reconnect=False)
        env = broker.ensure_session.call_args.kwargs["env"]
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["PATH"].split(os.pathsep)[:2], [str(Path(self.claude).parent), str(Path(self.codex).parent)])
        self.assertEqual(env["QORQBOT_MANAGED_CODEX"], self.codex)

    def test_a_session_for_an_owner_with_their_own_clis_has_the_same_environment_as_before(self):
        self.selected.clear()
        self.install()
        broker = self.broker()
        service, Execution = self.service(broker)
        execution = Execution(broker, "http://x", None, lambda: {}, lambda profile, business: {"A": "1"})
        service._open_admitted(None, None, "agent", cols=80, rows=24, execution=execution, reconnect=False)
        self.assertEqual(broker.ensure_session.call_args.kwargs["env"], {"A": "1"})

    def test_a_container_target_keeps_its_own_environment(self):
        self.install()
        broker = self.broker()
        service, Execution = self.service(broker)
        execution = Execution(broker, "http://x", None, lambda: {}, lambda profile, business: {"A": "1"}, target_identity="container")
        service._open_admitted(None, None, "agent", cols=80, rows=24, execution=execution, reconnect=False)
        self.assertNotIn("PATH", broker.ensure_session.call_args.kwargs["env"])

    def test_the_workspace_shell_is_the_owners_shell_with_no_updater_variables_and_reaches_our_copy_by_a_launcher(self):
        self.install()
        broker = self.broker()
        service, _ = self.service(broker)
        folder = self.base / "folder"
        with mock.patch.dict(os.environ, {"QORQUT_FOLDER_ROOT": str(folder)}):
            service._open_workspace_admitted(None, slot="1", cols=80, rows=24)
        env = broker.ensure_session.call_args.kwargs["env"]
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(folder / "tools/launchers"))
        self.assertEqual(env["OWNER_SETTING"], "1")
        for name in ("DISABLE_AUTOUPDATER", "DISABLE_UPDATES"):
            self.assertNotIn(name, env)
        self.assertNotIn(str(Path(self.claude).parent), env["PATH"].split(os.pathsep))


if __name__ == "__main__":
    unittest.main()
