"""One opt-in real desktop flow. All state/logs remain in a disposable private root."""

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from qorqut_browser.config import DesktopConfig, ServiceConfig
from qorqut_browser.contracts import Binding, BrowserConfig, BrowserError, Identity
from qorqut_browser.desktop import Desktop, initialize, read_state
from qorqut_browser.state import Store, mount_inventory
from qorqut_browser.supervisor import HelperState, check_ready, child_environment, helper_socket, present, verify_process
from test_browser import configuration
from wire_fixture import private_file


class DesktopInitializationTest(unittest.TestCase):
    def test_explicit_init_preserves_state_and_refuses_unclaimed_data(self):
        with tempfile.TemporaryDirectory(prefix="browser-desktop-init-") as temporary:
            config = configuration(Path(temporary))
            config.home.parent.chmod(0o700)
            for path in config.owned_directories:
                path.rmdir()
            initialize(config)
            before = Store(config).status()
            (config.profile / "retained").write_text("synthetic profile")
            initialize(config)
            self.assertEqual(Store(config).status(), before)
            self.assertEqual((config.profile / "retained").read_text(), "synthetic profile")
            self.assertIsNone(read_state(config))
            other = configuration(Path(temporary))
            (other.profile / "unknown").write_text("do not adopt")
            with self.assertRaises(BrowserError):
                initialize(other)
            self.assertFalse(other.database.exists())

    def test_failed_start_keeps_history_and_cleans_its_children(self):
        with tempfile.TemporaryDirectory(prefix="browser-desktop-failure-") as temporary:
            config = configuration(Path(temporary))
            initialize(config)
            service = ServiceConfig(browser=config, actor_id=uuid4(), agent_id=config.binding.identity.agent_id,
                helper_executable=Path(sys.executable), desktop=DesktopConfig(
                    chrome=Path(sys.executable), xvnc=Path(sys.executable), window_manager=Path(sys.executable)))
            desktop = Desktop(service)
            children = []

            def fail_start():
                self.assertEqual(read_state(config).phase, "starting")
                children.append(desktop.launch("display", [sys.executable, "-c", "import time; time.sleep(60)"],
                                               child_environment(config)))
                raise BrowserError("not_ready", "Synthetic startup failure after owned launch")

            with patch.object(desktop, "start_children", fail_start):
                self.assertEqual(desktop.run(), 1)
            self.assertIsNotNone(children[0].process.returncode)
            self.assertEqual(read_state(config).phase, "failed")
            self.assertEqual(read_state(config).processes, {})
            self.assertFalse(config.state_file.exists())
            self.assertTrue(config.profile.is_dir())
            with self.assertRaises(BrowserError) as error:
                Desktop(service).run()
            self.assertEqual(error.exception.code, "target_changed")


@unittest.skipUnless(os.environ.get("QORQUT_REAL_DESKTOP") == "1", "opt-in actual Chrome/TigerVNC flow")
class DesktopFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parent = os.environ.get("QORQUT_DESKTOP_EVIDENCE")
        self.root = Path(tempfile.mkdtemp(prefix="actual-desktop-", dir=parent))
        self.runtime = Path(tempfile.mkdtemp(prefix="qb-", dir="/tmp"))
        print("Desktop evidence: " + str(self.root), flush=True)
        self.desktop = self.service = None
        self.iteration = 0
        listeners = []
        for _ in range(3):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        ports = [s.getsockname()[1] for s in listeners]
        display = next(i for i in range(801, 999)
            if not os.path.lexists(f"/tmp/.X{i}-lock") and not os.path.lexists(f"/tmp/.X11-unix/X{i}"))
        mounts = tuple({max((m for m in mount_inventory() if path.is_relative_to(m.path)),
                           key=lambda m: len(m.path.parts)) for path in (self.root, self.runtime)})
        identity = Identity(agent_id=uuid4(), recovery_epoch=uuid4(), environment_generation=1,
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(), browser_instance=uuid4(), protocol=1)
        browser = BrowserConfig(binding=Binding(identity=identity, control_epoch=0), uid=os.getuid(),
            database=self.runtime / "browser.sqlite3", state_file=self.runtime / "ready.json",
            home=self.root / "home", profile=self.root / "profile", downloads=self.root / "downloads",
            artifacts=self.root / "artifacts", mounts=mounts, cdp_host="127.0.0.1",
            cdp_port=ports[0], vnc_port=ports[1], service_port=ports[2], display=display)
        self.config = ServiceConfig(browser=browser, actor_id=uuid4(), agent_id=identity.agent_id,
            helper_executable=Path(os.environ["QORQUT_TEST_HELPER"]), desktop=DesktopConfig(
                chrome=Path(os.environ.get("QORQUT_TEST_CHROME", "/opt/google/chrome/chrome")),
                xvnc=Path("/usr/bin/Xtigervnc"), window_manager=Path("/usr/bin/openbox")))
        self.config_path = private_file(self.root / "service.json", self.config.model_dump(mode="json"))
        self.url = f"http://127.0.0.1:{browser.service_port}"
        self.cookies = []
        fixture = self

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fixture.cookies.append(self.headers.get("Cookie", ""))
                body = b'''<!doctype html><html><head><title>Disposable browser fixture</title></head>
<body><h1>Disposable browser fixture</h1><label>Name <input id="name"></label>
<button id="save" onclick="document.cookie='qorqut_synthetic=persisted; Max-Age=86400; Path=/';
document.getElementById('result').textContent='Saved '+document.getElementById('name').value">Save</button>
<h2 id="result">Ready</h2></body></html>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.site = ThreadingHTTPServer(("127.0.0.1", 0), Page)
        self.site_thread = threading.Thread(target=self.site.serve_forever, daemon=True)
        self.site_thread.start()
        self.addCleanup(self.site.server_close)
        self.addCleanup(self.site.shutdown)
        self.addAsyncCleanup(self.cleanup_processes)
        for listener in listeners:
            listener.close()

    def cli(self, operation):
        result = subprocess.run([sys.executable, "-m", "qorqut_browser.desktop", operation,
            "--config", str(self.config_path)], capture_output=True, text=True, timeout=70)
        private_file(self.root / f"cli-{self.iteration}-{operation}.log", result.stdout + result.stderr)
        return result

    async def start_desktop(self):
        self.iteration += 1
        with (self.root / f"supervisor-{self.iteration}.log").open("w") as log:
            self.desktop = subprocess.Popen([sys.executable, "-m", "qorqut_browser.desktop", "start",
                "--config", str(self.config_path)], stdout=log, stderr=log)
        for _ in range(350):
            self.assertIsNone(self.desktop.poll(), (self.root / f"supervisor-{self.iteration}.log").read_text())
            saved = read_state(self.config.browser)
            if saved is not None and saved.phase == "running":
                check_ready(self.config.browser)
                return
            if saved is not None and saved.phase == "failed":
                self.fail((self.root / f"supervisor-{self.iteration}.log").read_text())
            await asyncio.sleep(.1)
        self.fail("Desktop did not publish actual readiness")

    async def start_service(self):
        with (self.root / f"service-{self.iteration}-{uuid4().hex}.log").open("w") as log:
            self.service = subprocess.Popen([sys.executable, "-m", "qorqut_browser.server",
                "--config", str(self.config_path)], stdout=log, stderr=log)
        async with httpx.AsyncClient(trust_env=False, timeout=.5) as client:
            for _ in range(100):
                self.assertIsNone(self.service.poll())
                try:
                    response = await client.get(self.url + "/v1/identity")
                    if response.status_code == 200:
                        return response.json()
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)
        self.fail("Service did not listen")

    def stop_service(self):
        if self.service is not None:
            self.service.terminate()
            self.service.wait(15)
            self.service = None

    async def cleanup_processes(self):
        self.stop_service()
        if self.desktop is not None and self.desktop.poll() is None:
            result = await asyncio.to_thread(self.cli, "stop")
            if result.returncode:
                print("Unconfirmed cleanup, evidence retained: " + result.stderr, flush=True)
            else:
                self.desktop.wait(5)
        # Keep the actual configured runtime in place; mirror regular evidence
        # files, never live Unix sockets, into the assigned private evidence root.
        shutil.copytree(self.runtime, self.root / "runtime-evidence", dirs_exist_ok=True,
            ignore=lambda root, names: [name for name in names if Path(root, name).is_socket()])

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(trust_env=False, timeout=60) as client:
            async with streamable_http_client(self.url + "/mcp", http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def reconcile(self, mode="automation"):
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            current = (await client.get(self.url + "/v1/identity")).json()["control"]["binding"]
            desired = {"identity": self.config.browser.binding.identity.model_dump(mode="json"),
                       "control_epoch": current["control_epoch"] + 1}
            response = await client.post(self.url + "/v1/browser/control", json={
                "command_id": str(uuid4()), "expected": current, "desired": desired,
                "mode": mode, "evidence": "Explicit disposable functional acceptance reconciliation"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["outcome"], "completed", response.text)
            return desired

    def command(self, action):
        return {"command_id": str(uuid4()), "binding": Store(self.config.browser).status().binding.model_dump(mode="json"),
                "action": action}

    async def action(self, session, action):
        command = self.command(action)
        result = await session.call_tool("browser_turn", command)
        self.assertFalse(result.isError, result)
        self.assertEqual(result.structuredContent["outcome"], "completed", result)
        return command, result.structuredContent


class RealDesktopTest(DesktopFixture):
    async def test_dom_persistence_holds_receipts_and_owned_restart(self):
        self.assertFalse(self.config.browser.profile.exists())
        initialized = self.cli("init")
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        self.assertEqual(json.loads(initialized.stdout)["phase"], "initialized")
        await self.start_desktop()
        first = read_state(self.config.browser)
        duplicate = self.cli("start")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertEqual(read_state(self.config.browser), first)
        self.assertTrue(json.loads(self.cli("status").stdout)["ready"])
        # Actual VNC server offers only SecurityTypes=None on its loopback socket.
        with socket.create_connection(("127.0.0.1", self.config.browser.vnc_port), timeout=2) as vnc:
            self.assertEqual(vnc.recv(12), b"RFB 003.008\n")
            vnc.sendall(b"RFB 003.008\n")
            self.assertEqual(vnc.recv(2), b"\x01\x01")
        observed = await self.start_service()
        self.assertFalse(observed["control"]["ready"])
        async with self.session() as session:
            self.assertEqual([t.name for t in (await session.list_tools()).tools], ["browser_turn"])
            blocked = await session.call_tool("browser_turn", self.command({"kind": "snapshot"}))
            self.assertEqual(blocked.structuredContent["error"]["code"], "not_ready")
            await self.reconcile()
            url = f"http://127.0.0.1:{self.site.server_port}/"
            await self.action(session, {"kind": "navigate", "url": url})
            _, snapshot = await self.action(session, {"kind": "snapshot", "interactive": True})
            self.assertIn("Save", snapshot["result"]["text"])
            await self.action(session, {"kind": "fill", "selector": "#name", "text": "synthetic value"})
            command, receipt = await self.action(session, {"kind": "click", "selector": "#save"})
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, receipt)
            _, snapshot = await self.action(session, {"kind": "snapshot"})
            self.assertIn("Saved synthetic value", snapshot["result"]["text"])
            _, screenshot = await self.action(session, {"kind": "screenshot"})
            image = Path(next(a["path"] for a in screenshot["result"]["artifacts"] if a["media_type"] == "image/png"))
            self.assertTrue(image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
            await self.action(session, {"kind": "navigate", "url": url + "?before-stop"})
            self.assertIn("qorqut_synthetic=persisted", self.cookies[-1])
        first_helper = HelperState.model_validate_json((self.runtime / "helper-process.json").read_text())
        socket_paths = [str(helper_socket(self.config.browser))]
        verify_process(first.processes["chrome"])  # client detach left actual Chrome alive
        verify_process(first_helper.process)
        await self.reconcile("human")
        async with self.session() as session:
            blocked = await session.call_tool("browser_turn", self.command({"kind": "snapshot"}))
            self.assertEqual(blocked.structuredContent["error"]["code"], "not_ready")
        self.stop_service()
        observed = await self.start_service()
        self.assertEqual(observed["control"]["mode"], "human")
        self.assertFalse(observed["control"]["ready"])
        verify_process(first.processes["chrome"])
        async with self.session() as session:
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, receipt)
            await self.reconcile()
            await self.action(session, {"kind": "snapshot"})
        await self.reconcile("human")
        self.stop_service()
        stopped = await asyncio.to_thread(self.cli, "stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["phase"], "stopped")
        self.assertEqual(self.desktop.wait(5), 0)
        self.assertFalse(present(first_helper.process))
        self.assertFalse(self.config.browser.state_file.exists())
        self.assertFalse(os.path.lexists(self.config.browser.profile / "SingletonLock"))
        self.assertFalse(os.path.lexists(f"/tmp/.X{self.config.browser.display}-lock"))
        same = self.cli("start")
        self.assertNotEqual(same.returncode, 0)
        self.assertEqual(json.loads(same.stderr)["error"], "target_changed")
        new_identity = self.config.browser.binding.identity.model_copy(update={"browser_instance": uuid4()})
        browser = self.config.browser.model_copy(update={"binding": Binding(identity=new_identity, control_epoch=0)})
        self.config = self.config.model_copy(update={"browser": browser})
        private_file(self.config_path, self.config.model_dump(mode="json"))
        await self.start_desktop()
        observed = await self.start_service()
        self.assertEqual(observed["control"]["mode"], "human")
        self.assertFalse(observed["control"]["ready"])
        async with self.session() as session:
            self.assertEqual((await session.call_tool("browser_turn", command)).structuredContent, receipt)
            await self.reconcile()
            stale = {**command, "command_id": str(uuid4())}
            self.assertEqual((await session.call_tool("browser_turn", stale)).structuredContent["error"]["code"], "target_changed")
            self.cookies.clear()
            await self.action(session, {"kind": "navigate", "url": url + "?after-restart"})
            self.assertTrue(any("qorqut_synthetic=persisted" in value for value in self.cookies), self.cookies)
        socket_paths.append(str(helper_socket(self.config.browser)))
        second_helper = HelperState.model_validate_json((self.runtime / "helper-process.json").read_text())
        self.stop_service()
        stopped = await asyncio.to_thread(self.cli, "stop")
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(self.desktop.wait(5), 0)
        self.assertFalse(present(second_helper.process))
        private_file(self.root / "result.json", {"result": "passed", "first_instance": str(first.identity.browser_instance),
            "second_instance": str(new_identity.browser_instance), "saved_command": command, "saved_receipt": receipt,
            "screenshot": str(image), "cdp_port": browser.cdp_port, "vnc_port": browser.vnc_port,
            "mcp_port": browser.service_port, "display": browser.display,
            "runtime": str(self.runtime), "helper_socket_paths": [
                {"path": p, "bytes": len(os.fsencode(p))} for p in socket_paths]})


if __name__ == "__main__":
    unittest.main()
