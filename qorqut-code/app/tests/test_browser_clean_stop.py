"""Stopping an agent's headless Chrome keeps what it had not written yet: Chrome is asked to close itself first."""

import json
import subprocess
import threading
import unittest
from unittest import mock

from qorqut.browser import agent_supervisor as supervisor


class Chrome:
    """Stands in for the launched Chrome process: it runs until it is closed or terminated."""

    pid = 4242

    def __init__(self, closes=True):
        self.closes, self.running, self.terminated, self.asked = closes, True, False, []

    def poll(self):
        return None if self.running else 0

    def close(self, port, timeout=2, pid=None):
        """What ``request_close`` does to a Chrome that answers."""
        self.asked.append((port, pid))
        self.running = not self.closes
        return True

    def wait(self, timeout=None):
        if self.running:
            raise subprocess.TimeoutExpired("chrome", timeout)
        return 0

    def terminate(self):
        self.terminated, self.running = True, False


class StopChildTest(unittest.TestCase):
    def stop(self, chrome, port, request=None):
        with mock.patch.object(supervisor, "request_close", request or chrome.close), \
                mock.patch.object(supervisor.os, "killpg", side_effect=ProcessLookupError) as group:
            supervisor.stop_child(chrome, port)
        self.assertEqual(group.call_args.args[0], chrome.pid)                       # only its own process group is cleared

    def test_chrome_is_asked_to_close_itself_and_is_not_terminated_when_it_does(self):
        chrome = Chrome()
        self.stop(chrome, 20400)
        self.assertEqual(chrome.asked, [(20400, chrome.pid)])      # asked on behalf of its own process
        self.assertFalse(chrome.terminated)

    def test_a_chrome_that_does_not_answer_is_terminated(self):
        chrome = Chrome()
        self.stop(chrome, 20400, request=lambda _port, pid=None: False)
        self.assertTrue(chrome.terminated)

    def test_a_chrome_that_keeps_running_after_the_request_is_terminated(self):
        chrome = Chrome(closes=False)
        self.stop(chrome, 20400)
        self.assertEqual(chrome.asked, [(20400, chrome.pid)])      # asked on behalf of its own process
        self.assertTrue(chrome.terminated)

    def test_without_a_port_chrome_is_terminated_as_before(self):
        chrome = Chrome()
        self.stop(chrome, None)
        self.assertEqual(chrome.asked, [])
        self.assertTrue(chrome.terminated)


class RequestCloseTest(unittest.TestCase):
    """Against a DevTools endpoint on a real loopback socket: the version document over HTTP, then the browser's own socket."""

    def serve(self, version):
        from websockets.sync.server import serve
        received = []

        def http(connection, request):
            if request.path == "/json/version":
                return connection.respond(200, json.dumps(version(server.socket.getsockname()[1])))

        def devtools(connection):
            received.append((connection.request.path, json.loads(connection.recv())))
            connection.send(json.dumps({"id": 1, "result": {}}))

        server = serve(devtools, "127.0.0.1", 0, process_request=http)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        return server.socket.getsockname()[1], received

    def test_it_sends_browser_close_to_the_browser_socket_chrome_names(self):
        port, received = self.serve(lambda port: {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc-123"})
        self.assertTrue(supervisor.request_close(port))
        self.assertEqual(received, [("/devtools/browser/abc-123", {"id": 1, "method": "Browser.close"})])

    def test_it_connects_only_to_the_given_loopback_port_whatever_address_the_answer_names(self):
        port, received = self.serve(lambda _port: {"webSocketDebuggerUrl": "ws://203.0.113.9:1/devtools/browser/abc-123"})
        self.assertTrue(supervisor.request_close(port))
        self.assertEqual(received[0][0], "/devtools/browser/abc-123")

    def test_a_listener_the_launched_chrome_does_not_own_is_never_asked_to_close(self):
        port, received = self.serve(lambda port: {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc-123"})
        with mock.patch.object(supervisor, "owns_listener", return_value=False) as owner:
            self.assertFalse(supervisor.request_close(port, pid=4242))
        owner.assert_called_once_with(4242, port, "127.0.0.1")
        self.assertEqual(received, [])                                    # another program held the port: it got nothing
        with mock.patch.object(supervisor, "owns_listener", return_value=True):
            self.assertTrue(supervisor.request_close(port, pid=4242))
        self.assertEqual(len(received), 1)

    def test_a_process_that_cannot_be_inspected_is_not_taken_for_the_owner(self):
        port, received = self.serve(lambda port: {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc-123"})
        with mock.patch.object(supervisor, "owns_listener", side_effect=FileNotFoundError("gone")):
            self.assertFalse(supervisor.request_close(port, pid=4242))
        self.assertEqual(received, [])

    def test_nothing_listening_is_reported_not_raised(self):
        with supervisor.socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        self.assertFalse(supervisor.request_close(port, timeout=0.5))


if __name__ == "__main__":
    unittest.main()
