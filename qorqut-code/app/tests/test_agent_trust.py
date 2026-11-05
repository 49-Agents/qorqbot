"""Owner consent lets Qorqbot record folder trust for its own agent workspaces, and nothing is written without it."""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager
import json
import os
import shutil
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
        isolated_cli_environment.isolate(self)
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


class FolderKeysTest(Base):
    """Only the agent's own workspace is ever recorded; never a git repository that encloses it."""

    def layouts(self):
        """Workspaces under (name, repository entry that encloses them) combinations, each in its own folder."""
        cases = {}
        for name, enclosing in (("the operations repository", "instance/operations/.git"),
                                ("an external repository above the instance", ".git"),
                                ("a git worktree file above the instance", ".git:file"),
                                ("no repository", None)):
            base = self.root / name.replace(" ", "-")
            agent = base / "instance" / "operations" / "agents" / "a1"
            agent.mkdir(parents=True)
            if enclosing:
                entry = base / enclosing.split(":")[0]
                if enclosing.endswith(":file"):
                    entry.write_text("gitdir: /elsewhere\n")
                else:
                    entry.mkdir(parents=True, exist_ok=True)
            cases[name] = (base, agent)
        return cases

    @staticmethod
    def only(folder):
        lexical = os.path.abspath(folder)
        return [lexical] if os.path.realpath(lexical) == lexical else [lexical, os.path.realpath(lexical)]

    def test_no_enclosing_repository_is_a_key(self):
        for name, (base, agent) in self.layouts().items():
            with self.subTest(name):
                self.assertEqual(trust.folder_keys(agent), self.only(agent))

    def test_a_symlinked_workspace_is_keyed_lexically_and_physically_and_nothing_more(self):
        for name, (base, agent) in self.layouts().items():
            with self.subTest(name):
                link = base / "link to agent"
                link.symlink_to(agent)
                self.assertEqual(trust.folder_keys(link), [os.path.abspath(link), os.path.realpath(agent)])
                self.assertEqual(trust.folder_keys(str(link) + "/"), [os.path.abspath(link), os.path.realpath(agent)])

    def test_ensure_records_only_the_workspace_in_both_clis(self):
        trust.consent(self.runtime, {"granted": True})
        for name, (base, agent) in self.layouts().items():
            with self.subTest(name):
                self.claude({"projects": {"/mine": {"hasTrustDialogAccepted": False}}})
                (self.home / ".codex" / "config.toml").write_text('model = "x"\n')
                self.assertEqual(trust.ensure(self.runtime, agent, self.environ), {"claude": trust.TRUSTED, "codex": trust.TRUSTED})
                keys = self.only(agent)
                projects = self.claude_data()["projects"]
                self.assertEqual(set(projects), {"/mine", *keys})
                self.assertTrue(all(projects[key]["hasTrustDialogAccepted"] is True for key in keys))
                codex = tomllib.loads(self.codex_text())["projects"]
                self.assertEqual(codex, {key: {"trust_level": "trusted"} for key in keys})
                for ancestor in (agent.parent, base / "instance" / "operations", base / "instance", base):
                    self.assertNotIn(os.path.realpath(ancestor), projects)
                    self.assertNotIn(os.path.realpath(ancestor), codex)

    def test_the_agent_shell_command_records_only_the_workspace_too(self):
        trust.consent(self.runtime, {"granted": True})
        base, agent = self.layouts()["the operations repository"]
        self.claude({"projects": {}})
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home),
                                          "CODEX_HOME": str(self.home / ".codex")}):
            self.assertEqual(trust.main(["ensure", "--runtime", str(self.runtime), "--folder", str(agent)]), 0)
        self.assertEqual(set(self.claude_data()["projects"]), set(self.only(agent)))
        self.assertEqual(set(tomllib.loads(self.codex_text())["projects"]), set(self.only(agent)))

    def test_codex_levels_the_owner_set_are_kept(self):
        trust.consent(self.runtime, {"granted": True})
        base, agent = self.layouts()["the operations repository"]
        self.claude({"projects": {}})
        enclosing = os.path.realpath(base / "instance" / "operations")
        original = f'# mine\n[projects.{json.dumps(os.path.realpath(agent))}]\ntrust_level = "untrusted"\n' \
                   f'[projects.{json.dumps(enclosing)}]\ntrust_level = "untrusted"\n'
        (self.home / ".codex" / "config.toml").write_text(original)
        result = trust.ensure(self.runtime, agent, self.environ)
        self.assertEqual(result["codex"], trust.KEPT)
        self.assertEqual(self.codex_text(), original)

    def test_nothing_is_recorded_after_the_owner_revokes(self):
        trust.consent(self.runtime, {"granted": True})
        trust.consent(self.runtime, {"granted": False})
        base, agent = self.layouts()["the operations repository"]
        self.claude({"projects": {}})
        self.assertIsNone(trust.ensure(self.runtime, agent, self.environ))
        self.assertEqual(self.claude_data(), {"projects": {}})
        self.assertFalse((self.home / ".codex" / "config.toml").exists())


class MainTest(unittest.TestCase):
    """What an agent shell runs before claude or codex."""

    def setUp(self):
        isolated_cli_environment.isolate(self)

    def test_ensure_trusts_the_folder_only_with_consent_and_always_exits_zero(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        runtime, folder = root / "runtime", root / "agent"
        runtime.mkdir(); folder.mkdir()
        (root / ".claude.json").write_text(json.dumps({"projects": {}}))
        argv = ["ensure", "--runtime", str(runtime), "--folder", str(folder)]
        with mock.patch.dict(os.environ, {"HOME": str(root)}, clear=False):
            for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "QORQUT_NATIVE_USER_ENV"):
                os.environ.pop(key, None)
            self.assertEqual(trust.main(argv), 0)
            self.assertEqual(json.loads((root / ".claude.json").read_text())["projects"], {})
            trust.consent(runtime, {"granted": True})
            self.assertEqual(trust.main(argv), 0)
            projects = json.loads((root / ".claude.json").read_text())["projects"]
            self.assertTrue(any(entry.get("hasTrustDialogAccepted") for entry in projects.values()))
            with mock.patch.object(trust, "ensure", side_effect=OSError("disk")):
                self.assertEqual(trust.main(argv), 0)


if __name__ == "__main__":
    unittest.main()
