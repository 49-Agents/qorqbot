"""Idle tabs: which tabs are unloaded and when, how a session's use is read, and how a command gets its page back."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qorqut.bootstrap import browser
from qorqut.browser import idle_tabs

TAB, OTHER = "C3016B049B49B2E2FBC864793BBCF652", "0C1ACCC1097C47AA9794654248616B5C"
LIMIT = idle_tabs.IDLE_LIMIT


def page(tab=TAB, url="https://example.com/", title="Example"):
    return {"id": tab, "type": "page", "url": url, "title": title}


class Socket:
    """Chrome's side of one tab's DevTools connection: answers by method, records what was sent."""

    def __init__(self, answers):
        self.answers, self.sent, self.closed = answers, [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        message = self.sent[-1]
        answer = self.answers[message["method"]]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        return json.dumps({"id": message["id"], **answer})

    def close(self):
        self.closed = True

    def methods(self):
        return [item["method"] for item in self.sent]


class SessionFiles(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.first, self.second = Path(folder.name, "runtime"), Path(folder.name, "home")
        self.first.mkdir()
        self.second.mkdir()

    def bind(self, directory, session, tab, at):
        path = directory / f"{session}.target"
        path.write_text(json.dumps({"targetId": tab, "url": "https://stale.example/", "pinned": True}))
        os.utime(path, (at, at))
        return path

    def test_a_use_is_recorded_on_the_session_file_and_names_its_tab(self):
        path = self.bind(self.first, "qorqut-a", TAB, 1000)
        self.assertEqual(idle_tabs.note_use("qorqut-a", [self.first, self.second]), TAB)
        self.assertGreater(path.stat().st_mtime, 1000)

    def test_a_session_without_a_tab_has_none(self):
        (self.first / "qorqut-a.target").write_text("not json")
        self.assertIsNone(idle_tabs.note_use("qorqut-a", [self.first, self.second]))
        self.assertIsNone(idle_tabs.note_use("qorqut-b", [self.first, self.second]))

    def test_the_newer_of_two_session_files_is_the_session(self):
        self.bind(self.first, "qorqut-a", OTHER, 1000)
        self.bind(self.second, "qorqut-a", TAB, 2000)
        self.assertEqual(idle_tabs.note_use("qorqut-a", [self.first, self.second]), TAB)

    def test_recent_uses_are_the_latest_per_tab_within_the_limit(self):
        self.bind(self.first, "qorqut-a", TAB, 5000)
        self.bind(self.second, "qorqut-b", TAB, 5200)
        self.bind(self.first, "qorqut-c", OTHER, 5300 - LIMIT)
        self.assertEqual(idle_tabs.recent_uses(5300, [self.first, self.second, self.first / "missing"]), {TAB: 5200})


class UnloadedPage(unittest.TestCase):
    def test_an_unloaded_tab_is_recognised_by_its_address(self):
        address = idle_tabs.parked_page("Inbox <3>", "https://mail.example/?a=1&b=2")
        self.assertTrue(idle_tabs.is_parked(address))
        self.assertIn("Idle%3A%20Inbox%20%26lt%3B3%26gt%3B", address)
        for other in ("https://example.com/", "about:blank", "data:text/html,hello", None):
            self.assertFalse(idle_tabs.is_parked(other))


class SweeperTest(unittest.TestCase):
    def setUp(self):
        self.now, self.pages, self.used, self.unloaded = 10_000.0, [page()], {}, []
        self.sweeper = idle_tabs.Sweeper(20400, clock=lambda: self.now, uses=lambda now: self.used,
                                         list_pages=lambda port: self.pages,
                                         unload=lambda port, item: self.unloaded.append(item["id"]))

    def after(self, seconds):
        self.now += seconds
        self.sweeper.sweep()
        return self.unloaded

    def test_a_tab_is_unloaded_once_it_was_idle_for_the_limit(self):
        self.assertEqual(self.after(0), [])
        self.assertEqual(self.after(LIMIT - 1), [])
        self.assertEqual(self.after(1), [TAB])

    def test_a_tab_a_session_used_within_the_limit_stays(self):
        self.after(0)
        self.used = {TAB: self.now + LIMIT - 60}
        self.assertEqual(self.after(LIMIT), [])

    def test_only_the_idle_tab_is_unloaded(self):
        self.pages.append(page(OTHER, "https://other.example/"))
        self.after(0)
        self.used = {OTHER: self.now + LIMIT}
        self.assertEqual(self.after(LIMIT), [TAB])

    def test_a_new_address_starts_the_idle_time_again(self):
        self.after(0)
        self.pages = [page(url="https://example.com/next")]
        self.assertEqual(self.after(LIMIT - 60), [])
        self.assertEqual(self.after(60), [])
        self.assertEqual(self.after(LIMIT - 60), [TAB])

    def test_blank_internal_and_already_unloaded_tabs_are_left(self):
        self.pages = [page(url="about:blank"), page(OTHER, "chrome://version/"),
                      page("A" * 32, idle_tabs.parked_page("Example", "https://example.com/"))]
        self.after(0)
        self.assertEqual(self.after(LIMIT * 3), [])

    def test_a_closed_tab_is_forgotten(self):
        self.after(0)
        self.pages = []
        self.after(LIMIT)
        self.pages = [page()]
        self.assertEqual(self.after(LIMIT - 1), [])


class WakeTest(unittest.TestCase):
    def history(self, current):
        return {"result": {"currentIndex": 1, "entries": [{"id": 7, "url": "https://example.com/"}, {"id": 8, "url": current}]}}

    def wake(self, answers):
        self.socket = Socket(answers)
        idle_tabs.wake(20400, TAB, connect=lambda port, tab: self.socket, sleep=lambda seconds: None)
        self.assertTrue(self.socket.closed)

    def test_waking_goes_one_step_back_and_waits_for_the_page(self):
        parked = idle_tabs.parked_page("Example", "https://example.com/")
        self.wake({"Page.getNavigationHistory": self.history(parked), "Page.navigateToHistoryEntry": {"result": {}},
                   "Runtime.evaluate": [{"result": {"result": {"value": "complete " + parked}}}, {"error": {"message": "gone"}},
                                        {"result": {"result": {"value": "loading https://example.com/"}}},
                                        {"result": {"result": {"value": "complete https://example.com/"}}}]})
        self.assertEqual(self.socket.sent[1]["params"], {"entryId": 7})
        self.assertEqual(self.socket.methods().count("Runtime.evaluate"), 4)

    def test_a_tab_that_is_not_unloaded_is_left_alone(self):
        self.wake({"Page.getNavigationHistory": self.history("https://example.com/next")})
        self.assertEqual(self.socket.methods(), ["Page.getNavigationHistory"])

    def test_waiting_ends_at_the_limit(self):
        parked, clock = idle_tabs.parked_page("Example", "https://example.com/"), iter(range(0, 1000, 5))
        self.socket = Socket({"Page.getNavigationHistory": self.history(parked), "Page.navigateToHistoryEntry": {"result": {}},
                              "Runtime.evaluate": {"result": {"result": {"value": "loading https://example.com/"}}}})
        idle_tabs.wake(20400, TAB, connect=lambda port, tab: self.socket, clock=lambda: next(clock), sleep=lambda seconds: None)
        self.assertEqual(self.socket.methods().count("Runtime.evaluate"), 2)      # asked at 5 and 10 seconds, not at 15


class BeforeCommand(unittest.TestCase):
    def run_before(self, pages, session_tab, pinned, endpoint="http://127.0.0.1:20400"):
        woken = []
        with mock.patch.object(idle_tabs, "note_use", return_value=session_tab) as note, \
                mock.patch.object(idle_tabs, "pages", return_value=pages), \
                mock.patch.object(idle_tabs, "wake", side_effect=lambda port, tab: woken.append((port, tab))):
            idle_tabs.before_command(endpoint, "qorqut-a", pinned)
        self.noted = note.called
        return woken

    def parked(self, tab):
        return page(tab, idle_tabs.parked_page("Example", "https://example.com/"))

    def test_the_sessions_unloaded_tab_is_loaded_again(self):
        self.assertEqual(self.run_before([self.parked(TAB), self.parked(OTHER)], TAB, True), [(20400, TAB)])
        self.assertTrue(self.noted)

    def test_a_loaded_tab_costs_no_devtools_connection(self):
        self.assertEqual(self.run_before([page(), self.parked(OTHER)], TAB, False), [])

    def test_an_unpinned_session_without_a_tab_gets_every_unloaded_tab_back(self):
        self.assertEqual(self.run_before([self.parked(TAB), self.parked(OTHER), page("B" * 32)], None, False),
                         [(20400, TAB), (20400, OTHER)])
        self.assertEqual(self.run_before([self.parked(OTHER)], TAB, False), [(20400, OTHER)])      # its tab was closed

    def test_a_pinned_session_without_a_tab_wakes_nobody_elses(self):
        self.assertEqual(self.run_before([self.parked(TAB)], None, True), [])

    def test_a_target_that_is_not_a_loopback_devtools_port_is_untouched(self):
        for endpoint in ("ws://127.0.0.1:20400", "http://203.0.113.9:9222", "http://[", ""):
            self.assertEqual(self.run_before([self.parked(TAB)], TAB, True, endpoint), [])
            self.assertFalse(self.noted)

    def test_a_failure_never_reaches_the_command(self):
        with mock.patch.object(idle_tabs, "note_use", side_effect=OSError("gone")):
            idle_tabs.before_command("http://127.0.0.1:20400", "qorqut-a", True)
        with mock.patch.object(idle_tabs, "note_use", return_value=TAB), \
                mock.patch.object(idle_tabs, "pages", side_effect=ValueError("not json")):
            idle_tabs.before_command("http://127.0.0.1:20400", "qorqut-a", True)


class CommandPath(unittest.TestCase):
    def test_every_browser_command_first_gets_its_tab_back(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        order = []
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(idle_tabs, "before_command", side_effect=lambda *args: order.append(args)) , \
                mock.patch.object(browser.subprocess, "run", side_effect=lambda command, **_: order.append("runtime") or completed):
            for pinned, args in ((True, ["click", "e3"]), (False, ["observe", "overview"])):
                browser.run(Path(folder.name), args, target=("http://127.0.0.1:20999", Path("/runtime/agent-browser")),
                            capture=True, pinned=pinned)
        (endpoint, session, pinned), runtime = order[0], order[1]
        self.assertEqual((endpoint, pinned, runtime), ("http://127.0.0.1:20999", True, "runtime"))
        self.assertRegex(session, r"qorqut-[0-9a-f]{20}")
        self.assertEqual((order[2][0], order[2][2], order[3]), ("http://127.0.0.1:20999", False, "runtime"))


if __name__ == "__main__":
    unittest.main()
