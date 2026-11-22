"""The prepared agent folder is the same bytes in managed mode as without the terminal code, and carries the prepared
shell and the mode mark in Terminals mode only.

The resolver and the agent configuration are small stand-ins; every path is a temporary folder, and nothing reads or writes
a real user's CLI files (isolated_cli_environment)."""
import isolated_cli_environment

import json
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.bootstrap import preparation
from qorqut.managed_runtime import mode

SHELL_FILES = (".qorqut/agent-shell.bash", ".qorqut/zsh/.zshenv", ".qorqut/zsh/.zshrc")


class Fixture(unittest.TestCase):
    def setUp(self):
        isolated_cli_environment.isolate(self)
        self.root = Path(tempfile.mkdtemp(prefix="prep modes ")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.runtime = self.root / "runtime"
        self.workspace = self.root / "operations" / "agents" / "worker one"
        for folder in (self.runtime, self.workspace):
            folder.mkdir(parents=True)
        self.paths = SimpleNamespace(runtime=self.runtime, context=self.root / "context", operations=self.root / "operations")
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE businesses (id TEXT, slug TEXT, archived_at TEXT)")
        self.connection.execute("CREATE TABLE agent_profiles (agent_id TEXT, home_business_id TEXT)")

        @contextmanager
        def read():
            yield SimpleNamespace(connection=self.connection)
        view = SimpleNamespace(runtime_instructions=lambda: SimpleNamespace(bytes=lambda: b"snapshot"))
        self.resolver = SimpleNamespace(
            paths=self.paths, database=SimpleNamespace(read=read), native_connections={}, registry=SimpleNamespace(entries=lambda: []),
            require_agentic_view=lambda: view, native_guidance_digest=lambda data: data["native_guidance_digest"],
            check_agentic_revision=lambda revision: None)
        self.data = {
            "schema_version": 1, "agent_id": "agent-1", "profile_revision": 1, "name": "worker-one", "display_name": "Worker One",
            "status": "active", "runtime": {"kind": "claude-code"}, "role": {"id": "worker"}, "scope": {"kind": "ecosystem"},
            "assignment_brief": "", "plugins": [], "tool_policy": {}, "presentation": {}, "created_at": "2026-01-01T00:00:00Z",
            "workspace": str(self.workspace), "tools": [], "skills": [], "native_guidance_digest": "d", "agentic_catalog_revision": 1,
            "agentic_selection": {}, "mandatory_context": {}}
        self.config = SimpleNamespace(to_dict=lambda: self.data)
        for target, replacement in (
                ("qorqut.bootstrap.preparation.external_connections", lambda resolver, data: []),
                ("qorqut.bootstrap.preparation.skill_projection", lambda resolver, data: ({}, {})),
                ("qorqut.agentic.text.render", lambda view, *names: "boundary"),
                ("qorqut_agentic.behavior.workspace.documents", lambda *a, **k: {"CLAUDE.md": b"c", "AGENTS.md": b"a"})):
            patch = mock.patch(target, replacement)
            patch.start()
            self.addCleanup(patch.stop)

    def build(self, terminal):
        with mock.patch.object(mode, "current", return_value=mode.TERMINAL if terminal else mode.MANAGED):
            return preparation.build_projection(
                self.resolver, self.config, registration_id="reg-1", endpoint="http://127.0.0.1:1096",
                interpreter=sys.executable)


class ManagedUnchanged(Fixture):
    def test_managed_has_no_shell_files_and_no_mode_key_anywhere(self):
        projection = self.build(False)
        for name in (*SHELL_FILES, ".qorqut/runtime-mode"):
            self.assertNotIn(name, projection.files)
            self.assertNotIn(name, projection.metadata["files"])
        self.assertNotIn("runtime_mode", json.loads(projection.files[".qorqut/registration.json"]))
        for name, body in projection.files.items():
            self.assertNotIn(b"runtime_mode", body, name)

    def test_managed_is_the_same_bytes_with_the_terminal_branch_unreachable(self):
        with_branch = self.build(False)
        with mock.patch.object(preparation, "_terminal_mode", return_value=False):
            without = preparation.build_projection(
                self.resolver, self.config, registration_id="reg-1", endpoint="http://127.0.0.1:1096", interpreter=sys.executable)
        self.assertEqual(with_branch.files, without.files)
        self.assertEqual(with_branch.metadata, without.metadata)

    def test_no_setting_file_is_managed_and_the_terminal_side_is_not_asked(self):
        self.assertFalse(mode.mode_file(self.paths).exists())
        with mock.patch("qorqut.agent_trust.marker_path", side_effect=AssertionError("terminal side used")):
            projection = preparation.build_projection(
                self.resolver, self.config, registration_id="reg-1", endpoint="http://127.0.0.1:1096", interpreter=sys.executable)
        self.assertNotIn(SHELL_FILES[0], projection.files)

    def test_only_the_terminal_additions_differ(self):
        managed, terminal = self.build(False), self.build(True)
        added = set(terminal.files) - set(managed.files)
        self.assertEqual(added, set(SHELL_FILES))
        changed = {name for name in managed.files if managed.files[name] != terminal.files[name]}
        self.assertEqual(changed, {".qorqut/registration.json"})
        before, after = json.loads(managed.files[changed.pop()]), json.loads(terminal.files[".qorqut/registration.json"])
        self.assertEqual({key: after[key] for key in after if key != "runtime_mode"}, before)


class TerminalProjection(Fixture):
    def test_the_registration_names_the_mode(self):
        registration = json.loads(self.build(True).files[".qorqut/registration.json"])
        self.assertEqual(registration["runtime_mode"], "terminal")

    def test_the_mode_is_asked_at_call_time(self):
        self.assertNotIn(SHELL_FILES[0], self.build(False).files)
        self.assertIn(SHELL_FILES[0], self.build(True).files)
        self.assertNotIn(SHELL_FILES[0], self.build(False).files)

    def test_the_shell_files_are_tracked_in_the_metadata(self):
        projection = self.build(True)
        for name in SHELL_FILES:
            self.assertEqual(projection.metadata["files"][name], preparation.content_hash(projection.files[name]))

    def test_the_wrapper_passes_the_settings_file_and_the_role_rule_and_has_no_codex(self):
        projection = self.build(True)
        settings = str(self.workspace / ".qorqut/claude-launch-settings.json")
        for name in SHELL_FILES[:1] + SHELL_FILES[2:]:
            text = projection.files[name].decode()
            self.assertIn("--settings " + shlex.quote(settings), text)
            self.assertIn("set -- /role", text)
            self.assertIn("QORQBOT_ROLE_SENT", text)
            self.assertIn("QORQBOT_MANAGED_CLAUDE", text)
            self.assertIn(shlex.quote(str(self.runtime / "agent-trust.json")), text)
            self.assertIn("qorqut.agent_trust ensure", text)
            self.assertIn(shlex.quote(str(self.workspace)), text)
            self.assertNotIn("codex", text.lower())
            self.assertNotIn("$role", text)
        self.assertNotIn(b"claude()", projection.files[SHELL_FILES[1]])

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_the_generated_bash_shell_parses_and_defines_only_the_claude_wrapper(self):
        path = self.root / "agent-shell.bash"
        path.write_bytes(self.build(True).files[SHELL_FILES[0]])
        process = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        process = subprocess.run(["bash", "-c", '. "$1"; declare -F', "bash", str(path)],
                                 capture_output=True, text=True, env={"HOME": str(self.root), "PATH": "/usr/bin:/bin"})
        functions = [line.split()[-1] for line in process.stdout.splitlines()]
        self.assertIn("claude", functions)
        self.assertNotIn("codex", functions)

    def test_the_launch_settings_file_is_the_same_in_both_modes(self):
        name = ".qorqut/claude-launch-settings.json"
        self.assertEqual(self.build(False).files[name], self.build(True).files[name])

    def test_only_ordubashy_is_denied_the_claude_tools_he_does_not_work_with(self):
        name = ".qorqut/claude-launch-settings.json"
        self.assertEqual(json.loads(self.build(False).files[name]), {"permissions": {"defaultMode": "bypassPermissions"}})
        self.data["role"] = {"id": "ordubashy"}
        with mock.patch("qorqut.management.identity.configured_ordubashy", lambda uow: "agent-1"):
            for terminal in (False, True):
                self.assertEqual(json.loads(self.build(terminal).files[name]), {"permissions": {
                    "defaultMode": "bypassPermissions", "deny": ["ListAgents", "ReportFindings", "ScheduleWakeup", "Workflow"]}})


if __name__ == "__main__":
    unittest.main()
