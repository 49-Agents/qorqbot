"""The tab-reference command: it names the tab the caller's session is bound to, for the owner to be shown in Chat.

Chrome is a stand-in: the tabs it lists are scripted. The session's bound tab is read from a real ``<session>.target`` file."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import browser
from qorqut.browser import idle_tabs, live, sign_in, tab_reference
from qorqut.browser.access import BrowserAccess
from qorqut.native_chat import references

AGENT, WORKER = "4f6c1a2e-8d35-4b97-a1c0-2e9d7f3b5a64", "8fdca155-2f3b-4735-800f-41624c6d26cc"
TAB, OTHER = "A1B2C3D4E5F60718293A4B5C6D7E8F90", "0F9E8D7C6B5A49382716051423324150"
ENDPOINT = "http://127.0.0.1:20450"
SHOW = tab_reference.SHOW


def listed(*ids):
    return [{"id": tab, "type": "page", "title": "Page", "url": "https://shop.example/"} for tab in ids]


class CommandTestCase(unittest.TestCase):
    """A workspace, a session directory ``agent-browser`` would have written, and a browser that lists scripted tabs."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.workspace, self.sessions = self.root / "workspace", self.root / "sessions"
        self.sessions.mkdir()
        environment = mock.patch.dict(os.environ, {idle_tabs.SESSIONS: str(self.sessions), "XDG_RUNTIME_DIR": str(self.root / "none")})
        environment.start()
        self.addCleanup(environment.stop)
        self.target = (ENDPOINT, Path("/runtime/run-agent-browser"))

    def bind(self, tab, *, instance=f"configured:{AGENT}", endpoint=ENDPOINT):
        session = browser.session_name(self.workspace, endpoint, instance)
        (self.sessions / f"{session}.target").write_text(json.dumps({"targetId": tab}))
        return session

    def run_command(self, tabs, *, instance=f"configured:{AGENT}", endpoint=ENDPOINT):
        with mock.patch.object(sign_in, "_get_json", return_value=tabs) as get:
            self.listing = get
            code, out, errors = tab_reference.runner()(self.workspace, ["tab-reference"], target=(endpoint, self.target[1]),
                                                       instance=instance, capture=True, pinned=True)
        self.assertEqual((errors, out.count("\n"), out.endswith("\n")), ("", 1, True))        # one JSON line
        return code, json.loads(out)


class CommandTest(CommandTestCase):
    def test_the_caller_gets_the_reference_of_the_tab_its_session_is_bound_to(self):
        self.bind(TAB)
        code, reply = self.run_command([*listed(OTHER, TAB), {"id": "FRAME", "type": "iframe"}])
        self.assertEqual((code, reply), (0, {"reference": {"type": "tab", "id": f"{AGENT}:{TAB}"}, "title": "Page",
                                             "url": "https://shop.example/", "next": SHOW}))
        self.listing.assert_called_with(20450, "/json/list")

    def test_the_reply_says_which_page_the_reference_is_and_the_reference_stays_identity_only(self):
        self.bind(TAB)
        code, reply = self.run_command([{"id": OTHER, "type": "page", "title": "PayPal", "url": "https://paypal.example/"},
                                        {"id": TAB, "type": "page", "title": "Mercury sign in", "url": "https://mercury.example/login"}])
        self.assertEqual((reply["title"], reply["url"]), ("Mercury sign in", "https://mercury.example/login"))
        self.assertEqual(set(reply["reference"]), {"type", "id"})

    def test_a_browser_that_answers_once_and_then_fails_still_gives_the_reference_without_a_label(self):
        self.bind(TAB)
        with mock.patch.object(sign_in, "_get_json", side_effect=[listed(TAB), OSError("gone")]):
            code, out, _ = tab_reference.runner()(self.workspace, ["tab-reference"], target=self.target, instance=f"configured:{AGENT}")
        reply = json.loads(out)
        self.assertEqual((code, reply["reference"], "title" in reply, "url" in reply), (0, {"type": "tab", "id": f"{AGENT}:{TAB}"}, False, False))

    def test_the_reference_is_exactly_what_request_user_input_takes(self):
        self.bind(TAB)
        reference = self.run_command(listed(TAB))[1]["reference"]
        self.assertEqual(references.normalize("Please look.", [reference])["references"],
                         [{"type": "tab", "id": f"{AGENT}:{TAB}", "project_id": None}])

    def test_each_session_gets_its_own_tab(self):
        self.bind(OTHER, instance=f"configured:{WORKER}")
        self.bind(TAB)
        tabs = listed(TAB, OTHER)
        self.assertEqual(self.run_command(tabs)[1]["reference"]["id"], f"{AGENT}:{TAB}")
        self.assertEqual(self.run_command(tabs, instance=f"configured:{WORKER}")[1]["reference"]["id"], f"{WORKER}:{OTHER}")

    def test_in_a_worker_s_browser_the_reference_names_the_worker_not_the_caller(self):
        """Ordubashy works in a worker's tabs: the owner's live view lists that worker's tabs, not Ordubashy's."""
        self.bind(TAB, instance=f"configured:{WORKER}")
        self.assertEqual(self.run_command(listed(TAB), instance=f"configured:{WORKER}")[1]["reference"]["id"], f"{WORKER}:{TAB}")

    def test_the_session_counts_as_having_used_its_tab_because_the_owner_may_take_long_to_answer(self):
        session = self.bind(TAB)
        path = self.sessions / f"{session}.target"
        os.utime(path, (1_000_000, 1_000_000))
        self.run_command(listed(TAB))
        self.assertGreater(path.stat().st_mtime, 1_000_000)

    def test_a_session_without_a_tab_is_told_to_open_the_page_first(self):
        code, reply = self.run_command(listed(TAB))                 # no <session>.target at all
        self.assertEqual((code, reply), (1, {"reason": tab_reference.NO_TAB}))
        self.assertIn("Open the page first", reply["reason"])
        self.assertNotIn("reference", reply)

    def test_a_bound_tab_that_has_been_closed_or_is_no_page_is_no_tab(self):
        self.bind(TAB)
        for tabs in ([], listed(OTHER), [{"id": TAB, "type": "iframe"}], [{"id": TAB.lower(), "type": "page"}]):
            code, reply = self.run_command(tabs)
            self.assertEqual((code, reply), (1, {"reason": tab_reference.NO_TAB}), tabs)

    def test_a_bound_target_file_that_cannot_be_read_is_no_tab(self):
        session = self.bind(TAB)
        for text in ("", "not json", "[]", '{"targetId": 7}'):
            (self.sessions / f"{session}.target").write_text(text)
            self.assertEqual(self.run_command(listed(TAB))[0], 1, text)

    def test_a_browser_that_does_not_answer_is_a_browser_error_like_any_commands_and_nothing_is_named(self):
        self.bind(TAB)
        for failure in (OSError("refused"), ValueError("not json")):
            with mock.patch.object(sign_in, "_get_json", side_effect=failure), self.assertRaises(browser.BrowserError):
                tab_reference.runner()(self.workspace, ["tab-reference"], target=self.target, instance=f"configured:{AGENT}")

    def test_a_browser_that_is_not_on_this_computer_is_refused_before_anything_is_touched(self):
        for endpoint in ("https://browser.example:9222", "http://10.0.0.5:9222", "not an address"):
            code, reply = self.run_command(listed(TAB), endpoint=endpoint)
            self.assertEqual((code, reply), (1, {"reason": tab_reference.NOT_LOCAL}), endpoint)
            self.listing.assert_not_called()

    def test_a_managed_desktop_belongs_to_no_agent_so_its_tabs_cannot_be_named(self):
        for instance in ("desktop:ordubashy-browser", ""):
            code, reply = self.run_command(listed(TAB), instance=instance)
            self.assertEqual((code, reply), (1, {"reason": tab_reference.NOT_AN_AGENT_BROWSER}), instance)
            self.listing.assert_not_called()

    def test_it_touches_nothing_in_the_workspace_and_takes_no_command_lock(self):
        self.bind(TAB)
        self.run_command(listed(TAB))
        self.assertFalse(self.workspace.exists())


class IsCommandTest(unittest.TestCase):
    def test_the_command_takes_no_arguments(self):
        self.assertTrue(tab_reference.is_command(["tab-reference"]))
        self.assertFalse(tab_reference.is_command(["click", "tab-reference"]))
        self.assertFalse(tab_reference.is_command(["tab-list"]))
        self.assertFalse(tab_reference.is_command([]))
        for command in (["tab-reference", TAB], ["tab-reference", "--tab", TAB]):
            with self.assertRaises(DomainError) as raised:
                tab_reference.is_command(command)
            self.assertEqual(raised.exception.code, "validation_error")
            self.assertNotIn(TAB, raised.exception.message)

    def test_it_is_not_one_of_the_agent_browser_commands(self):
        self.assertNotIn(tab_reference.COMMAND, browser.COMMANDS)
        with self.assertRaises(browser.BrowserError):
            browser.validate_arguments(["tab-reference"])

    def test_a_reference_is_made_only_of_what_the_tool_takes(self):
        self.assertEqual(tab_reference.reference(AGENT, TAB), {"type": "tab", "id": f"{AGENT}:{TAB}"})
        for agent, tab in ((AGENT, TAB.lower()), (AGENT, TAB[:-1]), ("agent-1", TAB), (None, TAB), ("", TAB), (AGENT.upper(), TAB)):
            self.assertIsNone(tab_reference.reference(agent, tab), (agent, tab))

    def test_the_advice_is_a_browser_action_signoff_by_ordubashy_and_still_says_to_check_the_page(self):
        for text in (tab_reference.SHOW, tab_reference.SHOW_FIELD):
            self.assertIn("browser_action", text)
            self.assertIn("signoffs_input_create", text)
            self.assertIn("attention queue", text)
            self.assertIn("any other agent gives the reference to Ordubashy", text)
            self.assertIn("title and url say which page it is", text)
            self.assertNotIn("request_user_input", text)          # a Chat card is only a pointer now, never the way to ask

    def test_the_agent_is_named_by_the_target_not_by_the_caller(self):
        self.assertEqual(tab_reference.browser_agent(f"configured:{WORKER}"), WORKER)
        for instance in ("desktop:ordubashy-browser", "", None):
            self.assertIsNone(tab_reference.browser_agent(instance))


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.access = BrowserAccess.__new__(BrowserAccess)
        self.access.runtime = SimpleNamespace()
        self.actor = SimpleNamespace(principal=SimpleNamespace(id=AGENT))

    def test_it_is_run_by_the_server_and_every_other_command_by_agent_browser(self):
        with mock.patch.object(tab_reference, "runner", return_value="tab-reference-runner") as runner:
            self.assertEqual(self.access._runner(self.actor, ["tab-reference"]), "tab-reference-runner")
        runner.assert_called_once_with()
        self.assertIs(self.access._runner(self.actor, ["click", "e3"]), browser.run)

    def test_arguments_are_refused_before_anything_runs(self):
        with self.assertRaises(DomainError) as raised:
            self.access._runner(self.actor, ["tab-reference", "e3"])
        self.assertEqual(raised.exception.code, "validation_error")

    def test_a_native_command_is_answered_in_the_shape_of_any_browser_command_under_the_use_lock(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.access.runtime = SimpleNamespace(paths=SimpleNamespace(runtime=Path(folder.name).resolve()))
        target = {"cdp_endpoint": ENDPOINT, "runtime_dir": "/runtime", "instance": f"configured:{AGENT}", "shared": True}
        with mock.patch.object(BrowserAccess, "_worker_target", return_value=target), mock.patch.object(BrowserAccess, "_managed_use") as use, \
                mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/run-agent-browser")), \
                mock.patch.object(browser, "run") as agent_browser, \
                mock.patch.object(idle_tabs, "note_use", return_value=TAB), mock.patch.object(live, "tabs", return_value=listed(TAB)):
            result = self.access._native_command(self.actor, {"command": ["tab-reference"], "action_id": "a-1"},
                                                 f"configured:{AGENT}", "b0123456789ab")
        self.assertEqual((result["exit_code"], json.loads(result["stdout"]), result["action"]),
                         (0, {"reference": {"type": "tab", "id": f"{AGENT}:{TAB}"}, "title": "Page", "url": "https://shop.example/", "next": SHOW},
                          {"action_id": "a-1", "state": "succeeded"}))
        use.assert_called_once_with(f"configured:{AGENT}")
        agent_browser.assert_not_called()

    def test_it_is_a_failed_command_when_the_session_has_no_tab(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.access.runtime = SimpleNamespace(paths=SimpleNamespace(runtime=Path(folder.name).resolve()))
        target = {"cdp_endpoint": ENDPOINT, "runtime_dir": "/runtime", "instance": f"configured:{AGENT}", "shared": False}
        with mock.patch.object(BrowserAccess, "_worker_target", return_value=target), mock.patch.object(BrowserAccess, "_managed_use"), \
                mock.patch.object(browser, "runtime_executable", return_value=Path("/runtime/run-agent-browser")), \
                mock.patch.object(idle_tabs, "note_use", return_value=None), mock.patch.object(live, "tabs", return_value=listed(TAB)):
            result = self.access._native_command(self.actor, {"command": ["tab-reference"], "action_id": "a-2"},
                                                 f"configured:{AGENT}", "b0123456789ab")
        self.assertEqual((result["exit_code"], json.loads(result["stdout"]), result["action"]["state"]),
                         (1, {"reason": tab_reference.NO_TAB}, "failed"))


if __name__ == "__main__":
    unittest.main()
