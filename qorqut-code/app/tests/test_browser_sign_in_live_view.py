"""On a computer without a screen the owner signs in through the live view: the sign-in page opens in a tab of the agent's browser."""

from importlib.resources import files
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap import qorqbot_guide
from qorqut.browser import agent_browsers, operations, setup, sign_in
from qorqut.browser.agent_browsers import MEMBERSHIP_FILE, SHARED_ID

ORDUBASHY, ATLAS, SCOUT = "ordubashy", "atlas", "scout"
PORTS = {ORDUBASHY: 41001, ATLAS: 41002, SCOUT: 41003, SHARED_ID: 41099}    # nothing connects: only the tab opener is replaced
NAMES = {ORDUBASHY: "Ordubashy", ATLAS: "Atlas", SCOUT: "Scout"}
TAB = "A" * 32


class Read:
    def __enter__(self):
        return SimpleNamespace()

    def __exit__(self, *_):
        return False


def private_file(path, content):
    """Browser records are read only when they are private to their owner, as the ones Qorqbot writes are."""
    path.write_text(json.dumps(content))
    path.chmod(0o600)


class LiveViewSignInTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.paths = SimpleNamespace(runtime=self.root)
        self.managed = []
        manage = lambda *_args, **kwargs: self.managed.append(_args[1]["action"])
        self.runtime = SimpleNamespace(browser_manager=SimpleNamespace(paths=self.paths, manage=manage), database=SimpleNamespace(read=Read))
        for name, port in PORTS.items():        # every browser has its ownership record, as a set up browser does
            directory = agent_browsers.agent_state(self.paths, name)
            directory.mkdir(parents=True)
            private_file(directory / "meta.json", {"mode": "managed", "managed": {"port": port}})
        agents = [{"agent_id": agent, "name": name, "role": "global-worker"} for agent, name in NAMES.items()]
        self.opened, self.ready = [], {"state": "ready"}
        def open_tab(port, url):
            self.opened.append((port, url))
            return TAB
        for patcher in (
                mock.patch.object(setup, "targets", return_value=("owner", agents)),
                mock.patch("qorqut.management.identity.configured_ordubashy", return_value=ORDUBASHY),
                mock.patch.object(sign_in, "has_screen", return_value=False),
                mock.patch.object(sign_in, "_open_tab", open_tab),
                mock.patch.object(agent_browsers, "meta_valid", return_value=True),
                mock.patch.object(agent_browsers, "supervisor_state", lambda *_args: self.ready),
                # The tab is the only thing done to the browser: no window, no page command, no other DevTools call.
                mock.patch.object(sign_in.subprocess, "Popen", side_effect=AssertionError("a window was opened")),
                mock.patch.object(sign_in, "_cdp", side_effect=AssertionError("a page was driven")),
                mock.patch.object(sign_in, "_get_json", side_effect=AssertionError("a browser was asked")),
                mock.patch.object(sign_in, "_window_cookies", side_effect=AssertionError("a window's cookies were read"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(sign_in._active.clear)

    def open(self, site="slack.com", caller=ORDUBASHY, **more):
        return sign_in.agent_call(self.runtime, caller, {"action": "open", "site": site, **more})

    def make_shared(self, *agents):
        for agent in agents:
            private_file(agent_browsers.agent_state(self.paths, agent) / MEMBERSHIP_FILE,
                         {"schema_version": 1, "agent_id": agent, "browser": "shared"})

    def sites(self, name):
        path = agent_browsers.agent_state(self.paths, name) / sign_in.SITES_FILE
        return [item["site"] for item in json.loads(path.read_text())] if path.exists() else []

    def test_the_sign_in_page_opens_in_a_tab_of_the_callers_own_browser_and_the_reply_says_live_view(self):
        reply = self.open()
        self.assertEqual(self.opened, [(PORTS[ORDUBASHY], "https://slack.com")])
        self.assertEqual({key: reply[key] for key in ("opened", "where", "agent")}, {"opened": "slack.com", "where": "live_view", "agent": "Ordubashy"})
        self.assertEqual(set(reply), {"opened", "where", "agent", "next"})
        for words in ("Browser tab", "Ordubashy", "slack.com", "wait until they say so", "password or code"):
            self.assertIn(words, reply["next"])
        self.assertNotIn("Shared", reply["next"])

    def test_the_browser_is_neither_stopped_nor_paused(self):
        self.open()
        self.assertEqual(self.managed, [])                 # not stopped, restarted or ensured
        self.assertFalse(sign_in.active(ORDUBASHY))        # setup goes on looking after this browser
        self.assertEqual(sign_in._active, {})

    def test_the_address_the_agent_gives_is_the_address_that_opens(self):
        reply = self.open("https://app.Slack.com/signin?team=1")
        self.assertEqual(self.opened, [(PORTS[ORDUBASHY], "https://app.Slack.com/signin?team=1")])
        self.assertEqual(reply["opened"], "app.slack.com")

    def test_a_site_that_is_not_https_is_still_refused_before_anything_opens(self):
        with self.assertRaises(DomainError):
            self.open("http://slack.com/login")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.open("http://127.0.0.1:9000/login")["opened"], "127.0.0.1")

    def test_a_worker_chosen_by_ordubashy_gets_the_tab_in_its_own_browser(self):
        reply = self.open(agent_id=ATLAS)
        self.assertEqual(self.opened, [(PORTS[ATLAS], "https://slack.com")])     # not Ordubashy's browser
        self.assertEqual(reply["agent"], "Atlas")
        self.assertIn("Atlas's Browser tab", reply["next"])
        self.assertEqual((self.sites(ATLAS), self.sites(ORDUBASHY)), (["slack.com"], []))
        self.assertEqual(self.managed, [])

    def test_ordubashy_on_the_shared_browser_opens_the_tab_there_and_the_site_is_the_shared_ones(self):
        self.make_shared(ORDUBASHY, SCOUT)
        reply = self.open()
        self.assertEqual(self.opened, [(PORTS[SHARED_ID], "https://slack.com")])
        self.assertEqual((self.sites(SHARED_ID), self.sites(ORDUBASHY)), (["slack.com"], []))
        self.assertIn("Shared", reply["next"])
        self.assertEqual(self.managed, [])             # the agents sharing it are not paused, as a window would pause them
        self.assertEqual(sign_in._active, {})

    def test_a_worker_on_the_shared_browser_gets_the_tab_in_the_shared_browser_not_in_its_own(self):
        self.make_shared(ATLAS)
        reply = self.open(agent_id=ATLAS)
        self.assertEqual(self.opened, [(PORTS[SHARED_ID], "https://slack.com")])
        self.assertEqual(reply["agent"], "Atlas")
        self.assertEqual((self.sites(SHARED_ID), self.sites(ATLAS)), (["slack.com"], []))
        self.assertIn("is open in the Shared browser.", reply["next"])
        self.assertNotIn("Atlas's browser", reply["next"])

    def test_someone_other_than_ordubashy_is_refused_and_nothing_opens(self):
        for caller in (ATLAS, "stranger"):
            with self.subTest(caller=caller), self.assertRaises(DomainError) as refused:
                self.open(caller=caller)
            self.assertEqual(refused.exception.code, "forbidden")
        self.assertEqual((self.opened, self.sites(ORDUBASHY), self.managed), ([], [], []))

    def test_an_agent_without_a_browser_is_refused_and_nothing_opens(self):
        with self.assertRaises(DomainError) as refused:
            self.open(agent_id="nobody")
        self.assertEqual(refused.exception.code, "not_found")
        self.assertEqual(self.opened, [])

    def test_a_browser_that_is_not_running_is_reported_and_nothing_opens_or_starts(self):
        self.ready = {"state": "stopped"}
        with self.assertRaises(DomainError) as refused:
            self.open()
        self.assertEqual(refused.exception.code, "browser_not_ready")
        self.assertEqual((self.opened, self.sites(ORDUBASHY), self.managed), ([], [], []))

    def test_a_tab_that_cannot_be_opened_is_reported_and_no_site_is_recorded(self):
        with mock.patch.object(sign_in, "_open_tab", side_effect=OSError("closed")), self.assertLogs(sign_in.logger, "ERROR"), \
                self.assertRaises(DomainError) as refused:
            self.open()
        self.assertEqual(refused.exception.code, "browser_not_ready")
        self.assertEqual(self.sites(ORDUBASHY), [])

    def test_a_site_list_that_cannot_be_written_does_not_stop_the_owner_signing_in(self):
        with mock.patch.object(sign_in, "_write_sites", side_effect=OSError("read-only")), self.assertLogs(sign_in.logger, "WARNING"):
            reply = self.open()
        self.assertEqual(reply["where"], "live_view")
        self.assertEqual(len(self.opened), 1)

    def test_opening_again_opens_another_tab_but_lists_the_site_once(self):
        self.open()
        self.open()
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(self.sites(ORDUBASHY), ["slack.com"])

    def test_status_lists_the_site_without_saying_the_owner_is_signed_in(self):
        self.open()
        with mock.patch.object(sign_in, "_cookies", return_value=[]):
            agent = next(item for item in sign_in.agent_call(self.runtime, ORDUBASHY, {"action": "status"})["agents"] if item["agent_id"] == ORDUBASHY)
        self.assertIsNone(agent["signing_in"])                           # no window is open, and none will be
        self.assertEqual([(item["site"], item["session_saved"]) for item in agent["sites"]], [("slack.com", False)])
        with mock.patch.object(sign_in, "_cookies", return_value=[{"domain": ".slack.com"}]):
            sites = sign_in.agent_call(self.runtime, ORDUBASHY, {"action": "status"})["agents"][0]["sites"]
        self.assertEqual([(item["site"], item["session_saved"]) for item in sites], [("slack.com", True)])    # cookies only, as always
        self.assertEqual(self.managed, [])

    def test_status_for_the_shared_browser_shows_the_site_to_every_agent_on_it(self):
        self.make_shared(ORDUBASHY, SCOUT)
        self.open()
        with mock.patch.object(sign_in, "_cookies", return_value=[]):
            agents = {item["agent_id"]: item for item in sign_in.agent_call(self.runtime, ORDUBASHY, {"action": "status"})["agents"]}
        self.assertEqual([item["site"] for item in agents[SCOUT]["sites"]], ["slack.com"])
        self.assertEqual(agents[ATLAS]["sites"], [])

    def test_sign_out_removes_the_site_that_was_opened_this_way(self):
        self.open()
        cookies = [{"name": "a", "domain": ".slack.com", "path": "/"}]
        with mock.patch.object(sign_in, "_cookies", return_value=cookies), mock.patch.object(sign_in, "_cdp", return_value=[]) as cdp:
            sign_in.agent_call(self.runtime, ORDUBASHY, {"action": "sign_out", "site": "slack.com"})
        self.assertEqual(cdp.call_args.args[1][0][0], "Network.deleteCookies")
        self.assertEqual(self.sites(ORDUBASHY), [])


class InstructionTextTest(unittest.TestCase):
    """The reply's name for this case is written in four instructions. They must keep naming it, and must not send an agent
    to type the owner's password as the first choice again."""

    def texts(self):
        registered = []
        operations.register_agent_browser_management(SimpleNamespace(registry=SimpleNamespace(register=registered.append)))
        description = next(item.description for item in registered if item.name == operations.WEBSITE_SIGN_IN)
        defaults = files("qorqut_agentic").joinpath("resources/defaults/operations")
        skill = defaults.joinpath("imported-role-skills/native/skills/managed-browser-control/SKILL.md").read_text()
        onboarding = defaults.joinpath("agentic-management/instructions/onboarding-connect-tools.md").read_text()
        return {"tool description": description, "managed browser skill": skill, "connect-tools step": onboarding}

    def test_each_says_live_view_and_the_browser_tab_and_none_still_names_the_old_error(self):
        for name, text in self.texts().items():
            with self.subTest(name):
                flat = " ".join(text.split())
                self.assertIn("live_view", flat)
                self.assertIn("Browser tab", flat)
                self.assertNotIn("browser_no_screen", flat)
                self.assertIn("password or code", flat)               # still the fallback, and only that

    def test_the_guide_for_the_owner_says_the_owner_signs_in_in_the_browser_tab(self):
        flat = " ".join(qorqbot_guide.SKILL.split())
        self.assertIn("the owner signs in there through that agent's **Browser** tab", flat)
        self.assertNotIn("Ordubashy signs in on the site's own page", flat)


class ScreenTest(unittest.TestCase):
    def check(self, platform, environment):
        with mock.patch.object(sign_in.sys, "platform", platform), mock.patch.dict(sign_in.os.environ, environment, clear=True):
            return sign_in.has_screen()

    def test_only_a_linux_computer_without_a_display_has_no_screen(self):
        self.assertFalse(self.check("linux", {}))
        self.assertTrue(self.check("linux", {"DISPLAY": ":0"}))
        self.assertTrue(self.check("linux", {"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertFalse(self.check("linux", {"DISPLAY": ""}))
        self.assertTrue(self.check("darwin", {}))


class DevToolsTest(unittest.TestCase):
    """The tab opener and the browser-level call behind it, against a stand-in for Chrome's DevTools socket."""

    class Socket:
        def __init__(self, replies):
            self.replies, self.sent = list(replies), []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def send(self, text):
            self.sent.append(json.loads(text))

        def recv(self, timeout=None):
            return json.dumps(self.replies.pop(0))

    def call(self, replies, function, *args):
        socket = self.Socket(replies)
        version = {"webSocketDebuggerUrl": "ws://127.0.0.1:41000/devtools/browser/x"}
        with mock.patch.object(sign_in, "_get_json", return_value=version) as asked, \
                mock.patch("websockets.sync.client.connect", return_value=socket) as connect:
            result = function(41000, *args)
        asked.assert_called_once_with(41000, "/json/version")
        self.assertEqual(connect.call_args.args[0], version["webSocketDebuggerUrl"])
        return result, socket.sent

    def test_the_tab_is_made_with_one_createtarget_on_the_browser_and_its_id_is_returned(self):
        tab, sent = self.call([{"method": "Target.targetCreated", "params": {}}, {"id": 1, "result": {"targetId": TAB}}],
                              sign_in._open_tab, "https://slack.com")
        self.assertEqual(tab, TAB)
        self.assertEqual(sent, [{"id": 1, "method": "Target.createTarget", "params": {"url": "https://slack.com"}}])

    def test_an_answer_that_is_an_error_is_an_error(self):
        with self.assertRaises(OSError):
            self.call([{"id": 1, "error": {"message": "no"}}], sign_in._open_tab, "https://slack.com")

    def test_the_window_cookies_are_still_read_at_browser_level(self):
        cookies, sent = self.call([{"id": 1, "result": {"cookies": [{"name": "s"}]}}], sign_in._window_cookies)
        self.assertEqual(cookies, [{"name": "s"}])
        self.assertEqual(sent, [{"id": 1, "method": "Storage.getCookies", "params": {}}])


if __name__ == "__main__":
    unittest.main()
