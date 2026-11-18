"""Cookie banners: how the refusal script reaches every document of a browser, and that the watcher outlives failures."""

import json
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
        with mock.patch.object(cookie_banners, "RETRY_AFTER", 0), mock.patch.object(cookie_banners, "script", return_value="SCRIPT"):
            cookie_banners.watch(20400, stop, connect=connect)
        self.assertEqual([item["params"]["filter"] for item in first.sent], [[{"type": "tab"}], [{"type": "page"}]])
        self.assertEqual([item.get("sessionId") for item in second.sent], [None, "T2"])
        self.assertTrue(first.closed and second.closed)

    def test_a_missing_script_leaves_the_browser_alone(self):
        with mock.patch.object(cookie_banners, "script", side_effect=OSError("missing")), mock.patch("sys.stderr"):
            cookie_banners.watch(20400, threading.Event(), connect=lambda port: self.fail("connected"))


class SupervisorTest(unittest.TestCase):
    def test_a_ready_browser_gets_both_watchers(self):
        started, stop = [], threading.Event()
        done = threading.Barrier(3)

        def watcher(name):
            def watch(port, stopping):
                started.append((name, port, stopping is stop))
                done.wait(timeout=5)
            return watch

        with mock.patch.object(cookie_banners, "watch", watcher("banners")), mock.patch.object(idle_tabs, "watch", watcher("idle")):
            agent_supervisor.attend(20400, stop)
            done.wait(timeout=5)
        self.assertEqual(sorted(started), [("banners", 20400, True), ("idle", 20400, True)])


if __name__ == "__main__":
    unittest.main()
