"""Owner consent lets Qorqbot record folder trust for its own agent workspaces, and nothing is written without it."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from bazarlyk import agent_trust as trust


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.folder = self.root / "agents" / "ordubashy"
        self.folder.mkdir(parents=True)
        self.environ = {"QORQUT_NATIVE_USER_ENV": json.dumps({"HOME": str(self.home)}), "HOME": "/app/home"}

    def claude(self, data):
        (self.home / ".claude.json").write_text(json.dumps(data))

    def claude_data(self):
        return json.loads((self.home / ".claude.json").read_text())

    def codex_text(self):
        return (self.home / ".codex" / "config.toml").read_text()


class ConsentTests(Base):
    def test_starts_without_consent_and_writes_nothing(self):
        self.assertEqual(trust.consent(self.runtime)["granted"], False)
        self.assertFalse(trust.marker_path(self.runtime).exists())

    def test_grant_and_revoke_round_trip(self):
        granted = trust.consent(self.runtime, {"granted": True})
        self.assertTrue(granted["granted"])
        self.assertIsInstance(granted["granted_at"], str)
        self.assertEqual(trust.consent(self.runtime), granted)
        self.assertEqual(os.stat(trust.marker_path(self.runtime)).st_mode & 0o777, 0o600)
        self.assertFalse(trust.consent(self.runtime, {"granted": False})["granted"])
        self.assertFalse(trust.marker_path(self.runtime).exists())

    def test_invalid_changes_are_refused(self):
        for change in ({}, {"granted": "yes"}, {"granted": True, "extra": 1}, [True]):
            with self.subTest(change=change), self.assertRaises(DomainError):
                trust.consent(self.runtime, change)
        self.assertFalse(trust.marker_path(self.runtime).exists())

    def test_corrupt_consent_is_reported(self):
        trust.marker_path(self.runtime).write_text("{")
        with self.assertRaises(DomainError):
            trust.consent(self.runtime)


class EnvironmentTests(Base):
    def test_the_users_paths_win_over_the_apps(self):
        env = trust.user_environment(self.environ)
        self.assertEqual(trust.claude_file(env), self.home / ".claude.json")
        self.assertEqual(trust.codex_file(env), self.home / ".codex" / "config.toml")

    def test_cli_config_directories_are_honoured(self):
        env = trust.user_environment({"HOME": str(self.home), "CLAUDE_CONFIG_DIR": "/c", "CODEX_HOME": "/x"})
        self.assertEqual(trust.claude_file(env), Path("/c/.claude.json"))
        self.assertEqual(trust.codex_file(env), Path("/x/config.toml"))

    def test_symlinked_folders_are_keyed_both_ways(self):
        link = self.root / "link"
        link.symlink_to(self.folder)
        self.assertEqual(trust.folder_keys(link), [str(link), os.path.realpath(self.folder)])
        self.assertEqual(trust.folder_keys(self.folder), [os.path.realpath(self.folder)] if str(self.folder) == os.path.realpath(self.folder) else [str(self.folder), os.path.realpath(self.folder)])


class ClaudeTests(Base):
    def test_trust_is_added_and_everything_else_kept(self):
        self.claude({"userID": "u", "projects": {"/other": {"hasTrustDialogAccepted": False}, "/a": {"history": [1]}}})
        os.chmod(self.home / ".claude.json", 0o600)
        self.assertEqual(trust.trust_claude(self.home / ".claude.json", ["/a"]), trust.TRUSTED)
        data = self.claude_data()
        self.assertEqual(data["userID"], "u")
        self.assertEqual(data["projects"]["/other"], {"hasTrustDialogAccepted": False})
        self.assertEqual(data["projects"]["/a"], {"history": [1], "hasTrustDialogAccepted": True})
        self.assertEqual(os.stat(self.home / ".claude.json").st_mode & 0o777, 0o600)

    def test_already_trusted_writes_nothing(self):
        self.claude({"projects": {"/a": {"hasTrustDialogAccepted": True}}})
        before = (self.home / ".claude.json").stat().st_mtime_ns
        self.assertEqual(trust.trust_claude(self.home / ".claude.json", ["/a"]), trust.ALREADY)
        self.assertEqual((self.home / ".claude.json").stat().st_mtime_ns, before)

    def test_missing_or_unreadable_file_is_unavailable_and_untouched(self):
        self.assertEqual(trust.trust_claude(self.home / ".claude.json", ["/a"]), trust.UNAVAILABLE)
        self.assertFalse((self.home / ".claude.json").exists())
        (self.home / ".claude.json").write_text("not json")
        self.assertEqual(trust.trust_claude(self.home / ".claude.json", ["/a"]), trust.UNAVAILABLE)
        self.assertEqual((self.home / ".claude.json").read_text(), "not json")

    def test_a_file_changed_by_the_cli_is_read_again(self):
        self.claude({"projects": {}})
        real = trust._replace_if_unchanged
        calls = []

        def racing(path, before, content):
            if not calls:
                calls.append(1)
                self.claude({"projects": {}, "written_by_cli": True})
                os.utime(path, ns=(before.st_mtime_ns + 10**9, before.st_mtime_ns + 10**9))
            return real(path, before, content)
        with mock.patch.object(trust, "_replace_if_unchanged", racing):
            self.assertEqual(trust.trust_claude(self.home / ".claude.json", ["/a"]), trust.TRUSTED)
        data = self.claude_data()
        self.assertTrue(data["written_by_cli"])
        self.assertTrue(data["projects"]["/a"]["hasTrustDialogAccepted"])


class CodexTests(Base):
    def path(self):
        return self.home / ".codex" / "config.toml"

    def test_trust_is_appended_and_comments_kept(self):
        self.path().write_text('# mine\nmodel = "x"\n\n[projects."/b"]\ntrust_level = "trusted"\n')
        self.assertEqual(trust.trust_codex(self.path(), ['/a "q"']), trust.TRUSTED)
        text = self.codex_text()
        self.assertTrue(text.startswith('# mine\nmodel = "x"\n'))
        projects = tomllib.loads(text)["projects"]
        self.assertEqual(projects['/a "q"'], {"trust_level": "trusted"})
        self.assertEqual(projects["/b"], {"trust_level": "trusted"})

    def test_missing_file_is_created_when_codex_is_set_up(self):
        self.assertEqual(trust.trust_codex(self.path(), ["/a"]), trust.TRUSTED)
        self.assertEqual(tomllib.loads(self.codex_text())["projects"]["/a"]["trust_level"], "trusted")
        self.assertEqual(trust.trust_codex(self.path(), ["/a"]), trust.ALREADY)

    def test_no_codex_directory_is_unavailable(self):
        self.assertEqual(trust.trust_codex(self.root / "nowhere" / "config.toml", ["/a"]), trust.UNAVAILABLE)

    def test_an_explicit_owner_setting_is_kept(self):
        original = '[projects."/a"]\ntrust_level = "untrusted"\n'
        self.path().write_text(original)
        self.assertEqual(trust.trust_codex(self.path(), ["/a"]), trust.KEPT)
        self.assertEqual(self.codex_text(), original)

    def test_a_file_that_cannot_take_an_appended_table_is_left_alone(self):
        for original in ('projects = { "/b" = { trust_level = "trusted" } }\n', "not = = toml"):
            with self.subTest(original=original):
                self.path().write_text(original)
                self.assertEqual(trust.trust_codex(self.path(), ["/a"]), trust.UNAVAILABLE)
                self.assertEqual(self.codex_text(), original)


class EnsureTests(Base):
    def test_without_consent_nothing_is_written(self):
        self.claude({"projects": {}})
        self.assertIsNone(trust.ensure(self.runtime, self.folder, self.environ))
        self.assertEqual(self.claude_data(), {"projects": {}})
        self.assertFalse((self.home / ".codex" / "config.toml").exists())

    def test_with_consent_both_clis_trust_the_folder(self):
        self.claude({"projects": {}})
        trust.consent(self.runtime, {"granted": True})
        result = trust.ensure(self.runtime, self.folder, self.environ)
        self.assertEqual(result, {"claude": trust.TRUSTED, "codex": trust.TRUSTED})
        key = os.path.realpath(self.folder)
        self.assertTrue(self.claude_data()["projects"][key]["hasTrustDialogAccepted"])
        self.assertEqual(tomllib.loads(self.codex_text())["projects"][key]["trust_level"], "trusted")


class OwnerViewTests(Base):
    def runtime_object(self, intents):
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE agent_profiles (agent_id TEXT, home_business_id TEXT, status TEXT)")
        connection.execute("CREATE TABLE native_session_owners (agent_id TEXT, desired_state TEXT)")
        connection.executemany("INSERT INTO agent_profiles VALUES (?,?,?)",
                               [("lead", None, "active"), ("old", None, "archived"), ("biz", "b1", "active")])
        connection.executemany("INSERT INTO native_session_owners VALUES (?,?)", intents)

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        return SimpleNamespace(paths=SimpleNamespace(runtime=self.runtime), database=SimpleNamespace(read=read))

    def view(self, runtime, change=None, agent_id="lead"):
        with mock.patch("bazarlyk.agents.global_scope.active_global", lambda connection, identity: {} if identity == "lead" else None), \
                mock.patch("bazarlyk.composition.locations.agent_workspace", lambda paths, identity, business: self.root / "agents" / identity):
            return trust.owner_view(runtime, change, agent_id, self.environ)

    def test_view_without_consent(self):
        view = self.view(self.runtime_object([]))
        self.assertFalse(view["granted"])
        self.assertNotIn("autostart", view)

    def test_grant_trusts_active_global_agents_only(self):
        self.claude({"projects": {}})
        view = self.view(self.runtime_object([]), {"granted": True})
        self.assertEqual(set(view["recorded"]), {"lead"})
        self.assertTrue(view["granted"])
        self.assertNotIn("autostart", view)


if __name__ == "__main__":
    unittest.main()
