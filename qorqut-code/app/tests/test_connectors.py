import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from bazarlyk import connectors
from qorqut_operations.contracts import DomainError

CLAUDE_LIST = """Checking MCP server health…

claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - ✔ Connected
claude.ai Notion: https://mcp.notion.com/mcp - ! Needs authentication
claude.ai Slack: https://mcp.slack.com/mcp - ✘ Failed to connect
plugin:playwright:playwright: npx @playwright/mcp@latest - ✔ Connected
"""


class FakeProbe:
    def __init__(self, result=None, checking=False):
        self.result, self.checking, self.refreshed = result, checking, []

    def view(self, environ, refresh=False, cache=None):
        self.refreshed.append(refresh)
        return {"checking": self.checking, "result": self.result}


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

    def test_parse_claude_list(self):
        self.assertEqual(connectors.parse_claude_list(CLAUDE_LIST),
                         {"Gmail": "connected", "Notion": "needs_sign_in", "Slack": "failed"})

    def test_view_combines_statuses(self):
        probe = FakeProbe({"available": True, "connectors": connectors.parse_claude_list(CLAUDE_LIST)})
        with mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}):
            result = connectors.view(self.runtime, environ=self.environ, probe=probe)
        status = {item["id"]: item["status"] for item in result["connectors"]}
        self.assertEqual(status["browser"], "connected")
        self.assertEqual(status["gmail"], "connected")
        self.assertEqual(status["notion"], "needs_sign_in")
        self.assertEqual(status["google_calendar"], "not_added")
        self.assertEqual(status["whatsapp"], "coming_soon" if connectors.host_platform() == "darwin" else "not_installed")
        self.assertNotIn("claude_name", result["connectors"][1])

    def test_the_agent_browser_and_gmail_are_recommended_and_no_local_browsers_remain(self):
        with mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}):
            items = connectors.view(self.runtime, environ=self.environ, probe=FakeProbe({"available": True, "connectors": {}}))
        ids = [item["id"] for item in items["connectors"]]
        self.assertEqual([item["id"] for item in items["connectors"] if item["tier"] == "recommended"], ["browser", "gmail"])
        self.assertNotIn("chrome", ids)
        self.assertNotIn("playwright", ids)

    def test_view_before_first_check_and_without_claude(self):
        with mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}):
            checking = connectors.view(self.runtime, environ=self.environ, probe=FakeProbe(None, True))
            missing = connectors.view(self.runtime, environ=self.environ,
                                      probe=FakeProbe({"available": False, "reason": "claude_missing"}))
        gmail = lambda result: next(item for item in result["connectors"] if item["id"] == "gmail")
        self.assertEqual(gmail(checking)["status"], "checking")
        self.assertTrue(checking["checking"])
        self.assertEqual(gmail(missing), {**gmail(missing), "status": "unavailable", "reason": "claude_missing"})

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

    def test_owner_view_only_refreshes(self):
        probe = FakeProbe({"available": True, "connectors": {}})
        with mock.patch.object(connectors, "PROBE", probe), \
                mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}):
            connectors.owner_view(self.runtime, {"refresh": True}, self.environ)
            connectors.owner_view(self.runtime, None, self.environ)
            for change in ({"id": "playwright", "enabled": True}, {"refresh": False}, {}):
                with self.assertRaises(DomainError):
                    connectors.owner_view(self.runtime, change, self.environ)
        self.assertEqual(probe.refreshed, [True, False])


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


class BrowserStatusTest(unittest.TestCase):
    def test_reports_what_is_missing(self):
        with mock.patch("bazarlyk.native_paths.installed_chrome", return_value=None), \
                mock.patch("bazarlyk.native_paths.installed_executable", return_value="/bin/agent-browser"):
            self.assertEqual(connectors._browser_status(), {"status": "needs_install", "missing": ["chrome"],
                                                            "install": "npm install -g agent-browser@0.38.1"})
        with mock.patch("bazarlyk.native_paths.installed_chrome", return_value="/Applications/Chrome"), \
                mock.patch("bazarlyk.native_paths.installed_executable", return_value=None):
            self.assertEqual(connectors._browser_status()["missing"], ["agent_browser"])
        with mock.patch("bazarlyk.native_paths.installed_chrome", return_value="/Applications/Chrome"), \
                mock.patch("bazarlyk.native_paths.installed_executable", return_value="/bin/agent-browser"):
            self.assertEqual(connectors._browser_status(), {"status": "connected"})


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
        with mock.patch.object(connectors, "host_platform", return_value="wsl"), \
                mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}):
            result = connectors.view(runtime, environ={}, probe=FakeProbe({"available": True, "connectors": {}}))
        self.assertEqual(result["platform"], "wsl")


class ClaudeProbeTest(unittest.TestCase):
    def test_runs_once_in_background_and_caches(self):
        calls, now = [], [0.0]

        def run(command, **options):
            calls.append((command, options["env"]["HOME"]))
            if command[1] == "auth":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                                                                         "email": "owner@example.com", "subscriptionType": "max"}))
            return SimpleNamespace(returncode=0, stdout=CLAUDE_LIST)

        probe = connectors.ClaudeProbe(run=run, clock=lambda: now[0])
        first = probe.view({"HOME": "/home/owner"})
        self.assertTrue(first["checking"])
        probe._thread.join()
        second = probe.view({"HOME": "/home/owner"})
        self.assertFalse(second["checking"])
        self.assertEqual(second["result"]["connectors"]["Gmail"], "connected")
        self.assertEqual(second["result"]["account"], {"signed_in": True, "method": "claude.ai", "email": "owner@example.com", "plan": "max"})
        self.assertEqual(calls, [(["claude", "auth", "status"], "/home/owner"), (["claude", "mcp", "list"], "/home/owner")])
        now[0] = connectors.PROBE_FRESH_FOR + 1
        probe.view({"HOME": "/home/owner"})
        probe._thread.join()
        self.assertEqual(len(calls), 4)

    def test_the_last_result_is_kept_across_restarts(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        cache = directory / "connectors-status.json"

        def run(command, **options):
            if command[1] == "auth":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True}))
            return SimpleNamespace(returncode=0, stdout=CLAUDE_LIST)

        first = connectors.ClaudeProbe(run=run)
        self.assertIsNone(first.view({}, cache=cache)["result"])
        first._thread.join()
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o600)
        # A new server process: the saved result shows at once while a fresh check runs.
        second = connectors.ClaudeProbe(run=lambda command, **options: SimpleNamespace(returncode=1, stdout=""))
        shown = second.view({}, cache=cache)
        self.assertTrue(shown["checking"])
        self.assertEqual(shown["result"]["connectors"]["Gmail"], "connected")
        second._thread.join()

    def test_a_damaged_saved_result_is_ignored(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        cache = directory / "connectors-status.json"
        for content in ("not json", "[]", json.dumps({"available": True})):
            cache.write_text(content)
            self.assertIsNone(connectors._read_cached(cache))

    def test_missing_or_failing_claude(self):
        def missing(command, **options):
            raise FileNotFoundError(command[0])

        probe = connectors.ClaudeProbe(run=missing)
        probe.view({})
        probe._thread.join()
        self.assertEqual(probe.view({})["result"], {"available": False, "reason": "claude_missing", "account": None})
        failing = connectors.ClaudeProbe(run=lambda command, **options: SimpleNamespace(returncode=1, stdout=""))
        failing.view({})
        failing._thread.join()
        self.assertEqual(failing.view({})["result"]["reason"], "claude_failed")


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
        from bazarlyk.agent_trust import marker_path
        from bazarlyk.bootstrap.preparation import agent_shell
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
        from bazarlyk.bootstrap.preparation import agent_shell
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
        from bazarlyk.agent_trust import marker_path
        from bazarlyk.bootstrap.preparation import ZSH_ENV, agent_zshrc
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
        self.prepared = self.workspace / ".bazarlyk/zsh"
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
