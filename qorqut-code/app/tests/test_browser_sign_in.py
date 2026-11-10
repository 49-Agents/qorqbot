"""The owner signs in to websites on an agent's browser profile; Qorqbot records sites, never passwords."""

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
        (self.root / AGENT).mkdir()
        self.calls = []
        def manage(owner, arguments, idempotency_key=None, owner_control=False):
            self.assertTrue(owner_control)
            self.calls.append(arguments["action"])
            return {}
        self.runtime = SimpleNamespace(browser_manager=SimpleNamespace(paths=None, manage=manage))
        for target, value in (("agent_state", lambda _p, agent: self.root / agent), ("_meta", lambda *_a: META)):
            patcher = mock.patch.object(sign_in, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(setup, "targets", return_value=("owner", [{"agent_id": AGENT, "name": "Ordubashy"}]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(sign_in._active.clear)

    def test_unknown_agent_is_refused(self):
        with self.assertRaises(DomainError):
            sign_in.start(self.runtime, "other", "slack.com")

    def test_only_one_window_per_agent(self):
        sign_in._active[AGENT] = "notion.so"
        with self.assertRaises(DomainError) as caught:
            sign_in.start(self.runtime, AGENT, "slack.com")
        self.assertIn("notion.so", str(caught.exception))
        self.assertEqual(self.calls, [])

    def test_failed_window_restores_the_browser_and_clears_the_session(self):
        (self.root / AGENT / "stop-requested.json").write_text("{}")
        with mock.patch.object(sign_in.subprocess, "Popen", side_effect=OSError("nope")), self.assertRaises(OSError):
            sign_in.start(self.runtime, AGENT, "slack.com")
        self.assertEqual(self.calls, ["stop", "ensure"])
        self.assertFalse(sign_in.active(AGENT))

    def test_finish_records_the_site_once_and_restores_the_browser(self):
        process = SimpleNamespace()
        sign_in._active[AGENT] = "slack.com"
        with mock.patch.object(sign_in, "watch_window"), mock.patch.object(sign_in, "stop_child") as stop:
            sign_in._finish(self.runtime, "owner", AGENT, "slack.com", process, 1)
            sign_in._active[AGENT] = "slack.com"
            sign_in._finish(self.runtime, "owner", AGENT, "slack.com", process, 1)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual([item["site"] for item in json.loads((self.root / AGENT / sign_in.SITES_FILE).read_text())], ["slack.com"])
        self.assertEqual(self.calls, ["ensure", "ensure"])
        self.assertFalse(sign_in.active(AGENT))

    def test_nothing_secret_is_recorded(self):
        with mock.patch.object(sign_in, "watch_window"), mock.patch.object(sign_in, "stop_child"):
            sign_in._active[AGENT] = "slack.com"
            sign_in._finish(self.runtime, "owner", AGENT, "slack.com", SimpleNamespace(), 1)
        self.assertEqual(set(json.loads((self.root / AGENT / sign_in.SITES_FILE).read_text())[0]), {"site", "added_at"})

    def test_accounts_report_the_real_session_state(self):
        (self.root / AGENT / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}, {"site": "notion.so"}]))
        with mock.patch.object(sign_in, "_cookies", return_value=[{"domain": ".slack.com"}]):
            sites = sign_in.accounts(self.runtime)["agents"][0]["sites"]
        self.assertEqual([item["session_saved"] for item in sites], [True, False])

    def test_accounts_say_unknown_when_the_browser_is_off(self):
        (self.root / AGENT / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        with mock.patch.object(sign_in, "_cookies", side_effect=OSError("down")):
            self.assertIsNone(sign_in.accounts(self.runtime)["agents"][0]["sites"][0]["session_saved"])

    def test_sign_out_deletes_only_that_sites_data(self):
        (self.root / AGENT / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}, {"site": "notion.so"}]))
        cookies = [{"name": "a", "domain": ".slack.com", "path": "/"}, {"name": "b", "domain": ".notion.so", "path": "/"}]
        with mock.patch.object(sign_in, "_cookies", return_value=cookies), mock.patch.object(sign_in, "_cdp", return_value=[]) as cdp:
            sign_in.sign_out(self.runtime, AGENT, "slack.com")
        calls = next(call.args[1] for call in cdp.call_args_list if call.args[1] and call.args[1][0][0] == "Network.deleteCookies")
        self.assertEqual([params["name"] for method, params in calls if method == "Network.deleteCookies"], ["a"])
        self.assertTrue(all("notion" not in params["origin"] for method, params in calls if method == "Storage.clearDataForOrigin"))
        self.assertEqual([item["site"] for item in json.loads((self.root / AGENT / sign_in.SITES_FILE).read_text())], ["notion.so"])

    def test_sign_out_keeps_the_record_when_the_browser_is_off(self):
        (self.root / AGENT / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        with mock.patch.object(sign_in, "_cookies", side_effect=OSError("down")), self.assertRaises(DomainError):
            sign_in.sign_out(self.runtime, AGENT, "slack.com")
        self.assertEqual(len(json.loads((self.root / AGENT / sign_in.SITES_FILE).read_text())), 1)

    def test_sign_out_waits_for_the_window(self):
        sign_in._active[AGENT] = "slack.com"
        with self.assertRaises(DomainError):
            sign_in.sign_out(self.runtime, AGENT, "slack.com")


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

    def test_only_ordubashy_can_call_it(self):
        with mock.patch.object(sign_in, "start") as start, self.assertRaises(DomainError) as caught:
            sign_in.agent_call(self.runtime, "worker", {"action": "open", "site": "slack.com"})
        self.assertEqual(caught.exception.code, "forbidden")
        start.assert_not_called()

    def test_open_targets_its_own_browser_and_guides_the_owner(self):
        with mock.patch.object(sign_in, "start") as start:
            result = sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com"})
        start.assert_called_once_with(self.runtime, "ordubashy", "slack.com")
        self.assertEqual(result["opened"], "slack.com")
        self.assertIn("Never ask for their password", result["next"])

    def test_open_can_target_a_worker(self):
        with mock.patch.object(sign_in, "start") as start:
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com", "agent_id": "worker"})
        self.assertEqual(start.call_args.args[1], "worker")

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
        with mock.patch.object(sign_in, "accounts", return_value={"agents": []}) as accounts:
            self.assertEqual(sign_in.agent_call(self.runtime, "ordubashy", {"action": "status"}), {"agents": []})
        accounts.assert_called_once()
        with mock.patch.object(sign_in, "sign_out") as out:
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "sign_out", "site": "slack.com"})
        out.assert_called_once_with(self.runtime, "ordubashy", "slack.com")


class SessionCookieTest(unittest.TestCase):
    def test_only_session_cookies_are_copied_and_they_last(self):
        cookies = [{"name": "s", "value": "1", "domain": "a.b", "path": "/", "secure": True, "httpOnly": True, "session": True, "size": 9},
                   {"name": "p", "value": "2", "domain": "a.b", "path": "/", "session": False, "expires": 5}]
        self.assertEqual(sign_in.lasting_copies(cookies, 1000),
                         [{"name": "s", "value": "1", "domain": "a.b", "path": "/", "secure": True, "httpOnly": True,
                           "expires": 1000 + sign_in.SESSION_COOKIE_LIFETIME}])

    def test_finish_gives_the_agent_the_window_session(self):
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
            sign_in._finish(runtime, "owner", AGENT, "a.b", SimpleNamespace(), 1)
        self.assertEqual(order[0][0], 20400)
        self.assertEqual(order[0][1][0][1]["cookies"][0]["name"], "s")
        runtime.browser_manager.manage.assert_called_once()


class CommandTest(unittest.TestCase):
    def test_window_and_headless_read_the_same_profile(self):
        headless = supervisor.chrome_command(META)
        window = supervisor.visible_chrome_command(META, "https://slack.com", 55555)
        self.assertIn("--headless=new", headless)
        self.assertNotIn("--headless=new", window)
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
