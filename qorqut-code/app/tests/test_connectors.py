import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut import connectors


class ConnectorsTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.runtime_dir = self.directory / "runtime"
        self.runtime_dir.mkdir()
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        npx = self.bin / "npx"
        npx.write_text("#!/bin/sh\n")
        npx.chmod(0o755)
        self.environ = {"PATH": str(self.bin), "HOME": str(self.directory), "SHELL": "/nonexistent"}
        self.runtime = SimpleNamespace(paths=SimpleNamespace(runtime=self.runtime_dir), resolver=SimpleNamespace(native_connections={}))

    def test_view_lists_the_extensions_with_their_statuses(self):
        result = connectors.view(self.runtime, environ=self.environ)
        self.assertEqual([item["id"] for item in result["connectors"]], ["treg", "whatsapp"])
        status = {item["id"]: item["status"] for item in result["connectors"]}
        self.assertEqual(status["treg"], "not_installed")
        self.assertEqual(status["whatsapp"], "coming_soon" if connectors.host_platform() == "darwin" else "not_installed")
        self.assertEqual(set(result), {"connectors", "clients", "platform"})

    def test_only_qorqbot_extensions_are_listed(self):
        items = connectors.view(self.runtime, environ=self.environ)["connectors"]
        self.assertEqual({item["kind"] for item in items}, {"extension"})
        ids = [item["id"] for item in items]
        for hidden in ("browser", "chrome", "playwright", "gmail", "google_calendar", "google_drive", "notion", "slack"):
            self.assertNotIn(hidden, ids)

    def test_the_view_never_runs_a_program(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no program may run")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no program may run")):
            connectors.view(self.runtime, environ=self.environ)

    def test_treg_follows_its_extension(self):
        treg = lambda: next(item for item in connectors.view(self.runtime, environ=self.environ)["connectors"]
                            if item["id"] == "treg")
        self.assertEqual(treg()["status"], "not_installed")
        extension = SimpleNamespace(id="treg", status=lambda: "not_set_up")
        self.runtime.selection = SimpleNamespace(extensions=(SimpleNamespace(id="other"), extension))
        self.assertEqual(treg(), {"id": "treg", "kind": "extension", "clients": ["claude-code", "codex"],
                                  "status": "not_set_up"})
        extension.status = lambda: "connected"
        self.assertEqual(treg()["status"], "connected")

    def test_whatsapp_without_extension_is_coming_soon_on_mac(self):
        self.runtime.resolver.native_connections = {}
        self.assertEqual(connectors._whatsapp_status(self.runtime, "darwin"), {"status": "coming_soon"})
        self.assertEqual(connectors._whatsapp_status(self.runtime, "wsl"), {"status": "not_installed"})
        self.assertEqual(connectors._whatsapp_status(self.runtime, "linux"), {"status": "not_installed"})

    def test_whatsapp_status_follows_setup(self):
        self.runtime.resolver.native_connections = {"whatsapp": object()}
        instance = self.runtime_dir.parent
        self.assertEqual(connectors._whatsapp_status(self.runtime), {"status": "not_set_up"})
        self.assertEqual(connectors._whatsapp_status(self.runtime, "darwin"), {"status": "not_set_up"})
        (instance / "config").mkdir()
        (instance / "config/whatsapp-personal.json").write_text("{}")
        self.assertEqual(connectors._whatsapp_status(self.runtime), {"status": "not_paired"})
        (instance / "private/whatsapp").mkdir(parents=True)
        (instance / "private/whatsapp/session.sqlite3").write_text("")
        self.assertEqual(connectors._whatsapp_status(self.runtime), {"status": "connected"})


class ProbeEnvironmentTest(unittest.TestCase):
    def test_matches_agent_terminals(self):
        environ = {"HOME": "/app/home", "CLAUDE_CONFIG_DIR": "/Users/owner/.claude", "PATH": "/app/bin",
                   "QORQUT_NATIVE_USER_ENV": json.dumps({"HOME": "/Users/owner", "CLAUDE_CONFIG_DIR": None,
                                                         "PATH": "/Users/owner/.local/bin:/usr/bin"})}
        result = connectors._probe_environment(environ)
        self.assertEqual(result["HOME"], "/Users/owner")
        self.assertNotIn("CLAUDE_CONFIG_DIR", result)
        self.assertEqual(result["PATH"].split(":")[0], "/Users/owner/.local/bin")
        self.assertEqual(connectors._probe_environment({"HOME": "/h", "QORQUT_NATIVE_USER_ENV": "{"})["HOME"], "/h")


class PlatformTest(unittest.TestCase):
    def test_wsl_is_detected(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        version = directory / "version"
        version.write_text("Linux version 5.15.153.1-microsoft-standard-WSL2")
        with mock.patch.object(connectors.os, "uname", return_value=SimpleNamespace(sysname="Linux")):
            self.assertEqual(connectors.host_platform(str(version)), "wsl")
            version.write_text("Linux version 6.8.0-generic")
            self.assertEqual(connectors.host_platform(str(version)), "linux")
            self.assertEqual(connectors.host_platform(str(directory / "missing")), "linux")
        runtime_dir = directory / "runtime"
        runtime_dir.mkdir()
        runtime = SimpleNamespace(paths=SimpleNamespace(runtime=runtime_dir), resolver=SimpleNamespace(native_connections={}))
        with mock.patch.object(connectors, "host_platform", return_value="wsl"):
            result = connectors.view(runtime, environ={})
        self.assertEqual(result["platform"], "wsl")


class AgentShellTest(unittest.TestCase):
    """The generated wrappers, run in the system bash (3.2 on macOS) against stand-in CLIs that print their arguments, from
    the agent's workspace root. Where they apply is tested in test_agent_shell_scope.py."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="agent shell "))
        self.addCleanup(shutil.rmtree, self.directory)
        self.runtime_dir = self.directory / "runtime"
        self.runtime_dir.mkdir()
        self.workspace = self.directory / "agent workspace"
        self.workspace.mkdir()
        bin_dir = self.directory / "bin"
        bin_dir.mkdir()
        for name in ("claude", "codex", "npx"):
            path = bin_dir / name
            path.write_text("#!/bin/sh\nprintf '%s|' " + name + " \"$@\"\necho\n")
            path.chmod(0o755)
        self.environ = {"PATH": str(bin_dir) + ":/usr/bin:/bin", "HOME": str(self.directory)}
        from qorqut.agent_trust import marker_path
        from qorqut.bootstrap.preparation import agent_shell
        self.marker = marker_path(self.runtime_dir)
        self.shell = self.directory / "agent-shell.bash"
        self.trusted = self.directory / "trust ran"
        self.shell.write_text(agent_shell(shlex.quote(str(self.directory / "launch settings.json")), shlex.quote(str(self.marker)),
                                          shlex.quote(str(self.workspace)),
                                          "touch " + shlex.quote(str(self.trusted))))

    def run_shell(self, command):
        return subprocess.run(["/bin/bash", "-c", ". \"$1\"; " + command, "bash", str(self.shell)], capture_output=True,
                              text=True, check=True, env=self.environ, cwd=self.workspace).stdout.splitlines()

    def test_without_consent(self):
        claude, codex = self.run_shell("claude 'hello there'; codex resume --last")
        self.assertEqual(claude, "claude|--settings|" + str(self.directory / "launch settings.json") + "|hello there|")
        self.assertEqual(codex, "codex|resume|--last|")

    def test_a_bare_launch_opens_with_the_role_prompt_unless_start_sends_it(self):
        claude, codex, started_claude, started_codex = self.run_shell(
            "claude; codex; QORQBOT_ROLE_SENT=1 claude; QORQBOT_ROLE_SENT=1 codex")
        self.assertEqual(claude.split("|")[-2], "/role")
        self.assertEqual(codex, "codex|$role|")
        self.assertTrue(started_claude.endswith("launch settings.json|"))
        self.assertEqual(started_codex, "codex|")

    def test_folder_trust_is_rechecked_before_each_launch_only_with_consent(self):
        self.run_shell("claude; codex")
        self.assertFalse(self.trusted.exists())
        self.marker.write_text("{}")
        self.run_shell("claude")
        self.assertTrue(self.trusted.exists())
        self.trusted.unlink()
        self.run_shell("codex")
        self.assertTrue(self.trusted.exists())

    def test_a_failing_trust_check_never_blocks_the_launch(self):
        from qorqut.bootstrap.preparation import agent_shell
        self.marker.write_text("{}")
        self.shell.write_text(agent_shell(shlex.quote(str(self.directory / "launch settings.json")), shlex.quote(str(self.marker)),
                                          shlex.quote(str(self.workspace)), "false"))
        claude, = self.run_shell("claude hi")
        self.assertTrue(claude.startswith("claude|--settings|"))


class ZshAgentShellTest(unittest.TestCase):
    """The zsh agent shell, run in the real zsh: the owner's own zsh setup loads first, then the wrappers."""

    def setUp(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh is not installed")
        from qorqut.agent_trust import marker_path
        from qorqut.bootstrap.preparation import ZSH_ENV, agent_zshrc
        self.directory = Path(tempfile.mkdtemp(prefix="zsh shell "))
        self.addCleanup(shutil.rmtree, self.directory)
        self.home = self.directory / "home"
        bin_dir = self.home / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("claude", "codex", "npx"):
            path = bin_dir / name
            path.write_text("#!/bin/sh\nprintf '%s|' " + name + " \"$@\"\necho\n")
            path.chmod(0o755)
        (self.home / ".zshenv").write_text("export FROM_ZSHENV=yes\n")
        (self.home / ".zshrc").write_text('export PATH="$HOME/bin:$PATH"\nalias hello="echo hi"\n')
        self.runtime_dir = self.directory / "runtime"
        self.runtime_dir.mkdir()
        self.marker = marker_path(self.runtime_dir)
        self.workspace = self.directory / "workspace"
        self.prepared = self.workspace / ".qorqut/zsh"
        self.prepared.mkdir(parents=True)
        (self.prepared / ".zshenv").write_text(ZSH_ENV)
        (self.prepared / ".zshrc").write_text(agent_zshrc(shlex.quote(str(self.directory / "launch settings.json")),
                                                          shlex.quote(str(self.marker)),
                                                                          shlex.quote(str(self.workspace))))

    def run_zsh(self, command, user_zdotdir=""):
        environ = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "ZDOTDIR": str(self.prepared),
                   "QORQBOT_USER_ZDOTDIR": user_zdotdir, "TERM": "dumb"}
        return subprocess.run(["zsh", "-i", "-c", command], capture_output=True, text=True, env=environ,
                              check=True, timeout=30, cwd=self.workspace).stdout.splitlines()

    def test_owner_setup_loads_and_wrappers_apply(self):
        self.marker.write_text("{}")
        lines = self.run_zsh('claude "a b"; codex resume; echo "zdotdir=${ZDOTDIR-unset}"; echo "env=$FROM_ZSHENV"; '
                             'alias hello; echo "left=${QORQBOT_USER_ZDOTDIR-unset}"')
        self.assertEqual(lines[0], "claude|--settings|" + str(self.directory / "launch settings.json") + "|a b|")
        self.assertEqual(lines[1], "codex|--dangerously-bypass-hook-trust|resume|")
        self.assertEqual(lines[2:], ["zdotdir=unset", "env=yes", "hello='echo hi'", "left=unset"])

    def test_owner_zdotdir_is_kept(self):
        custom = self.directory / "custom zsh"
        custom.mkdir()
        (custom / ".zshrc").write_text('export PATH="$HOME/bin:$PATH"\nexport FROM_CUSTOM=yes\n')
        lines = self.run_zsh('echo "$FROM_CUSTOM"; echo "$ZDOTDIR"; codex', user_zdotdir=str(custom))
        self.assertEqual(lines, ["yes", str(custom), "codex|$role|"])

    def test_start_prefix_reaches_the_wrapper_in_zsh(self):
        lines = self.run_zsh("claude; QORQBOT_ROLE_SENT=1 claude; codex; QORQBOT_ROLE_SENT=1 codex")
        self.assertEqual([line.split("|")[-2] for line in lines[:2]],
                         ["/role", str(self.directory / "launch settings.json")])
        self.assertEqual(lines[2:], ["codex|$role|", "codex|"])


if __name__ == "__main__":
    unittest.main()
