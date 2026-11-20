import json
from pathlib import Path
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
        self.assertEqual([item["id"] for item in result["connectors"]], ["openrouter", "treg", "whatsapp"])
        status = {item["id"]: item["status"] for item in result["connectors"]}
        self.assertEqual(status["treg"], "not_installed")
        self.assertEqual(status["openrouter"], "not_installed")
        self.assertEqual(status["whatsapp"], "coming_soon" if connectors.host_platform() == "darwin" else "not_installed")
        self.assertEqual(set(result), {"connectors", "platform"})

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

    def test_openrouter_follows_its_extension(self):
        openrouter = lambda: next(item for item in connectors.view(self.runtime, environ=self.environ)["connectors"]
                                  if item["id"] == "openrouter")
        self.assertEqual(openrouter()["status"], "not_installed")
        extension = SimpleNamespace(id="openrouter", status=lambda: "not_set_up")
        self.runtime.selection = SimpleNamespace(extensions=(extension, SimpleNamespace(id="treg", status=lambda: "connected")))
        self.assertEqual(openrouter(), {"id": "openrouter", "kind": "extension", "clients": ["claude-code", "codex"],
                                        "status": "not_set_up"})
        extension.status = lambda: "connected"
        self.assertEqual(openrouter()["status"], "connected")

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
    def test_matches_the_owners_own_command_line(self):
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


if __name__ == "__main__":
    unittest.main()
