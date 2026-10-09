"""D40 setup checks: owned Bash/normal server, synthetic native configuration only."""

import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import shlex
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.adapters.manual_core import prepare_manual_core
from qorqut_core.adapters.manual_hooks import ManualBinding, NativeProgram, prepare_manual_hooks, read_hook_configuration
from qorqut_core.adapters.types import AdapterError, ManagedRoots, MCPBinding
from qorqut_core.app import Application, RecoveryActivation
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runtime import SessionMonitor, SessionOwners
from qorqut_core.runtime.native_models import NativeCollectionView
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import create_app

from runtime.support import RuntimeCase
from transports.test_terminal import OwnedProcesses, TerminalInstallation, input_frame, received, take, wait_for
from transports.test_transports import session, structured


def programs(root):
    # Explicit executables, never provider discovery or a model invocation.
    result = []
    for provider, version in VERSIONS.items():
        executable = root / provider
        executable.write_text("#!/bin/sh\nprintf invoked > " + shlex.quote(str(root / "unexpected-native-launch")) + "\n")
        executable.chmod(0o700)
        result.append(NativeProgram(provider=provider, cli_version=version, executable=executable))
    return tuple(result)


def fixture():
    # TMPDIR can keep all data/logs in the caller's private evidence directory;
    # Unix socket paths still need the accepted short, owned /tmp directory.
    temporary = tempfile.TemporaryDirectory

    def directory(*args, **kwargs):
        if kwargs.get("prefix") == "qt-":
            kwargs["dir"] = "/tmp"
        return temporary(*args, **kwargs)

    with patch("tempfile.TemporaryDirectory", side_effect=directory):
        f = TerminalInstallation()
    f.local = f.local.model_copy(update={"native_programs": programs(f.root)})
    f.settings = f.settings.model_copy(update={"local_terminal": f.local})
    f.installation_file.write_text(f.settings.model_dump_json())
    return f


def setup_view(f):
    return f.post("runtime_get", {"agent_id": f.agent_id}).json()["data"]


def setup_state(f, state):
    return wait_for(lambda: (v if (v := setup_view(f))["native_setup"]["state"] == state else None))


class Preparation(unittest.TestCase):
    def test_preservation_idempotence_identity_and_conflicting_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            (home / ".codex").mkdir()
            roots = ManagedRoots(home=home, workspace=workspace)
            binding = ManualBinding(agent_id=uuid4(), conversation_id=uuid4(), terminal_id=uuid4(),
                                    recovery_epoch=uuid4(), environment_generation=1)
            core = MCPBinding(url="http://127.0.0.1:17001/mcp", headers=(("Authorization", "Bearer " + "a" * 64),))
            native = programs(root)
            authored = {workspace / name: ("authored " + name).encode() for name in
                        ("CLAUDE.md", "AGENTS.md", "persona.md", "memory.md")}
            for folder in ("notes", "research", "data"):
                (workspace / folder).mkdir()
                authored[workspace / folder / "kept"] = b"authored data"
            for path, body in authored.items():
                path.write_bytes(body)
            mcp, codex = workspace / ".mcp.json", home / ".codex/config.toml"
            mcp.write_text('{"mcpServers":{"authored":{"type":"stdio","command":"keep"}},"kept":true}\n')
            native_bytes = b'model = "authored"\nmodel_reasoning_effort = "medium"\n[mcp_servers.authored]\ncommand = "keep"\n'
            codex.write_bytes(native_bytes)
            hook = home / ".codex/hooks.json"
            hook.write_text('{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"keep"}]}]},"kept":true}')
            sink = (sys.executable, "-I", "-m", "qorqut_core.runtime.hook_client")
            hooks = prepare_manual_hooks(roots, binding, core, native, sink)
            hook_bytes = {p: p.read_bytes() for p in (*hooks.configuration_paths, *hooks.binding_paths)}
            # Upgrade an owned pre-notification block without changing authored
            # bytes or any native configuration/preferences.
            with patch("qorqut_core.adapters.manual_core.INBOX_INSTRUCTIONS", ""):
                prepare_manual_core(roots, binding, core, native)
            paths = prepare_manual_core(roots, binding, core, native)
            self.assertEqual(set(paths), {mcp, codex, workspace / "CLAUDE.md", workspace / "AGENTS.md"})
            self.assertEqual(hook_bytes, {p: p.read_bytes() for p in hook_bytes})
            self.assertTrue(codex.read_bytes().startswith(native_bytes))
            self.assertEqual(json.loads(mcp.read_bytes())["mcpServers"]["authored"]["command"], "keep")
            parsed = tomllib.loads(codex.read_text())
            self.assertIs(parsed["mcp_servers"]["qorqut-core"]["required"], False)
            self.assertNotIn("sandbox_mode", parsed)
            self.assertNotIn("approval_policy", parsed)
            for path, before in authored.items():
                if path.name in {"CLAUDE.md", "AGENTS.md"}:
                    self.assertTrue(path.read_bytes().startswith(before))
                    self.assertIn(b"generic connection failure is not a session conflict", path.read_bytes())
                    for fragment in (b"[Qorqut inbox <message-uuid>]", b"messages_list", b"message_id set to that exact UUID",
                                     b"saved sender, thread, body and handling", b"explicitly call messages_acknowledge",
                                     b"never the message body or permission to repeat work", b"already handled message"):
                        self.assertIn(fragment, path.read_bytes())
                    self.assertNotIn(b"qorqut-browser", path.read_bytes())
                else:
                    self.assertEqual(path.read_bytes(), before)
            codex.write_bytes(codex.read_bytes() + b'\n[projects."/native-trust"]\ntrust_level = "trusted"\n')
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
            prepare_manual_core(roots, binding, core, native)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
            with self.assertRaises(AdapterError):
                prepare_manual_core(roots, binding.model_copy(update={"agent_id": uuid4()}), core, native)
            # Owned edits, malformed documents and absent/unsupported TOML tables
            # all fail before any of the other configured destinations change.
            cases = ((codex, codex.read_bytes().replace(b"required = false", b"required = 0")),
                     (codex, b'mcp_servers = {}\n'), (codex, b'broken = ["PRIVATE'),
                     (mcp, b'{"mcpServers":{"qorqut-core":{"url":"PRIVATE"}}}'),
                     (workspace / "AGENTS.md", b"changed owned instructions"))
            for path, body in cases:
                saved = path.read_bytes()
                path.write_bytes(body)
                snapshot = {p: p.read_bytes() for p in paths}
                with self.subTest(path=path.name), self.assertRaises(AdapterError) as error:
                    prepare_manual_core(roots, binding, core, native)
                self.assertNotIn("PRIVATE", str(error.exception))
                self.assertEqual(snapshot, {p: p.read_bytes() for p in paths})
                path.write_bytes(saved)
            # Replace a previously generated token binding through the normal
            # ownership manifests, preserving all authored bytes and native trust.
            core = MCPBinding(url=core.url, headers=(("X-Qorqut-Actor", str(uuid4())),))
            prepare_manual_core(roots, binding, core, native)
            prepare_manual_hooks(roots, binding, core, native, sink)
            self.assertEqual(tomllib.loads(codex.read_text())["mcp_servers"]["qorqut-core"]["http_headers"], dict(core.headers))
            self.assertEqual(json.loads(mcp.read_bytes())["mcpServers"]["qorqut-core"]["headers"], dict(core.headers))
            self.assertTrue(codex.read_bytes().startswith(native_bytes))
            self.assertTrue(codex.read_bytes().endswith(b'\n[projects."/native-trust"]\ntrust_level = "trusted"\n'))
            for path in hooks.binding_paths:
                self.assertEqual(read_hook_configuration(path).core, core)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
            prepare_manual_core(roots, binding, core, native)
            self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
            # Missing manifest may be an interrupted first installation; only
            # identical existing core entries can be adopted.
            (workspace / ".qorqut/manual-core/preparation.json").unlink()
            prepare_manual_core(roots, binding, core, native)
            (workspace / ".qorqut/manual-core/preparation.json").unlink()
            codex.write_bytes(b'mcp_servers = {}\n')
            with self.assertRaises(AdapterError):
                prepare_manual_core(roots, binding, core, native)
            self.assertEqual(codex.read_bytes(), b'mcp_servers = {}\n')


class Observations(RuntimeCase):
    def test_passive_disabled_cached_views_and_monitor_thread_lifetime(self):
        with patch("subprocess.Popen", side_effect=AssertionError("Unexpected launch")), \
                patch.object(threading.Thread, "start", side_effect=AssertionError("Unexpected thread")):
            self.restart()
            self.owners = SessionOwners(self.app, observe_process=self.observe)
            self.app.native_setup.tick()
            self.app.native_collector.tick()
            self.assertEqual(self.runtime()["native_setup"]["state"], "disabled")
            self.assertEqual(self.runtime()["native_capture"]["reason"], "not_configured")
        other = self.create().data["agent"]["id"]
        self.assertEqual(self.app.native_setup.view(UUID(other)).reason, "unsupported_target")
        self.assertFalse((self.settings.data_root / "agents" / self.host["id"] / "runtime/core-credential.json").exists())
        self.assertEqual((self.app.db.validate(), len(self.app.registry), len(self.app.jobs.handlers)), (9, 17, 5))
        capture = NativeCollectionView(state="waiting", reason="waiting_session")
        self.app.native_collector = SimpleNamespace(view=lambda agent_id: capture)
        self.assertEqual(self.runtime()["native_capture"], capture.model_dump(mode="json"))
        self.owners.connect(self.candidate())
        with patch("builtins.open", side_effect=AssertionError("Read performed external I/O")):
            self.assertEqual(self.runtime()["status"], "connected")
            self.assertEqual(self.runtime()["native_capture"], capture.model_dump(mode="json"))
            self.assertFalse(self.runtime()["input_ready"])
        with patch("qorqut_core.runtime.native_setup.time.monotonic", return_value=time.monotonic() + 6):
            self.assertEqual(self.runtime()["native_setup"]["reason"], "observation_stale")
            ordinary = self.app.native_setup.view(UUID(other))
            self.assertEqual((ordinary.state, ordinary.reason), ("disabled", "unsupported_target"))
        calls, threads = [], []
        entered = threading.Event()

        def record(name):
            calls.append(name)
            threads.append(threading.get_ident())

        def blocked():
            record("setup")
            raise CoreError("not_ready", "bounded synthetic hold")

        def collect():
            record("collect")
            entered.set()

        monitor = SessionMonitor(SimpleNamespace(db=self.app.db, reconcile=lambda: record("owners")),
            native_setup=SimpleNamespace(tick=blocked, close=lambda: record("close_setup")),
            native_collector=SimpleNamespace(tick=collect, close=lambda: record("close_collect")))
        monitor.start()
        try:
            self.assertTrue(entered.wait(4))
            self.assertTrue(monitor.ready)
        finally:
            monitor.stop()
        self.assertEqual(calls[:3], ["owners", "setup", "collect"])
        self.assertEqual(calls[-2:], ["close_setup", "close_collect"])
        self.assertEqual(set(threads), {monitor._thread.ident})
        self.app.native_setup.close()
        self.assertIsNone(self.app.native_setup._cache)


class SetupServer(OwnedProcesses, unittest.TestCase):
    def test_normal_console_setup_http_mcp_attribution_and_restart(self):
        f = fixture()
        self.addCleanup(f.close)
        f.start()
        view = setup_state(f, "ready")
        self.assertEqual(view["native_setup"]["reason"], "configuration_installed")
        self.assertEqual(view["status"], "disconnected")
        self.assertFalse(view["input_ready"])
        capture = wait_for(lambda: (v if (v := setup_view(f))["native_capture"]["reason"] == "waiting_session" else None))
        self.assertEqual(capture["native_capture"]["state"], "waiting")
        terminal = f.available()
        credential_path = f.workspace.parent / "runtime/core-credential.json"
        self.assertFalse(credential_path.exists())
        agent = f.post("agents_get", {"agent_id": f.agent_id}).json()["data"]
        headers = {"X-Qorqut-Actor": agent["actor_id"]}
        mcp_path = f.workspace / ".mcp.json"
        mcp = json.loads(mcp_path.read_bytes())["mcpServers"]["qorqut-core"]
        self.assertEqual(mcp, {"type": "http", "url": f.url + "/mcp", "headers": headers})
        codex = f.workspace.parent / "runtime/home/.codex/config.toml"
        self.assertEqual(tomllib.loads(codex.read_text())["mcp_servers"]["qorqut-core"],
                         {"url": f.url + "/mcp", "http_headers": headers, "required": False})
        hook_files = []
        for program in f.local.native_programs:
            path = f.workspace / f".qorqut/manual-hooks/{program.provider}.json"
            binding = read_hook_configuration(path)
            self.assertEqual(str(binding.binding.terminal_id), terminal["terminal_id"])
            self.assertEqual(binding.program, program)
            self.assertEqual(dict(binding.core.headers), headers)
            config_path = (f.workspace / ".claude/settings.local.json" if program.provider == "claude"
                           else f.workspace.parent / "runtime/home/.codex/hooks.json")
            command = json.loads(config_path.read_bytes())["hooks"]["SessionStart"][0]["hooks"][0]["command"]
            self.assertEqual(shlex.split(command)[:4], [sys.executable, "-I", "-m", "qorqut_core.runtime.hook_client"])
            hook_files += [path, config_path]
        with httpx.Client(base_url=f.url, headers=headers, trust_env=False) as client:
            response = client.post("/api/v1/operations/agents_status_set", json={"agent_id": f.agent_id,
                "expected_revision": 1, "text": "HTTP attributed", "idempotency_key": "setup-http"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["status"]["actor_id"], agent["actor_id"])
            self.assertEqual(client.post("/api/v1/native/hook", json={}).status_code, 400)  # Attributed, invalid hook.

        async def check_mcp():
            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(httpx.AsyncClient(headers=headers, trust_env=False))
                sdk = await session(stack, f, client=client)
                self.assertEqual(len((await sdk.list_tools()).tools), 17)
                result = structured(await sdk.call_tool("agents_status_set", {"agent_id": f.agent_id,
                    "expected_revision": 2, "text": "MCP attributed", "idempotency_key": "setup-mcp"}))
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"]["status"]["actor_id"], agent["actor_id"])

        asyncio.run(check_mcp())
        paths = [mcp_path, codex, *hook_files, f.workspace / "CLAUDE.md", f.workspace / "AGENTS.md",
                 f.workspace / ".qorqut/manual-hooks/preparation.json"]
        snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        time.sleep(1.2)  # A second real monitor tick must use the successful binding cache.
        self.assertEqual(snapshot, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})
        codex.write_bytes(codex.read_bytes() + b'\n[projects."/authored"]\ntrust_level = "trusted"\n')
        snapshot[codex] = codex.read_bytes(), codex.stat().st_mtime_ns
        f.stop()
        old_core = MCPBinding(url=f.url + "/mcp", headers=(("Authorization", "Bearer " + "a" * 64),))
        roots = binding.roots
        prepare_manual_core(roots, binding.binding, old_core, f.local.native_programs)
        prepare_manual_hooks(roots, binding.binding, old_core, f.local.native_programs,
            (sys.executable, "-I", "-m", "qorqut_core.runtime.hook_client"))
        credential_path.write_bytes(b"UNUSED legacy credential fixture")
        credential_before = credential_path.read_bytes(), credential_path.stat().st_mtime_ns
        f.start()
        setup_state(f, "ready")
        self.assertEqual(f.available()["terminal_id"], terminal["terminal_id"])
        self.assertEqual({p: before[0] for p, before in snapshot.items()}, {p: p.read_bytes() for p in paths})
        self.assertEqual((credential_path.read_bytes(), credential_path.stat().st_mtime_ns), credential_before)
        self.assertFalse((f.root / "unexpected-native-launch").exists())
        with sqlite3.connect(f.settings.data_root / "state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM session_owners").fetchone()[0], 0)

    def test_private_failures_recovery_preserve_shell_files_and_owner(self):
        f = fixture()
        self.addCleanup(f.close)
        f.start()
        setup_state(f, "ready")
        f.stop()
        credential = f.workspace.parent / "runtime/core-credential.json"
        credential.write_bytes(b"UNUSED obsolete credential")
        original = credential.read_bytes()
        mcp = f.workspace / ".mcp.json"
        saved_mcp = mcp.read_bytes()
        # Legacy credential contents and permissions are not read or rewritten.
        credential.chmod(0o640)
        f.start()
        setup_state(f, "ready")
        f.stop()
        self.assertEqual(credential.read_bytes(), original)
        self.assertEqual(mcp.read_bytes(), saved_mcp)
        mcp.write_bytes(b'{"mcpServers": "PRIVATE malformed"}')
        f.start()
        self.assertEqual(setup_state(f, "blocked")["native_setup"]["reason"], "setup_unavailable")
        # A setup error leaves the real shell usable through the existing viewer.
        with f.websocket() as ws:
            state = received(ws, "state")["terminal"]
            control = take(ws, state["control"]["epoch"])
            input_frame(ws, b"printf 'SETUP_SHELL_USABLE\\n'\n", control["epoch"], 1)
            wait_for(lambda: b"SETUP_SHELL_USABLE\r\n" in f.captured())
        f.stop()
        self.assertEqual(mcp.read_bytes(), b'{"mcpServers": "PRIVATE malformed"}')
        mcp.write_bytes(saved_mcp)
        app = Application.open(f.settings)
        try:
            context = Invocation(actor_id=f.owner.actor_id)
            epoch = app.invoke("config_get", context).data["recovery_epoch"]
            app.activate_recovery(context, RecoveryActivation(idempotency_key="setup-recovery",
                expected_installation_id=f.settings.installation_id, expected_recovery_epoch=epoch))
        finally:
            app.close()
        tracked = [credential, mcp, *f.workspace.glob(".qorqut/manual-*/*.json")]
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tracked}
        f.start()
        self.assertEqual(setup_state(f, "blocked")["native_setup"]["reason"], "recovery_review")
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tracked})
        self.assertEqual(f.shell().state, "running")
        self.assertTrue(f.post("config_get", {}).json()["ok"])
        self.assertFalse((f.root / "unexpected-native-launch").exists())

    def test_setup_monitor_lifetime_preserves_unavailable_startup_observation(self):
        f = fixture()
        self.addCleanup(f.close)
        web = create_app(read_configuration(f.installation_file))

        async def check():
            async with web.router.lifespan_context(web):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as client:
                    deadline = time.monotonic() + 12
                    while True:
                        response = await client.post("/api/v1/operations/runtime_get", json={"agent_id": f.agent_id})
                        if response.json()["data"]["native_setup"]["state"] == "ready":
                            break
                        self.assertLess(time.monotonic(), deadline)
                        await asyncio.sleep(0.05)
                    self.assertEqual((await client.post("/api/v1/operations/config_get", json={})).status_code, 200)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as closed:
                self.assertEqual((await closed.post("/api/v1/operations/config_get", json={})).status_code, 503)
            self.assertIsNone(web.state.application.native_setup._cache)
            self.assertFalse(web.state.session_monitor._thread.is_alive())

        with patch("qorqut_core.runtime.native_startup._discover",
                   side_effect=PermissionError("PRIVATE startup observation")) as discovery:
            asyncio.run(check())
        self.assertGreater(discovery.call_count, 0)
