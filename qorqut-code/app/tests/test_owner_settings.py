import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut import owner_settings


class AboutTest(unittest.TestCase):
    def test_version_and_folders_of_a_folder_install(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "runtime").mkdir()
        (root / "runtime/PAYLOAD.json").write_text(json.dumps({"version": "1.3.2-local.abc"}))
        (root / "instance/logs").mkdir(parents=True)
        view = owner_settings.about_view({"QORQUT_FOLDER_ROOT": str(root)})
        self.assertEqual((view["version"], view["install_folder"]), ("1.3.2-local.abc", str(root)))
        self.assertEqual(view["data_folder"], str(root / "instance"))
        self.assertEqual(view["logs_folder"], str(root / "instance/logs"))

    def test_a_development_server_has_no_install_facts(self):
        view = owner_settings.about_view({})
        self.assertEqual((view["version"], view["install_folder"], view["data_folder"]), (None, None, None))

    def test_reports_whether_chrome_is_installed(self):
        from unittest import mock
        with mock.patch("qorqut.native_paths.installed_chrome", return_value=None):
            self.assertIs(owner_settings.about_view({})["chrome_installed"], False)
        with mock.patch("qorqut.native_paths.installed_chrome", return_value="/Applications/Google Chrome.app"):
            self.assertIs(owner_settings.about_view({})["chrome_installed"], True)


class AgentClientsTest(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        self.calls = []
        from qorqut import agent_cli_sign_in
        agent_cli_sign_in._pending.clear(); agent_cli_sign_in._failed.clear(); agent_cli_sign_in._cli_status_cache.clear()
        owner_settings._VERSIONS.clear()
        # The install module belongs to another change: these tests use the CLI on PATH unless one is supplied.
        self.install = None
        patch = mock.patch.object(agent_cli_sign_in, "_install", side_effect=lambda: self.install)
        patch.start(); self.addCleanup(patch.stop)

    def cli(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def stored_sign_in(self):
        (self.bin / ".claude").mkdir(exist_ok=True)
        (self.bin / ".claude/.credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"refreshToken": "r", "subscriptionType": "max"}}))
        (self.bin / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "o@x.y"}}))

    def fake_run(self, outputs):
        def run(command, **options):
            self.calls.append(command[1:])
            code, out = outputs.get(tuple(command[1:]), (1, ""))
            return SimpleNamespace(returncode=code, stdout=out, stderr="")
        return run

    def view(self, outputs):
        # Only discover this fixture's CLIs, even when the development host has
        # Claude installed in one of the production fallback directories.
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()):
            return owner_settings.agent_clients_view({"PATH": str(self.bin), "HOME": str(self.bin)}, run=self.fake_run(outputs))

    def test_both_installed_and_signed_in_without_asking_claude(self):
        self.cli("claude"); self.cli("codex"); self.stored_sign_in()
        view = self.view({("--version",): (0, "2.1.318 (Claude Code)\n"), ("login", "status"): (0, "Logged in using ChatGPT\n")})
        self.assertEqual((view["claude"]["version"], view["claude"]["signed_in"], view["claude"]["account"]["plan"],
                          view["claude"]["account"]["email"]), ("2.1.318", True, "max", "o@x.y"))
        self.assertEqual((view["codex"]["signed_in"], view["codex"]["account"]["method"]), (True, "chatgpt"))
        self.assertNotIn("detail", view["codex"])               # the CLI's own text is never passed on
        self.assertNotIn(["auth", "status", "--json"], self.calls)
        self.assertNotIn(["auth", "status"], self.calls)
        self.assertEqual((view["claude"]["source"], view["claude"]["sign_in"]), ("system", {"state": "idle", "reason": None}))

    def test_missing_and_signed_out(self):
        self.cli("codex")
        view = self.view({("login", "status"): (1, "Not logged in\n")})
        self.assertFalse(view["claude"]["installed"])
        self.assertEqual((view["claude"]["signed_in"], view["claude"]["account"], view["claude"]["install"]["phase"]),
                         (False, None, "not_downloaded"))
        self.assertFalse(view["codex"]["signed_in"])

    def test_a_managed_copy_reports_source_version_and_download_progress(self):
        class Managed:
            found = {"claude": {"path": str(self.bin / "claude"), "source": "managed", "version": "2.1.269"}}
            def resolve(self, provider, environ=None): return self.found.get(provider)
            def status(self, provider):
                return ({"phase": "ready", "progress": 100, "message": None, "version": "2.1.269"} if provider in self.found else
                        {"phase": "downloading", "progress": 40, "message": "Downloading", "version": None})
            def launch_environment(self, provider): return {"DISABLE_AUTOUPDATER": "1"}
        self.install = Managed()
        self.cli("claude"); self.stored_sign_in()
        view = self.view({})
        self.assertEqual((view["claude"]["installed"], view["claude"]["source"], view["claude"]["version"]), (True, "managed", "2.1.269"))
        self.assertEqual(view["claude"]["install"], {"phase": "ready", "progress": 100, "message": None})
        self.assertEqual(view["codex"]["install"], {"phase": "downloading", "progress": 40, "message": "Downloading"})
        self.assertFalse(view["codex"]["installed"])
        self.assertNotIn(["--version"], self.calls)            # the install module already knows the version

    def test_a_hung_version_probe_is_not_repeated_on_every_read(self):
        self.cli("claude"); self.stored_sign_in()
        calls = []

        def hung(command, **options):
            calls.append(command[1:])
            if command[1:] == ["--version"]:
                raise subprocess.TimeoutExpired(command, 15)
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        env = {"PATH": str(self.bin), "HOME": str(self.bin)}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()):
            for _ in range(5):
                view = owner_settings.agent_clients_view(env, run=hung)
        self.assertIsNone(view["claude"]["version"])
        self.assertEqual(calls.count(["--version"]), 1)
        for key, (expires, version) in list(owner_settings._VERSIONS.items()):
            owner_settings._VERSIONS[key] = (time.monotonic() - 1, version)         # the failure window has passed
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()):
            owner_settings.agent_clients_view(env, run=hung)
        self.assertEqual(calls.count(["--version"]), 2)

    def test_a_version_that_was_read_is_kept(self):
        self.cli("claude"); self.stored_sign_in()
        run = self.fake_run({("--version",): (0, "2.1.318 (Claude Code)\n")})
        env = {"PATH": str(self.bin), "HOME": str(self.bin)}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()):
            for _ in range(3):
                self.assertEqual(owner_settings.agent_clients_view(env, run=run)["claude"]["version"], "2.1.318")
        self.assertEqual(self.calls.count(["--version"]), 1)

    def test_a_version_the_install_module_knows_for_a_system_copy_is_used_without_a_probe(self):
        self.cli("claude"); self.stored_sign_in()
        path = str(self.bin / "claude")
        self.install = SimpleNamespace(
            resolve=lambda provider, environ=None: {"path": path, "source": "system", "version": "2.1.200"} if provider == "claude" else None,
            status=lambda provider: {"phase": "ready", "progress": None, "message": None, "version": None},
            launch_environment=lambda provider: {})
        self.assertEqual(self.view({})["claude"]["version"], "2.1.200")
        self.assertNotIn(["--version"], self.calls)

    def test_an_api_key_in_the_environment_is_signed_in_and_the_cli_is_never_asked(self):
        self.cli("claude"); self.stored_sign_in()
        env = {"PATH": str(self.bin), "HOME": str(self.bin), "ANTHROPIC_API_KEY": "k"}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()), mock.patch("qorqut.connectors._probe_environment", side_effect=lambda environ=None: dict(environ or {})):
            view = owner_settings.agent_clients_view(env, run=self.fake_run({("--version",): (0, "2.1.1 (Claude Code)")}))
        self.assertEqual((view["claude"]["signed_in"], view["claude"]["account"]["method"]), (True, "api_key"))
        self.assertNotIn(["auth", "status", "--json"], self.calls)

    def test_actions_drive_the_install_module_and_the_sign_in_and_return_the_view(self):
        from qorqut import agent_cli_sign_in
        from qorqut.errors import DomainError
        calls = []
        self.install = SimpleNamespace(
            resolve=lambda provider, environ=None: None,
            status=lambda provider: {"phase": "not_downloaded", "progress": None, "message": None, "version": None},
            launch_environment=lambda provider: {},
            start_download=lambda provider: calls.append(("download", provider)),
            cancel_download=lambda provider: calls.append(("cancel_download", provider)))
        env = {"PATH": str(self.bin), "HOME": str(self.bin)}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()):
            view = owner_settings.agent_clients_action("codex", "download", env, run=self.fake_run({}))
            owner_settings.agent_clients_action("claude", "cancel_download", env, run=self.fake_run({}))
            self.assertEqual(calls, [("download", "codex"), ("cancel_download", "claude")])
            self.assertEqual(set(view), {"claude", "codex"})
            with mock.patch.object(agent_cli_sign_in, "start_sign_in") as start, mock.patch.object(agent_cli_sign_in, "cancel_sign_in") as cancel:
                owner_settings.agent_clients_action("claude", "connect", env, run=self.fake_run({}))
                owner_settings.agent_clients_action("claude", "cancel_connect", env, run=self.fake_run({}))
            start.assert_called_once_with("claude", environ=env)
            cancel.assert_called_once_with("claude")
            for provider, action in (("gemini", "connect"), ("claude", "explode")):
                with self.assertRaises(DomainError):
                    owner_settings.agent_clients_action(provider, action, env)

    def test_download_without_the_install_module_is_unavailable(self):
        from qorqut.errors import DomainError
        with self.assertRaises(DomainError):
            owner_settings.agent_clients_action("claude", "download", {"PATH": str(self.bin), "HOME": str(self.bin)})


if __name__ == "__main__":
    unittest.main()
