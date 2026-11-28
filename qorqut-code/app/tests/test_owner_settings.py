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

    def clean_environment(self):
        """No explicit Chrome choice from the environment this runs in."""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("QORQUT_CHROME_EXECUTABLE", "QORQUT_NATIVE_ROOT"):
            os.environ.pop(name, None)

    def test_reports_whether_google_chrome_is_installed(self):
        self.clean_environment()
        with mock.patch("qorqut.native_paths.installed_google_chrome", return_value=None), \
                mock.patch("qorqut.native_paths.installed_chrome", return_value="/usr/bin/chromium"):
            self.assertIs(owner_settings.about_view({})["chrome_installed"], False, "Chromium does not count")
        with mock.patch("qorqut.native_paths.installed_google_chrome", return_value="/Applications/Google Chrome.app"):
            self.assertIs(owner_settings.about_view({})["chrome_installed"], True)
        with mock.patch("qorqut.native_paths.installed_google_chrome", return_value=None), \
                mock.patch.dict(os.environ, {"QORQUT_CHROME_EXECUTABLE": "/opt/chosen/chrome"}):
            self.assertIs(owner_settings.about_view({})["chrome_installed"], True, "an explicit choice is never asked about")

    def test_without_google_chrome_the_owner_is_asked_for_it_unless_a_browser_keeps_a_usable_chrome(self):
        from qorqut.browser import managed_chrome
        self.clean_environment()
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        with mock.patch.object(managed_chrome, "platform_key", return_value="mac-arm64"), \
                mock.patch("qorqut.native_paths.installed_google_chrome", return_value=None):
            self.assertEqual(managed_chrome.view(folder)["state"], "not_needed")
            self.assertIs(owner_settings.about_view({}, runtime_directory=folder)["chrome_installed"], False)
            record = folder / "agent-browsers/earlier"
            record.mkdir(parents=True)
            (record / "meta.json").write_text(json.dumps({"mode": "managed",
                                                          "managed": {"chrome": str(folder / "chrome/current/x/chrome")}}))
            self.assertEqual(managed_chrome.view(folder)["state"], "waiting")
            self.assertIs(owner_settings.about_view({}, runtime_directory=folder)["chrome_installed"], True,
                          "a browser on Qorqbot's own build: its build is on its way")
            for state, enough in {"ready": True, "waiting": True, "downloading": True, "failed": True,
                                  "unusable": False, "unsupported": False}.items():
                with self.subTest(state=state), mock.patch.object(managed_chrome, "view", return_value={"state": state}):
                    self.assertIs(owner_settings.about_view({}, runtime_directory=folder)["chrome_installed"], enough)
        with mock.patch("qorqut.native_paths.installed_google_chrome", return_value="/usr/bin/google-chrome-stable"):
            self.assertIs(owner_settings.about_view({}, runtime_directory=folder)["chrome_installed"], True)


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
            if command[0] == "/usr/bin/security":     # macOS: the Keychain has no such item (exit status 44), so the file is read
                return SimpleNamespace(returncode=44, stdout="", stderr="")
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
        self.assertEqual((view["claude"]["source"], view["claude"]["sign_in"]["state"], view["claude"]["sign_in"]["reason"]), ("system", "idle", None))

    def test_the_row_carries_the_limit_the_last_switch_and_the_ways_to_sign_in(self):
        from qorqut import account_state
        account_state.reset_for_tests(); self.addCleanup(account_state.reset_for_tests)
        self.cli("claude"); self.cli("codex"); self.stored_sign_in()
        account_state.observe_turn("claude", "usage_limit", "2999-01-01T00:00:00Z", "Jan 1", "2026-11-24T10:00:00Z", "error")
        view = self.view({("--version",): (0, "2.1.318 (Claude Code)\n"), ("login", "status"): (0, "Logged in using ChatGPT\n")})
        self.assertEqual(view["claude"]["limit"], {"reached": True, "resets_at": "2999-01-01T00:00:00Z", "resets_text": "Jan 1"})
        self.assertEqual(view["codex"]["limit"], {"reached": False, "resets_at": None, "resets_text": None})
        self.assertEqual((view["claude"]["switched"], view["codex"]["switched"]), (None, None))
        self.assertEqual(view["claude"]["sign_in_methods"], {"available": ["browser"], "default": "browser"})
        self.assertEqual(view["codex"]["sign_in_methods"]["available"], ["browser", "device_code"])
        self.assertEqual(view["claude"]["sign_in"]["page_url"], None)
        self.assertEqual(account_state.clients_view(lambda: 1 / 0)["claude"]["limit"]["reached"], True)      # Chat reuses this read

    def test_the_agents_never_see_the_page_or_the_code(self):
        view = {"claude": {"sign_in": {"state": "connecting", "page_url": "https://claude.com/x?state=s", "user_code": "AB-1"}, "x": 1},
                "codex": None}
        shown = owner_settings.for_agents(view)
        self.assertEqual((shown["claude"]["sign_in"]["page_url"], shown["claude"]["sign_in"]["user_code"], shown["claude"]["x"]), (None, None, 1))
        self.assertEqual(view["claude"]["sign_in"]["user_code"], "AB-1")

    def test_switch_needs_a_signed_in_product_and_connect_on_a_signed_in_one_is_the_same_sign_in(self):
        from qorqut import agent_cli_sign_in
        from qorqut.errors import DomainError
        env = {"PATH": str(self.bin), "HOME": str(self.bin)}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()), mock.patch.object(agent_cli_sign_in, "start_sign_in") as start:
            with self.assertRaises(DomainError) as refused:
                owner_settings.agent_clients_action("claude", "switch", env, run=self.fake_run({}))
            self.assertIn("Sign in first", refused.exception.message)
            start.assert_not_called()
            self.cli("claude"); self.stored_sign_in()
            owner_settings.agent_clients_action("claude", "switch", env, run=self.fake_run({("--version",): (0, "2.1.318 (Claude Code)\n")}))
            owner_settings.agent_clients_action("claude", "connect", env, run=self.fake_run({("--version",): (0, "2.1.318 (Claude Code)\n")}))
            owner_settings.agent_clients_action("codex", "connect", env, method="device_code", run=self.fake_run({}))
        self.assertEqual([call.kwargs for call in start.call_args_list],
                         [{"environ": env}, {"environ": env}, {"environ": env, "method": "device_code"}])

    def test_submit_code_reaches_the_sign_in_and_its_refusals_are_plain_validation_errors(self):
        from qorqut import agent_cli_sign_in
        from qorqut.errors import DomainError
        env = {"PATH": str(self.bin), "HOME": str(self.bin)}
        with mock.patch("qorqut.connectors.SYSTEM_PATH", ()), mock.patch.object(agent_cli_sign_in, "submit_code") as submit:
            owner_settings.agent_clients_action("claude", "submit_code", env, run=self.fake_run({}), code="abc#def")
            submit.assert_called_once_with("claude", "abc#def")
            submit.side_effect = agent_cli_sign_in.SignInError("No sign-in is waiting. Start one first.")
            with self.assertRaises(DomainError) as refused:
                owner_settings.agent_clients_action("claude", "submit_code", env, run=self.fake_run({}), code="abc")
        self.assertEqual((refused.exception.code, refused.exception.message), ("validation_error", "No sign-in is waiting. Start one first."))

    def test_missing_and_signed_out(self):
        self.cli("codex")
        view = self.view({("login", "status"): (1, "Not logged in\n")})
        self.assertFalse(view["claude"]["installed"])
        self.assertEqual((view["claude"]["signed_in"], view["claude"]["account"], view["claude"]["install"]["phase"]),
                         (False, None, "not_downloaded"))
        self.assertFalse(view["codex"]["signed_in"])

    def test_a_managed_copy_reports_source_version_and_download_progress(self):
        class Managed:
            found = {"claude": {"path": str(self.bin / "claude"), "source": "managed", "version": "2.1.327"}}
            def resolve(self, provider, environ=None): return self.found.get(provider)
            def status(self, provider, environ=None):
                return ({"phase": "ready", "progress": 100, "message": None, "version": "2.1.327"} if provider in self.found else
                        {"phase": "downloading", "progress": 40, "message": "Downloading", "version": None})
            def launch_environment(self, provider): return {"DISABLE_AUTOUPDATER": "1"}
        self.install = Managed()
        self.cli("claude"); self.stored_sign_in()
        view = self.view({})
        self.assertEqual((view["claude"]["installed"], view["claude"]["source"], view["claude"]["version"]), (True, "managed", "2.1.327"))
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
            status=lambda provider, environ=None: {"phase": "ready", "progress": None, "message": None, "version": None},
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
            status=lambda provider, environ=None: {"phase": "not_downloaded", "progress": None, "message": None, "version": None},
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
