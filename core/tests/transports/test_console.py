"""Local HTTP sessions/static assets and explicit worker lifetime acceptance."""

import asyncio
import hashlib
import os
import threading
import time
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.storage.worker import JobWorker
from qorqut_core.transports.configuration import MAX_INPUT_BYTES, InputError, read_configuration
from qorqut_core.transports.server import create_app
from qorqut_core.transports.sessions import COOKIE_NAME, MAX_SESSIONS, SESSION_SECONDS, Sessions
from transports.support import Installation
from transports.test_transports import session, structured


class BrowserHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_login_mutation_sdk_rotation_logout_restart_and_real_workspace(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start()
        cookies = []
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(httpx.AsyncClient(base_url=f.url, trust_env=False))
            origin = {"Origin": f.url}
            self.assertEqual((await client.get("/")).status_code, 404)  # Headless.
            self.assertEqual((await client.get("/api/v1/session")).status_code, 401)
            self.assertEqual((await client.head("/api/v1/session")).status_code, 401)
            signed = await client.post("/api/v1/session", json={"token": f.tokens[f.owner.actor_id]}, headers=origin)
            self.assertEqual(signed.json(), {"authenticated": True, "actor_id": str(f.owner.actor_id), "api_version": 1})
            header = signed.headers["set-cookie"]
            for flag in ("HttpOnly", "SameSite=strict", "Path=/", "Max-Age=28800"):
                self.assertIn(flag, header)
            self.assertNotIn("Secure", header)
            first_cookie = client.cookies.get(COOKIE_NAME)
            cookies.append(first_cookie)
            self.assertEqual((await client.get("/api/v1/session")).json(), signed.json())
            self.assertEqual((await client.head("/api/v1/session")).status_code, 200)
            self.assertEqual((await client.get("/api/v1/schema")).status_code, 200)
            changed = await client.post("/api/v1/operations/config_update", json=f.update(), headers=origin)
            self.assertEqual(changed.status_code, 200)
            sdk = await session(stack, f)
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

            rotated = await client.post("/api/v1/session", json={"token": f.tokens[f.agent.actor_id]}, headers=origin)
            self.assertEqual(rotated.json()["actor_id"], str(f.agent.actor_id))
            second_cookie = client.cookies.get(COOKIE_NAME)
            cookies.append(second_cookie)
            self.assertNotEqual(first_cookie, second_cookie)
            self.assertEqual((await client.get("/api/v1/session", headers={"Cookie": f"{COOKIE_NAME}={first_cookie}"})).status_code, 401)
            logout = await client.delete("/api/v1/session", headers=origin)
            self.assertEqual(logout.json(), {"authenticated": False})
            self.assertIn("Max-Age=0", logout.headers["set-cookie"])
            self.assertEqual((await client.delete("/api/v1/session", headers=origin)).status_code, 200)
            self.assertEqual((await client.get("/api/v1/session", headers={"Cookie": f"{COOKIE_NAME}={second_cookie}"})).status_code, 401)
            await client.post("/api/v1/session", json={"token": f.tokens[f.owner.actor_id]}, headers=origin)
            cookies.append(client.cookies.get(COOKIE_NAME))
        f.stop()
        f.start()
        async with httpx.AsyncClient(base_url=f.url, trust_env=False) as client:
            self.assertEqual((await client.get("/api/v1/session", headers={"Cookie": f"{COOKIE_NAME}={cookies[-1]}"})).status_code, 401)
        for secret in [*cookies, *f.tokens.values()]:
            self.assertNotIn(secret, f.log_file.read_text())

    async def test_origin_credential_ambiguity_bounds_and_sanitized_refusal(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start()
        async with httpx.AsyncClient(base_url=f.url, trust_env=False) as client:
            token = f.tokens[f.owner.actor_id]
            for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.invalid"},
                            [("Origin", f.url), ("Origin", f.url)], {"Origin": f.url, "Host": "evil.invalid"}):
                refused = await client.post("/api/v1/session", json={"token": token}, headers=headers)
                self.assertIn(refused.status_code, (403, 421))
                self.assertNotIn(token, refused.text)
            signed = await client.post("/api/v1/session", json={"token": token}, headers={"Origin": f.url})
            cookie = client.cookies.get(COOKIE_NAME)
            self.assertEqual(signed.status_code, 200)
            cookie_header = f"{COOKIE_NAME}={cookie}"
            for headers in ({}, {"Origin": "https://evil.invalid"},
                {"Origin": f.url, **f.headers()},
                [("Origin", f.url), ("Cookie", cookie_header), ("Cookie", cookie_header)],
                {"Origin": f.url, "Cookie": cookie_header + "; " + cookie_header},
                {"Origin": f.url, "Cookie": cookie_header + '; qorqut_session="ambiguous"'},
                {"Origin": f.url, "Cookie": "qorqut_session=invalid"},
                {"Origin": f.url, "X-Qorqut-Actor": str(f.agent.actor_id)}):
                refused = await client.post("/api/v1/operations/config_update", json=f.update(), headers=headers)
                self.assertIn(refused.status_code, (400, 401, 403))
                self.assertNotIn("operation_id", refused.json())
                self.assertNotIn(cookie, refused.text)
                self.assertNotIn(token, refused.text)
            self.assertEqual((await client.get("/api/v1/session", headers={"Origin": "null"})).status_code, 403)
            self.assertEqual((await client.get("/api/v1/session", headers=f.headers())).status_code, 401)
            self.assertEqual((await client.delete("/api/v1/session")).status_code, 403)
            self.assertEqual((await client.post("/mcp", json={})).status_code, 401)
            # MCP still uses the exact supplied bearer even with a browser cookie.
            async with AsyncExitStack() as stack:
                bearer = await stack.enter_async_context(httpx.AsyncClient(headers={**f.headers(), "Cookie": cookie_header}, trust_env=False))
                sdk = await session(stack, f, client=bearer)
                self.assertEqual(structured(await sdk.call_tool("config_get", {})), f.initial)
            origin = {"Origin": f.url, "Content-Type": "application/json"}
            for content in (b'[]', b'{"token":123}', b'{"token":{"private":"SECRET"}}',
                            json_token_duplicate(token), b'{"token":"SECRET","extra":1}'):
                refused = await client.post("/api/v1/session", content=content, headers=origin)
                self.assertEqual(refused.status_code, 400)
                self.assertNotIn("SECRET", refused.text)
                self.assertNotIn(token, refused.text)
            for content in (b"x" * (MAX_INPUT_BYTES + 1), large_chunks()):
                refused = await client.post("/api/v1/session", content=content, headers=origin)
                self.assertEqual(refused.status_code, 413)
            refused = await client.post("/api/v1/session", json={"token": "SECRET"}, headers=origin)
            self.assertEqual(refused.status_code, 401)
            self.assertNotIn("SECRET", refused.text)
            self.assertEqual((await client.get("/api/v1/session?token=" + token)).status_code, 400)
            self.assertEqual((await client.get("/api/v1/session")).status_code, 200)  # Failed sign-in did not revoke.
            self.assertEqual(f.post("events_list").json()["data"]["items"], f.initial_events)
        for secret in (token, cookie, "SECRET"):
            self.assertNotIn(secret, f.log_file.read_text())

    async def test_absolute_expiry_over_real_http(self):
        f = Installation()
        self.addCleanup(f.close)
        f.start(session_seconds=0.5)  # Test-only clock acceleration, production cookie contract unchanged.
        async with httpx.AsyncClient(base_url=f.url, trust_env=False) as client:
            self.assertEqual((await client.post("/api/v1/session", headers={"Origin": f.url},
                json={"token": f.tokens[f.owner.actor_id]})).status_code, 200)
            deadline = time.monotonic() + 3
            while (await client.get("/api/v1/session")).status_code == 200:
                self.assertLess(time.monotonic(), deadline)
                await asyncio.sleep(0.03)
            self.assertEqual((await client.post("/api/v1/operations/config_get", json={}, headers={"Origin": f.url})).status_code, 401)


def json_token_duplicate(token):
    return ('{"token":"' + token + '","token":"' + token + '"}').encode()


async def large_chunks():
    for _ in range(17):
        yield b"x" * 65536


class SessionCapacity(unittest.IsolatedAsyncioTestCase):
    async def test_digest_only_bounded_capacity_rotation_and_expiry(self):
        f = Installation()
        self.addCleanup(f.close)
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        store = Sessions()
        with patch("qorqut_core.transports.sessions.monotonic", return_value=10) as clock:
            tokens = [store.issue(f.owner.actor_id, None) for _ in range(MAX_SESSIONS)]
            self.assertEqual(set(store._entries), {hashlib.sha256(token.encode()).digest() for token in tokens})
            self.assertNotIn(tokens[0], repr(store))
            with patch("qorqut_core.transports.server.Sessions", return_value=store):
                web = create_app(settings, credentials)
            async with web.router.lifespan_context(web):
                tokens = [store.issue(f.owner.actor_id, None) for _ in range(MAX_SESSIONS)]
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url,
                                             headers={"Origin": f.url}) as client:
                    login = {"token": f.tokens[f.agent.actor_id]}
                    self.assertEqual((await client.post("/api/v1/session", json=login)).status_code, 503)
                    self.assertEqual(store.actor(tokens[-1]), f.owner.actor_id)
                    rotated = await client.post("/api/v1/session", json=login,
                        headers={"Cookie": f"{COOKIE_NAME}={tokens[0]}"})
                    self.assertEqual(rotated.status_code, 200)
                    self.assertIsNone(store.actor(tokens[0]))
                    clock.return_value = 10 + SESSION_SECONDS
                    self.assertEqual((await client.get("/api/v1/session")).status_code, 401)
                    self.assertEqual((await client.post("/api/v1/session", json=login)).status_code, 200)
                    self.assertEqual(len(store._entries), 1)
            self.assertEqual(store._entries, {})


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
                self.assertNotIn("unsafe-", response.headers["content-security-policy"])
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
            (root / "assets/app.js").symlink_to(f.credentials_file)
            self.assertEqual(client.get("/assets/app.js").status_code, 404)
        f.stop()
        for secret in f.tokens.values():
            self.assertNotIn(secret, f.log_file.read_text())


class ConsoleLifetime(unittest.IsolatedAsyncioTestCase):
    async def test_bad_ui_and_worker_start_failure_release_lock(self):
        f = Installation()
        self.addCleanup(f.close)
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        root = f.root / "ui"
        root.mkdir()
        (root / "index.html").write_text("synthetic")
        link = f.root / "linked"
        link.symlink_to(root, target_is_directory=True)
        (root / "bad").symlink_to(f.root, target_is_directory=True)
        for path in (Path("relative"), f.root / "missing", f.root, link, root / "bad/../ui"):
            web = create_app(settings, credentials, path)
            with self.assertRaises(InputError):
                async with web.router.lifespan_context(web):
                    self.fail("Invalid static build admitted")
        (root / "index.html").unlink()
        (root / "index.html").symlink_to(f.credentials_file)
        with self.assertRaises(InputError):
            async with create_app(settings, credentials, root).router.lifespan_context(None):
                self.fail("Symlinked index admitted")
        web = create_app(settings, credentials)
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
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        web = create_app(settings, credentials)
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
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        web = create_app(settings, credentials)
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
        settings, credentials = read_configuration(f.installation_file, f.transport_file)
        web = create_app(settings, credentials)
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
