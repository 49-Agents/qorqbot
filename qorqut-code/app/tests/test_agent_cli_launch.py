"""Every place that starts an agent CLI finds Qorqbot's own downloaded copy at that moment, and only that copy is kept from updating.

``agent_cli_install`` is replaced by a stand-in that says which copy is selected, so nothing is downloaded and no real
Claude Code or Codex runs. """
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
from qorqut.bootstrap.preparation import installed_versions

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


class ManagedSessions(Fixture):
    """A managed session opened after a download sees our copy first, with nothing relaunched."""

    def launch(self, environment=lambda profile, business: {"A": "1"}):
        from qorqut.transport.managed_launch import ManagedLaunch
        launch = ManagedLaunch.__new__(ManagedLaunch)
        launch.agent_environment = environment
        launch.environment = launch.global_environment = lambda *a: {}
        return launch

    def test_a_new_agent_session_gets_our_copy_first_on_path(self):
        self.install()
        launch = self.launch()
        with mock.patch.dict(os.environ, {"PATH": "/service"}):
            env = launch._environment(SimpleNamespace(agent_id="a"), None)
            from qorqut import agent_cli_launch as acl
            env = acl.session_environment(dict(env))
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["PATH"].split(os.pathsep)[:2], [str(Path(self.claude).parent), str(Path(self.codex).parent)])
        self.assertEqual(env["QORQBOT_MANAGED_CODEX"], self.codex)

    def test_a_session_for_an_owner_with_their_own_clis_has_the_same_environment_as_before(self):
        self.selected.clear()
        self.install()
        from qorqut import agent_cli_launch as acl
        self.assertEqual(acl.session_environment({"A": "1"}), {"A": "1"})


if __name__ == "__main__":
    unittest.main()
