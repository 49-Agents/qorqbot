import asyncio
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.contracts import CoreError, Invocation, utc_now
from qorqut_core.runner.models import ProcessFingerprint
from qorqut_core.runtime import OwnerCandidate, ProcessObservation, SessionMonitor, SessionOwners
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import create_app
from runtime.support import RuntimeCase
from transports.support import Installation


class Monitor(RuntimeCase):
    def test_sequential_periodic_observation_unknown_retention_and_join(self):
        with patch.object(threading.Thread, "start", side_effect=AssertionError("Unexpected thread")), \
                patch("subprocess.Popen", side_effect=AssertionError("Unexpected process")):
            self.restart()
        self.owners = SessionOwners(self.app, observe_process=self.observe)
        candidate = self.candidate()
        self.owners.connect(candidate)
        calls, threads = [], set()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def observe(process):
            calls.append(time.monotonic())
            threads.add(threading.get_ident())
            if len(calls) == 2:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Owned observation barrier timed out")
            raise PermissionError("synthetic inspection failure")

        owners = SessionOwners(self.app, observe_process=observe)
        monitor = SessionMonitor(owners)
        self.assertFalse(monitor.ready)
        self.assertIsNone(monitor._thread.ident)
        monitor.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertEqual(len(calls), 2)
            self.assertGreaterEqual(calls[1] - calls[0], 0.9)
            self.assertEqual(len(threads), 1)
            self.assertTrue(monitor.ready)
            self.assertEqual(self.runtime()["status"], "unknown")
            self.assertEqual(self.rows("session_owners")[0]["state"], "active")
            stopped = threading.Event()
            stopper = threading.Thread(target=lambda: (monitor.stop(), stopped.set()))
            stopper.start()
            try:
                self.assertFalse(stopped.wait(0.05))
            finally:
                release.set()
                stopper.join(5)
            self.assertTrue(stopped.is_set())
            self.assertFalse(monitor._thread.is_alive())
        finally:
            release.set()
            monitor.stop()


class MonitorLifetime(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(0.01)

    def fixture(self):
        fixture = Installation()
        self.addCleanup(fixture.close)
        settings, credentials = read_configuration(fixture.installation_file, fixture.transport_file)
        return fixture, settings, create_app(settings, credentials)

    async def test_uncaught_failure_is_not_ready_and_claim_is_retained(self):
        fixture, settings, web = self.fixture()
        async with web.router.lifespan_context(web):
            await self.wait_until(lambda: web.state.session_monitor.ready and web.state.worker.ready)
            application = web.state.application
            context = Invocation(actor_id=fixture.owner.actor_id)
            host = application.invoke("agents_list", context).data["onbash_agent_id"]
            epoch = application.invoke("config_get", context).data["recovery_epoch"]
            # Synthetic association/observer only; the default /proc observer is
            # never given this artificial PID.
            application.session_owners.observe_process = lambda p: ProcessObservation(state="live", observed_at=utc_now())
            connection = application.session_owners.connect(OwnerCandidate(agent_id=host, recovery_epoch=epoch,
                environment_generation=1, terminal_id=uuid4(), native_session_id=uuid4(),
                provider="codex", cli_version=VERSIONS["codex"], process=ProcessFingerprint(
                    boot_id=uuid4(), pid=123, start_ticks=1, uid=1000, pid_namespace=1)))
            with self.assertLogs("qorqut_core.runtime.monitor", level="ERROR") as logs:
                with patch.object(web.state.application.session_owners, "reconcile",
                                  side_effect=RuntimeError("PRIVATE OBSERVER CONTENT")):
                    await self.wait_until(lambda: not web.state.session_monitor._thread.is_alive())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=fixture.url) as client:
                    self.assertEqual((await client.get("/health/ready")).status_code, 503)
                    self.assertEqual((await client.get("/health/live")).json(), {"live": True})
                self.assertNotIn("PRIVATE", str(logs.output))
                with application.db.read() as tx:
                    self.assertEqual(tx.connection.execute("SELECT state FROM session_owners WHERE owner_id=?",
                                     (str(connection.owner_id),)).fetchone()[0], "active")
        self.assertFalse(web.state.worker._thread.is_alive())
        self.assertFalse(web.state.session_monitor._thread.is_alive())
        reopened = Application.open(settings)
        reopened.close()

    async def test_cancelled_partial_startup_joins_monitor_before_storage_close(self):
        fixture, settings, web = self.fixture()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        start = SessionMonitor.start

        def blocked_start(monitor):
            start(monitor)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Owned startup barrier timed out")

        async def startup():
            async with web.router.lifespan_context(web):
                self.fail("Cancelled startup admitted")

        with patch.object(SessionMonitor, "start", blocked_start):
            task = asyncio.create_task(startup())
            try:
                await self.wait_until(entered.is_set)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                with self.assertRaises(CoreError):
                    Application.open(settings)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(web.state.session_monitor._thread.is_alive())
                self.assertFalse(web.state.worker._thread.is_alive())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        reopened = Application.open(settings)
        reopened.close()
