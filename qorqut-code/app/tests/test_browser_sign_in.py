"""The owner signs in to websites on the Shared browser's profile; Qorqbot records sites, never passwords."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap.preparation import connector_gate_guard
from qorqut.browser import agent_supervisor as supervisor, setup, sign_in
from qorqut.browser.agent_browsers import SHARED_ID

AGENT = "8fdca155-2f3b-4735-800f-41624c6d26cc"
META = {"mode": "managed", "managed": {"chrome": "/chrome", "port": 20400, "profile": "/state/profile"}}


class ParseTest(unittest.TestCase):
    def test_accepts_domains_and_addresses(self):
        self.assertEqual(sign_in.parse(" slack.com "), ("slack.com", "https://slack.com"))
        self.assertEqual(sign_in.parse("https://www.Notion.so/login?x=1"), ("notion.so", "https://www.Notion.so/login?x=1"))
        self.assertEqual(sign_in.parse("http://127.0.0.1:9000/login")[0], "127.0.0.1")

    def test_refuses_anything_else(self):
        for value in ("", "   ", None, 7, "nodots", "file:///etc/passwd", "javascript://x.com", "https://me:pw@slack.com", "a b.com", "x" * 301):
            with self.subTest(value=value), self.assertRaises(DomainError):
                sign_in.parse(value)


class CoversTest(unittest.TestCase):
    def test_host_subdomain_and_parent(self):
        self.assertTrue(sign_in.covers(".slack.com", "slack.com"))
        self.assertTrue(sign_in.covers("app.slack.com", "slack.com"))
        self.assertTrue(sign_in.covers(".slack.com", "app.slack.com"))

    def test_lookalikes_do_not_match(self):
        self.assertFalse(sign_in.covers("notslack.com", "slack.com"))
        self.assertFalse(sign_in.covers("slack.com.evil.io", "slack.com"))


class WatchWindowTest(unittest.TestCase):
    def watch(self, counts, *, alive=lambda: None, limit=100):
        replies = iter(counts)
        def pages():
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return [{}] * value
        now = [0]
        def sleep(_):
            now[0] += 1
        sign_in.watch_window(SimpleNamespace(poll=alive), 1, limit=limit, sleep=sleep, clock=lambda: now[0], pages=pages)
        return now[0]

    def test_returns_after_the_last_window_stays_closed(self):
        self.assertEqual(self.watch([1, 1, 0, 0]), 4)

    def test_a_tab_swap_blip_does_not_end_sign_in(self):
        self.assertEqual(self.watch([1, 0, 1, 0, 0]), 5)

    def test_waits_for_a_window_to_appear_first(self):
        self.assertEqual(self.watch([0, 0, 0, 1, 0, 0]), 6)

    def test_ignores_unreachable_moments(self):
        self.assertEqual(self.watch([OSError(), 1, OSError(), 0, 0]), 5)

    def test_gives_the_browser_back_after_the_limit(self):
        self.assertEqual(self.watch([1] * 10, limit=3), 3)

    def test_a_chrome_that_quit_ends_at_once(self):
        self.assertEqual(self.watch([], alive=lambda: 0), 0)


class FlowTest(unittest.TestCase):
    def setUp(self):
        # These flows mock the window process; no real display is required.
        self.enterContext(mock.patch.dict(sign_in.os.environ, {"DISPLAY": ":synthetic-test"}))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.state = self.root / SHARED_ID        # sites and the browser's record are the Shared browser's, whoever asks
        self.state.mkdir()
        self.calls = []
        def manage(owner, arguments, idempotency_key=None, owner_control=False):
            self.assertTrue(owner_control)
            self.assertEqual(set(arguments), {"action"})
            self.calls.append(arguments["action"])
            return {}
        self.runtime = SimpleNamespace(browser_manager=SimpleNamespace(paths=None, manage=manage))
        for target, value in (("agent_state", lambda _p, agent: self.root / agent), ("_meta", lambda _runtime: META)):
            patcher = mock.patch.object(sign_in, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(setup, "targets", return_value=("owner", [{"agent_id": AGENT, "name": "Ordubashy"}]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(sign_in._window.clear)

    def no_window(self):
        """The window is a stand-in without a DevTools port: nothing may ask a real port on this computer for its cookies."""
        return mock.patch.multiple(sign_in, _window_cookies=mock.Mock(return_value=[]), _quit_cleanly=mock.Mock())

    def test_a_second_window_is_refused_while_one_is_open_and_names_the_first_site(self):
        sign_in._window["site"] = "notion.so"
        with mock.patch.object(sign_in.subprocess, "Popen") as popen, self.assertRaises(DomainError) as caught:
            sign_in.start(self.runtime, "slack.com")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("notion.so", str(caught.exception))
        popen.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(sign_in._window, {"site": "notion.so"})        # the open window is still the one being waited for

    def test_a_window_is_never_started_without_a_screen_and_the_browser_is_left_running(self):
        with mock.patch.object(sign_in, "has_screen", return_value=False), mock.patch.object(sign_in.subprocess, "Popen") as popen, \
                self.assertRaises(DomainError) as refused:
            sign_in.start(self.runtime, "slack.com")
        self.assertEqual(refused.exception.code, "browser_no_screen")
        self.assertIn("Browser tab", str(refused.exception))
        popen.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertFalse(sign_in.active())

    def test_failed_window_restores_the_browser_and_clears_the_session(self):
        (self.state / "stop-requested.json").write_text("{}")
        # The window's environment is not under test; outside an installed folder it depends on how deep the system's
        # temporary directory is.
        with mock.patch.object(sign_in, "chrome_environment", return_value={}), \
                mock.patch.object(sign_in.subprocess, "Popen", side_effect=OSError("nope")), self.assertRaises(OSError):
            sign_in.start(self.runtime, "slack.com")
        self.assertEqual(self.calls, ["stop", "ensure"])
        self.assertFalse(sign_in.active())
        self.assertFalse((self.state / "stop-requested.json").exists())     # the pause is not the owner's stop

    def test_a_window_that_closes_at_once_points_the_owner_to_the_live_view_not_to_installing_chrome(self):
        # The browser's own Chrome ran headless a moment ago, so it is installed; a computer without a screen is the usual cause.
        with mock.patch.object(sign_in, "chrome_environment", return_value={}), mock.patch.object(sign_in.time, "sleep"), \
                mock.patch.object(sign_in.subprocess, "Popen", return_value=SimpleNamespace(poll=lambda: 1)), \
                self.assertRaises(DomainError) as refused:
            sign_in.start(self.runtime, "slack.com")
        self.assertEqual(refused.exception.code, "browser_start_failed")
        self.assertIn("Browser tab", str(refused.exception))
        self.assertNotIn("installed", str(refused.exception))
        self.assertEqual(self.calls, ["stop", "ensure"])

    def test_finish_records_the_site_once_and_restores_the_browser(self):
        process = SimpleNamespace()
        sign_in._window["site"] = "slack.com"
        with mock.patch.object(sign_in, "watch_window"), mock.patch.object(sign_in, "stop_child") as stop, self.no_window():
            sign_in._finish(self.runtime, "owner", "slack.com", process, 1)
            sign_in._window["site"] = "slack.com"
            sign_in._finish(self.runtime, "owner", "slack.com", process, 1)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([item["site"] for item in json.loads((self.state / sign_in.SITES_FILE).read_text())], ["slack.com"])
        self.assertEqual(self.calls, ["ensure", "ensure"])
        self.assertFalse(sign_in.active())

    def test_nothing_secret_is_recorded(self):
        with mock.patch.object(sign_in, "watch_window"), mock.patch.object(sign_in, "stop_child"), self.no_window():
            sign_in._window["site"] = "slack.com"
            sign_in._finish(self.runtime, "owner", "slack.com", SimpleNamespace(), 1)
        self.assertEqual(set(json.loads((self.state / sign_in.SITES_FILE).read_text())[0]), {"site", "added_at"})

    def test_accounts_report_the_real_session_state(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}, {"site": "notion.so"}]))
        with mock.patch.object(sign_in, "_cookies", return_value=[{"domain": ".slack.com"}]):
            accounts = sign_in.accounts(self.runtime)
        self.assertEqual(set(accounts), {"signing_in", "sites"})            # one browser: no list of agents
        self.assertIsNone(accounts["signing_in"])
        self.assertEqual([set(item) for item in accounts["sites"]], [{"site", "added_at", "session_saved"}] * 2)
        self.assertEqual([item["session_saved"] for item in accounts["sites"]], [True, False])

    def test_accounts_say_unknown_when_the_browser_is_off(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        with mock.patch.object(sign_in, "_cookies", side_effect=OSError("down")):
            self.assertIsNone(sign_in.accounts(self.runtime)["sites"][0]["session_saved"])

    def test_accounts_name_the_site_being_signed_in_to_and_leave_the_paused_browser_alone(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        sign_in._window["site"] = "notion.so"
        with mock.patch.object(sign_in, "_cookies", side_effect=AssertionError("the paused browser was asked")):
            accounts = sign_in.accounts(self.runtime)
        self.assertEqual(accounts["signing_in"], "notion.so")
        self.assertEqual([(item["site"], item["session_saved"]) for item in accounts["sites"]], [("slack.com", None)])

    def test_sign_out_deletes_only_that_sites_data(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}, {"site": "notion.so"}]))
        cookies = [{"name": "a", "domain": ".slack.com", "path": "/"}, {"name": "b", "domain": ".notion.so", "path": "/"}]
        with mock.patch.object(sign_in, "_cookies", return_value=cookies), mock.patch.object(sign_in, "_cdp", return_value=[]) as cdp:
            sign_in.sign_out(self.runtime, "slack.com")
        calls = next(call.args[1] for call in cdp.call_args_list if call.args[1] and call.args[1][0][0] == "Network.deleteCookies")
        self.assertEqual([params["name"] for method, params in calls if method == "Network.deleteCookies"], ["a"])
        self.assertTrue(all("notion" not in params["origin"] for method, params in calls if method == "Storage.clearDataForOrigin"))
        self.assertEqual([item["site"] for item in json.loads((self.state / sign_in.SITES_FILE).read_text())], ["notion.so"])

    def test_sign_out_keeps_the_record_when_the_browser_is_off(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        with mock.patch.object(sign_in, "_cookies", side_effect=OSError("down")), self.assertRaises(DomainError):
            sign_in.sign_out(self.runtime, "slack.com")
        self.assertEqual(len(json.loads((self.state / sign_in.SITES_FILE).read_text())), 1)

    def test_sign_out_waits_for_the_window(self):
        sign_in._window["site"] = "slack.com"
        with self.assertRaises(DomainError) as refused:
            sign_in.sign_out(self.runtime, "slack.com")
        self.assertEqual(refused.exception.code, "conflict")


class AgentToolTest(unittest.TestCase):
    def setUp(self):
        class Read:
            def __enter__(self):
                return SimpleNamespace()
            def __exit__(self, *_):
                return False
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=Read))
        patcher = mock.patch("qorqut.management.identity.configured_ordubashy", return_value="ordubashy")
        patcher.start()
        self.addCleanup(patcher.stop)
        # These calls reach the window, which is what a computer with a screen does; the live view has its own tests.
        patcher = mock.patch.object(sign_in, "has_screen", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_ordubashy_can_call_it(self):
        with mock.patch.object(sign_in, "start") as start, self.assertRaises(DomainError) as caught:
            sign_in.agent_call(self.runtime, "worker", {"action": "open", "site": "slack.com"})
        self.assertEqual(caught.exception.code, "forbidden")
        start.assert_not_called()

    def test_open_takes_only_the_site_and_guides_the_owner(self):
        with mock.patch.object(sign_in, "start") as start:
            result = sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com"})
        start.assert_called_once_with(self.runtime, "slack.com")
        self.assertEqual(result["opened"], "slack.com")
        self.assertIn("sign in themselves", result["next"])

    def test_with_a_screen_the_window_opens_as_before_and_the_live_view_is_not_used(self):
        with mock.patch.object(sign_in, "has_screen", return_value=True), mock.patch.object(sign_in, "start") as start, \
                mock.patch.object(sign_in, "open_in_live_view") as live_view:
            result = sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com"})
        start.assert_called_once_with(self.runtime, "slack.com")
        live_view.assert_not_called()
        self.assertEqual(set(result), {"opened", "next"})
        self.assertIn("close the window", result["next"])

    def test_without_a_screen_no_window_is_tried_and_the_page_opens_for_the_live_view(self):
        with mock.patch.object(sign_in, "has_screen", return_value=False), mock.patch.object(sign_in, "start") as start, \
                mock.patch.object(sign_in, "open_in_live_view", return_value={"where": "live_view"}) as live_view:
            result = sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com"})
        start.assert_not_called()
        live_view.assert_called_once_with(self.runtime, "ordubashy", "slack.com", "https://slack.com")    # the caller names the tab
        self.assertEqual(result, {"where": "live_view"})

    def test_plain_http_is_refused_except_on_this_computer(self):
        with mock.patch.object(sign_in, "start") as start:
            with self.assertRaises(DomainError):
                sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "http://slack.com/login"})
            start.assert_not_called()
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "http://127.0.0.1:9000/login"})
            start.assert_called_once()

    def test_a_missing_site_is_refused(self):
        with self.assertRaises(DomainError):
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "open"})

    def test_status_and_sign_out(self):
        with mock.patch.object(sign_in, "accounts", return_value={"signing_in": None, "sites": []}) as accounts:
            self.assertEqual(sign_in.agent_call(self.runtime, "ordubashy", {"action": "status"}), {"signing_in": None, "sites": []})
        accounts.assert_called_once_with(self.runtime)
        with mock.patch.object(sign_in, "sign_out") as out:
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "sign_out", "site": "slack.com"})
        out.assert_called_once_with(self.runtime, "slack.com")


class SessionCookieTest(unittest.TestCase):
    def test_only_session_cookies_are_copied_and_they_last(self):
        cookies = [{"name": "s", "value": "1", "domain": "a.b", "path": "/", "secure": True, "httpOnly": True, "session": True, "size": 9},
                   {"name": "p", "value": "2", "domain": "a.b", "path": "/", "session": False, "expires": 5}]
        self.assertEqual(sign_in.lasting_copies(cookies, 1000),
                         [{"name": "s", "value": "1", "domain": "a.b", "path": "/", "secure": True, "httpOnly": True,
                           "expires": 1000 + sign_in.SESSION_COOKIE_LIFETIME}])

    def test_finish_gives_the_browser_the_window_session(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        runtime = SimpleNamespace(browser_manager=SimpleNamespace(paths=None, manage=mock.Mock(return_value={})))
        cookie = {"name": "s", "value": "1", "domain": "a.b", "path": "/", "session": True}
        order = []
        with mock.patch.object(sign_in, "agent_state", return_value=root), mock.patch.object(sign_in, "_meta", return_value=META), \
                mock.patch.object(sign_in, "watch_window"), mock.patch.object(sign_in, "_quit_cleanly"), mock.patch.object(sign_in, "stop_child"), \
                mock.patch.object(sign_in, "_window_cookies", return_value=[cookie]), \
                mock.patch.object(sign_in, "_cdp", side_effect=lambda port, calls: order.append((port, calls))):
            sign_in._finish(runtime, "owner", "a.b", SimpleNamespace(), 1)
        self.assertEqual(order[0][0], 20400)
        self.assertEqual(order[0][1][0][1]["cookies"][0]["name"], "s")
        runtime.browser_manager.manage.assert_called_once()


class CommandTest(unittest.TestCase):
    def test_window_and_headless_read_the_same_profile(self):
        headless = supervisor.chrome_command(META)
        window = supervisor.visible_chrome_command(META, "https://slack.com", 55555)
        self.assertIn("--headless=new", headless)
        self.assertNotIn("--headless=new", window)
        # Agents' headless browsers draw no 3D; the owner's sign-in window still can.
        self.assertIn("--disable-3d-apis", headless)
        self.assertNotIn("--disable-3d-apis", window)
        self.assertEqual(headless[-1], "about:blank")
        self.assertEqual(window[-1], "https://slack.com")
        for flag in supervisor.profile_flags(META["managed"]):
            self.assertIn(flag, headless)
            self.assertIn(flag, window)

    def test_window_never_uses_the_port_agents_know(self):
        window = supervisor.visible_chrome_command(META, "https://slack.com", 55555)
        self.assertIn("--remote-debugging-port=55555", window)
        self.assertNotIn("--remote-debugging-port=20400", window)


class GuardTest(unittest.TestCase):
    def test_protects_the_hook_files_from_edit_and_bash(self):
        rules = connector_gate_guard(Path("/w"))
        for name in (".claude/settings.json", ".claude/settings.local.json", ".qorqut/activity-hook.py"):
            self.assertTrue(any(rule.startswith("Edit(") and name in rule for rule in rules), name)
        self.assertIn("Bash(*.claude/settings*)", rules)
        self.assertIn("Bash(*.qorqut/activity-hook*)", rules)


class PolicyTest(unittest.TestCase):
    def test_only_the_ordubashy_role_gets_the_tool(self):
        from qorqut.composition.policy import _owner_approvals
        decide = lambda role, overrides=None: _owner_approvals("website_sign_in", {}, overrides or {}, None, role_id=role, scope=None, resolve=None).mode
        self.assertEqual(decide("ordubashy"), "allow")
        self.assertEqual(decide("global-worker"), "deny")
        self.assertEqual(decide("ordubashy", {"website_sign_in": "deny"}), "deny")
