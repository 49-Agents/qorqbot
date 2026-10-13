"""D39 owning checks. Copied Python executables stand in for native CLIs.

Real disposable Bash/process ancestry; synthetic ShellViews and native payloads.
No authenticated native stores, native model turns, or live owner sessions.
"""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.adapters.manual_hooks import (ManualBinding, NativeProgram, prepare_manual_hooks,
                                               read_hook_configuration)
from qorqut_core.adapters.types import AdapterError, ManagedRoots, MCPBinding
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runner.process import fingerprint
from qorqut_core.runner.shell_models import ShellControl, ShellSpec, ShellView
from qorqut_core.runtime.hook_client import main, submit
from qorqut_core.runtime.native_hooks import (CONFLICT_MESSAGE, MAX_HOOK_BYTES, UNAVAILABLE_MESSAGE,
                                            NativeHookRequest, NativeHookResult)
from qorqut_core.transcripts.probe import ProbeResult
from qorqut_core.transports.server import Dispatcher, create_app
from runtime.support import RuntimeCase


class NativeHookCase(RuntimeCase):
    def setUp(self):
        super().setUp()
        root = self.settings.data_root / "agents" / self.host["id"]
        self.roots = ManagedRoots(home=root / "runtime/home", workspace=root / "workspace")
        self.roots.home.mkdir(parents=True)
        self.programs = tuple(NativeProgram(provider=p, cli_version=v, executable=self.root / f"synthetic-{p}-python")
                              for p, v in VERSIONS.items())
        for program in self.programs:
            shutil.copyfile(Path(sys.executable).resolve(), program.executable)
            program.executable.chmod(0o700)
        self.native = self.programs[0].executable
        self.core = MCPBinding(url="http://127.0.0.1:17501/mcp", headers=(("X-Qorqut-Actor", str(self.context.actor_id)),))
        self.processes = []
        self.script = self.root / "chain.py"
        self.script.write_text(
            "import os, subprocess, sys\n"
            "executables = sys.argv[1:]\n"
            "if not executables:\n"
            "    print(os.getpid(), flush=True)\n"
            "    sys.stdin.buffer.read()\n"
            "else:\n"
            "    child = subprocess.Popen([executables[0], __file__, *executables[1:]])\n"
            "    sys.exit(child.wait())\n")

    def tearDown(self):
        for process in self.processes:
            self.exit_child(process)
            process.stdout.close()
        super().tearDown()

    def chain(self, *, depth=1, provider="claude", session=None, terminal_id=None, native_paths=None):
        # Bash -> ordinary Python wrapper (like Node) -> copied native binary
        # -> hook Python. depth=2 adds another configured native ancestor.
        if native_paths is None:
            native_paths = (next(p.executable for p in self.programs if p.provider == provider),) * depth
        command = shlex.join((sys.executable, str(self.script), *(str(path) for path in native_paths), sys.executable))
        process = subprocess.Popen(["/usr/bin/bash", "--noprofile", "--norc", "-c", command + "; wait"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "HOME": str(self.roots.home), "LC_ALL": "C.UTF-8"})
        self.processes.append(process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "Synthetic chain did not start")
        hook_pid = int(process.stdout.readline())
        spec = ShellSpec(installation_id=self.settings.installation_id, agent_id=self.host["id"],
            recovery_epoch=self.config()["recovery_epoch"], environment_generation=1,
            home=self.roots.home, workspace=self.roots.workspace, path=os.defpath)
        shell = ShellView(spec=spec, terminal_id=terminal_id or uuid4(), state="running", reason="fixture",
            shell=fingerprint(process.pid), source_id=uuid4(), source_epoch=uuid4(), probe_address="fixture",
            capture=ProbeResult(verified=False, terminal_active=False, reason="unavailable"),
            control=ShellControl(mode="unclaimed", epoch=0, viewer_id=None, last_sequence=0))
        session = session or uuid4()
        store = ".claude/projects/test" if provider == "claude" else ".codex/sessions/2026/10/08"
        request = NativeHookRequest(provider=provider, agent_id=self.host["id"], terminal_id=shell.terminal_id,
            hook_process=fingerprint(hook_pid), payload={"hook_event_name": "SessionStart", "session_id": str(session),
                "transcript_path": str(self.roots.home / store / (str(session) + ".jsonl")),
                "source": "startup", "cwd": "/unrelated-cwd", "prompt": "PRIVATE-PROMPT-BODY"})
        return process, shell, request

    def handle(self, shell, request, **kw):
        return self.app.native_hooks.handle(self.context, request, terminal=shell,
            program=kw.get("program", next(p for p in self.programs if p.provider == request.provider)),
            configured_programs=kw.get("configured_programs", self.programs))

    def configured(self, shell, *, programs=None):
        binding = ManualBinding(agent_id=shell.spec.agent_id, conversation_id=self.host["conversation_id"],
            terminal_id=shell.terminal_id, recovery_epoch=shell.spec.recovery_epoch, environment_generation=1)
        return prepare_manual_hooks(self.roots, binding, self.core, self.programs if programs is None else programs,
                                    (sys.executable, "-I", "-m", "qorqut_core.runtime.hook_client"))


class NativeHooks(NativeHookCase):

    def test_real_admission_reconnect_conflict_same_uuid_stop_unknown_and_exit(self):
        first, shell, request = self.chain()
        result = self.handle(shell, request)
        self.assertEqual(result.outcome, "connected")  # Fresh transcript deliberately absent.
        native_pid = json.loads(self.rows("session_owners")[0]["process_json"])["pid"]
        self.assertEqual(os.readlink(f"/proc/{native_pid}/exe"), str(self.native))
        self.assertEqual(self.handle(shell, request).owner_id, result.owner_id)
        self.assertEqual(self.handle(shell, request).outcome, "reconnected")
        shared_executable = (self.programs[0], self.programs[1].model_copy(update={"executable": self.native}))
        self.assertEqual(self.handle(shell, request, configured_programs=shared_executable).outcome, "reconnected")
        self.assertFalse(self.runtime()["input_ready"])
        for event, activity in (("Stop", "idle"), ("SessionEnd", "unknown")):
            observation = request.model_copy(update={"payload": {**request.payload, "hook_event_name": event}})
            self.assertEqual(self.handle(shell, observation).outcome, "observed")
            self.assertEqual(json.loads(self.events(self.host["id"], "native.hook")[-1]["payload_json"])["activity"], activity)
            self.assertEqual(self.rows("session_owners")[0]["state"], "active")
        unknown = request.model_copy(update={"payload": {**request.payload, "hook_event_name": "PRIVATE-UNKNOWN"}})
        self.assertEqual(self.handle(shell, unknown).outcome, "unavailable")
        second, other_shell, other = self.chain(session=UUID(request.payload["session_id"]), terminal_id=shell.terminal_id)
        conflict = self.handle(other_shell, other)
        self.assertEqual((conflict.outcome, conflict.message), ("conflict", CONFLICT_MESSAGE))
        different = other.model_copy(update={"payload": {**other.payload, "session_id": str(uuid4())}})
        self.assertEqual(self.handle(other_shell, different).outcome, "conflict")
        self.assertEqual(len(self.rows("session_owners")), 1)
        self.exit_child(first)
        self.app.session_owners.reconcile()
        resumed = self.handle(other_shell, other)
        self.assertEqual((resumed.outcome, resumed.run_generation), ("connected", 2))
        self.assertNotEqual(resumed.owner_id, result.owner_id)
        events = self.events(self.host["id"], "native.hook")
        for event in events:
            self.assertEqual(event["actor_id"], self.host["actor_id"])
            self.assertNotIn("PRIVATE-PROMPT-BODY", event["payload_json"])
            self.assertNotIn("PRIVATE-UNKNOWN", event["payload_json"])
        self.assertIsNone(second.poll())

    def test_children_wrong_ancestry_fingerprint_store_profile_and_unknown_incumbent(self):
        _, shell, request = self.chain(provider="codex")
        for field, value in (("agent_id", "native-child"), ("parent_session_id", str(uuid4())),
                             ("run_id", str(uuid4())), ("transcript_path", str(self.root / "outside"))):
            changed = request.model_copy(update={"payload": {**request.payload, field: value}})
            self.assertEqual(self.handle(shell, changed).outcome, "unavailable")
        stale = request.model_copy(update={"hook_process": request.hook_process.model_copy(update={"start_ticks": 1})})
        self.assertEqual(self.handle(shell, stale).outcome, "unavailable")
        self.assertEqual(self.handle(shell, request, program=self.programs[1].model_copy(update={"cli_version": "unknown"})).outcome,
                         "unavailable")
        _, nested_shell, nested = self.chain(depth=2)
        self.assertEqual(self.handle(nested_shell, nested).outcome, "unavailable")
        self.assertEqual(self.handle(nested_shell.model_copy(update={"terminal_id": request.terminal_id}), request).outcome,
                         "unavailable")
        for parent, child in (self.programs, self.programs[::-1]):
            self.assertNotEqual(parent.executable, child.executable)
            _, nested_shell, nested = self.chain(provider=child.provider, native_paths=(parent.executable, child.executable))
            self.configured(nested_shell)
            self.assertEqual(self.handle(nested_shell, nested).outcome, "unavailable")
            with patch.object(self.app.terminals, "native_context", return_value=nested_shell):
                self.assertEqual(self.app.native_hooks.receive(self.context, nested).outcome, "unavailable")
            self.assertEqual(len(self.rows("session_owners")), 0)
            self.assertFalse(self.events(self.host["id"], "session.connected"))
            self.assertTrue(all(json.loads(event["payload_json"])["outcome"] == "unavailable"
                                for event in self.events(self.host["id"], "native.hook")))
        stop = request.model_copy(update={"payload": {**request.payload, "hook_event_name": "Stop"}})
        self.assertEqual(self.handle(shell, stop).outcome, "unavailable")
        self.assertEqual(len(self.rows("session_owners")), 0)
        connected = self.handle(shell, request)
        self.assertEqual(connected.outcome, "connected")
        _, rival_shell, rival = self.chain(provider="codex")
        with patch.object(self.app.session_owners, "observe_process", side_effect=PermissionError("PRIVATE-ERROR")):
            self.assertEqual(self.handle(rival_shell, rival).outcome, "unavailable")
        self.assertEqual(len(self.rows("session_owners")), 1)
        self.assertEqual(self.rows("session_owners")[0]["state"], "active")

    def test_preparation_preserves_authored_mcp_and_hooks_idempotently_and_refuses_edits(self):
        _, shell, _ = self.chain()
        claude = self.roots.workspace / ".claude/settings.local.json"
        codex = self.roots.home / ".codex/hooks.json"
        authored = {"permissions": {"allow": ["Read"]}, "mcpServers": {"kept": {}},
                    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo authored"}]}]}}
        for path in (claude, codex):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(authored))
        mcp = self.roots.workspace / ".mcp.json"
        mcp.write_text('{"mcpServers":{"qorqut-core":{},"qorqut-browser":{}}}\n')
        toml = self.roots.home / ".codex/config.toml"
        toml.write_text('[mcp_servers.qorqut-core]\nurl = "http://localhost:1/mcp"\n')
        before_mcp = mcp.read_bytes(), toml.read_bytes()
        setup = self.configured(shell)
        before = {p: p.read_bytes() for p in (*setup.configuration_paths, *setup.binding_paths)}
        self.configured(shell)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(before_mcp, (mcp.read_bytes(), toml.read_bytes()))
        for path, provider in zip(setup.configuration_paths, VERSIONS):
            saved = json.loads(path.read_bytes())
            self.assertEqual(saved["permissions"], authored["permissions"])
            self.assertEqual(saved["hooks"]["SessionStart"][0], authored["hooks"]["SessionStart"][0])
            command = saved["hooks"]["SessionStart"][1]["hooks"][0]["command"]
            self.assertEqual(shlex.split(command)[-2:], ["--provider", provider])
            saved["unrelated"] = "preserved later edit"
            path.write_text(json.dumps(saved))
        self.configured(shell)
        for path in setup.binding_paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(claude.read_bytes())
        saved["hooks"]["SessionStart"][1]["hooks"][0]["command"] += " edited"
        claude.write_text(json.dumps(saved))
        with self.assertRaises(AdapterError):
            self.configured(shell)
        self.assertEqual(json.loads(claude.read_bytes()), saved)

    def test_internal_http_resolves_binding_and_has_fixed_private_failures(self):
        _, shell, request = self.chain()
        web = create_app(self.settings)
        web.state.local_owner = self.app.local_owner()
        web.state.application = self.app
        web.state.dispatcher = Dispatcher(self.app)
        headers = {"X-Qorqut-Actor": str(self.context.actor_id)}

        async def check():
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://127.0.0.1:17501") as client:
                    path = "/api/v1/native/hook"
                    response = await client.post(path, json=request.model_dump(mode="json"))
                    self.assertEqual((response.status_code, response.json()["outcome"]), (503, "unavailable"))
                    response = await client.post(path, headers=headers, json=request.model_dump(mode="json"))
                    self.assertEqual((response.status_code, response.json()["outcome"]), (503, "unavailable"))
                    setup = self.configured(shell, programs=(self.programs[0],))
                    self.assertFalse(setup.binding_paths[0].with_name("codex.json").exists())
                    with patch.object(self.app.terminals, "native_context", return_value=shell):
                        response = await client.post(path, json=request.model_dump(mode="json"))
                        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "connected"))
                        setup = self.configured(shell)
                        response = await client.post(path, headers=headers, json=request.model_dump(mode="json"))
                        self.assertEqual((response.status_code, response.json()["outcome"]), (200, "reconnected"))
                        peer_path = setup.binding_paths[1]
                        peer = peer_path.read_bytes()
                        bad_peers = [b"{"]
                        for section, field, value in (
                                *(("binding", field, str(uuid4())) for field in (
                                    "agent_id", "conversation_id", "terminal_id", "recovery_epoch")),
                                ("binding", "environment_generation", 2),
                                ("roots", "home", str(self.root / "other-home")),
                                ("roots", "workspace", str(self.root / "other-workspace")),
                                ("program", "provider", "claude")):
                            changed = json.loads(peer)
                            changed[section][field] = value
                            bad_peers.append(json.dumps(changed).encode())
                        for bad_peer in bad_peers:
                            peer_path.write_bytes(bad_peer)
                            response = await client.post(path, headers=headers, json=request.model_dump(mode="json"))
                            self.assertEqual((response.status_code, response.json()["outcome"]), (503, "unavailable"))
                        peer_path.write_bytes(peer)
                        rival = request.model_copy(update={"payload": {**request.payload, "session_id": str(uuid4())}})
                        response = await client.post(path, headers=headers, json=rival.model_dump(mode="json"))
                        self.assertEqual((response.status_code, response.json()["message"]), (409, CONFLICT_MESSAGE))
                        binding = setup.binding_paths[0]
                        data = json.loads(binding.read_bytes())
                        data["binding"]["recovery_epoch"] = str(uuid4())
                        binding.write_text(json.dumps(data))
                        response = await client.post(path, headers=headers, json=request.model_dump(mode="json"))
                        self.assertEqual((response.status_code, response.json()["outcome"]), (503, "unavailable"))
                    response = await client.post(path, headers=headers,
                        json={**request.model_dump(mode="json"), "filename": "PRIVATE-FILENAME"})
                    self.assertEqual(response.status_code, 400)
                    self.assertNotIn("PRIVATE-FILENAME", response.text)
                    response = await client.post(path, headers={**headers, "Content-Type": "application/json"},
                                                 content=b"x" * (MAX_HOOK_BYTES + 1))
                    self.assertEqual(response.status_code, 413)
                    schema = await client.get("/api/v1/schema", headers=headers)
                    self.assertNotIn("native_hook", schema.text)
            finally:
                await web.state.dispatcher.close()

        asyncio.run(check())
        for event in self.events(self.host["id"], "native.hook"):
            self.assertNotIn("PRIVATE-PROMPT-BODY", event["payload_json"])

    def test_client_conflict_unavailable_redaction_and_native_context(self):
        _, shell, request = self.chain()
        setup = self.configured(shell)
        path = setup.binding_paths[0]
        client_class = httpx.Client
        seen = []

        def transport(request):
            seen.append(request)
            return httpx.Response(409, stream=httpx.ByteStream(NativeHookResult.result("conflict").model_dump_json().encode()))

        with patch("qorqut_core.runtime.hook_client.httpx.Client", side_effect=lambda **kw: client_class(
                transport=httpx.MockTransport(transport), **kw)):
            result = submit(path, "claude", json.dumps(request.payload).encode())
        self.assertEqual(result.message, CONFLICT_MESSAGE)
        self.assertEqual(seen[0].url.path, "/api/v1/native/hook")
        self.assertEqual(seen[0].headers["X-Qorqut-Actor"], str(self.context.actor_id))
        self.assertNotIn("Authorization", seen[0].headers)
        self.assertEqual(json.loads(seen[0].content)["hook_process"]["pid"], os.getpid())
        with patch("qorqut_core.runtime.hook_client.httpx.Client", side_effect=RuntimeError("PRIVATE-BODY")):
            result = submit(path, "claude", json.dumps(request.payload).encode())
        self.assertEqual((result.outcome, result.message), ("unavailable", UNAVAILABLE_MESSAGE))
        for status, body in ((409, b'{"message":"PRIVATE-BODY"}'),
                             (503, NativeHookResult.result("conflict").model_dump_json().encode()),
                             (200, b"PRIVATE-BODY" * 2000)):
            with patch("qorqut_core.runtime.hook_client.httpx.Client", side_effect=lambda **kw: client_class(
                    transport=httpx.MockTransport(lambda _: httpx.Response(status, stream=httpx.ByteStream(body))), **kw)):
                result = submit(path, "claude", json.dumps(request.payload).encode())
            self.assertEqual((result.outcome, result.message), ("unavailable", UNAVAILABLE_MESSAGE))
        self.assertEqual(submit(path, "codex", b"{}").outcome, "unavailable")
        self.assertEqual(submit(path, "claude", b"x" * (MAX_HOOK_BYTES + 1)).outcome, "unavailable")
        for outcome in ("conflict", "unavailable"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("qorqut_core.runtime.hook_client.submit", return_value=NativeHookResult.result(outcome)), \
                    patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request.payload).encode()))), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(main(["--binding", str(path), "--provider", "claude"]), 0)
            message = CONFLICT_MESSAGE if outcome == "conflict" else UNAVAILABLE_MESSAGE
            self.assertEqual(stderr.getvalue(), message + "\n")
            self.assertEqual(json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"], message)

        terminals = self.app.terminals
        with self.assertRaises(CoreError):
            terminals.native_context(UUID(self.host["id"]), shell.terminal_id)
        self.assertEqual(terminals._managers, {})
        terminals.settings = SimpleNamespace(runtime_root=self.root / "runtime")
        with self.app.db.read() as tx:
            target, _, _ = terminals._target(tx, shell.spec.agent_id)
        # This seam receives a synthetic positive recorder observation; admission
        # ancestry above is real. Actual recorder/native startup is integration's gate.
        fresh = shell.model_copy(update={"capture": ProbeResult.model_construct(
            verified=True, terminal_active=True, observation=None, reason="active")})
        manager = SimpleNamespace(inspect=lambda terminal_id: fresh)
        terminals._managers[shell.spec.agent_id] = manager
        terminals._publish(target, terminals._presentation(target, fresh))
        self.assertEqual(terminals.native_context(shell.spec.agent_id, shell.terminal_id), fresh)
        self.assertEqual(fresh.control, shell.control)
        with self.assertRaises(CoreError):
            terminals.native_context(shell.spec.agent_id, uuid4())
        with patch.object(manager, "inspect", side_effect=lambda _: (self.recovery(), fresh)[1]):
            with self.assertRaises(CoreError):
                terminals.native_context(shell.spec.agent_id, shell.terminal_id)

    def test_cancelled_http_waiter_keeps_effect_until_dispatcher_shutdown(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def effect(context, request):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test release missing")
            finished.set()
            return NativeHookResult.result("unavailable")

        async def check():
            dispatcher = Dispatcher(SimpleNamespace(native_hooks=SimpleNamespace(receive=effect)))
            task = asyncio.create_task(dispatcher.native_hook(self.context, None))
            closing = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(len(dispatcher.pending), 1)
                closing = asyncio.create_task(dispatcher.close())
                await asyncio.sleep(0.03)
                self.assertFalse(closing.done())
                self.assertFalse(finished.is_set())
                self.assertEqual((await dispatcher.native_hook(self.context, None)).outcome, "unavailable")
            finally:
                release.set()
                if closing is not None:
                    await closing
                else:
                    await dispatcher.close()
            self.assertTrue(finished.is_set())

        asyncio.run(check())
