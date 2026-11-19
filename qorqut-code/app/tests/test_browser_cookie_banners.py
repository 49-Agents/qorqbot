"""Cookie banners: how the refusal script reaches every document of a browser, and that the watcher outlives failures."""

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

from qorqut.browser import agent_supervisor, cookie_banners, idle_tabs


class Socket:
    def __init__(self, incoming=()):
        self.incoming, self.sent, self.closed = list(incoming), [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        if not self.incoming:
            raise TimeoutError()
        message = self.incoming.pop(0)
        if isinstance(message, Exception):
            raise message
        return json.dumps(message)

    def close(self):
        self.closed = True


def attached(kind, below, session=None):
    message = {"method": "Target.attachedToTarget", "params": {"sessionId": below, "targetInfo": {"type": kind}}}
    return message if session is None else {**message, "sessionId": session}


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.socket = Socket()
        self.installer = cookie_banners.Installer(self.socket, "SCRIPT")

    def handle(self, message):
        before = len(self.socket.sent)
        self.installer.handle(message)
        return self.socket.sent[before:]

    def test_it_starts_by_following_the_browsers_tabs(self):
        self.installer.start()
        [sent] = self.socket.sent
        self.assertEqual((sent["method"], sent["params"]["filter"], sent["params"]["flatten"], "sessionId" in sent),
                         ("Target.setAutoAttach", [{"type": "tab"}], True, False))
        self.assertFalse(sent["params"]["waitForDebuggerOnStart"])

    def test_a_tab_is_followed_to_its_page(self):
        [sent] = self.handle(attached("tab", "T"))
        self.assertEqual((sent["method"], sent["params"]["filter"], sent["sessionId"]), ("Target.setAutoAttach", [{"type": "page"}], "T"))

    def test_a_page_or_frame_reports_its_documents_and_its_frames_of_other_sites(self):
        for kind in ("page", "iframe"):
            sent = self.handle(attached(kind, "P", "T"))
            self.assertEqual([(item["method"], item["sessionId"]) for item in sent],
                             [("Page.enable", "P"), ("Page.getFrameTree", "P"), ("Target.setAutoAttach", "P")])
            self.assertEqual(sent[2]["params"]["filter"], [{"type": "iframe"}])

    def test_other_targets_are_left_alone(self):
        self.assertEqual(self.handle(attached("service_worker", "W")), [])

    def test_each_loaded_document_gets_the_script_in_a_world_of_its_own(self):
        [world] = self.handle({"method": "Page.frameNavigated", "sessionId": "P", "params": {"frame": {"id": "F1"}}})
        self.assertEqual((world["method"], world["params"], world["sessionId"]),
                         ("Page.createIsolatedWorld", {"frameId": "F1", "worldName": cookie_banners.WORLD}, "P"))
        [run] = self.handle({"id": world["id"], "sessionId": "P", "result": {"executionContextId": 42}})
        self.assertEqual((run["method"], run["params"], run["sessionId"]),
                         ("Runtime.evaluate", {"expression": "SCRIPT", "contextId": 42}, "P"))
        self.assertEqual(self.handle({"id": world["id"], "sessionId": "P", "result": {"executionContextId": 42}}), [])

    def test_documents_open_before_the_connection_get_it_too(self):
        tree = self.handle(attached("page", "P", "T"))[1]
        sent = self.handle({"id": tree["id"], "sessionId": "P", "result": {"frameTree": {
            "frame": {"id": "MAIN"}, "childFrames": [{"frame": {"id": "CHILD"}, "childFrames": [{"frame": {"id": "INNER"}}]}]}}})
        self.assertEqual(sorted(item["params"]["frameId"] for item in sent), ["CHILD", "INNER", "MAIN"])
        self.assertEqual({item["method"] for item in sent}, {"Page.createIsolatedWorld"})

    def test_a_frame_that_is_gone_is_skipped(self):
        [world] = self.handle({"method": "Page.frameNavigated", "sessionId": "P", "params": {"frame": {"id": "F1"}}})
        self.assertEqual(self.handle({"id": world["id"], "sessionId": "P", "error": {"message": "No frame"}}), [])


class ScriptTest(unittest.TestCase):
    def test_the_script_ships_with_the_module(self):
        source = cookie_banners.script()
        self.assertIn("qorqutCookieBanner", source)
        self.assertTrue(source.rstrip().endswith("})();"))


class WatchTest(unittest.TestCase):
    def test_it_installs_reconnects_after_a_failure_and_ends_when_stopped(self):
        stop = threading.Event()
        first = Socket([attached("tab", "T"), ConnectionError("Chrome went away")])
        second = Socket([attached("tab", "T2")])
        sockets, original = [OSError("not listening yet"), first, second], second.recv

        def connect(port):
            item = sockets.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def last(timeout=None):
            if not second.incoming:
                stop.set()
            return original(timeout)

        second.recv = last
        with mock.patch.object(cookie_banners, "RETRY_AFTER", 0), mock.patch.object(cookie_banners, "script", return_value="SCRIPT"), \
                mock.patch("builtins.print"):
            cookie_banners.watch(20400, stop, connect=connect)
        self.assertEqual([item["params"]["filter"] for item in first.sent], [[{"type": "tab"}], [{"type": "page"}]])
        self.assertEqual([item.get("sessionId") for item in second.sent], [None, "T2"])
        self.assertTrue(first.closed and second.closed)

    def test_a_connection_that_fails_is_reported_once_however_often_it_is_tried(self):
        stop, tries = threading.Event(), []

        def connect(port):
            tries.append(port)
            if len(tries) == 3:
                stop.set()
            raise TypeError("connect() got an unexpected keyword argument 'proxy'")

        with mock.patch.object(cookie_banners, "RETRY_AFTER", 0), mock.patch.object(cookie_banners, "script", return_value="SCRIPT"), \
                mock.patch("builtins.print") as printed:
            cookie_banners.watch(20400, stop, connect=connect)
        self.assertEqual(len(tries), 3)
        printed.assert_called_once()
        self.assertIn("unexpected keyword argument 'proxy'", printed.call_args.args[0])

    def test_the_connection_is_made_to_the_address_chrome_names_without_anything_of_the_application(self):
        from qorqut.browser import control
        blocked = {name: None for name in ("qorqut.browser.sign_in", "qorqut.browser.agent_browsers", "qorqut.agent_trust")}
        with mock.patch.dict(sys.modules, blocked), mock.patch("websockets.sync.client.connect") as connect, \
                mock.patch.object(control, "devtools_json", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:20400/devtools/browser/x"}) as asked:
            self.assertIs(cookie_banners._connect(20400), connect.return_value)
        asked.assert_called_once_with(20400, "/json/version")
        self.assertEqual(connect.call_args.args, ("ws://127.0.0.1:20400/devtools/browser/x",))

    def test_a_missing_script_leaves_the_browser_alone(self):
        with mock.patch.object(cookie_banners, "script", side_effect=OSError("missing")), mock.patch("sys.stderr"):
            cookie_banners.watch(20400, threading.Event(), connect=lambda port: self.fail("connected"))


class SupervisorTest(unittest.TestCase):
    STATE = Path("/runtime/agent-browsers/browser")

    def attend(self, started=None):
        """Run ``attend`` with stand-in watchers; returns what it wrote to the supervisor's log."""
        stop, started = threading.Event(), [] if started is None else started
        done = threading.Barrier(3)

        def watcher(name):
            def watch(*args):
                started.append((name, *args[:1], args[1] is stop, *args[2:]))
                done.wait(timeout=5)
            return watch

        with mock.patch.object(cookie_banners, "watch", watcher("banners")), mock.patch.object(idle_tabs, "watch", watcher("idle")), \
                mock.patch("builtins.print") as printed:
            agent_supervisor.attend(self.STATE, 20400, stop)
            if threading.active_count() > 1:
                try:
                    done.wait(timeout=1)
                except threading.BrokenBarrierError:
                    pass
        self.assertTrue(all(call.kwargs.get("file") is sys.stderr for call in printed.call_args_list))
        return [call.args[0] for call in printed.call_args_list]

    def test_a_ready_browser_gets_both_watchers_and_its_log_says_so(self):
        started = []
        log = self.attend(started)
        self.assertEqual(sorted(started), [("banners", 20400, True), ("idle", 20400, True, self.STATE / idle_tabs.WATCHED)])
        self.assertEqual(len(log), 1)
        self.assertRegex(log[0], "cookie banners.*idle tabs")

    def test_a_python_without_what_the_watchers_need_is_reported_once_and_the_browser_runs_on(self):
        started = []
        with mock.patch.dict(sys.modules, {"websockets.sync.client": None}):          # importing it now fails
            log = self.attend(started)
        self.assertEqual(started, [])
        self.assertEqual(len(log), 1)
        self.assertRegex(log[0], "not refused.*not unloaded")
        self.assertIn("websockets", log[0])


if __name__ == "__main__":
    unittest.main()
