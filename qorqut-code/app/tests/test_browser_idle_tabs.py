"""Idle tabs: which tabs are unloaded and when, how a session's use is read, and how a command gets its page back."""

import json
import os
from pathlib import Path
import tempfile
import threading
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


class SupervisorSessions(unittest.TestCase):
    """The sweeper runs in the browser's supervisor, whose home directory is the browser's state directory, while the
    session files are written by the application's commands, in the application's home or runtime directory."""

    APPLICATION = [Path("/run/app/agent-browser"), Path("/home/app/.agent-browser")]

    def test_a_supervisor_also_looks_where_the_application_that_started_it_keeps_sessions(self):
        handed = os.pathsep.join(map(str, self.APPLICATION))
        with mock.patch.object(Path, "home", return_value=Path("/state")):
            places = idle_tabs.session_directories({idle_tabs.SESSIONS: handed + os.pathsep + os.pathsep + "relative/path"})
        self.assertEqual(places[-2:], self.APPLICATION)
        self.assertIn(Path("/state/.agent-browser"), places)
        self.assertEqual(len(places), len(set(places)))

    def test_a_use_recorded_by_the_application_is_seen_from_the_supervisor(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder, "application-home/.agent-browser")
            home.mkdir(parents=True)
            (home / "qorqut-a.target").write_text(json.dumps({"targetId": TAB}))
            with mock.patch.object(Path, "home", return_value=Path(folder, "state")), \
                    mock.patch.dict(os.environ, {idle_tabs.SESSIONS: str(home)}):
                self.assertIn(TAB, idle_tabs.recent_uses(__import__("time").time()))       # this computer's own sessions aside

    @unittest.skipUnless(__import__("sys").platform == "linux", "user service units are a Linux feature")
    def test_the_service_unit_is_handed_the_applications_session_directories(self):
        from types import SimpleNamespace
        from qorqut.browser.agent_browsers import SYSTEMD_RUN, UserUnitLauncher
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n", stderr="")
        with mock.patch.object(idle_tabs, "session_directories", return_value=self.APPLICATION):
            UserUnitLauncher(runner=runner, sleep=lambda _seconds: None).start(
                "qorqut-agent-browser-0123456789ab-8fdca155-2f3b-4735-800f-41624c6d26cc.service", Path("/srv/state"))
        argv = next(argv for argv in calls if argv[0] == SYSTEMD_RUN)
        self.assertIn(f"--setenv={idle_tabs.SESSIONS}=/run/app/agent-browser{os.pathsep}/home/app/.agent-browser", argv)

    def test_the_folder_installations_supervisor_is_handed_them_too(self):
        from qorqut.browser import native_launcher
        from qorqut.browser.agent_browsers import unit_name
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder, "8fdca155-2f3b-4735-800f-41624c6d26cc")
            directory.mkdir()
            with mock.patch.object(idle_tabs, "session_directories", return_value=self.APPLICATION), \
                    mock.patch.object(native_launcher.subprocess, "Popen") as popen:
                self.assertTrue(native_launcher.NativeLauncher(folder).start(unit_name(Path(folder), directory.name), directory))
        self.assertEqual(popen.call_args.kwargs["env"][idle_tabs.SESSIONS],
                         f"/run/app/agent-browser{os.pathsep}/home/app/.agent-browser")


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

    def test_one_tab_that_cannot_be_unloaded_does_not_keep_the_others_loaded_and_is_reported(self):
        self.pages.append(page(OTHER, "https://other.example/"))
        self.after(0)

        def unload(port, item):
            if item["id"] == TAB:
                raise TimeoutError("no answer from the tab")
            self.unloaded.append(item["id"])
        self.sweeper._unload = unload
        with self.assertRaises(TimeoutError):                  # raised after the look, for the supervisor's log
            self.after(LIMIT)
        self.assertEqual(self.unloaded, [OTHER])

    def test_a_page_that_refuses_to_leave_is_asked_again_only_after_another_limit(self):
        asked = []
        self.sweeper._unload = lambda port, item: bool(asked.append(self.now))        # False: it has unsaved work
        self.after(0)
        for seconds in (LIMIT, 60, LIMIT - 120, 60):
            self.after(seconds)
        self.assertEqual(asked, [10_000.0 + LIMIT, 10_000.0 + 2 * LIMIT])

    def test_a_closed_tab_is_forgotten(self):
        self.after(0)
        self.pages = []
        self.after(LIMIT)
        self.pages = [page()]
        self.assertEqual(self.after(LIMIT - 1), [])


class WatchedTabs(unittest.TestCase):
    """The owner's live view runs in the application and the sweeper in the browser's supervisor. What both see is a
    file per watched tab in the browser's state directory."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.marks = Path(folder.name, "watched")
        self.now, self.unloaded = 10_000.0, []
        self.sweeper = idle_tabs.Sweeper(20400, self.marks, clock=lambda: self.now, uses=lambda now: {},
                                         list_pages=lambda port: [page(), page(OTHER, "https://other.example/")],
                                         unload=lambda port, item: self.unloaded.append(item["id"]))

    def watched(self, tab, at):
        idle_tabs.note_watch(self.marks, tab)
        os.utime(self.marks / tab, (at, at))

    def after(self, seconds):
        self.now += seconds
        self.sweeper.sweep()
        return self.unloaded

    def test_a_browser_nobody_watched_has_no_marks_and_needs_none(self):
        self.assertEqual(idle_tabs.recent_watches(self.now, self.marks), {})
        self.after(0)
        self.assertEqual(self.after(LIMIT), [TAB, OTHER])

    def test_a_watch_is_one_private_file_named_by_the_tab(self):
        idle_tabs.note_watch(self.marks, TAB)
        idle_tabs.note_watch(self.marks, TAB)
        self.assertEqual([path.name for path in self.marks.iterdir()], [TAB])
        self.assertEqual((self.marks.stat().st_mode & 0o777, (self.marks / TAB).stat().st_mode & 0o777), (0o700, 0o600))

    def test_a_tab_the_owner_watches_is_not_unloaded_however_long_no_agent_used_it(self):
        self.after(0)
        for _ in range(4):
            self.watched(TAB, self.now + LIMIT - 60)             # the view marks it about once a minute while it is open
            self.assertEqual(set(self.after(LIMIT)), {OTHER})

    def test_a_tab_is_unloaded_the_limit_after_the_owner_stopped_watching(self):
        self.after(0)
        self.watched(TAB, self.now)
        self.assertEqual(self.after(LIMIT - 1), [])
        self.assertEqual(self.after(1), [TAB, OTHER])

    def test_a_mark_older_than_the_limit_is_removed(self):
        self.watched(TAB, self.now - LIMIT)
        self.watched(OTHER, self.now - LIMIT + 1)
        self.assertEqual(idle_tabs.recent_watches(self.now, self.marks), {OTHER: self.now - LIMIT + 1})
        self.assertEqual([path.name for path in self.marks.iterdir()], [OTHER])


class WatchLoop(unittest.TestCase):
    def test_a_look_that_fails_is_reported_once_and_tried_again(self):
        stop, looks = threading.Event(), []

        def sweep(self):
            looks.append(1)
            if len(looks) == 3:
                stop.set()
            raise ImportError("No module named 'websockets'")

        with mock.patch.object(idle_tabs, "SWEEP_EVERY", 0), mock.patch.object(idle_tabs.Sweeper, "sweep", sweep), \
                mock.patch("builtins.print") as printed:
            idle_tabs.watch(20400, stop, Path("/nowhere"))
        self.assertEqual(len(looks), 3)
        printed.assert_called_once()
        self.assertIn("No module named 'websockets'", printed.call_args.args[0])


class Scripted:
    """Chrome's side of a tab's connection that sends the given messages in order, whatever was asked."""

    def __init__(self, incoming):
        self.incoming, self.sent, self.closed = list(incoming), [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.incoming:
            raise TimeoutError()
        return json.dumps(self.incoming.pop(0))

    def close(self):
        self.closed = True


class ParkTest(unittest.TestCase):
    ASKED = [{"id": 1, "result": {}}, {"id": 2, "result": {"result": {"objectId": "W"}}}]      # page events on, the window found

    def park(self, incoming):
        self.socket = Scripted(self.ASKED + incoming)
        try:
            return idle_tabs.park(20400, page(), connect=lambda port, tab: self.socket)
        finally:
            self.assertTrue(self.socket.closed)

    def methods(self):
        return [item["method"] for item in self.socket.sent]

    def test_an_unloaded_tab_shows_the_small_page(self):
        self.assertTrue(self.park([{"id": 3, "result": {"listeners": [{"type": "resize"}]}}, {"method": "Page.frameStartedLoading"},
                                   {"id": 4, "result": {"frameId": "F"}}]))
        self.assertEqual(self.methods(), ["Page.enable", "Runtime.evaluate", "DOMDebugger.getEventListeners", "Page.navigate"])
        self.assertEqual(self.socket.sent[2]["params"], {"objectId": "W"})
        self.assertTrue(idle_tabs.is_parked(self.socket.sent[3]["params"]["url"]))

    def test_a_page_that_wants_to_be_asked_before_it_is_left_is_not_asked_and_stays(self):
        self.assertFalse(self.park([{"id": 3, "result": {"listeners": [{"type": "resize"}, {"type": "beforeunload"}]}}]))
        self.assertNotIn("Page.navigate", self.methods())

    def test_a_question_that_still_comes_up_is_answered_stay_so_it_cannot_block_the_page(self):
        # What Chrome 153 sends when a frame inside the page asks; nothing else is attached to answer.
        self.assertFalse(self.park([{"id": 3, "result": {"listeners": []}}, {"method": "Page.javascriptDialogOpening", "params": {"type": "beforeunload"}},
                                    {"id": 4, "result": {"frameId": "F", "errorText": "net::ERR_ABORTED"}}]))
        self.assertEqual(self.socket.sent[-1], {"id": 5, "method": "Page.handleJavaScriptDialog", "params": {"accept": False}})

    def test_a_question_somebody_else_answered_with_leave_is_an_unloaded_tab(self):
        # agent-browser answers every dialog of a browser it is connected to, at once and with "leave".
        self.assertTrue(self.park([{"id": 3, "result": {"listeners": []}}, {"method": "Page.javascriptDialogOpening", "params": {"type": "beforeunload"}},
                                   {"method": "Page.javascriptDialogClosed", "params": {"result": True}},
                                   {"id": 5, "error": {"code": -32602, "message": "No dialog is showing"}}, {"id": 4, "result": {"frameId": "F"}}]))

    def test_a_tab_that_does_not_answer_is_an_error(self):
        with self.assertRaises(TimeoutError):
            self.park([{"id": 3, "result": {"listeners": []}}])


class SupervisorImports(unittest.TestCase):
    def test_listing_a_browsers_pages_needs_nothing_of_the_application(self):
        import sys
        from qorqut.browser import control
        blocked = {name: None for name in ("qorqut.browser.sign_in", "qorqut.browser.agent_browsers", "qorqut.agent_trust")}
        with mock.patch.dict(sys.modules, blocked), \
                mock.patch.object(control, "devtools_json", return_value=[page(), {"type": "iframe"}, "junk"]) as asked:
            self.assertEqual(idle_tabs.pages(20400), [page()])
        asked.assert_called_once_with(20400, "/json/list")


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

    def test_the_owners_view_gets_the_page_back_without_waiting_for_it(self):
        parked = idle_tabs.parked_page("Example", "https://example.com/")
        self.socket = Socket({"Page.getNavigationHistory": self.history(parked), "Page.navigateToHistoryEntry": {"result": {}}})
        idle_tabs.wake(20400, TAB, 0, connect=lambda port, tab: self.socket)
        self.assertEqual(self.socket.methods(), ["Page.getNavigationHistory", "Page.navigateToHistoryEntry"])
        self.assertTrue(self.socket.closed)

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
        woken, self.waits, self.now = [], [], 0.0

        def wake(port, tab, wait):
            woken.append((port, tab))
            self.waits.append(wait)
            self.now += min(wait, 9)                      # each page takes nine seconds to load
        with mock.patch.object(idle_tabs, "note_use", return_value=session_tab) as note, \
                mock.patch.object(idle_tabs, "pages", return_value=pages), mock.patch.object(idle_tabs, "wake", wake):
            idle_tabs.before_command(endpoint, "qorqut-a", pinned, clock=lambda: self.now)
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

    def test_however_many_tabs_come_back_the_command_waits_no_longer_than_for_one(self):
        tabs = [self.parked(f"{number:032X}") for number in range(5)]
        self.assertEqual(len(self.run_before(tabs, None, False)), 5)                   # all are asked to load again
        self.assertEqual(self.waits, [idle_tabs.WAKE_WAIT, idle_tabs.WAKE_WAIT - 9, 0, 0, 0])
        self.assertEqual(self.now, idle_tabs.WAKE_WAIT)

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
