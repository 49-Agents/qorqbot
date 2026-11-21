"""Every agent uses the one Shared browser: one action connects every agent, who may manage it, repair, and no fallback."""

from contextlib import nullcontext
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap import browser
from qorqut.bootstrap.client import write_json_private
from qorqut.browser import agent_browsers, setup, sign_in
from qorqut.browser.access import BrowserAccess
from qorqut.browser.agent_browsers import AgentBrowserManager, MEMBERSHIP_FILE, SHARED_ID, command_use, shared_target, uses_shared

ATLAS = "11111111-1111-4111-8111-111111111111"
SCOUT = "22222222-2222-4222-8222-222222222222"
LATER = "33333333-3333-4333-8333-333333333333"
NAMES = {ATLAS: "Atlas", SCOUT: "Scout", LATER: "Later"}
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
    """Atlas (Ordubashy) and Scout are the permanent agents; Later has a workspace and joins only where a test says so."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()  # the temporary folder is behind a symbolic link on macOS
        self.paths = SimpleNamespace(runtime=self.root / "runtime", instance_root=self.root)
        self.workspaces = {}
        for agent in (ATLAS, SCOUT, LATER):
            self.workspaces[agent] = self.root / "agents" / agent
            self.workspaces[agent].mkdir(parents=True)
        self.agents = [self.agent(ATLAS), self.agent(SCOUT)]
        read = lambda: nullcontext(SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_a: SimpleNamespace(fetchall=lambda: []))))
        self.runtime = SimpleNamespace(paths=self.paths, application=SimpleNamespace(writer_admission=None),
                                       require_authority=lambda: None, database=SimpleNamespace(read=read))
        self.launcher = Launcher()
        self.manager = AgentBrowserManager(self.runtime, launcher=self.launcher, probe=self.answers, chrome=sys.executable,
                                           ports=range(20400, 20410), runtime_directory=lambda: RUNTIME_DIR,
                                           deployments=(), sleep=lambda _seconds: None, health_timeout=0)
        self.manager._authorize = lambda _principal, owner_control=False: list(self.agents)
        self.manager._stop_managed = self.stop
        self.manager._supervisor = self.supervisor
        for owner, name, value in ((agent_browsers, "supervisor_state", self.supervisor),
                                   (agent_browsers, "port_free", lambda _port: True),
                                   (browser, "runtime_executable", lambda runtime: Path(runtime) / "agent-browser")):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.keys = 0

    def agent(self, agent):
        return SimpleNamespace(agent_id=agent, name=NAMES[agent], lead=agent == ATLAS, workspace=self.workspaces[agent])

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
        """CDP answers on an endpoint exactly when the Shared browser owns that port and is running."""
        try:
            meta = json.loads((self.state(SHARED_ID) / "meta.json").read_text())
        except (OSError, ValueError):
            return False
        return meta["target"]["content"]["cdp_endpoint"] == endpoint and str(self.state(SHARED_ID)) in self.launcher.running

    def manage(self, action, key=None, principal="owner", owner_control=True):
        self.keys += 1
        return self.manager.manage(principal, {"action": action}, None if action == "status" else key or f"key-{self.keys}",
                                   owner_control=owner_control)

    def refused(self, action, **kwargs):
        with self.assertRaises(DomainError) as refused:
            self.manage(action, **kwargs)
        return refused.exception

    def connected(self, result):
        return {item["agent_id"]: item["connected"] for item in result["agents"]}

    def entry(self, result, agent):
        return next(item for item in result["agents"] if item["agent_id"] == agent)

    def target_file(self, agent):
        return self.workspaces[agent] / ".qorqut/browser.json"

    def target(self, agent):
        return json.loads(self.target_file(agent).read_text())

    def state(self, agent):
        return self.paths.runtime / "agent-browsers" / agent

    def own(self, agent, port=20490, **changes):
        """A running managed Chrome of the agent's own, as this manager once started one, with its target file."""
        directory = self.state(agent)
        for folder in (directory.parent, directory, directory / "profile"):
            agent_browsers._private_directory(folder)
        content = {"schema_version": 1, "cdp_endpoint": f"http://127.0.0.1:{port}", "runtime_dir": RUNTIME_DIR}
        write_json_private(directory / "meta.json", {
            "schema_version": 1, "agent_id": agent, "mode": "managed", "target": {"workspace": str(self.workspaces[agent]), "content": content},
            "managed": {"port": port, "profile": str(directory / "profile"), "chrome": sys.executable,
                        "unit": agent_browsers.unit_name(directory.parent, agent)}, **changes})
        self.install(agent, content)
        self.launcher.running.add(str(directory))
        return content

    def install(self, agent, content=None):
        """A target file the manager did not write: one written by hand, or left from an earlier browser."""
        installed = content or {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": RUNTIME_DIR}
        (self.workspaces[agent] / ".qorqut").mkdir(exist_ok=True)
        self.target_file(agent).write_text(json.dumps(installed))
        return installed


class ConnectTest(SharedProfileTest):
    def test_one_ensure_starts_the_shared_browser_and_connects_every_agent(self):
        status = self.manage("status")
        self.assertEqual((status["state"], self.connected(status)), ("missing", {ATLAS: False, SCOUT: False}))
        result = self.manage("ensure")
        self.assertEqual((result["state"], result["browser"], result["changed"]), ("healthy", "shared", True))
        self.assertEqual(result["verified"], {"cdp_health": True})
        self.assertEqual(result["agents"], [{"agent_id": ATLAS, "name": "Atlas", "connected": True},
                                            {"agent_id": SCOUT, "name": "Scout", "connected": True}])
        self.assertEqual(self.manage("status")["agents"], result["agents"])
        self.assertIs(self.target(ATLAS)["shared"], True)
        for agent in (ATLAS, SCOUT):
            self.assertEqual(self.target(agent), shared_target(self.paths))
            self.assertTrue(uses_shared(self.paths, agent))
            self.assertFalse((self.state(agent) / "meta.json").exists())           # no Chrome of the agent's own
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_an_agent_that_is_not_connected_yet_is_reported_and_the_next_ensure_connects_it(self):
        self.manage("ensure")
        self.agents.append(self.agent(LATER))                                       # a worker created after setup
        status = self.manage("status")
        self.assertEqual((status["state"], status["browser"]), ("target_missing", "shared"))   # running, Later not connected yet
        self.assertEqual(self.connected(status), {ATLAS: True, SCOUT: True, LATER: False})
        self.assertNotIn("problem", self.entry(status, LATER))
        self.assertIn("Later", status["diagnostic"])
        result = self.manage("ensure")
        self.assertEqual((result["state"], result["changed"], self.connected(result)[LATER]), ("healthy", True, True))
        self.assertEqual(self.target(ATLAS), self.target(LATER))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_ensure_twice_changes_nothing_more(self):
        self.manage("ensure")
        again = self.manage("ensure")
        self.assertEqual((again["state"], again["changed"]), ("healthy", False))
        self.assertEqual(self.launcher.starts, [SHARED_ID])

    def test_a_repeated_key_replays_the_saved_answer(self):
        first = self.manage("ensure", key="connect-everyone")
        replay = self.manage("ensure", key="connect-everyone")
        self.assertEqual(replay, {**first, "replayed": True})

    def test_an_agent_whose_folder_does_not_exist_is_reported_and_the_others_are_connected(self):
        shutil.rmtree(self.workspaces[SCOUT])
        result = self.manage("ensure")
        for answer in (result, self.manage("status")):
            self.assertEqual((answer["state"], self.connected(answer)), ("healthy", {ATLAS: True, SCOUT: False}))
            self.assertIn("folder isn't ready", self.entry(answer, SCOUT)["problem"])
            self.assertNotIn("problem", self.entry(answer, ATLAS))
        self.assertFalse(self.workspaces[SCOUT].exists())                           # the folder is the agent's to create
        self.assertFalse(uses_shared(self.paths, SCOUT))

    def test_a_hand_written_target_file_is_replaced_by_the_shared_one(self):
        installed = self.install(SCOUT)
        result = self.manage("ensure")
        self.assertEqual((result["state"], self.connected(result)), ("healthy", {ATLAS: True, SCOUT: True}))
        self.assertNotEqual(self.target(SCOUT)["cdp_endpoint"], installed["cdp_endpoint"])
        self.assertEqual(self.target(SCOUT), shared_target(self.paths))
        self.assertTrue(uses_shared(self.paths, SCOUT))

    def test_a_target_file_that_is_a_symbolic_link_is_refused_for_that_agent_only(self):
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_text("keep")
        (self.workspaces[SCOUT] / ".qorqut").mkdir()
        self.target_file(SCOUT).symlink_to(elsewhere)
        result = self.manage("ensure")
        for answer in (result, self.manage("status")):
            self.assertEqual((answer["state"], self.connected(answer)), ("healthy", {ATLAS: True, SCOUT: False}))
            self.assertIn("symbolic link", self.entry(answer, SCOUT)["problem"])
        self.assertTrue(self.target_file(SCOUT).is_symlink())
        self.assertEqual(elsewhere.read_text(), "keep")
        self.assertFalse(uses_shared(self.paths, SCOUT))
        self.assertEqual(self.target(ATLAS), shared_target(self.paths))

    def test_a_chrome_of_the_agents_own_is_stopped_and_its_profile_kept(self):
        own_target = self.own(SCOUT)
        (self.state(SCOUT) / "profile/Cookies").write_text("signed in")
        self.assertEqual(self.manage("status")["state"], "missing")                 # setup connects it by itself

        result = self.manage("ensure")

        self.assertEqual((result["state"], result["browser"], self.connected(result)[SCOUT]), ("healthy", "shared", True))
        self.assertNotEqual(self.target(SCOUT)["cdp_endpoint"], own_target["cdp_endpoint"])
        self.assertEqual(self.target(SCOUT), shared_target(self.paths))
        self.assertEqual(self.launcher.stops, [SCOUT])
        self.assertNotIn(str(self.state(SCOUT)), self.launcher.running)
        self.assertEqual((self.state(SCOUT) / "profile/Cookies").read_text(), "signed in")

    def test_a_record_in_an_agents_folder_that_is_not_a_managed_chrome_is_ignored(self):
        # An operator-installed ("legacy") browser is no longer a kind of browser, and an unreadable record names nothing.
        for changes in ({"mode": "legacy"}, {"schema_version": 2}):
            with self.subTest(changes=changes):
                self.launcher.stops.clear()
                self.own(SCOUT, **changes)
                (self.state(SCOUT) / MEMBERSHIP_FILE).unlink(missing_ok=True)
                result = self.manage("ensure")
                self.assertEqual((result["state"], self.connected(result)), ("healthy", {ATLAS: True, SCOUT: True}))
                self.assertEqual(self.target(SCOUT), shared_target(self.paths))
                self.assertEqual(self.launcher.stops, [])                           # nothing is stopped on its word

    def test_there_is_no_action_that_chooses_a_browser(self):
        for action in ("use_shared", "use_own"):
            with self.subTest(action=action):
                self.assertEqual(self.refused(action).code, "validation_error")

    def test_an_action_names_no_agent(self):
        # A caller that still sends an agent gets the one browser's answer: there is no other browser to name.
        self.keys += 1
        result = self.manager.manage("owner", {"agent_id": SCOUT, "action": "ensure"}, f"key-{self.keys}", owner_control=True)
        self.assertEqual((result["browser"], self.connected(result)), ("shared", {ATLAS: True, SCOUT: True}))
        self.assertNotIn("agent_id", result)
        self.assertEqual(self.launcher.starts, [SHARED_ID])


class AuthorizationTest(SharedProfileTest):
    """Who may manage the browser, with the manager's own check: the owner from Settings, and Ordubashy for every action."""

    def setUp(self):
        super().setUp()
        del self.manager._authorize
        names = {ATLAS: "Zed", SCOUT: "Scout", LATER: "Able"}                       # the lead agent is first whatever its name
        rows = [{"agent_id": agent, "display_name": names[agent], "slug": None}
                for agent in (ATLAS, SCOUT, LATER)]
        self.runtime.database = SimpleNamespace(read=lambda: nullcontext(SimpleNamespace(connection=SimpleNamespace(
            execute=lambda *_a: SimpleNamespace(fetchall=lambda: rows)))))
        self.runtime.registry = mock.Mock()
        self.runtime.application.authorize = mock.Mock()
        self.runtime.application.policy = SimpleNamespace(context_for=lambda principal, **_kw: SimpleNamespace(
            principal_id=principal, principal_kind="human" if principal == "owner" else "agent"))
        for target, value in (
                ("qorqut.management.identity.configured_ordubashy", lambda _uow: ATLAS),
                ("qorqut.management.identity.is_active_installation_owner", lambda _uow, principal, _kind: principal == "owner"),
                ("qorqut.agents.global_scope.active_global",
                 lambda _connection, agent: {"role": {"id": "ordubashy" if agent == ATLAS else "global-worker"}}),
                ("qorqut.browser.agent_browsers.agent_workspace_of", lambda _paths, agent, *_a: self.workspaces[agent])):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_ordubashy_runs_every_action_without_naming_an_agent(self):
        for action, state in (("status", "missing"), ("ensure", "healthy"), ("repair", "healthy"), ("restart", "healthy"),
                              ("stop", "stopped"), ("status", "stopped")):
            with self.subTest(action=action):
                result = self.manage(action, principal=ATLAS, owner_control=False)
                self.assertEqual((result["action"], result["state"], result["browser"]), (action, state, "shared"))
                self.assertEqual([item["agent_id"] for item in result["agents"]], [ATLAS, LATER, SCOUT])
        self.assertEqual(self.connected(result), {ATLAS: True, LATER: True, SCOUT: True})
        self.runtime.application.authorize.assert_called()                          # the operation grant is still needed

    def test_any_other_caller_is_refused_every_action(self):
        for principal in (SCOUT, "owner"):                                          # a worker; the owner outside Settings
            for action in agent_browsers.ACTIONS:
                with self.subTest(principal=principal, action=action):
                    self.assertEqual(self.refused(action, principal=principal, owner_control=False).code, "forbidden")
        self.assertEqual(self.launcher.starts, [])
        self.assertFalse(self.state(SHARED_ID).exists())

    def test_owner_control_is_the_owners_alone(self):
        for principal in (ATLAS, SCOUT):
            with self.subTest(principal=principal):
                self.assertEqual(self.refused("ensure", principal=principal).code, "forbidden")
        self.assertEqual(self.launcher.starts, [])
        self.assertEqual(self.manage("ensure")["state"], "healthy")
        self.runtime.application.authorize.assert_not_called()


class RoutingTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage("ensure")

    def test_status_reports_the_shared_browser_and_who_is_connected(self):
        status = self.manage("status")
        self.assertEqual((status["state"], status["browser"], status["changed"]), ("healthy", "shared", False))
        self.assertEqual(self.connected(status), {ATLAS: True, SCOUT: True})
        self.assertTrue(status["profile_preserved"])
        for gone in ("agent_id", "ownership", "target"):
            self.assertNotIn(gone, status)

    def test_stop_and_ensure_act_on_the_browser_for_everyone(self):
        stopped = self.manage("stop")
        self.assertEqual((stopped["state"], stopped["changed"]), ("stopped", True))
        self.assertNotIn("verified", stopped)
        status = self.manage("status")
        self.assertEqual((status["state"], status["stop_requested"]), ("stopped", True))
        self.assertEqual(self.manage("ensure")["state"], "healthy")
        self.assertNotIn("stop_requested", self.manage("status"))

    def test_stop_connects_nobody(self):
        self.agents.append(self.agent(LATER))
        self.assertIs(self.connected(self.manage("stop"))[LATER], False)
        self.assertFalse(self.target_file(LATER).exists())
        self.assertFalse(uses_shared(self.paths, LATER))

    def test_restart_restarts_the_shared_browser_and_connects_every_agent(self):
        self.launcher.starts.clear()
        self.agents.append(self.agent(LATER))
        result = self.manage("restart")
        self.assertEqual((result["state"], result["changed"]), ("healthy", True))
        self.assertEqual((self.launcher.stops[-1], self.launcher.starts), (SHARED_ID, [SHARED_ID]))
        self.assertEqual(self.connected(result), {ATLAS: True, SCOUT: True, LATER: True})

    def test_a_workers_missing_target_file_is_reported_and_repair_restores_it(self):
        expected = self.target(SCOUT)
        self.target_file(SCOUT).unlink()
        status = self.manage("status")
        self.assertEqual((status["state"], self.connected(status)), ("target_missing", {ATLAS: True, SCOUT: False}))
        self.assertIn("Scout", status["diagnostic"])
        self.assertEqual(self.manage("repair")["state"], "healthy")
        self.assertEqual(self.target(SCOUT), expected)

    def test_an_agents_target_file_naming_another_browser_is_replaced(self):
        cases = {"hand-written": {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": RUNTIME_DIR},
                 "shared mark on another endpoint": {**shared_target(self.paths), "cdp_endpoint": "http://127.0.0.1:9225"},
                 "unreadable": None}
        for action in ("ensure", "repair"):
            for name, content in cases.items():
                with self.subTest(action=action, name=name):
                    self.target_file(SCOUT).write_text(json.dumps(content) if content else "not json at all")
                    self.assertEqual(self.manage("status")["state"], "target_missing")
                    result = self.manage(action)
                    self.assertEqual((result["state"], result["changed"]), ("healthy", True))
                    self.assertEqual(self.target(SCOUT), shared_target(self.paths))
        self.assertEqual(self.launcher.starts, [SHARED_ID])                         # the browser itself was never touched

    def test_a_damaged_record_is_replaced_not_trusted(self):
        for damaged in ("{}", "not json at all", json.dumps({"schema_version": 1, "agent_id": SCOUT, "browser": "shared"})):
            with self.subTest(damaged=damaged):
                (self.state(ATLAS) / MEMBERSHIP_FILE).write_text(damaged)
                with self.assertRaises(DomainError) as refused:                     # a command never reads it as "somewhere else"
                    uses_shared(self.paths, ATLAS)
                self.assertEqual(refused.exception.code, "browser_metadata_invalid")
                self.assertEqual(self.manage("status")["state"], "target_missing")
                self.assertEqual(self.manage("ensure")["state"], "healthy")
                self.assertTrue(uses_shared(self.paths, ATLAS))
                self.assertEqual(self.target(ATLAS), shared_target(self.paths))

    def test_a_record_that_is_a_symbolic_link_is_refused_for_that_agent_and_nothing_is_written_through_it(self):
        record = self.state(SCOUT) / MEMBERSHIP_FILE
        record.unlink()
        elsewhere = self.root / "elsewhere"
        elsewhere.write_text("keep")
        record.symlink_to(elsewhere)
        for action in ("status", "ensure", "repair"):
            with self.subTest(action=action):
                result = self.manage(action)
                self.assertEqual((result["state"], self.connected(result)), ("healthy", {ATLAS: True, SCOUT: False}))
                self.assertIn("symbolic link", self.entry(result, SCOUT)["problem"])
        self.assertEqual(elsewhere.read_text(), "keep")

    def test_automatic_setup_connects_an_agent_whose_record_is_damaged(self):
        (self.state(SCOUT) / MEMBERSHIP_FILE).write_text("{}")
        self.addCleanup(setup.LAST_FAILURE.clear)
        owner = SimpleNamespace(paths=self.paths, browser_manager=self.manager, browser_access=SimpleNamespace(runtime_directory=lambda: RUNTIME_DIR))
        listed = [{"agent_id": agent, "name": NAMES[agent], "role": "x"} for agent in (ATLAS, SCOUT)]
        with mock.patch.object(setup, "targets", return_value=("owner", listed)), \
                mock.patch.object(sign_in, "active", return_value=False), \
                mock.patch.object(setup.managed_chrome, "start"), \
                mock.patch.object(setup.managed_chrome, "view", return_value={"state": "ready"}):
            view = setup.view(owner)
            self.assertEqual((view["state"], view["ready"], self.connected(view)), ("target_missing", False, {ATLAS: True, SCOUT: False}))
            self.assertIn("Retry setup", view["explanation"])
            view = setup.ensure_all(owner, backoff={})
            self.assertEqual((view["state"], view["ready"], self.connected(view)), ("healthy", True, {ATLAS: True, SCOUT: True}))


class RepairTest(SharedProfileTest):
    """The Shared browser's own two files: ``ensure`` refuses to guess and says to repair; ``repair`` writes them again."""

    def setUp(self):
        super().setUp()
        self.manage("ensure")
        self.own_target = agent_browsers.shared_workspace(self.paths) / ".qorqut/browser.json"
        (self.state(SHARED_ID) / "profile/Cookies").write_text("signed in")

    def test_repair_rewrites_a_shared_target_file_that_differs_from_the_ownership_record(self):
        expected = json.loads(self.own_target.read_text())
        cases = {"conflict": (json.dumps({**expected, "cdp_endpoint": "http://127.0.0.1:9225"}), "browser_target_conflict"),
                 "invalid": ("not json at all", "browser_configuration_invalid")}
        for state, (content, code) in cases.items():
            with self.subTest(state=state):
                self.own_target.write_text(content)
                self.assertEqual(self.manage("status")["state"], state)
                refused = self.refused("ensure")
                self.assertEqual(refused.code, code)
                self.assertIn("action=repair", str(refused))
                self.assertEqual(self.own_target.read_text(), content)              # ensure overwrote nothing
                result = self.manage("repair")
                self.assertEqual((result["state"], result["changed"], self.connected(result)),
                                 ("healthy", True, {ATLAS: True, SCOUT: True}))
                self.assertEqual(json.loads(self.own_target.read_text()), expected)
        self.assertEqual((self.launcher.starts, self.launcher.stops), ([SHARED_ID], []))   # the running browser was left alone
        self.assertEqual((self.state(SHARED_ID) / "profile/Cookies").read_text(), "signed in")

    def damage(self):
        """The ownership record can no longer be read and the browser is not running."""
        self.launcher.running.clear()
        (self.state(SHARED_ID) / "meta.json").write_text("not json at all")

    def test_repair_reserves_the_browser_again_and_keeps_the_profile_when_its_record_is_unreadable(self):
        self.damage()
        status = self.manage("status")
        self.assertEqual((status["state"], self.connected(status)), ("invalid", {ATLAS: False, SCOUT: False}))
        self.assertIn("metadata_error", status)
        refused = self.refused("ensure")
        self.assertEqual(refused.code, "browser_metadata_invalid")
        self.assertIn("action=repair", str(refused))
        self.assertEqual((self.state(SHARED_ID) / "meta.json").read_text(), "not json at all")
        self.assertEqual(self.launcher.starts, [SHARED_ID])

        result = self.manage("repair")

        self.assertEqual((result["state"], result["changed"], result["verified"]), ("healthy", True, {"cdp_health": True}))
        self.assertNotIn("metadata_error", result)
        self.assertEqual(self.connected(result), {ATLAS: True, SCOUT: True})
        self.assertEqual(self.launcher.starts, [SHARED_ID, SHARED_ID])
        self.assertEqual((self.state(SHARED_ID) / "profile/Cookies").read_text(), "signed in")
        self.assertIsNotNone(shared_target(self.paths))
        for agent in (ATLAS, SCOUT):
            self.assertEqual(self.target(agent), shared_target(self.paths))

    def test_repair_drops_no_record_while_a_process_holds_the_browsers_lock(self):
        self.damage()
        lock = os.open(self.state(SHARED_ID) / "supervisor.lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.refused("repair").code, "browser_state_unverified")
        self.assertEqual((self.state(SHARED_ID) / "meta.json").read_text(), "not json at all")
        self.assertTrue(self.own_target.exists())
        self.assertEqual(self.launcher.starts, [SHARED_ID])
        self.assertEqual((self.state(SHARED_ID) / "profile/Cookies").read_text(), "signed in")


class CommandTest(SharedProfileTest):
    def setUp(self):
        super().setUp()
        self.manage("ensure")

    def test_a_command_holds_the_shared_browsers_use_lock(self):
        with command_use(self.paths, ATLAS):
            with self.assertRaises(DomainError) as refused:
                self.manage("restart")
            self.assertEqual(refused.exception.code, "browser_in_use")
        self.assertEqual(self.manage("restart")["state"], "healthy")

    def test_a_stopped_shared_browser_refuses_the_command_and_nothing_else_is_used(self):
        self.manage("stop")
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
        self.install(LATER, expected)
        with self.assertRaises(DomainError) as refused:                             # a shared mark without the record
            self.shared(LATER)
        self.assertEqual(refused.exception.code, "browser_configuration_invalid")


class SignInTest(SharedProfileTest):
    """The owner's sign-in window opens the Shared browser's profile, whose sites list is the one every agent has."""

    def setUp(self):
        super().setUp()
        self.manage("ensure")
        self.runtime = SimpleNamespace(browser_manager=self.manager)

    def test_a_sign_in_is_on_the_shared_profile(self):
        self.assertEqual(sign_in._state(self.runtime), self.state(SHARED_ID))
        self.assertEqual(sign_in._meta(self.runtime)["managed"]["profile"], str(self.state(SHARED_ID) / "profile"))

    def test_the_sites_list_is_the_browsers(self):
        sign_in._write_sites(self.runtime, [{"site": "shared.example", "added_at": "t"}])
        self.assertEqual([item["site"] for item in sign_in._read_sites(self.runtime)], ["shared.example"])
        self.assertTrue((self.state(SHARED_ID) / sign_in.SITES_FILE).exists())


if __name__ == "__main__":
    unittest.main()
