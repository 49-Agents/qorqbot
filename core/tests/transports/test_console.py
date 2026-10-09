"""Local identity/static assets and explicit worker lifetime acceptance."""

import asyncio
import os
import threading
import time
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.storage.worker import JobWorker
from qorqut_core.transports.configuration import MAX_INPUT_BYTES, InputError, read_configuration
from qorqut_core.transports.server import create_app
from transports.support import Installation
from transports.test_transports import session, structured


class BrowserHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_local_identity_mutation_sdk_restart_and_real_workspace(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start()
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(base_url=f.url, trust_env=False))
            origin = {"Origin": f.url}
            self.assertEqual((await client.get("/")).status_code, 404)  # Headless.
            identity = await client.get("/api/v1/session")
            self.assertEqual(identity.json(), {"actor_id": str(f.owner.actor_id), "api_version": 1})
            self.assertNotIn("set-cookie", identity.headers)
            self.assertEqual(dict(client.cookies), {})
            self.assertEqual((await client.head("/api/v1/session")).status_code, 200)
            for method in ("POST", "DELETE"):
                self.assertEqual((await client.request(method, "/api/v1/session")).status_code, 404)
            self.assertEqual((await client.get("/api/v1/schema")).status_code, 200)
            changed = await client.post("/api/v1/operations/config_update", json=f.update(), headers=origin)
            self.assertEqual(changed.status_code, 200)
            sdk = await session(stack, f, client=client)
            self.assertEqual(structured(await sdk.call_tool("config_update", f.update())), changed.json())
            self.assertEqual(f.post("config_update", f.update()).json(), changed.json())

            # Real existing workspace handler, driven only by the headless server.
            created = await client.post("/api/v1/operations/agents_create", headers=origin, json={
                "idempotency_key": "browser-create", "display_name": "Synthetic local agent",
                "cli": f.defaults.cli.model_dump(mode="json"), "persona": "Preserved synthetic persona"})
            self.assertEqual(created.status_code, 200)
            saved = created.json()
            self.assertEqual(saved["outcome"], "accepted")
            deadline = time.monotonic() + 5
            while True:
                lookup = (await client.post("/api/v1/operations/operations_get", headers=origin,
                    json={"operation_id": saved["operation_id"]})).json()["data"]
                job = lookup["jobs"][0]
                if job["state"] == "waiting":
                    break
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.03)
            self.assertEqual(job["wait_reason"], "dependency_unavailable")
            self.assertEqual(lookup["original_result"], saved)
            agent_id = saved["data"]["agent"]["id"]
            self.assertEqual((f.settings.data_root / "agents" / agent_id / "workspace/persona.md").read_text(),
                             "Preserved synthetic persona")
            view = f.post("agents_get", {"agent_id": agent_id}).json()["data"]
            self.assertEqual(view["lifecycle"], "provisioning")
            self.assertIsNotNone(view["workspace_initialized_at"])

        f.stop()
        f.start()
        async with httpx.AsyncClient(base_url=f.url, trust_env=False) as client:
            self.assertEqual((await client.get("/api/v1/session")).json(), identity.json())

    async def test_origin_identity_legacy_headers_and_bounds(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start()
        async with httpx.AsyncClient(base_url=f.url, trust_env=False) as client:
            for headers, status in (({"Origin": "null"}, 403), ({"Origin": "https://evil.invalid"}, 403),
                    ([("Origin", f.url), ("Origin", f.url)], 403), ({"Host": "evil.invalid"}, 421),
                    ({"X-Qorqut-Actor": "PRIVATE-invalid"}, 400), ({"X-Qorqut-Actor": ""}, 400),
                    ({"X-Qorqut-Actor": f"{f.owner.actor_id},{f.agent.actor_id}"}, 400),
                    ([("X-Qorqut-Actor", str(f.owner.actor_id))] * 2, 400)):
                refused = await client.post("/api/v1/operations/config_update", json=f.update(), headers=headers)
                self.assertEqual(refused.status_code, status)
                self.assertNotIn("operation_id", refused.json())
                self.assertNotIn("PRIVATE", refused.text)
            identity = {"actor_id": str(f.owner.actor_id), "api_version": 1}
            for headers in ({}, {"Authorization": "Bearer obsolete", "Cookie": "qorqut_session=obsolete"},
                    [("Authorization", "one"), ("Authorization", "two"), ("Cookie", "broken"), ("Cookie", "duplicate")]):
                response = await client.get("/api/v1/session", headers=headers)
                self.assertEqual(response.json(), identity)
                self.assertNotIn("set-cookie", response.headers)
                self.assertEqual((await client.post("/api/v1/operations/config_get", json={}, headers=headers)).json(), f.initial)
            selected = await client.get("/api/v1/session", headers=f.headers(f.agent))
            self.assertEqual(selected.json(), {"actor_id": str(f.agent.actor_id), "api_version": 1})
            unknown = {"X-Qorqut-Actor": str(uuid4())}
            self.assertEqual((await client.get("/api/v1/session", headers=unknown)).status_code, 404)
            refusal = await client.post("/api/v1/operations/config_update", json=f.update(), headers=unknown)
            self.assertEqual(refusal.status_code, 404)
            self.assertIsNone(refusal.json()["operation_id"])
            for content in (b"x" * (MAX_INPUT_BYTES + 1), large_chunks()):
                refused = await client.post("/api/v1/operations/config_update", content=content,
                                           headers={"Content-Type": "application/json"})
                self.assertEqual(refused.status_code, 413)
            self.assertEqual((await client.get("/api/v1/session?token=PRIVATE")).status_code, 400)
            self.assertEqual(f.post("events_list").json()["data"]["items"], f.initial_events)
        self.assertNotIn("PRIVATE", f.log_file.read_text())


async def large_chunks():
    for _ in range(17):
        yield b"x" * 65536


class StaticHTTP(unittest.TestCase):
    def test_optional_assets_headers_no_fallback_or_symlink_escape(self):
        f = Installation()
        self.addCleanup(f.close)
        root = f.root / "ui"
        (root / "assets/nested").mkdir(parents=True)
        (root / "index.html").write_text('<!doctype html><script src="/assets/app.js"></script>')
        (root / "assets/app.js").write_text('document.title = "Synthetic UI";')
        (root / "assets/nested/style.css").write_text("body { color: white; }")
        (root / "private.txt").write_text("PRIVATE ROOT FILE")
        (root / "assets/linked.txt").symlink_to(root / "private.txt")
        (root / "assets/directory").symlink_to(f.root, target_is_directory=True)
        os.mkfifo(root / "assets/pipe")
        f.start(ui_root=root)
        with httpx.Client(base_url=f.url, trust_env=False) as client:
            for path in ("/", "/assets/app.js", "/assets/nested/style.css"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertIn("frame-ancestors 'self'", response.headers["content-security-policy"])
                self.assertIn("script-src 'self'", response.headers["content-security-policy"])
                self.assertIn("script-src 'self';", response.headers["content-security-policy"])
                self.assertIn("style-src 'self' 'unsafe-inline';", response.headers["content-security-policy"])
                self.assertEqual(response.headers["content-security-policy"].count("unsafe-"), 1)
                self.assertNotIn("access-control-allow-origin", response.headers)
            self.assertEqual(client.head("/").content, b"")
            for path in ("/private.txt", "/assets/%2e%2e/private.txt", "/assets/linked.txt",
                         "/assets/directory/credentials.json", "/assets/pipe", "/assets/nested/",
                         "/assets/%2e%2e%5cprivate.txt", "/assets/%00", "/assets//app.js",
                         "/health/unknown", "/mcp/", "/api/v1/unknown", "/api/v2/schema"):
                response = client.get(path, headers=f.headers())
                self.assertIn(response.status_code, (400, 404, 405))
                self.assertNotIn("PRIVATE ROOT FILE", response.text)
                self.assertNotIn("doctype", response.text)
            self.assertEqual(client.get("/?token=secret").status_code, 400)
            self.assertEqual(client.get("/assets").status_code, 404)
            # Replacing a previously good asset with a symlink cannot bypass the walk.
            (root / "assets/app.js").unlink()
            (root / "assets/app.js").symlink_to(f.installation_file)
            self.assertEqual(client.get("/assets/app.js").status_code, 404)
        f.stop()


class ConsoleLifetime(unittest.IsolatedAsyncioTestCase):
    async def test_bad_ui_and_worker_start_failure_release_lock(self):
        f = Installation()
        self.addCleanup(f.close)
        settings = read_configuration(f.installation_file)
        root = f.root / "ui"
        root.mkdir()
        (root / "index.html").write_text("synthetic")
        link = f.root / "linked"
        link.symlink_to(root, target_is_directory=True)
        (root / "bad").symlink_to(f.root, target_is_directory=True)
        for path in (Path("relative"), f.root / "missing", f.root, link, root / "bad/../ui"):
            web = create_app(settings, path)
            with self.assertRaises(InputError):
                async with web.router.lifespan_context(web):
                    self.fail("Invalid static build admitted")
        (root / "index.html").unlink()
        (root / "index.html").symlink_to(f.installation_file)
        with self.assertRaises(InputError):
            async with create_app(settings, root).router.lifespan_context(None):
                self.fail("Symlinked index admitted")
        web = create_app(settings)
        with patch.object(JobWorker, "start", side_effect=RuntimeError("synthetic startup failure")):
            with self.assertRaises(ExceptionGroup) as raised:
                async with web.router.lifespan_context(web):
                    self.fail("Worker startup failure admitted")
            self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)
        reopened = Application.open(settings)
        reopened.close()

    async def test_cancelled_startup_after_worker_launch_joins_before_lock_release(self):
        f = Installation()
        self.addCleanup(f.close)
        settings = read_configuration(f.installation_file)
        web = create_app(settings)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        start_worker = JobWorker.start

        def blocked_start(worker):
            start_worker(worker)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Synthetic startup barrier timed out")

        async def startup():
            async with web.router.lifespan_context(web):
                self.fail("Cancelled startup began serving")

        with patch.object(JobWorker, "start", blocked_start):
            task = asyncio.create_task(startup())
            try:
                deadline = time.monotonic() + 3
                while not entered.is_set():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                with self.assertRaises(CoreError):
                    Application.open(settings)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(web.state.worker._thread.is_alive())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        reopened = Application.open(settings)
        reopened.close()

    async def test_cancelled_shutdown_drains_worker_effect_and_closes_admission(self):
        f = Installation()
        self.addCleanup(f.close)
        settings = read_configuration(f.installation_file)
        web = create_app(settings)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        opened = Application.open
        calls = []

        def blocked_open(settings):
            app = opened(settings)
            original = app.jobs.run_next
            def run_next(**kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Synthetic effect barrier timed out")
                result = original(**kwargs)
                calls.append(result)
                return result
            app.jobs.run_next = run_next
            return app

        started, finish = asyncio.Event(), asyncio.Event()
        async def lifetime():
            async with web.router.lifespan_context(web):
                started.set()
                await finish.wait()

        with patch.object(Application, "open", side_effect=blocked_open):
            task = asyncio.create_task(lifetime())
            try:
                await asyncio.wait_for(started.wait(), 3)
                deadline = time.monotonic() + 3
                while not entered.is_set():
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.01)
                finish.set()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as client:
                    while (await client.get("/health/live")).status_code != 503:
                        self.assertLess(time.monotonic(), deadline)
                        await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                with self.assertRaises(CoreError):
                    opened(settings)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(len(calls), 1)
                self.assertFalse(web.state.worker.ready)
                self.assertFalse(web.state.worker._thread.is_alive())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        reopened = opened(settings)
        reopened.close()

    async def test_worker_failure_and_lost_lock_are_not_ready(self):
        f = Installation()
        self.addCleanup(f.close)
        settings = read_configuration(f.installation_file)
        web = create_app(settings)
        async with web.router.lifespan_context(web):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as client:
                deadline = time.monotonic() + 4
                while (await client.get("/health/ready")).status_code != 200:
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.02)
                with self.assertLogs("qorqut_core.storage.worker", level="WARNING") as logs:
                    with patch.object(web.state.application.jobs, "run_next", side_effect=RuntimeError("SECRET HANDLER PAYLOAD")):
                        while web.state.worker.ready:
                            self.assertLess(time.monotonic(), deadline)
                            await asyncio.sleep(0.02)
                        self.assertTrue(web.state.worker._thread.is_alive())
                        self.assertEqual((await client.get("/health/ready")).json(), {"ready": False})
                    while not web.state.worker.ready:
                        self.assertLess(time.monotonic(), deadline)
                        await asyncio.sleep(0.02)
                    self.assertNotIn("SECRET", str(logs.output))
                await asyncio.to_thread(web.state.application.close)
                self.assertEqual((await client.get("/health/ready")).status_code, 503)
                while web.state.worker._thread.is_alive():
                    self.assertLess(time.monotonic(), deadline + 2)
                    await asyncio.sleep(0.02)
                self.assertFalse(web.state.worker.ready)
                self.assertEqual((await client.get("/health/live")).json(), {"live": True})
