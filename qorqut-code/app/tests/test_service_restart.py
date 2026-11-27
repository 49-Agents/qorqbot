"""The service asks to be started again: request_restart stops the server and the service exits with EXIT_RESTART."""
from types import SimpleNamespace
import os
import signal
import unittest
from unittest import mock

import uvicorn

from qorqut.bootstrap import extension_change
from qorqut.composition import assembly
from qorqut import extensions
from qorqut.service import main as service_main
from qorqut.service import restart
from qorqut.transport.http import STOPPING


class FakeServer:
    def __init__(self):
        self.exits = []

    def handle_exit(self, sig, frame):
        self.exits.append(sig)


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(restart.attach, None)

    def test_the_code_is_ex_tempfail(self):
        self.assertEqual(restart.EXIT_RESTART, 75)
        self.assertEqual(restart.EXIT_RESTART, os.EX_TEMPFAIL)

    def test_no_server_no_restart(self):
        restart.attach(None)
        self.assertFalse(restart.request_restart("extension installed"))
        self.assertIsNone(restart.requested())

    def test_a_request_stops_the_server_once(self):
        server = FakeServer()
        restart.attach(server)
        self.assertTrue(restart.request_restart("extension installed"))
        self.assertTrue(restart.request_restart("again"))
        self.assertEqual(server.exits, [signal.SIGTERM])
        self.assertEqual(restart.requested(), "extension installed")


class ServiceExitTest(unittest.TestCase):
    """service.main with the store and the app replaced, so only the start and the exit run."""

    def run_service(self, during_run):
        order = []
        self.addCleanup(STOPPING.clear)
        self.addCleanup(restart.attach, None)

        def run(server, *args, **kwargs):
            server.started = True
            during_run()

        def selection(*args, **kwargs):
            order.append("selection")
            return ()

        with mock.patch.object(service_main, "instance_paths_from_arguments", return_value=SimpleNamespace(runtime=None)), \
                mock.patch.object(extension_change, "apply_pending", side_effect=lambda paths: order.append("pending")), \
                mock.patch.object(extensions, "installed_selection", side_effect=selection), \
                mock.patch.object(assembly, "host_assembly"), mock.patch.object(assembly, "open_database"), \
                mock.patch.object(service_main, "assemble"), mock.patch.object(uvicorn.Server, "run", run):
            status = service_main.main(["--workspace", "/nonexistent", "--port", "1"])
        return status, order

    def test_the_service_returns_the_restart_code_when_asked(self):
        status, _ = self.run_service(lambda: restart.request_restart("extension installed"))
        self.assertEqual(status, restart.EXIT_RESTART)

    def test_a_plain_stop_still_returns_zero(self):
        status, _ = self.run_service(lambda: None)
        self.assertEqual(status, 0)

    def test_the_pending_change_comes_before_the_selection(self):
        _, order = self.run_service(lambda: None)
        self.assertEqual(order, ["pending", "selection"])


if __name__ == "__main__":
    unittest.main()
