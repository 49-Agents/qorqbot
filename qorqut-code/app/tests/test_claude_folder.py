"""The owner's Claude Code folder (``qorqut/claude_folder.py``) and its owner-only route ``/api/root/agents/claude-folder``.

Each test passes its own environment mapping, so the process environment is never changed. What matters is what a client is
started and checked with: ``agent_cli_launch.owner_variables`` (managed sessions) and ``connectors._probe_environment`` (the
sign-in check and button) must both see the chosen folder, and clearing it must give back exactly what the service started
with, including an unset variable."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut import agent_cli_launch, claude_folder, connectors

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:                                           # the service extras are not installed
    SERVICE = False

OWNER = "owner-principal"


class FolderTest(unittest.TestCase):
    def setUp(self):
        claude_folder._started.clear()
        self.addCleanup(claude_folder._started.clear)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.home = self.base / "home"
        self.personal = self.home / ".claude-personal"
        self.personal.mkdir(parents=True)
        self.instance = self.base / "instance"

    def environ(self, **native):
        """A service environment as the launcher writes it: the owner's own command line in QORQUT_NATIVE_USER_ENV."""
        return {"HOME": str(self.base / "service-home"), "PATH": "/usr/bin:/bin",
                "QORQUT_NATIVE_USER_ENV": json.dumps({"HOME": str(self.home), "PATH": "/usr/bin:/bin", **native})}

    def test_a_saved_folder_reaches_managed_launches_and_the_sign_in_check(self):
        environ = self.environ(CLAUDE_CONFIG_DIR=None)
        view = claude_folder.change(self.instance, "~/.claude-personal", environ)
        self.assertEqual(view["path"], str(self.personal))
        self.assertEqual(view["effective"], str(self.personal))
        self.assertEqual(view["default"], str(self.home / ".claude"))
        self.assertEqual(agent_cli_launch.owner_variables(environ)["CLAUDE_CONFIG_DIR"], str(self.personal))
        self.assertEqual(connectors._probe_environment(environ)["CLAUDE_CONFIG_DIR"], str(self.personal))
        saved = json.loads(claude_folder.file_path(self.instance).read_text())
        self.assertEqual((saved["version"], saved["path"]), (1, str(self.personal)))
        self.assertEqual(claude_folder.file_path(self.instance).stat().st_mode & 0o777, 0o600)

    def test_clearing_restores_an_unset_variable(self):
        environ = self.environ(CLAUDE_CONFIG_DIR=None)
        claude_folder.change(self.instance, str(self.personal), environ)
        view = claude_folder.change(self.instance, None, environ)
        self.assertIsNone(view["path"])
        self.assertFalse(claude_folder.file_path(self.instance).exists())
        self.assertIsNone(agent_cli_launch.owner_variables(environ)["CLAUDE_CONFIG_DIR"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", connectors._probe_environment(environ))
        self.assertEqual(view["effective"], str(self.home / ".claude"))

    def test_clearing_restores_the_folder_the_service_started_with(self):
        own = str(self.base / "instance-home-claude")
        environ = self.environ(CLAUDE_CONFIG_DIR=own)
        claude_folder.change(self.instance, str(self.personal), environ)
        claude_folder.change(self.instance, "  ", environ)
        self.assertEqual(agent_cli_launch.owner_variables(environ)["CLAUDE_CONFIG_DIR"], own)

    def test_without_the_launcher_record_the_service_variable_is_changed(self):
        environ = {"HOME": str(self.home)}
        claude_folder.change(self.instance, str(self.personal), environ)
        self.assertEqual(environ["CLAUDE_CONFIG_DIR"], str(self.personal))
        claude_folder.change(self.instance, None, environ)
        self.assertNotIn("CLAUDE_CONFIG_DIR", environ)

    def test_a_saved_choice_applies_at_start_and_its_default_is_kept(self):
        first = self.environ()
        claude_folder.change(self.instance, str(self.personal), first)
        restarted = self.environ()                         # a new service process: same files, fresh environment
        claude_folder.apply_saved(self.instance, restarted)
        self.assertEqual(agent_cli_launch.owner_variables(restarted)["CLAUDE_CONFIG_DIR"], str(self.personal))
        claude_folder.change(self.instance, None, restarted)
        self.assertNotIn("CLAUDE_CONFIG_DIR", agent_cli_launch.owner_variables(restarted))

    def test_nothing_saved_changes_nothing_at_start(self):
        environ = self.environ(CLAUDE_CONFIG_DIR=None)
        before = dict(environ)
        claude_folder.apply_saved(self.instance, environ)
        self.assertEqual(environ, before)

    def test_unusable_folders_are_refused_and_nothing_is_saved(self):
        environ = self.environ()
        for value in ("relative/folder", "~/missing", str(self.base / "nope"), "a\nb", "x" * 2000, 7):
            with self.subTest(value=value), self.assertRaises(DomainError) as caught:
                claude_folder.change(self.instance, value, environ)
            self.assertEqual(caught.exception.code, "validation_error")
        self.assertFalse(claude_folder.file_path(self.instance).exists())
        self.assertNotIn("CLAUDE_CONFIG_DIR", json.loads(environ["QORQUT_NATIVE_USER_ENV"]))

    def test_a_damaged_or_linked_file_counts_as_no_choice(self):
        path = claude_folder.file_path(self.instance)
        path.parent.mkdir(parents=True)
        for content in ("not json", json.dumps({"version": 2, "path": str(self.personal)}),
                        json.dumps({"version": 1, "path": "relative"})):
            path.write_text(content)
            self.assertIsNone(claude_folder.saved(self.instance), content)
        target = self.base / "elsewhere.json"
        target.write_text(json.dumps({"version": 1, "path": str(self.personal)}))
        path.unlink()
        path.symlink_to(target)
        self.assertIsNone(claude_folder.saved(self.instance))


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class RouteTest(unittest.TestCase):
    def setUp(self):
        from qorqut.transport.http import HTTPAPI
        self.kind, self.principal = "human", OWNER
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": OWNER})
            yield SimpleNamespace(repository=lambda name, **options: repository)

        self.restart = mock.Mock(return_value={"restarted": 2, "continued": 0, "failed": 1})
        chat = SimpleNamespace(account_switch=SimpleNamespace(restart_product=self.restart))
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None,
                                      paths=SimpleNamespace(runtime=None, instance_root=self.root), native_chat=chat)
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=OWNER, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api.context = lambda actor, slug=None, owner=False: (
            SimpleNamespace(principal_kind=self.kind, principal_id=self.principal), None)
        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)
        self.view = {"path": "/x", "effective": "/x", "default": "/d"}
        self.terminal = mock.patch("qorqut.managed_runtime.mode.is_terminal", return_value=False)
        self.terminal.start()
        self.addCleanup(self.terminal.stop)

    def post(self, body, headers=None):
        return self.client.post("/api/root/agents/claude-folder", content=json.dumps(body),
                                headers={"Content-Type": "application/json", **(headers or {})})

    def test_the_owner_reads_and_saves_uncached(self):
        with mock.patch("qorqut.claude_folder.view", return_value=self.view) as view, \
                mock.patch("qorqut.claude_folder.change", return_value=self.view) as change:
            got = self.client.get("/api/root/agents/claude-folder")
            saved = self.post({"path": "~/.claude-personal"})
        self.assertEqual((got.status_code, got.json()["data"]), (200, self.view))
        self.assertEqual(got.headers["cache-control"], "private, no-store")
        view.assert_called_once_with(self.root)
        change.assert_called_once_with(self.root, "~/.claude-personal")
        self.assertEqual(saved.json()["data"], self.view)
        self.restart.assert_not_called()

    def test_restart_saves_first_then_restarts_claude_sessions(self):
        order = []
        with mock.patch("qorqut.claude_folder.change", side_effect=lambda *a: order.append("save") or self.view):
            self.restart.side_effect = lambda *a: order.append("restart") or {"restarted": 2, "continued": 0, "failed": 1}
            response = self.post({"path": "/x", "restart": True})
        self.assertEqual(order, ["save", "restart"])
        self.assertEqual(response.json()["data"]["restart"], {"restarted": 2, "continued": 0, "failed": 1, "terminals": False})
        self.assertEqual(self.restart.call_args.args[0], "claude")

    def test_restart_alone_in_terminals_mode_restarts_nothing(self):
        self.terminal.stop()
        with mock.patch("qorqut.managed_runtime.mode.is_terminal", return_value=True), \
                mock.patch("qorqut.claude_folder.view", return_value=self.view), \
                mock.patch("qorqut.claude_folder.change") as change:
            response = self.post({"restart": True})
        self.terminal.start()
        self.assertTrue(response.json()["data"]["restart"]["terminals"])
        change.assert_not_called()
        self.restart.assert_not_called()

    def test_bad_bodies_are_refused_before_anything_changes(self):
        with mock.patch("qorqut.claude_folder.change") as change:
            for body in ({}, {"path": 5}, {"restart": "yes"}, {"path": "/x", "command": "rm"}, []):
                with self.subTest(body=body):
                    response = self.post(body)
                    expected = (400, "invalid_request") if body == [] else (422, "validation_error")
                    self.assertEqual((response.status_code, response.json()["error"]["code"]), expected)
        change.assert_not_called()
        self.restart.assert_not_called()

    def test_only_the_owners_dashboard_may_use_it(self):
        with mock.patch("qorqut.claude_folder.change") as change, mock.patch("qorqut.claude_folder.view") as view:
            responses = [self.post({"path": "/x"}, headers={"Authorization": "Bearer x"})]
            for kind, principal in (("agent", OWNER), ("human", "someone-else")):
                self.kind, self.principal = kind, principal
                responses += [self.client.get("/api/root/agents/claude-folder"), self.post({"path": "/x", "restart": True})]
        for response in responses:
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))
        change.assert_not_called()
        view.assert_not_called()
        self.restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
