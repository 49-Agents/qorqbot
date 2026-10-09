import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.adapters.manual_hooks import NativeProgram
from qorqut_core.config import LocalTerminalSettings
from qorqut_core.contracts import CoreError, Invocation, utc_now
from qorqut_core.runner.models import ProcessFingerprint
from qorqut_core.runtime import OwnerCandidate, ProcessObservation, SessionMonitor, SessionOwners
from qorqut_core.runtime.native_collector import NativeCollector
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import create_app
from runtime.support import RuntimeCase
from adapters.test_native_records import codex, raw
from messaging.support import ChatCase
from transports.support import Installation


class Monitor(RuntimeCase):
    def test_final_collection_order_failure_cleanup_and_producer_survival(self):
        # Only an owned Python child and a synthetic pinned native file are used.
        self.app.close()
        local = LocalTerminalSettings(runtime_root=Path("/tmp") / uuid4().hex,
            python_executable=Path(sys.executable), tmux_executable=self.root / "unused-tmux",
            bash_executable=Path("/usr/bin/bash"), path=os.defpath, native_programs=(
                NativeProgram(provider="codex", cli_version=VERSIONS["codex"], executable=self.root / "unused-codex"),))
        self.settings = self.settings.model_copy(update={"data_root": self.root / "configured", "local_terminal": local})
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        ChatCase.pump(self)
        self.host = next(row for row in self.rows("agents") if row["kind"] == "host")
        base = self.settings.data_root / "agents" / self.host["id"]
        home = base / "runtime/home"
        home.mkdir(mode=0o700)
        child, process = self.child()
        candidate = self.candidate(process)
        connection = self.app.session_owners.connect(candidate)
        path = home / f".codex/sessions/2026/10/09/rollout-2026-10-09T12-00-00-{candidate.native_session_id}.jsonl"
        path.parent.mkdir(parents=True)
        initial = raw({"type": "session_meta", "payload": {"id": str(candidate.native_session_id),
            "source": "cli", "cli_version": VERSIONS["codex"]}})
        path.write_bytes(initial)
        collector = NativeCollector(self.app)
        calls, threads, final_states = [], [], []
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def record(action):
            calls.append(action)
            threads.append(threading.get_ident())

        def reconcile():
            record("owners")
            self.app.session_owners.reconcile()

        def collect():
            record("collect")
            collector.tick()
            if calls.count("collect") == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Owned collection barrier timed out")

        def close():
            record("close_collect")
            final_states.extend(collector._capture.list_states())
            collector.close()

        monitor = SessionMonitor(SimpleNamespace(db=self.app.db, reconcile=reconcile),
            native_setup=SimpleNamespace(tick=lambda: record("setup"), close=lambda: record("close_setup")),
            native_collector=SimpleNamespace(tick=collect, close=close))
        with self.subTest("settle iteration, reconcile, collect once, close on the same thread"), \
                patch.object(self.app.session_owners, "release", side_effect=AssertionError("shutdown release")), \
                patch("subprocess.Popen", side_effect=AssertionError("shutdown launch")):
            monitor.start()
            stopper = None
            try:
                self.assertTrue(entered.wait(4))
                suffix_record = codex("fsynced after the regular poll", role="AgentMessage")
                suffix_record["payload"]["thread_id"] = str(candidate.native_session_id)
                suffix = raw(suffix_record)
                with path.open("ab") as stream:
                    stream.write(suffix)
                    stream.flush()
                    os.fsync(stream.fileno())
                stopper = threading.Thread(target=lambda: (monitor.stop(), stopped.set()))
                stopper.start()
                self.assertFalse(stopped.wait(0.05))
                self.assertTrue(monitor._stop.is_set())
                release.set()
                stopper.join(5)
                self.assertTrue(stopped.is_set())
            finally:
                release.set()
                monitor.stop()
                if stopper is not None:
                    stopper.join(5)
            self.assertEqual(calls, ["owners", "setup", "collect", "owners", "collect", "close_setup", "close_collect"])
            self.assertEqual(set(threads), {monitor._thread.ident})
            self.assertFalse(monitor.ready)
            self.assertFalse(monitor._healthy.is_set())
            self.assertIsNone(child.poll())
            self.assertEqual(self.rows("session_owners")[0]["state"], "active")
            self.assertEqual(self.rows("session_owners")[0]["owner_id"], str(connection.owner_id))
            self.assertEqual(len(final_states), 1)
            self.assertIsNone(final_states[0].checkpoint)
            self.assertEqual(final_states[0].host_archived_offset, len(initial + suffix))
            chunks = sorted(self.rows("archive_chunks"), key=lambda r: r["offset"])
            self.assertEqual(b"".join((self.settings.data_root / r["object_path"]).read_bytes() for r in chunks), initial + suffix)
            self.assertEqual(self.rows("native_parse_gaps"), [])
            pending = [r for r in self.rows("jobs") if r["kind"] == "conversation_publish"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["state"], "queued")
            self.restart()
            ChatCase.pump(self)
            published = list((base / "transcripts").glob("*.jsonl"))
            self.assertEqual(len(published), 1)
            self.assertEqual([json.loads(line)["text"] for line in published[0].read_bytes().splitlines()],
                             ["fsynced after the regular poll"])
            self.assertIsNone(child.poll())

        for failure in ("final_owners", "final_collect", "unexpected"):
            with self.subTest(failure=failure):
                calls, threads = [], []

                def failed_reconcile():
                    record("owners")
                    if failure == "final_owners" and calls.count("owners") == 2:
                        raise OSError("PRIVATE owner error")

                def failed_collect():
                    record("collect")
                    monitor._stop.set()
                    if failure == "unexpected" or calls.count("collect") == 2:
                        raise RuntimeError("PRIVATE collection error")

                def failed_close():
                    record("close_setup")
                    raise OSError("PRIVATE close error")

                monitor = SessionMonitor(SimpleNamespace(db=self.app.db, reconcile=failed_reconcile),
                    native_setup=SimpleNamespace(tick=lambda: record("setup"), close=failed_close),
                    native_collector=SimpleNamespace(tick=failed_collect, close=lambda: record("close_collect")))
                with self.assertLogs("qorqut_core.runtime.monitor", level="ERROR") as logs:
                    monitor.start()
                    monitor._thread.join(5)
                    monitor.stop()
                expected = ["owners", "setup", "collect"]
                if failure != "unexpected":
                    expected += ["owners"] + ([] if failure == "final_owners" else ["collect"])
                self.assertEqual(calls, expected + ["close_setup", "close_collect"])
                self.assertEqual(set(threads), {monitor._thread.ident})
                self.assertFalse(monitor._thread.is_alive())
                self.assertFalse(monitor.ready)
                self.assertFalse(monitor._healthy.is_set())
                self.assertNotIn("PRIVATE", str(logs.output))
                diagnostic = "session_monitor_stopped" if failure == "unexpected" else "session_monitor_final_collection_failed"
                self.assertEqual(logs.output, [f"ERROR:qorqut_core.runtime.monitor:{diagnostic}",
                                              "ERROR:qorqut_core.runtime.monitor:native_observation_close_failed"])
                self.assertIsNone(child.poll())

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
        settings = read_configuration(fixture.installation_file)
        return fixture, settings, create_app(settings)

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
