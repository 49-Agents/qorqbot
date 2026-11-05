import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest

from bazarlyk import owner_settings


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


class AgentClientsTest(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.bin)
        self.calls = []

    def cli(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def fake_run(self, outputs):
        def run(command, **options):
            self.calls.append(command[1:])
            code, out = outputs.get(tuple(command[1:]), (1, ""))
            return SimpleNamespace(returncode=code, stdout=out, stderr="")
        return run

    def view(self, outputs):
        return owner_settings.agent_clients_view({"PATH": str(self.bin), "HOME": str(self.bin)}, run=self.fake_run(outputs))

    def test_both_installed_and_signed_in(self):
        self.cli("claude"); self.cli("codex")
        view = self.view({("--version",): (0, "2.1.318 (Claude Code)\n"),
                          ("auth", "status"): (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "o@x.y", "subscriptionType": "max"})),
                          ("login", "status"): (0, "Logged in using ChatGPT\n")})
        self.assertEqual((view["claude"]["version"], view["claude"]["signed_in"], view["claude"]["account"]["plan"]), ("2.1.318", True, "max"))
        self.assertEqual((view["codex"]["signed_in"], view["codex"]["detail"]), (True, "Logged in using ChatGPT"))

    def test_missing_and_signed_out(self):
        self.cli("codex")
        view = self.view({("login", "status"): (1, "Not logged in\n")})
        self.assertEqual(view["claude"], {"installed": False})
        self.assertFalse(view["codex"]["signed_in"])


if __name__ == "__main__":
    unittest.main()
