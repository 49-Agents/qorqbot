"""The owner signs in to websites on the Shared browser's profile; Qorqbot records sites, never passwords."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap.preparation import connector_gate_guard
from qorqut.browser import setup, sign_in
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


class FlowTest(unittest.TestCase):
    def setUp(self):
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

    def test_accounts_report_the_real_session_state(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}, {"site": "notion.so"}]))
        with mock.patch.object(sign_in, "_cookies", return_value=[{"domain": ".slack.com"}]):
            accounts = sign_in.accounts(self.runtime)
        self.assertEqual(set(accounts), {"sites"})            # one browser: no list of agents, no window
        self.assertEqual([set(item) for item in accounts["sites"]], [{"site", "added_at", "session_saved"}] * 2)
        self.assertEqual([item["session_saved"] for item in accounts["sites"]], [True, False])

    def test_accounts_say_unknown_when_the_browser_is_off(self):
        (self.state / sign_in.SITES_FILE).write_text(json.dumps([{"site": "slack.com"}]))
        with mock.patch.object(sign_in, "_cookies", side_effect=OSError("down")):
            self.assertIsNone(sign_in.accounts(self.runtime)["sites"][0]["session_saved"])

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
        with mock.patch.object(sign_in, "open_in_live_view") as live_view, self.assertRaises(DomainError) as caught:
            sign_in.agent_call(self.runtime, "worker", {"action": "open", "site": "slack.com"})
        self.assertEqual(caught.exception.code, "forbidden")
        live_view.assert_not_called()

    def test_open_always_puts_the_page_in_the_live_view_whatever_the_computer(self):
        for platform, environment in (("darwin", {}), ("linux", {"DISPLAY": ":0"}), ("linux", {"WAYLAND_DISPLAY": "wayland-0"}),
                                      ("linux", {}), ("win32", {})):
            with self.subTest(platform=platform, environment=environment), \
                    mock.patch.object(sys, "platform", platform), mock.patch.dict(os.environ, environment), \
                    mock.patch.object(sign_in, "open_in_live_view", return_value={"where": "live_view"}) as live_view:
                result = sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "slack.com"})
            live_view.assert_called_once_with(self.runtime, "ordubashy", "slack.com", "https://slack.com")    # the caller names the tab
            self.assertEqual(result, {"where": "live_view"})

    def test_a_screen_does_not_stop_or_pause_the_browser(self):
        manage = mock.Mock()
        runtime = SimpleNamespace(database=self.runtime.database, browser_manager=SimpleNamespace(manage=manage))
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch("qorqut.browser.live.shared_port", return_value=20400), \
                mock.patch.object(sign_in, "_open_tab", return_value="tab") as open_tab, \
                mock.patch.object(sign_in, "_record_site"), mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(setup, "targets", return_value=("owner", [{"agent_id": "ordubashy", "name": "Ordubashy"}])):
            result = sign_in.agent_call(runtime, "ordubashy", {"action": "open", "site": "slack.com"})
        open_tab.assert_called_once_with(20400, "https://slack.com")
        manage.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(result["where"], "live_view")
        self.assertIn("sign in there", result["next"])
        self.assertNotIn("window", result["next"])

    def test_plain_http_is_refused_except_on_this_computer(self):
        with mock.patch.object(sign_in, "open_in_live_view") as live_view:
            with self.assertRaises(DomainError):
                sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "http://slack.com/login"})
            live_view.assert_not_called()
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "open", "site": "http://127.0.0.1:9000/login"})
            live_view.assert_called_once()

    def test_a_missing_site_is_refused(self):
        with self.assertRaises(DomainError):
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "open"})

    def test_status_and_sign_out(self):
        with mock.patch.object(sign_in, "accounts", return_value={"sites": []}) as accounts:
            self.assertEqual(sign_in.agent_call(self.runtime, "ordubashy", {"action": "status"}), {"sites": []})
        accounts.assert_called_once_with(self.runtime)
        with mock.patch.object(sign_in, "sign_out") as out:
            sign_in.agent_call(self.runtime, "ordubashy", {"action": "sign_out", "site": "slack.com"})
        out.assert_called_once_with(self.runtime, "slack.com")


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
