"""One Shared browser for the agents set to it: the choice, the target file, the routing of management and no fallback."""

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
        self.root = Path(folder.name)
        self.paths = SimpleNamespace(runtime=self.root / "runtime", instance_root=self.root)
        self.workspaces = {}
        for agent in (ATLAS, SCOUT):
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


class ChoiceTest(SharedProfileTest):
    def test_an_agent_with_its_own_browser_moves_to_the_shared_one_and_keeps_its_profile(self):
        own = self.manage(ATLAS, "ensure")
        self.assertEqual((own["state"], own["browser"]), ("healthy", "own"))
        own_target = self.target(ATLAS)
        (self.state(ATLAS) / "profile/Cookies").write_text("signed in")

        result = self.manage(ATLAS, "use_shared")

        self.assertEqual((result["state"], result["browser"], result["agent_id"]), ("healthy", "shared", ATLAS))
        self.assertEqual(result["verified"], {"cdp_health": True})
        shared = self.target(ATLAS)
        self.assertIs(shared["shared"], True)
        self.assertNotEqual(shared["cdp_endpoint"], own_target["cdp_endpoint"])
        self.assertEqual(shared, shared_target(self.paths))
        self.assertTrue(uses_shared(self.paths, ATLAS))
        self.assertEqual(self.launcher.stops, [ATLAS])                              # its own browser is stopped,
        self.assertEqual((self.state(ATLAS) / "profile/Cookies").read_text(), "signed in")   # and its sign-ins stay
        self.assertIn(str(self.state(SHARED_ID)), self.launcher.running)

    def test_two_agents_use_one_shared_browser(self):
        self.manage(ATLAS, "use_shared")
        self.manage(SCOUT, "use_shared")
        self.assertEqual(self.target(ATLAS), self.target(SCOUT))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_an_agent_that_never_had_a_browser_can_use_the_shared_one(self):
        result = self.manage(SCOUT, "use_shared")
        self.assertEqual((result["state"], result["browser"]), ("healthy", "shared"))
        self.assertFalse((self.state(SCOUT) / "meta.json").exists())

    def test_choosing_shared_twice_changes_nothing_more(self):
        self.manage(ATLAS, "use_shared")
        again = self.manage(ATLAS, "use_shared")
        self.assertEqual((again["state"], again["changed"]), ("healthy", False))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_a_repeated_key_replays_the_saved_answer(self):
        first = self.manage(ATLAS, "use_shared", key="move-atlas")
        replay = self.manage(ATLAS, "use_shared", key="move-atlas")
        self.assertEqual(replay, {**first, "replayed": True})

    def test_an_operator_installed_browser_is_not_replaced(self):
        installed = {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": RUNTIME_DIR}
        (self.workspaces[ATLAS] / ".qorqut").mkdir()
        (self.workspaces[ATLAS] / ".qorqut/browser.json").write_text(json.dumps(installed))
        with self.assertRaises(DomainError) as refused:
            self.manage(ATLAS, "use_shared")
        self.assertEqual(refused.exception.code, "browser_target_conflict")
        self.assertEqual(self.target(ATLAS), installed)
        self.assertFalse(uses_shared(self.paths, ATLAS))
        self.assertEqual(self.launcher.starts, [])

    def test_the_shared_browser_is_not_an_agent(self):
        with self.assertRaises(DomainError) as refused:
            self.manage(SHARED_ID, "status")
        self.assertEqual(refused.exception.code, "not_found")

    def test_back_to_its_own_browser_restores_the_kept_one(self):
        self.manage(ATLAS, "ensure")
        own_target = self.target(ATLAS)
        self.manage(ATLAS, "use_shared")
        self.manage(SCOUT, "use_shared")

        result = self.manage(ATLAS, "use_own")

        self.assertEqual((result["state"], result["browser"], result["action"]), ("healthy", "own", "use_own"))
        self.assertEqual(self.target(ATLAS), own_target)
        self.assertFalse(uses_shared(self.paths, ATLAS))
        self.assertIn(str(self.state(ATLAS)), self.launcher.running)
        self.assertIn(str(self.state(SHARED_ID)), self.launcher.running)            # Scout still uses it

    def test_the_shared_browser_stops_when_its_last_agent_leaves_and_keeps_its_profile(self):
        self.manage(ATLAS, "use_shared")
        (self.state(SHARED_ID) / "profile/Cookies").write_text("shared sign-in")
        self.manage(ATLAS, "use_own")
        self.assertNotIn(str(self.state(SHARED_ID)), self.launcher.running)
        self.assertEqual((self.state(SHARED_ID) / "profile/Cookies").read_text(), "shared sign-in")
        # The next agent gets the same Shared browser back, on the same port.
        endpoint = shared_target(self.paths)["cdp_endpoint"]
        self.manage(SCOUT, "use_shared")
        self.assertEqual(self.target(SCOUT)["cdp_endpoint"], endpoint)

    def test_an_agent_without_a_browser_that_leaves_shared_gets_its_own(self):
        self.manage(SCOUT, "use_shared")
        result = self.manage(SCOUT, "use_own")
        self.assertEqual((result["state"], result["browser"]), ("healthy", "own"))
        self.assertNotIn("shared", self.target(SCOUT))


class RoutingTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")
        self.manage(ATLAS, "use_shared")
        self.manage(SCOUT, "use_shared")

    def test_status_reports_the_shared_browser(self):
        status = self.manage(ATLAS, "status")
        self.assertEqual((status["state"], status["browser"], status["agent_id"]), ("healthy", "shared", ATLAS))
        self.assertEqual(status["target"], {"configured": True, "managed": True})

    def test_stop_and_ensure_act_on_the_shared_browser_for_everyone(self):
        stopped = self.manage(ATLAS, "stop")
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(self.manage(SCOUT, "status")["state"], "stopped")
        self.assertTrue(self.manage(SCOUT, "status")["stop_requested"])
        self.assertNotIn(str(self.state(ATLAS)), self.launcher.running)             # the agent's own browser stays off
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
        other = {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": RUNTIME_DIR}
        (self.workspaces[ATLAS] / ".qorqut/browser.json").write_text(json.dumps(other))
        self.assertEqual(self.manage(ATLAS, "status")["state"], "conflict")
        for action in ("repair", "use_own"):
            with self.subTest(action=action), self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, action)
            self.assertEqual(refused.exception.code, "browser_target_conflict")
        self.assertEqual(self.target(ATLAS), other)
        self.assertTrue(uses_shared(self.paths, ATLAS))

    def test_an_interrupted_move_is_finished_by_repair(self):
        # The choice was recorded but the target file still names the agent's own stopped browser.
        own = json.loads((self.state(ATLAS) / "meta.json").read_text())["target"]["content"]
        (self.workspaces[ATLAS] / ".qorqut/browser.json").write_text(json.dumps(own))
        self.assertEqual(self.manage(ATLAS, "status")["state"], "conflict")
        self.assertEqual(self.manage(ATLAS, "repair")["state"], "healthy")
        self.assertEqual(self.target(ATLAS), shared_target(self.paths))

    def test_a_damaged_choice_is_an_error_not_the_agents_own_browser(self):
        (self.state(ATLAS) / MEMBERSHIP_FILE).write_text("{}")
        for action in ("status", "ensure"):
            with self.subTest(action=action), self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, action)
            self.assertEqual(refused.exception.code, "browser_metadata_invalid")
        self.assertNotIn(str(self.state(ATLAS)), self.launcher.running)


class CommandTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")
        self.manage(ATLAS, "use_shared")

    def test_a_command_holds_the_shared_browsers_use_lock(self):
        with command_use(self.paths, ATLAS):
            with self.assertRaises(DomainError) as refused:
                self.manage(ATLAS, "restart")
            self.assertEqual(refused.exception.code, "browser_in_use")
        self.assertEqual(self.manage(ATLAS, "restart")["state"], "healthy")

    def test_a_stopped_shared_browser_refuses_the_command_and_nothing_else_is_used(self):
        self.manage(ATLAS, "stop")
        self.launcher.running.add(str(self.state(ATLAS)))                           # even if its own browser were running
        with self.assertRaises(DomainError) as refused:
            with command_use(self.paths, ATLAS):
                self.fail("the command must not run")
        self.assertEqual(refused.exception.code, "browser_not_ready")

    def shared(self, agent):
        access = BrowserAccess.__new__(BrowserAccess)
        access.runtime = SimpleNamespace(paths=self.paths)
        return access._shared_target(agent, self.workspaces[agent], browser.read_target(self.workspaces[agent])[0])

    def test_the_command_target_is_marked_shared_only_for_an_agent_on_shared(self):
        self.manage(SCOUT, "ensure")
        self.assertTrue(self.shared(ATLAS))
        self.assertFalse(self.shared(SCOUT))

    def test_a_target_file_that_disagrees_with_the_choice_is_refused(self):
        own = json.loads((self.state(ATLAS) / "meta.json").read_text())["target"]["content"]
        cases = {"own target while set to shared": own,
                 "shared mark on another endpoint": {**own, "shared": True}}
        for name, content in cases.items():
            (self.workspaces[ATLAS] / ".qorqut/browser.json").write_text(json.dumps(content))
            with self.subTest(name), self.assertRaises(DomainError) as refused:
                self.shared(ATLAS)
            self.assertEqual(refused.exception.code, "browser_configuration_invalid")
        self.manage(SCOUT, "ensure")
        (self.workspaces[SCOUT] / ".qorqut/browser.json").write_text(json.dumps(shared_target(self.paths)))
        with self.assertRaises(DomainError) as refused:                             # a shared mark without the choice
            self.shared(SCOUT)
        self.assertEqual(refused.exception.code, "browser_configuration_invalid")


class SignInTest(SharedProfileTest):
    """The owner's sign-in window opens the profile the agent really uses and pauses everyone on it."""

    def setUp(self):
        super().setUp()
        self.manage(ATLAS, "ensure")
        self.runtime = SimpleNamespace(browser_manager=self.manager)
        agents = [{"agent_id": agent, "name": agent[:4], "role": "global-worker"} for agent in (ATLAS, SCOUT, LATER)]
        patcher = mock.patch.object(setup, "targets", return_value=("owner", agents))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_agent_with_its_own_browser_signs_in_on_its_own_profile_alone(self):
        self.assertEqual(sign_in._state(self.runtime, ATLAS), self.state(ATLAS))
        self.assertEqual(sign_in._paused(self.runtime, ATLAS), [ATLAS])

    def test_an_agent_on_shared_signs_in_on_the_shared_profile_and_pauses_every_agent_on_it(self):
        self.manage(ATLAS, "use_shared")
        self.manage(SCOUT, "use_shared")
        self.assertEqual(sign_in._state(self.runtime, ATLAS), self.state(SHARED_ID))
        self.assertEqual(sign_in._paused(self.runtime, ATLAS), [ATLAS, SCOUT])      # LATER has no browser choice at all
        self.assertEqual(sign_in._paused(self.runtime, SCOUT), [SCOUT, ATLAS])

    def test_the_sites_list_follows_the_browser(self):
        sign_in._write_sites(self.runtime, ATLAS, [{"site": "own.example", "added_at": "t"}])
        self.manage(ATLAS, "use_shared")
        self.manage(SCOUT, "use_shared")
        self.assertEqual(sign_in._read_sites(self.runtime, ATLAS), [])
        sign_in._write_sites(self.runtime, ATLAS, [{"site": "shared.example", "added_at": "t"}])
        self.assertEqual([item["site"] for item in sign_in._read_sites(self.runtime, SCOUT)], ["shared.example"])
        self.manage(ATLAS, "use_own")
        self.assertEqual([item["site"] for item in sign_in._read_sites(self.runtime, ATLAS)], ["own.example"])


if __name__ == "__main__":
    unittest.main()
