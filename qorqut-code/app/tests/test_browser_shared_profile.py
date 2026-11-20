"""Every agent uses the one Shared browser: connecting an agent, the target file, management for everyone and no fallback."""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap import browser
from qorqut.browser import agent_browsers, setup, sign_in
from qorqut.browser.access import BrowserAccess
from qorqut.browser.agent_browsers import AgentBrowserManager, MEMBERSHIP_FILE, SHARED_ID, command_use, shared_target, uses_shared

ATLAS = "11111111-1111-4111-8111-111111111111"
SCOUT = "22222222-2222-4222-8222-222222222222"
LATER = "33333333-3333-4333-8333-333333333333"
RUNTIME_DIR = "/runtime/browser"


class Launcher:
    """Stands in for the service launcher and the supervisor: a started browser is the set entry of its state directory."""

    def __init__(self):
        self.running, self.starts, self.stops = set(), [], []

    def active(self, _unit):
        return False

    def start(self, _unit, directory):
        self.running.add(str(directory))
        self.starts.append(Path(directory).name)

    def stop(self, _unit):
        pass


class SharedProfileTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()  # the temporary folder is behind a symbolic link on macOS
        self.paths = SimpleNamespace(runtime=self.root / "runtime", instance_root=self.root)
        self.workspaces = {}
        for agent in (ATLAS, SCOUT, LATER):
            self.workspaces[agent] = self.root / "agents" / agent
            self.workspaces[agent].mkdir(parents=True)
        read = lambda: nullcontext(SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_a: SimpleNamespace(fetchall=lambda: []))))
        runtime = SimpleNamespace(paths=self.paths, application=SimpleNamespace(writer_admission=None),
                                  require_authority=lambda: None, database=SimpleNamespace(read=read))
        self.launcher = Launcher()
        self.manager = AgentBrowserManager(runtime, launcher=self.launcher, probe=self.answers, chrome=sys.executable,
                                           ports=range(20400, 20410), runtime_directory=lambda: RUNTIME_DIR,
                                           deployments=(), sleep=lambda _seconds: None, health_timeout=0)
        self.manager._authorize = lambda _principal, agent_id, owner_control=False: self.workspaces[agent_id]
        self.manager._stop_managed = self.stop
        self.manager._supervisor = self.supervisor
        for owner, name, value in ((agent_browsers, "supervisor_state", self.supervisor),
                                   (agent_browsers, "port_free", lambda _port: True),
                                   (browser, "runtime_executable", lambda runtime: Path(runtime) / "agent-browser")):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.keys = 0

    def supervisor(self, directory, _meta):
        running = str(directory) in self.launcher.running
        return {"state": "ready" if running else "stopped", "held": running, "detail": None, "pid": 1, "identity": "x"}

    def stop(self, ctx, s):
        if str(s.directory) not in self.launcher.running:
            return False
        ctx.effect = True
        self.launcher.running.discard(str(s.directory))
        self.launcher.stops.append(s.directory.name)
        return True

    def answers(self, endpoint):
        """CDP answers on an endpoint exactly when the browser that owns that port is running."""
        for meta_file in (self.paths.runtime / "agent-browsers").glob("*/meta.json"):
            meta = json.loads(meta_file.read_text())
            if meta["target"]["content"]["cdp_endpoint"] == endpoint:
                return str(meta_file.parent) in self.launcher.running
        return False

    def manage(self, agent, action, key=None):
        self.keys += 1
        return self.manager.manage("owner", {"agent_id": agent, "action": action},
                                   None if action == "status" else key or f"key-{self.keys}", owner_control=True)

    def target(self, agent):
        return json.loads((self.workspaces[agent] / ".qorqut/browser.json").read_text())

    def state(self, agent):
        return self.paths.runtime / "agent-browsers" / agent

    def own(self, agent):
        """A managed Chrome of the agent's own, as this manager once started one: ``ensure`` replaces it."""
        self.manager._apply(SimpleNamespace(effect=False), "ensure",
                            self.manager._snapshot(agent, self.workspaces[agent], mutating=True))

    def install(self, agent):
        """A target file the operator wrote by hand."""
        installed = {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": RUNTIME_DIR}
        (self.workspaces[agent] / ".qorqut").mkdir(exist_ok=True)
        (self.workspaces[agent] / ".qorqut/browser.json").write_text(json.dumps(installed))
        return installed


class ConnectTest(SharedProfileTest):
    def test_ensure_starts_the_shared_browser_and_connects_the_agent(self):
        self.assertEqual(self.manage(ATLAS, "status")["state"], "missing")
        result = self.manage(ATLAS, "ensure")
        self.assertEqual((result["state"], result["browser"], result["agent_id"]), ("healthy", "shared", ATLAS))
        self.assertEqual(result["verified"], {"cdp_health": True})
        self.assertIs(self.target(ATLAS)["shared"], True)
        self.assertEqual(self.target(ATLAS), shared_target(self.paths))
        self.assertTrue(uses_shared(self.paths, ATLAS))
        self.assertEqual(self.launcher.starts, [SHARED_ID])
        self.assertFalse((self.state(ATLAS) / "meta.json").exists())               # no Chrome of the agent's own

    def test_every_agent_uses_the_one_browser(self):
        self.manage(ATLAS, "ensure")
        status = self.manage(SCOUT, "status")
        self.assertEqual((status["state"], status["browser"]), ("target_missing", "shared"))   # running, Scout not connected yet
        self.manage(SCOUT, "ensure")
        self.assertEqual(self.target(ATLAS), self.target(SCOUT))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_ensure_twice_changes_nothing_more(self):
        self.manage(ATLAS, "ensure")
        again = self.manage(ATLAS, "ensure")
        self.assertEqual((again["state"], again["changed"]), ("healthy", False))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_a_repeated_key_replays_the_saved_answer(self):
        first = self.manage(ATLAS, "ensure", key="connect-atlas")
        replay = self.manage(ATLAS, "ensure", key="connect-atlas")
        self.assertEqual(replay, {**first, "replayed": True})

    def test_a_chrome_of_the_agents_own_is_stopped_and_its_profile_kept(self):
        self.own(ATLAS)
        own_target = self.target(ATLAS)
        (self.state(ATLAS) / "profile/Cookies").write_text("signed in")
        self.assertEqual(self.manage(ATLAS, "status")["state"], "missing")          # setup connects it by itself

        result = self.manage(ATLAS, "ensure")

        self.assertEqual((result["state"], result["browser"]), ("healthy", "shared"))
        self.assertNotEqual(self.target(ATLAS)["cdp_endpoint"], own_target["cdp_endpoint"])
        self.assertEqual(self.target(ATLAS), shared_target(self.paths))
        self.assertEqual(self.launcher.stops, [ATLAS])
        self.assertNotIn(str(self.state(ATLAS)), self.launcher.running)
        self.assertEqual((self.state(ATLAS) / "profile/Cookies").read_text(), "signed in")

    def test_an_unreadable_record_of_that_chrome_stops_ensure_before_anything_changes(self):
        self.own(ATLAS)
        meta = json.loads((self.state(ATLAS) / "meta.json").read_text())
        (self.state(ATLAS) / "meta.json").write_text(json.dumps({**meta, "schema_version": 2}))
        with self.assertRaises(DomainError) as refused:
            self.manage(ATLAS, "ensure")
        self.assertEqual(refused.exception.code, "browser_metadata_invalid")
        self.assertFalse(uses_shared(self.paths, ATLAS))
        self.assertEqual(self.launcher.starts, [ATLAS])

    def test_an_operator_installed_browser_is_not_replaced(self):
        installed = self.install(ATLAS)
        status = self.manage(ATLAS, "status")
        self.assertEqual((status["browser"], status["ownership"]), ("operator", "external"))
        with self.assertRaises(DomainError) as refused:
            self.manage(ATLAS, "ensure")
        self.assertEqual(refused.exception.code, "browser_management_unconfigured")
        self.assertEqual(self.target(ATLAS), installed)
        self.assertFalse(uses_shared(self.paths, ATLAS))
        self.assertEqual(self.launcher.starts, [])

    def test_the_shared_browser_is_not_an_agent(self):
        with self.assertRaises(DomainError) as refused:
            self.manage(SHARED_ID, "status")
        self.assertEqual(refused.exception.code, "not_found")

    def test_there_is_no_action_that_chooses_a_browser(self):
        for action in ("use_shared", "use_own"):
            with self.subTest(action=action), self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, action)
            self.assertEqual(refused.exception.code, "validation_error")


class RoutingTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")
        self.manage(SCOUT, "ensure")

    def test_status_reports_the_shared_browser(self):
        status = self.manage(ATLAS, "status")
        self.assertEqual((status["state"], status["browser"], status["agent_id"]), ("healthy", "shared", ATLAS))
        self.assertEqual(status["target"], {"configured": True, "managed": True})

    def test_stop_and_ensure_act_on_the_browser_for_everyone(self):
        stopped = self.manage(ATLAS, "stop")
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(self.manage(SCOUT, "status")["state"], "stopped")
        self.assertTrue(self.manage(SCOUT, "status")["stop_requested"])
        self.assertEqual(self.manage(SCOUT, "ensure")["state"], "healthy")
        self.assertEqual(self.manage(ATLAS, "status")["state"], "healthy")

    def test_restart_restarts_the_shared_browser(self):
        self.launcher.starts.clear()
        result = self.manage(ATLAS, "restart")
        self.assertEqual((result["state"], result["changed"]), ("healthy", True))
        self.assertEqual((self.launcher.stops[-1], self.launcher.starts), (SHARED_ID, [SHARED_ID]))

    def test_a_missing_target_file_is_reported_and_repair_restores_it(self):
        expected = self.target(ATLAS)
        (self.workspaces[ATLAS] / ".qorqut/browser.json").unlink()
        self.assertEqual(self.manage(ATLAS, "status")["state"], "target_missing")
        self.assertEqual(self.manage(ATLAS, "repair")["state"], "healthy")
        self.assertEqual(self.target(ATLAS), expected)

    def test_a_target_file_naming_another_browser_is_a_conflict_and_is_left_alone(self):
        other = self.install(ATLAS)
        self.assertEqual(self.manage(ATLAS, "status")["state"], "conflict")
        with self.assertRaises(DomainError) as refused:
            self.manage(ATLAS, "repair")
        self.assertEqual(refused.exception.code, "browser_target_conflict")
        self.assertEqual(self.target(ATLAS), other)
        self.assertTrue(uses_shared(self.paths, ATLAS))

    def test_a_target_file_still_naming_a_chrome_of_the_agents_own_is_replaced(self):
        self.own(LATER)
        own = self.target(LATER)
        self.manage(LATER, "ensure")
        (self.workspaces[LATER] / ".qorqut/browser.json").write_text(json.dumps(own))     # where an interrupted ensure left it
        self.assertEqual(self.manage(LATER, "status")["state"], "target_missing")
        self.assertEqual(self.manage(LATER, "repair")["state"], "healthy")
        self.assertEqual(self.target(LATER), shared_target(self.paths))

    def test_a_damaged_record_is_replaced_not_trusted(self):
        for damaged in ("{}", "not json at all", json.dumps({"schema_version": 1, "agent_id": SCOUT, "browser": "shared"})):
            with self.subTest(damaged=damaged):
                (self.state(ATLAS) / MEMBERSHIP_FILE).write_text(damaged)
                with self.assertRaises(DomainError) as refused:                     # a command never reads it as "somewhere else"
                    uses_shared(self.paths, ATLAS)
                self.assertEqual(refused.exception.code, "browser_metadata_invalid")
                self.assertEqual(self.manage(ATLAS, "status")["state"], "target_missing")
                self.assertEqual(self.manage(ATLAS, "ensure")["state"], "healthy")
                self.assertTrue(uses_shared(self.paths, ATLAS))
                self.assertEqual(self.target(ATLAS), shared_target(self.paths))

    def test_a_record_that_is_a_symbolic_link_is_refused_and_nothing_is_written_through_it(self):
        record = self.state(ATLAS) / MEMBERSHIP_FILE
        record.unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("keep")
        record.symlink_to(elsewhere)
        for action in ("status", "ensure", "repair"):
            with self.subTest(action=action), self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, action)
            self.assertEqual(refused.exception.code, "browser_metadata_invalid")
        self.assertEqual(elsewhere.read_text(), "keep")

    def test_automatic_setup_connects_an_agent_whose_record_is_damaged(self):
        (self.state(ATLAS) / MEMBERSHIP_FILE).write_text("{}")
        owner = SimpleNamespace(paths=self.paths, browser_manager=self.manager, browser_access=SimpleNamespace(runtime_directory=lambda: RUNTIME_DIR))
        with mock.patch.object(setup, "targets", return_value=("owner", [{"agent_id": ATLAS, "name": "Atlas", "role": "x"}])), \
                mock.patch.object(sign_in, "active", return_value=False), \
                mock.patch.object(setup.managed_chrome, "start"), \
                mock.patch.object(setup.managed_chrome, "view", return_value={"state": "ready"}):
            agent = setup.view(owner)["agents"][0]
            self.assertEqual((agent["state"], agent["browser"]), ("target_missing", "shared"))
            self.assertIn("Retry setup", agent["explanation"])
            agent = setup.ensure_all(owner)["agents"][0]
            self.assertEqual((agent["state"], agent["browser"]), ("healthy", "shared"))


class CommandTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")

    def test_a_command_holds_the_shared_browsers_use_lock(self):
        with command_use(self.paths, ATLAS):
            with self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, "restart")
            self.assertEqual(refused.exception.code, "browser_in_use")
        self.assertEqual(self.manage(ATLAS, "restart")["state"], "healthy")

    def test_a_stopped_shared_browser_refuses_the_command_and_nothing_else_is_used(self):
        self.manage(ATLAS, "stop")
        with self.assertRaises(DomainError) as refused:
            with command_use(self.paths, ATLAS):
                self.fail("the command must not run")
        self.assertEqual(refused.exception.code, "browser_not_ready")

    def shared(self, agent):
        access = BrowserAccess.__new__(BrowserAccess)
        access.runtime = SimpleNamespace(paths=self.paths)
        return access._shared_target(agent, self.workspaces[agent], browser.read_target(self.workspaces[agent])[0])

    def test_the_command_target_of_a_connected_agent_is_the_shared_browser(self):
        self.assertTrue(self.shared(ATLAS))

    def test_a_target_file_that_disagrees_with_the_record_is_refused(self):
        expected = shared_target(self.paths)
        cases = {"another browser while connected": {key: value for key, value in expected.items() if key != "shared"} | {"cdp_endpoint": "http://127.0.0.1:9225"},
                 "shared mark on another endpoint": {**expected, "cdp_endpoint": "http://127.0.0.1:9225"}}
        for name, content in cases.items():
            (self.workspaces[ATLAS] / ".qorqut/browser.json").write_text(json.dumps(content))
            with self.subTest(name), self.assertRaises(DomainError) as refused:
                self.shared(ATLAS)
            self.assertEqual(refused.exception.code, "browser_configuration_invalid")
        (self.workspaces[SCOUT] / ".qorqut").mkdir()
        (self.workspaces[SCOUT] / ".qorqut/browser.json").write_text(json.dumps(expected))
        with self.assertRaises(DomainError) as refused:                             # a shared mark without the record
            self.shared(SCOUT)
        self.assertEqual(refused.exception.code, "browser_configuration_invalid")


class SignInTest(SharedProfileTest):
    """The owner's sign-in window opens the Shared browser's profile and pauses every agent connected to it."""

    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")
        self.manage(SCOUT, "ensure")
        self.runtime = SimpleNamespace(browser_manager=self.manager)
        agents = [{"agent_id": agent, "name": agent[:4], "role": "global-worker"} for agent in (ATLAS, SCOUT, LATER)]
        patcher = mock.patch.object(setup, "targets", return_value=("owner", agents))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_sign_in_is_on_the_shared_profile_and_pauses_every_connected_agent(self):
        self.assertEqual(sign_in._state(self.runtime, ATLAS), self.state(SHARED_ID))
        self.assertEqual(sign_in._paused(self.runtime, ATLAS), [ATLAS, SCOUT])      # LATER is not connected yet
        self.assertEqual(sign_in._paused(self.runtime, SCOUT), [SCOUT, ATLAS])

    def test_the_sites_list_is_the_browsers(self):
        sign_in._write_sites(self.runtime, ATLAS, [{"site": "shared.example", "added_at": "t"}])
        self.assertEqual([item["site"] for item in sign_in._read_sites(self.runtime, SCOUT)], ["shared.example"])


if __name__ == "__main__":
    unittest.main()
