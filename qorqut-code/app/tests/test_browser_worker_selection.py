"""The configured Ordubashy can operate a permanent worker's own browser by the worker's agent ID; nobody else can."""

from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE
from qorqut.bootstrap import browser
from qorqut.browser import agent_browsers, operations, setup
from qorqut.browser.access import BrowserAccess

ORDUBASHY = "0a6f3c52-9d1e-4b7a-8c35-2f4e6d8b1a90"
ATLAS = "8fdca155-2f3b-4735-800f-41624c6d26cc"
COMPASS = "c3b2f6a1-3d58-4c0e-9a55-5f0e8f1f7c11"


def actor(principal_id):
    return SimpleNamespace(principal=SimpleNamespace(id=principal_id), registration_id="registration", credential_id="credential",
                           delegation_assignment_id=None, delegation_generation=None, delegation_binding_id=None)


class WorkerBrowserSelectionTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE browser_handles (id TEXT, turn_id TEXT, binding_json TEXT, arguments_json TEXT, superseded INT)")
        self.addCleanup(connection.close)

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        runtime = SimpleNamespace(database=SimpleNamespace(read=read),
                                  paths=SimpleNamespace(runtime=self.root, instance_root=self.root))
        self.access = BrowserAccess(runtime)
        self.workers = [{"agent_id": ORDUBASHY, "name": "Ordubashy", "role": ORDUBASHY_ROLE},
                        {"agent_id": ATLAS, "name": "Atlas", "role": GLOBAL_WORKER_ROLE},
                        {"agent_id": COMPASS, "name": "Compass", "role": GLOBAL_WORKER_ROLE}]
        self.resolved, self.locked, self.ran = [], [], []

        def authorize(caller):
            if caller.principal.id != ORDUBASHY:
                raise DomainError("forbidden", "Browser selection by id is limited to the configured Ordubashy")

        def target(browser_key, owner):
            self.resolved.append((browser_key, owner))
            return {"cdp_endpoint": "http://127.0.0.1:20400", "generation": "configured", "instance": browser_key,
                    "runtime_dir": "/runtime", "browser_key": browser_key}

        @contextmanager
        def use(_paths, agent_id):
            self.locked.append(agent_id)
            yield

        def run(workspace, command, *, target, instance, capture):
            self.ran.append((workspace, command, target[0], instance))
            return 0, "done", ""
        self.authorize = mock.Mock(side_effect=authorize)
        for owner, name, value in ((self.access, "authorize_ordubashy", self.authorize), (self.access, "_worker_target", target),
                                   (self.access.targets, "registry", lambda: None),
                                   (setup, "targets", lambda _runtime: ("owner", list(self.workers))),
                                   (agent_browsers, "command_use", use), (browser, "run", run),
                                   (browser, "validate_arguments", lambda _command: None),
                                   (browser, "runtime_executable", lambda directory: directory)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def acquire(self, caller, browser_id):
        return self.access.tool(actor(caller), "acquire", {"action": "acquire", "protocol": "handles", "browser_id": browser_id})

    def command(self, caller, handle):
        return self.access.tool(actor(caller), "command", {"action": "command", "handle": handle, "command": ["observe", "overview"]})

    def test_ordubashy_acquires_a_workers_own_browser_by_the_workers_id(self):
        receipt = self.acquire(ORDUBASHY, ATLAS)
        self.assertEqual(receipt["status"], "owning")
        self.assertEqual(receipt["browser_id"], ATLAS)
        self.assertRegex(receipt["handle"], r"^b[a-f0-9]{12}$")
        self.assertEqual(self.resolved, [("configured:" + ATLAS, ATLAS)])
        self.assertNotEqual(receipt["handle"], self.acquire(ORDUBASHY, COMPASS)["handle"])

    def test_commands_on_that_handle_run_in_the_workers_browser_under_its_use_lock(self):
        handle = self.acquire(ORDUBASHY, ATLAS)["handle"]
        self.authorize.reset_mock()
        result = self.command(ORDUBASHY, handle)
        self.assertEqual(result["stdout"], "done")
        self.authorize.assert_called_once()                       # proved afresh on every call
        self.assertEqual(self.locked, [ATLAS])
        workspace, command, endpoint, instance = self.ran[0]
        self.assertEqual((command, endpoint, instance), (["observe", "overview"], "http://127.0.0.1:20400", "configured:" + ATLAS))
        self.assertEqual(workspace, self.root / "browser-turns" / ORDUBASHY / handle)      # artifacts stay Ordubashy's

    def test_an_agents_own_browser_is_unchanged(self):
        receipt = self.access.tool(actor(ATLAS), "acquire", {"action": "acquire", "protocol": "handles"})
        self.command(ATLAS, receipt["handle"])
        self.authorize.assert_not_called()
        self.assertEqual(self.resolved, [("configured:" + ATLAS, ATLAS)] * 2)
        self.assertEqual(self.locked, [ATLAS])

    def test_a_worker_cannot_select_another_workers_browser(self):
        with self.assertRaises(DomainError) as caught:
            self.acquire(COMPASS, ATLAS)
        self.assertEqual(caught.exception.code, "forbidden")
        forged = self.access.native_handle(COMPASS, "default", ATLAS)
        with self.assertRaises(DomainError) as caught:
            self.command(COMPASS, forged)
        self.assertEqual(caught.exception.code, "forbidden")
        ordubashys = self.acquire(ORDUBASHY, ATLAS)["handle"]
        self.resolved.clear()
        with self.assertRaises(DomainError) as caught:
            self.command(COMPASS, ordubashys)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual((self.resolved, self.ran), ([], []))

    def test_only_active_permanent_workers_are_selectable(self):
        for browser_id in ("3b2f6a1e-3d58-4c0e-9a55-5f0e8f1f7c11", ORDUBASHY, "outlook-browser"):
            with self.subTest(browser_id=browser_id), self.assertRaises(DomainError) as caught:
                self.acquire(ORDUBASHY, browser_id)
            self.assertEqual(caught.exception.code, "not_found")
        self.assertEqual(self.resolved, [])

    def test_a_handle_stops_working_when_the_worker_is_gone(self):
        handle = self.acquire(ORDUBASHY, ATLAS)["handle"]
        self.workers[:] = [worker for worker in self.workers if worker["agent_id"] != ATLAS]
        with self.assertRaises(DomainError) as caught:
            self.command(ORDUBASHY, handle)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertEqual(self.ran, [])

    def test_the_list_names_each_workers_browser(self):
        with mock.patch.object(self.access.targets, "catalog", return_value=[{"id": "ordubashy-browser", "label": "Ordubashy"}]):
            listed = self.access.tool(actor(ORDUBASHY), "list", {"action": "list"})["browsers"]
        self.assertEqual(listed, [{"id": "ordubashy-browser", "label": "Ordubashy"},
                                  {"id": ATLAS, "label": "Atlas's own browser"}, {"id": COMPASS, "label": "Compass's own browser"}])
        with self.assertRaises(DomainError):
            self.access.tool(actor(ATLAS), "list", {"action": "list"})

    def test_a_workers_browser_that_is_down_tells_ordubashy_to_repair_it(self):
        def missing(_browser_key, _owner):
            raise DomainError("browser_not_configured", "You have no browser. Ask Ordubashy.")
        with mock.patch.object(self.access, "_worker_target", missing):
            with self.assertRaises(DomainError) as caught:
                self.acquire(ORDUBASHY, ATLAS)
            self.assertEqual(caught.exception.code, "browser_not_configured")
            self.assertIn("agent_browser_manage", caught.exception.message)
            with self.assertRaises(DomainError) as caught:            # an agent's own missing browser keeps its own wording
                self.access.tool(actor(ATLAS), "acquire", {"action": "acquire", "protocol": "handles"})
            self.assertEqual(caught.exception.message, "You have no browser. Ask Ordubashy.")

        @contextmanager
        def stopped(_paths, _agent_id):
            raise DomainError("browser_not_ready", "Your dedicated browser is not running. Tell Ordubashy.")
            yield
        handle = self.acquire(ORDUBASHY, ATLAS)["handle"]
        with mock.patch.object(agent_browsers, "command_use", stopped), self.assertRaises(DomainError) as caught:
            self.command(ORDUBASHY, handle)
        self.assertEqual(caught.exception.code, "browser_not_ready")
        self.assertIn("agent_browser_manage", caught.exception.message)

    def test_the_tool_accepts_an_agent_id_as_browser_id(self):
        registered = []
        operations.register_browser_operation(SimpleNamespace(registry=SimpleNamespace(register=registered.append)))
        pattern = registered[0].input_schema["properties"]["browser_id"]["pattern"]
        for value in (ATLAS, COMPASS, "ordubashy-browser"):
            self.assertIsNotNone(re.fullmatch(pattern, value), value)
        for value in ("", "Atlas", "../x", "a b", "x" * 64):
            self.assertIsNone(re.fullmatch(pattern, value), value)


if __name__ == "__main__":
    unittest.main()
