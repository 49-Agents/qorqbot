"""D38 boundary checks: disposable recorded Bash and the actual console process."""

import asyncio
import base64
import ctypes
import hashlib
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import termios
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from qorqut_core.app import Application, RecoveryActivation
from qorqut_core.config import InstallationSettings, LocalTerminalSettings
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runner.models import RunnerError
from qorqut_core.runner.process import fingerprint, verify_process
from qorqut_core.runner.shell import ShellManager
from qorqut_core.runner.shell_models import ShellDimensions
from qorqut_core.runner.storage import input_lock
from qorqut_core.runtime.terminal_models import CLIENT_MESSAGE, TerminalView
from qorqut_core.transports.boundary import Admission, Boundary
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import TerminalSockets, create_app

from transports.support import Installation
from transports.test_transports import session, structured


def wait_for(function, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = function()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError("Owned Terminal fixture did not reach the expected state")


class TerminalInstallation(Installation):
    def __init__(self):
        super().__init__()
        self.socket_temp = tempfile.TemporaryDirectory(prefix="qt-")
        self.local = LocalTerminalSettings(runtime_root=Path(self.socket_temp.name) / "t",
            python_executable=Path(sys.executable), tmux_executable=Path("/usr/bin/tmux"),
            bash_executable=Path("/usr/bin/bash"), path="/usr/bin:/bin")
        self.settings = self.settings.model_copy(update={"local_terminal": self.local})
        self.installation_file.write_text(self.settings.model_dump_json())
        self.agent_id = self.initial_events[1]["agent_id"]
        self.workspace = self.settings.data_root / "agents" / self.agent_id / "workspace"
        self.database = self.workspace.parent / "runtime/terminal.sqlite3"
        self.known = []

    def manager(self):
        runtime = self.local.runtime_root / UUID(self.agent_id).hex
        return ShellManager(self.database, runtime, runtime / "tmux.sock", self.local.python_executable,
                            self.local.tmux_executable, self.local.bash_executable)

    def shell(self):
        with sqlite3.connect(self.database) as db:
            terminal_id = UUID(db.execute("SELECT terminal_id FROM runner_shell_terminals").fetchone()[0])
        return self.manager().inspect(terminal_id)

    def captured(self):
        shell = self.shell()
        # Automatic archival can reclaim local BLOBs. Follow their immutable
        # coordinates to the durable host copy without weakening byte checks.
        with sqlite3.connect(self.database) as db:
            rows = db.execute("SELECT offset,length,sha256,payload FROM capture_chunks "
                              "WHERE source_id=? AND source_epoch=? ORDER BY offset",
                              (str(shell.source_id), str(shell.source_epoch))).fetchall()
        data = b""
        for offset, length, digest, payload in rows:
            if payload is None:
                # D50 host objects may contain several original recorder chunks.
                with sqlite3.connect(self.settings.data_root / "state.sqlite3") as db:
                    archived = db.execute("SELECT offset,length,sha256,object_path FROM archive_chunks "
                        "WHERE source_id=? AND source_epoch=? AND offset<=? AND offset+length>=?",
                        (str(shell.source_id), str(shell.source_epoch), offset, offset + length)).fetchone()
                assert archived is not None
                start, size, sha256, path = archived
                body = (self.settings.data_root / path).read_bytes()
                assert len(body) == size and hashlib.sha256(body).hexdigest() == sha256
                payload = body[offset - start:offset - start + length]
            assert offset == len(data) and len(payload) == length
            assert hashlib.sha256(payload).hexdigest() == digest
            data += payload
        return data

    def terminal(self):
        result = self.post("terminal_get", {"agent_id": self.agent_id}).json()
        assert result["ok"], result
        return result["data"]

    def available(self):
        return wait_for(lambda: (v if (v := self.terminal())["status"] == "available" else None))

    def websocket(self, *, path=None, origin=True, headers=None):
        return connect(self.url.replace("http:", "ws:") + (path or "/api/v1/terminal/" + self.agent_id),
            origin=self.url if origin is True else origin or None,
            additional_headers=headers, proxy=None,
            open_timeout=4, close_timeout=2, max_size=96 * 1024)

    def close(self):
        self.stop()
        try:
            if self.database.exists():
                view = self.shell()
                self.known += [p for p in (view.shell, view.recorder, view.tmux.server if view.tmux else None) if p]
                self.manager().shutdown(view.terminal_id)
            for process in self.known:
                try:
                    fd = os.pidfd_open(process.pid)
                    try:
                        verify_process(process)
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                    finally:
                        os.close(fd)
                except (ProcessLookupError, RunnerError):
                    pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if not os.waitpid(-1, os.WNOHANG)[0]:
                        time.sleep(0.02)
                except ChildProcessError:
                    break
            else:
                raise AssertionError("Owned test descendants were not reaped")
        finally:
            self.socket_temp.cleanup()
            super().close()


def received(ws, kind, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = ws.recv(timeout=deadline - time.monotonic())
        if isinstance(raw, bytes):
            assert len(raw) <= 65536
            continue
        message = json.loads(raw)
        if message["type"] == kind:
            return message
        if message["type"] == "error":
            raise AssertionError(message)
    raise AssertionError("Expected Terminal receipt was not received")


def send(ws, message, kind):
    ws.send(json.dumps(message))
    return received(ws, kind)


def take(ws, epoch):
    return send(ws, {"type": "control", "action": "take", "expected_epoch": epoch}, "control")["control"]


def input_frame(ws, text, epoch, sequence):
    return send(ws, {"type": "input", "epoch": epoch, "sequence": sequence,
                     "data": base64.b64encode(text).decode()}, "frame")


class OwnedProcesses:
    @classmethod
    def setUpClass(cls):
        cls.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or cls.libc.prctl(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "subreaper setup")
        cls.previous = previous.value

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.previous, 0, 0, 0)


class TerminalTests(OwnedProcesses, unittest.TestCase):
    def test_passive_settings_registration_cache_and_owned_workspace(self):
        f = Installation()
        self.addCleanup(f.close)
        legacy = json.loads(f.installation_file.read_text())
        legacy.pop("local_terminal", None)
        f.installation_file.write_text(json.dumps(legacy))
        before = f.installation_file.read_bytes()
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        host = app.invoke("agents_list", Invocation(actor_id=f.owner.actor_id)).data["items"][0]["id"]
        with patch("subprocess.Popen", side_effect=AssertionError("Read launched a process")):
            view = app.invoke("terminal_get", Invocation(actor_id=f.owner.actor_id), {"agent_id": host}).data
            self.assertEqual(view["status"], "disabled")
            self.assertIsNone(view["observed_at"])
            self.assertIsNone(view["dimensions"])
            app.terminals.observe()
        self.assertEqual(f.installation_file.read_bytes(), before)
        self.assertFalse((f.settings.data_root / "agents").exists())
        self.assertEqual(len(app.registry), 17)
        self.assertEqual(len(app.jobs.handlers), 5)
        self.assertEqual(app.db.validate(), 9)
        for delta in ({"runtime_root": "/tmp/" + "x" * 70}, {"runtime_root": "relative"},
                      {"path": "bad\0PATH"}, {"bash_executable": "/bad/../bash"}, {"path": True}):
            with self.assertRaises(ValidationError):
                LocalTerminalSettings.model_validate({"runtime_root": "/tmp/t", "python_executable": "/usr/bin/python3.12",
                    "tmux_executable": "/usr/bin/tmux", "bash_executable": "/usr/bin/bash", "path": "/bin", **delta})
        for frame in ({"type": "input", "epoch": True, "sequence": 1, "data": "eA=="},
                      {"type": "input", "epoch": 0, "sequence": 1, "data": "eB=="},
                      {"type": "input", "epoch": 0, "sequence": 1, "data": "eA==\n"},
                      {"type": "input", "epoch": 0, "sequence": 1, "data": base64.b64encode(b"x" * 65537).decode()},
                      {"type": "resize", "epoch": 0, "sequence": 1, "columns": True, "rows": 30},
                      {"type": "resize", "epoch": 0, "sequence": 1, "columns": 501, "rows": 30},
                      {"type": "resize", "epoch": 0, "sequence": 1, "columns": 80, "rows": 4},
                      {"type": "control", "action": "take", "expected_epoch": 0, "viewer_id": str(uuid4())}):
            with self.assertRaises(ValidationError):
                CLIENT_MESSAGE.validate_python(frame)
        with self.assertRaises(ValidationError):
            TerminalView.model_validate({**view, "status": "available"})
        # Bound the complete saved UTF-8 document, including formatting/newline,
        # before setup creates a directory or acquires the host lock.
        for index, character in enumerate(("x", "ü")):
            local = LocalTerminalSettings(runtime_root=Path("/tmp/t"), python_executable=Path(sys.executable),
                tmux_executable=Path("/usr/bin/tmux"), bash_executable=Path("/usr/bin/bash"),
                path="/usr/bin:/" + character * 100)
            bounded = f.settings.model_copy(update={"data_root": f.root / f"bounded-{index}", "local_terminal": local})
            padding = 16384 - len((bounded.model_dump_json(indent=2) + "\n").encode("utf-8"))
            local_args = local.model_dump()
            local_args["path"] += "x" * padding
            bounded = bounded.model_copy(update={"local_terminal": LocalTerminalSettings(**local_args)})
            encoded = (bounded.model_dump_json(indent=2) + "\n").encode("utf-8")
            self.assertEqual(len(encoded), 16384)
            local_args["path"] += "x"
            oversized = bounded.model_copy(update={"local_terminal": LocalTerminalSettings(**local_args)})
            if character == "ü":
                self.assertLess(len(oversized.model_dump_json(indent=2) + "\n"), 16384)
            with patch("qorqut_core.app.HostLock.acquire", side_effect=AssertionError("Oversized setup acquired lock")):
                with self.assertRaises(CoreError) as error:
                    Application.initialize(oversized, f.defaults, f.owner)
            self.assertEqual(error.exception.error.code, "invalid_argument")
            self.assertFalse(bounded.data_root.exists())
            initialized = Application.initialize(bounded, f.defaults, f.owner)
            initialized.close()
            saved = bounded.data_root / "installation.json"
            self.assertEqual(saved.read_bytes(), encoded)
            reopened = Application.open(bounded)
            reopened.close()
            self.assertEqual(saved.read_bytes(), encoded)

    def test_cookie_free_console_recording_control_and_restart(self):
        f = TerminalInstallation()
        self.addCleanup(f.close)
        f.start()  # Normal installed console entrypoint; no test server wrapper.
        view = f.available()
        self.assertEqual(view["recording"]["state"], "healthy")
        self.assertGreater(view["recording"]["host_archived_offset"], 0)
        self.assertLessEqual(view["recording"]["host_archived_offset"], view["recording"]["local_captured_offset"])
        self.assertEqual(view["control"]["mode"], "unclaimed")
        self.assertEqual(set(view), {"agent_id", "recovery_epoch", "status", "terminal_id", "observed_at", "reason", "control", "recording", "dimensions"})
        self.assertEqual(view["dimensions"], {"columns": 80, "rows": 24})
        shell = f.shell()
        self.assertEqual(Path(f"/proc/{shell.shell.pid}/cmdline").read_bytes().split(b"\0")[:-1],
                         [b"/usr/bin/bash", b"--noprofile", b"--norc", b"-i"])
        self.assertEqual(Path(f"/proc/{shell.shell.pid}/task/{shell.shell.pid}/children").read_text().strip(), "")
        self.assertEqual(Path(f"/proc/{shell.recorder.pid}/task/{shell.recorder.pid}/children").read_text().split(), [str(shell.shell.pid)])
        self.assertFalse(f.post("runtime_get", {"agent_id": f.agent_id}).json()["data"]["input_ready"])
        agent = f.post("agents_create", {"display_name": "VM stays unavailable", "idempotency_key": "terminal-ordinary"}).json()["data"]["agent"]["id"]
        ordinary = f.post("terminal_get", {"agent_id": agent}).json()["data"]
        self.assertEqual(ordinary["status"], "unsupported_target")
        self.assertFalse((f.local.runtime_root / UUID(agent).hex).exists())
        for options in ({"origin": False}, {"origin": "http://foreign.invalid"},
                        {"headers": {"X-Qorqut-Actor": str(uuid4())}},
                        {"headers": {"X-Qorqut-Actor": "invalid"}},
                        {"path": "/api/v1/terminal/" + f.agent_id + "?command=x"},
                        {"path": "/api/v1/terminal/" + agent}, {"path": "/unknown"},
                        {"path": "/api/v1/terminal/not-a-uuid"}):
            with self.assertRaises(InvalidStatus):
                f.websocket(**options)
        with f.websocket() as oversized:
            received(oversized, "hello")
            oversized.send("x" * (96 * 1024 + 1))
            with self.assertRaises(ConnectionClosed):
                received(oversized, "never")
        wait_for(lambda: b"bash-" in f.captured())
        baseline = f.captured()
        with f.websocket() as first, f.websocket() as second:
            hello1, hello2 = received(first, "hello"), received(second, "hello")
            self.assertEqual(hello1["protocol"], 2)
            self.assertEqual(hello1["terminal"]["dimensions"], {"columns": 80, "rows": 24})
            self.assertNotEqual(hello1["viewer_id"], hello2["viewer_id"])
            self.assertEqual(received(first, "state")["terminal"]["terminal_id"], view["terminal_id"])
            self.assertEqual(f.captured(), baseline, "viewer initialization injected input")
            with f.websocket() as third, f.websocket() as fourth:
                received(third, "hello")
                received(fourth, "hello")
                with self.assertRaises(InvalidStatus):
                    f.websocket()
            denied = send(first, {"type": "input", "epoch": 0, "sequence": 1, "data": "eA=="}, "error")
            self.assertEqual(denied["code"], "revision_conflict")
            control1 = take(first, 0)
            self.assertEqual(control1["viewer_id"], hello1["viewer_id"])
            self.assertEqual(input_frame(first, "discard\x15printf '%s\\n' 'literal ü ☃' > literal\r".encode(), control1["epoch"], 1)["outcome"], "submitted")
            wait_for(lambda: (f.workspace / "literal").exists())
            self.assertEqual((f.workspace / "literal").read_text(), "literal ü ☃\n")
            control2 = take(second, control1["epoch"])
            stale = send(first, {"type": "input", "epoch": control1["epoch"], "sequence": 2,
                                "data": base64.b64encode(b"printf BAD > bad\r").decode()}, "error")
            self.assertEqual(stale["code"], "revision_conflict")
            self.assertFalse((f.workspace / "bad").exists())
            # Hold the actual runner flock while a real WebSocket resize waits.
            # State delivery must continue at its one-second cadence.
            received(second, "state")
            with input_lock(f.database, shell.terminal_id):
                second.send(json.dumps({"type": "resize", "epoch": control2["epoch"], "sequence": 1, "columns": 113, "rows": 31}))
                started = time.monotonic()
                states = [started]
                while time.monotonic() < started + 3.25:
                    try:
                        raw = second.recv(timeout=max(0.001, started + 3.25 - time.monotonic()))
                    except TimeoutError:
                        break
                    if isinstance(raw, bytes):
                        self.assertLessEqual(len(raw), 65536)
                        continue
                    message = json.loads(raw)
                    self.assertEqual(message["type"], "state", "Blocked resize settled before its gate opened")
                    self.assertEqual(message["terminal"]["status"], "available")
                    states.append(time.monotonic())
                    if len(states) == 2:
                        self.assertEqual(httpx.get(f.url + "/health/ready", trust_env=False, timeout=1).status_code, 200)
                self.assertGreaterEqual(len(states), 4)
                # Allow 150 ms of real process/network scheduling jitter.
                self.assertLess(max(b - a for a, b in zip(states, states[1:])), 1.15)
            changes = {}
            while not {"frame", "size"}.issubset(changes):
                raw = second.recv(timeout=5)
                if isinstance(raw, str):
                    message = json.loads(raw)
                    self.assertNotEqual(message["type"], "error", message)
                    changes[message["type"]] = message
                else:
                    self.assertLessEqual(len(raw), 65536)
            resized = changes["frame"]
            self.assertEqual(resized["outcome"], "submitted")
            self.assertEqual(resized["sequence"], 1)
            for size in (received(first, "size"), changes["size"]):
                self.assertEqual(size, {"type": "size", "columns": 113, "rows": 31})
            def clients_follow():
                lines = f.manager()._command("list-clients", "-F",
                    "#{client_width}|#{client_height}|#{client_flags}").decode().splitlines()
                return len(lines) == 2 and all(line.startswith("113|31|") and
                    {"read-only", "ignore-size"}.issubset(line.split("|")[2].split(",")) for line in lines)
            wait_for(clients_follow)
            with f.websocket() as wide_viewer:
                wide_hello = received(wide_viewer, "hello")
                self.assertEqual(wide_hello["terminal"]["dimensions"], {"columns": 113, "rows": 31})
            input_frame(second, b"stty size > dimensions; sleep 40 & echo $! > child.pid\r", control2["epoch"], 2)
            wait_for(lambda: (f.workspace / "child.pid").exists())
            child = fingerprint(int((f.workspace / "child.pid").read_text()))
            f.known.append(child)
            self.assertEqual((f.workspace / "dimensions").read_text(), "31 113\n")
            for malformed in ({"type": "control", "action": "take", "expected_epoch": True},
                              {"type": "input", "epoch": control2["epoch"], "sequence": 3, "data": "%%%"},
                              {"type": "resize", "epoch": control2["epoch"], "sequence": 3, "columns": 19, "rows": 31},
                              {"type": "resize", "epoch": control2["epoch"], "sequence": 3, "columns": 113, "rows": 251},
                              {"type": "resize", "epoch": control2["epoch"], "sequence": 3, "columns": 113, "rows": 31, "agent_id": agent}):
                self.assertEqual(send(second, malformed, "error")["code"], "invalid_argument")
            self.assertEqual(f.manager().control(shell.terminal_id).last_sequence, 2)
            input_frame(second, b"(sleep 0.4; printf 'AWAY_%s\\n' MARKER) &\r", control2["epoch"], 3)
        # Disconnect releases this viewer's hold without stopping execution.
        released = wait_for(lambda: (c if (c := f.manager().control(shell.terminal_id)).mode == "unclaimed" else None))
        self.assertEqual((released.epoch, released.viewer_id, released.last_sequence), (control2["epoch"] + 1, None, 0))
        wait_for(lambda: f.terminal()["control"] == released.model_dump(mode="json"))
        f.stop()
        verify_process(child)
        verify_process(shell.shell)
        wait_for(lambda: b"AWAY_MARKER\r\n" in f.captured())
        f.start()
        recovered = f.available()
        again = f.shell()
        self.assertEqual((again.tmux, again.recorder, again.shell, again.terminal_id),
                         (shell.tmux, shell.recorder, shell.shell, shell.terminal_id))
        self.assertEqual(recovered["control"], released.model_dump(mode="json"))
        self.assertEqual(recovered["dimensions"], {"columns": 113, "rows": 31})
        self.assertGreaterEqual(recovered["recording"]["local_captured_offset"], view["recording"]["local_captured_offset"])
        with f.websocket() as viewer:
            hello = received(viewer, "hello")
            self.assertNotIn(hello["viewer_id"], (hello1["viewer_id"], hello2["viewer_id"]))
            final = take(viewer, recovered["control"]["epoch"])
            released = send(viewer, {"type": "control", "action": "release", "expected_epoch": final["epoch"]}, "control")
            self.assertEqual(released["control"]["mode"], "unclaimed")
            held = take(viewer, released["control"]["epoch"])
            with httpx.Client(trust_env=False) as client:
                self.assertEqual(client.delete(f.url + "/api/v1/session", headers={"Origin": f.url}).status_code, 404)
            self.assertEqual(input_frame(viewer, b"printf 'LOCAL_ACCESS\\n'\r", held["epoch"], 1)["outcome"], "submitted")
            self.assertEqual(f.manager().control(shell.terminal_id).epoch, held["epoch"])
        args = f.root / "terminal-arguments.json"
        args.write_text(json.dumps({"agent_id": f.agent_id}))
        command = f.command("call", "terminal_get", "--url", f.url, "--arguments-file", args, "--json")
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)["data"]["terminal_id"], str(shell.terminal_id))
        with sqlite3.connect(f.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runner_shell_terminals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT run_id FROM capture_runs").fetchall(), [(str(shell.terminal_id),)])
        async def parity():
            async with AsyncExitStack() as stack:
                sdk = await session(stack, f, f.agent)
                tools = {tool.name: tool for tool in (await sdk.list_tools()).tools}
                self.assertEqual(len(tools), 17)
                self.assertEqual(tools["terminal_get"].inputSchema["required"], ["agent_id"])
                result = structured(await sdk.call_tool("terminal_get", {"agent_id": f.agent_id}))
                self.assertTrue(result["ok"])
                self.assertEqual(result["data"]["terminal_id"], str(shell.terminal_id))
                self.assertIsNone(result["operation_id"])
        asyncio.run(parity())
        # An extra, positively owned window invalidates the exact capture
        # binding even though the terminal UUID and recovery epoch are unchanged.
        with f.websocket() as changed:
            current = received(changed, "hello")["terminal"]["control"]
            take(changed, current["epoch"])
            held = f.manager().control(shell.terminal_id)
            verify_process(shell.tmux.server)
            tmux = ["/usr/bin/tmux", "-N", "-S", str(f.manager().socket_path), "-f", "/dev/null"]
            result = subprocess.run([*tmux, "new-window", "-P", "-F", "#{window_id}|#{pane_id}|#{pane_pid}",
                "-t", shell.tmux.session_id, "-n", "owned-test", "--", "/usr/bin/bash", "--noprofile", "--norc", "-c",
                "printf 'ALTERED_INITIAL\\n'; read -r -t 3 REPLY; printf 'ALTERED_AFTER\\n'; read -r -t 20 REPLY"],
                capture_output=True, text=True, check=True, timeout=3)
            window, pane, pid = result.stdout.strip().split("|")
            try:
                f.known.append(fingerprint(int(pid)))
                unavailable = wait_for(lambda: (v if (v := f.terminal())["status"] == "unavailable" else None))
                self.assertEqual((unavailable["terminal_id"], unavailable["reason"]), (str(shell.terminal_id), "binding_unconfirmed"))
                with self.assertRaises(ConnectionClosed):
                    while True:
                        raw = changed.recv(timeout=2)
                        if isinstance(raw, bytes):
                            self.assertNotIn(b"ALTERED_AFTER", raw)
                def altered_output():
                    verify_process(shell.tmux.server)
                    return b"ALTERED_AFTER" in subprocess.run([*tmux, "capture-pane", "-p", "-t", pane],
                        capture_output=True, check=True, timeout=3).stdout
                wait_for(altered_output, timeout=5)
                self.assertEqual(f.manager().control(shell.terminal_id), held)
                self.assertNotIn(b"ALTERED_AFTER", f.captured())
                verify_process(shell.shell)
            finally:
                verify_process(shell.tmux.server)
                subprocess.run([*tmux, "kill-window", "-t", window], capture_output=True, check=True, timeout=3)
        self.assertEqual(f.available()["terminal_id"], str(shell.terminal_id))
        with f.websocket() as exiting:
            current = received(exiting, "hello")["terminal"]["control"]
            control = take(exiting, current["epoch"])
            input_frame(exiting, b"exit\r", control["epoch"], 1)
        stopped = wait_for(lambda: (v if (v := f.terminal())["status"] == "stopped" else None))
        self.assertEqual(stopped["terminal_id"], str(shell.terminal_id))
        with self.assertRaises(InvalidStatus):
            f.websocket()
        self.assertEqual(httpx.get(f.url + "/health/ready", trust_env=False).status_code, 200)

    def test_workspace_and_recovery_configuration_fences_never_replace_shell(self):
        f = TerminalInstallation()
        self.addCleanup(f.close)
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        context = Invocation(actor_id=f.owner.actor_id)
        self.assertEqual(app.terminals.observe().status, "waiting_workspace")
        self.assertFalse(f.local.runtime_root.exists())
        app.jobs.run_next(worker_id=uuid4())
        marker = f.workspace.parent / ".qorqut-owner.json"
        original = marker.read_bytes()
        value = json.loads(original)
        value["operation_id"] = str(uuid4())
        marker.write_text(json.dumps(value))
        with patch.object(ShellManager, "ensure", side_effect=AssertionError("Unowned workspace reached shell")):
            self.assertEqual(app.terminals.observe().status, "unavailable")
        self.assertFalse(f.database.exists())
        marker.write_bytes(original)
        view = app.terminals.observe()
        self.assertEqual(view.status, "available")
        shell = f.shell()
        original_streams = app.terminals._streams
        app.terminals._streams = {uuid4(): (uuid4(), None) for _ in range(16)}
        try:
            with patch.object(ShellManager, "attach", side_effect=AssertionError("Capacity refusal attached")):
                with self.assertRaises(CoreError):
                    app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        finally:
            app.terminals._streams = original_streams
        stream = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        observer = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        control = stream.control("take", 0)
        self.assertEqual(stream.resize(132, 40, epoch=control.epoch, sequence=1).outcome, "submitted")
        for viewer in (stream, observer):
            data, closed, dimensions = viewer.read()
            self.assertFalse(closed)
            self.assertLessEqual(len(data), 65536)
            self.assertEqual(dimensions, ShellDimensions(columns=132, rows=40))
            self.assertEqual(termios.tcgetwinsize(viewer.attachment._fd), (40, 132))
        # Uncertain resize is never retried/promoted, but its actual settled
        # pane observation replaces the cached pre-effect dimensions.
        manager, original = app.terminals._manager, app.terminals._manager._effect
        def lost_resize(*args, **kwargs):
            original(*args, **kwargs)
            raise RunnerError("unavailable", "synthetic resize reply loss")
        with patch.object(manager, "_effect", side_effect=lost_resize) as effect:
            self.assertEqual(stream.resize(120, 35, epoch=control.epoch, sequence=2).outcome, "uncertain")
            self.assertEqual(effect.call_count, 1)
        for viewer in (stream, observer):
            self.assertEqual(viewer.read()[2], ShellDimensions(columns=120, rows=35))
        observer.close()
        stream.control("release", control.epoch)
        target, cached, observed = app.terminals._cache
        app.terminals._cache = target, cached, observed - 6
        with patch.object(ShellManager, "inspect", side_effect=AssertionError("Read probed processes")):
            stale = app.invoke("terminal_get", context, {"agent_id": f.agent_id}).data
        self.assertEqual((stale["status"], stale["recording"]["state"]), ("unavailable", "unknown"))
        with patch.object(stream.attachment, "read", side_effect=AssertionError("Stale viewer read output")), \
             patch.object(stream.attachment, "set_display_size", side_effect=AssertionError("Stale viewer resized display")):
            with self.assertRaises(CoreError):
                stream.state()
            with self.assertRaises(CoreError):
                stream.read()
        stream.close()
        app.close()
        changed_local = f.local.model_copy(update={"path": "/usr/bin"})
        changed = f.settings.model_copy(update={"local_terminal": changed_local})
        f.installation_file.write_text(changed.model_dump_json())
        changed_app = Application.open(changed)
        try:
            refusal = changed_app.terminals.observe()
            self.assertEqual((refusal.status, refusal.reason, refusal.terminal_id), ("unavailable", "target_changed", shell.terminal_id))
        finally:
            changed_app.close()
        f.installation_file.write_text(f.settings.model_dump_json())
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        self.assertEqual(app.terminals.observe().terminal_id, shell.terminal_id)
        stream = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        held = stream.control("take", f.manager().control(shell.terminal_id).epoch)
        result = app.activate_recovery(context, RecoveryActivation(expected_installation_id=f.settings.installation_id,
            expected_recovery_epoch=view.recovery_epoch, idempotency_key="terminal-recovery"))
        self.assertTrue(result.ok)
        with self.assertRaises(CoreError):
            stream.input(b"bad\r", epoch=held.epoch, sequence=1)
        with self.assertRaises(CoreError):
            stream.state()
        recovered = app.terminals.observe()
        self.assertEqual((recovered.status, recovered.reason, recovered.terminal_id), ("unavailable", "target_changed", shell.terminal_id))
        verify_process(shell.shell)
        self.assertEqual(f.manager().control(shell.terminal_id).last_sequence, 0)
        with sqlite3.connect(f.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runner_shell_terminals").fetchone()[0], 1)
        stream.close()
        self.assertTrue(stream.attachment.closed)
        self.assertEqual(app.terminals._streams, {})
        self.assertEqual(f.manager().control(shell.terminal_id), held)

    def test_close_releases_only_its_hold_and_always_cleans_up(self):
        f = TerminalInstallation()
        self.addCleanup(f.close)
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        app.jobs.run_next(worker_id=uuid4())
        app.terminals.observe()
        manager = app.terminals._manager
        shell = f.shell()
        old = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        held = old.control("take", 0)
        replacement = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        held = replacement.control("take", held.epoch)
        with patch.object(manager, "release_control", wraps=manager.release_control) as release:
            old.close()
            old.close()
            release.assert_not_called()
            self.assertTrue(old.attachment.closed)
            self.assertNotIn(old.viewer_id, app.terminals._streams)
            self.assertEqual(manager.control(shell.terminal_id), held)
            # A runner mirror disconnect alone retains control; core still owns
            # release and cleanup even when attachment.closed is already true.
            replacement.attachment.close()
            self.assertEqual(manager.control(shell.terminal_id), held)
            replacement.close()
            replacement.close()
            release.assert_called_once_with(shell.terminal_id, replacement.viewer_id, held.epoch)
        released = manager.control(shell.terminal_id)
        self.assertEqual((released.mode, released.epoch, released.viewer_id, released.last_sequence),
                         ("unclaimed", held.epoch + 1, None, 0))
        self.assertEqual(app.terminals._cache[1].control, released)
        self.assertEqual(app.terminals._streams, {})
        verify_process(shell.shell)
        verify_process(shell.recorder)

        stream = app.terminals.attach(UUID(f.agent_id), f.owner.actor_id)
        held = stream.control("take", released.epoch)
        close_attachment = stream.attachment.close
        def failed_cleanup():
            close_attachment()
            raise OSError("synthetic attachment cleanup failure")
        with patch.object(manager, "release_control", side_effect=RunnerError("revision_conflict", "synthetic release race")) as release:
            with patch.object(stream.attachment, "close", side_effect=failed_cleanup):
                with self.assertRaises(OSError):
                    stream.close()
            self.assertTrue(stream.attachment.closed)
            self.assertEqual(app.terminals._streams, {})
            self.assertEqual(manager.control(shell.terminal_id), held)
            self.assertEqual(app.terminals._cache[1].control, held)
            stream.close()
            release.assert_called_once_with(shell.terminal_id, stream.viewer_id, held.epoch)


class Socket:
    """Bounded ASGI send/receive seam for deterministic disconnect/backpressure."""
    def __init__(self, app, actor, agent_id, *, stalled=False, stall_hello=False):
        self.app = SimpleNamespace(state=SimpleNamespace(application=app))
        self.scope = {"qorqut.invocation": Invocation(actor_id=actor)}
        self.path_params = {"agent_id": agent_id}
        self.incoming, self.messages = asyncio.Queue(), asyncio.Queue()
        self.stalled = stalled
        self.stall_hello = stall_hello
        self.sending = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.accepted = False
        self.binary_sends = 0
        self.wire = []

    async def accept(self):
        self.accepted = True

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        if self.stall_hello and message["type"] == "hello":
            self.sending.set()
            await self.release.wait()
        await self.messages.put(message)
        self.wire.append(message)

    async def send_bytes(self, data):
        assert len(data) <= 65536
        self.binary_sends += 1
        self.wire.append(data)
        if self.stalled:
            self.sending.set()
            await self.release.wait()

    async def close(self, code):
        self.closed = True

    async def next(self, kind):
        async with asyncio.timeout(5):
            while True:
                message = await self.messages.get()
                if message["type"] == kind:
                    return message

    async def frame(self, message):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(message)})


class TerminalLifetime(OwnedProcesses, unittest.IsolatedAsyncioTestCase):
    async def fixture(self):
        f = TerminalInstallation()
        self.addCleanup(f.close)
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        await asyncio.to_thread(app.jobs.run_next, worker_id=uuid4())
        view = await asyncio.to_thread(app.terminals.observe)
        self.assertEqual(view.status, "available")
        sockets = TerminalSockets(Admission())
        self.addAsyncCleanup(sockets.close)
        return f, app, sockets

    async def test_uncertain_input_and_cancelled_shutdown_settle_owned_effect(self):
        f, app, sockets = await self.fixture()
        ws = Socket(app, f.owner.actor_id, f.agent_id)
        task = asyncio.create_task(sockets.endpoint(ws))
        hello = await ws.next("hello")
        await ws.frame({"type": "control", "action": "take", "expected_epoch": 0})
        control = (await ws.next("control"))["control"]
        manager = app.terminals._manager
        original = manager._effect

        def lost_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise RunnerError("unavailable", "synthetic reply loss")

        with patch.object(manager, "_effect", side_effect=lost_reply):
            await ws.frame({"type": "input", "epoch": control["epoch"], "sequence": 1,
                            "data": base64.b64encode(b"printf x >> once\r").decode()})
            receipt = await ws.next("frame")
        self.assertEqual(receipt["outcome"], "uncertain")
        await asyncio.to_thread(wait_for, lambda: (f.workspace / "once").exists())
        await ws.frame({"type": "input", "epoch": control["epoch"], "sequence": 1,
                        "data": base64.b64encode(b"printf x >> once\r").decode()})
        self.assertEqual((await ws.next("error"))["code"], "revision_conflict")
        self.assertEqual((f.workspace / "once").read_bytes(), b"x")
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def admitted(*args, **kwargs):
            original(*args, **kwargs)
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Owned effect barrier timed out")

        with patch.object(manager, "_effect", side_effect=admitted):
            await ws.frame({"type": "input", "epoch": control["epoch"], "sequence": 2,
                            "data": base64.b64encode(b"printf y >> once\r").decode()})
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            task.cancel()
            closing = asyncio.create_task(asyncio.to_thread(app.close))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertFalse(closing.done())
            self.assertFalse(ws.closed)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            await asyncio.wait_for(closing, 5)
        self.assertTrue(ws.closed)
        self.assertEqual(app.terminals._streams, {})
        self.assertEqual((f.workspace / "once").read_bytes(), b"xy")
        control = f.manager().control(UUID(hello["terminal"]["terminal_id"]))
        self.assertEqual((str(control.viewer_id), control.last_sequence), (hello["viewer_id"], 2))
        verify_process(f.shell().shell)

        # Unlike the synthetic successful-effect barrier above, this delays an
        # actual stale-control error with the runner's real durable input lock.
        app = Application.open(f.settings)
        self.addCleanup(app.close)
        await asyncio.to_thread(app.terminals.observe)
        ws = Socket(app, f.owner.actor_id, f.agent_id)
        task = asyncio.create_task(sockets.endpoint(ws))
        await ws.next("hello")
        await ws.frame({"type": "control", "action": "take", "expected_epoch": control.epoch})
        held = (await ws.next("control"))["control"]
        manager = app.terminals._manager
        inspect, take_control = manager.inspect, manager.take_control
        entered, failed = threading.Event(), threading.Event()
        settled = []
        def inspecting(*args, **kwargs):
            entered.set()
            return inspect(*args, **kwargs)
        def stale_control(*args, **kwargs):
            try:
                return take_control(*args, **kwargs)
            except RunnerError as error:
                settled.append(error.code)
                failed.set()
                raise
        with patch.object(manager, "inspect", side_effect=inspecting), \
             patch.object(manager, "take_control", side_effect=stale_control), \
             patch.object(manager, "release_control", wraps=manager.release_control) as release_control:
            with input_lock(f.database, UUID(hello["terminal"]["terminal_id"])):
                await ws.frame({"type": "control", "action": "take", "expected_epoch": control.epoch})
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                closing = asyncio.create_task(sockets.close())
                await asyncio.sleep(0.1)
                self.assertFalse(closing.done())
                self.assertFalse(ws.closed)
                release_control.assert_not_called()
            self.assertTrue(await asyncio.to_thread(failed.wait, 3))
            done, _ = await asyncio.wait({closing}, timeout=3)
            self.assertIn(closing, done, "Shutdown needed a second cancellation after the effect settled")
            await closing
            release_control.assert_called_once_with(UUID(hello["terminal"]["terminal_id"]), UUID(held["viewer_id"]), held["epoch"])
        self.assertEqual(settled, ["revision_conflict"])
        self.assertTrue(task.cancelled())
        self.assertTrue(ws.closed)
        self.assertEqual((sockets.tasks, app.terminals._streams), (set(), {}))
        released = manager.control(UUID(hello["terminal"]["terminal_id"]))
        self.assertEqual((released.mode, released.epoch, released.viewer_id, released.last_sequence),
                         ("unclaimed", held["epoch"] + 1, None, 0))
        self.assertEqual(app.terminals._cache[1].control, released)

    async def test_slow_output_idle_disconnect_and_recovery_close_only_viewers(self):
        f, app, sockets = await self.fixture()
        greeting = Socket(app, f.owner.actor_id, f.agent_id, stall_hello=True)
        task = asyncio.create_task(sockets.endpoint(greeting))
        await asyncio.wait_for(greeting.sending.wait(), 3)
        await sockets.close()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        self.assertTrue(greeting.closed)
        self.assertEqual(app.terminals._streams, {})
        # The writer must use the immutable size supplied with each read, even
        # when it carries no bytes. This synthetic read seam isolates ordering;
        # the owning shell/console checks separately use real PTYs and tmux.
        ordered = Socket(app, f.owner.actor_id, f.agent_id)
        task = asyncio.create_task(sockets.endpoint(ordered))
        hello = await ordered.next("hello")
        self.assertEqual(hello["protocol"], 2)
        stream = app.terminals._streams[UUID(hello["viewer_id"])][1]
        reads = iter(((b"OLD_GRID", False, ShellDimensions(columns=80, rows=24)),
                      (b"", False, ShellDimensions(columns=132, rows=40)),
                      (b"NEW_GRID", False, ShellDimensions(columns=132, rows=40))))
        with patch.object(stream, "read", side_effect=lambda: next(reads, (b"", False, None))):
            await ordered.next("size")
            async with asyncio.timeout(3):
                while b"NEW_GRID" not in ordered.wire:
                    await asyncio.sleep(0.01)
            begin = ordered.wire.index(b"OLD_GRID")
            self.assertEqual([item for item in ordered.wire[begin:] if isinstance(item, bytes) or item["type"] == "size"],
                [b"OLD_GRID", {"type": "size", "columns": 132, "rows": 40}, b"NEW_GRID"])
            await ordered.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(task, 3)
        ws = Socket(app, f.owner.actor_id, f.agent_id, stalled=True)
        with patch("qorqut_core.transports.server.TERMINAL_SEND_TIMEOUT", 0.15):
            task = asyncio.create_task(sockets.endpoint(ws))
            await ws.next("hello")
            await asyncio.wait_for(ws.sending.wait(), 3)
            await asyncio.wait_for(task, 3)
        self.assertEqual(ws.binary_sends, 1)
        self.assertTrue(ws.closed)
        self.assertEqual(app.terminals._streams, {})
        shell = f.shell()
        verify_process(shell.shell)
        # Real idle reads return b'' without closing the observer; an explicit
        # disconnect closes its viewer and releases only its control hold.
        ws = Socket(app, f.owner.actor_id, f.agent_id)
        task = asyncio.create_task(sockets.endpoint(ws))
        await ws.next("hello")
        await ws.next("state")
        await ws.next("state")
        self.assertFalse(ws.closed)
        await ws.frame({"type": "control", "action": "take", "expected_epoch": 0})
        held = (await ws.next("control"))["control"]
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, 3)
        self.assertTrue(ws.closed)
        released = f.manager().control(shell.terminal_id)
        self.assertEqual((released.mode, released.epoch, released.viewer_id), ("unclaimed", held["epoch"] + 1, None))
        ws = Socket(app, f.owner.actor_id, f.agent_id)
        task = asyncio.create_task(sockets.endpoint(ws))
        hello = await ws.next("hello")
        result = await asyncio.to_thread(app.activate_recovery, Invocation(actor_id=f.owner.actor_id),
            RecoveryActivation(expected_installation_id=f.settings.installation_id,
                expected_recovery_epoch=hello["terminal"]["recovery_epoch"], idempotency_key="stream-recovery"))
        self.assertTrue(result.ok)
        await asyncio.wait_for(task, 3)
        self.assertTrue(ws.closed)
        verify_process(shell.shell)

    async def test_origin_identity_boundary_and_observation_loop_readiness(self):
        f = Installation()
        self.addCleanup(f.close)
        settings = read_configuration(f.installation_file)
        admission = Admission()
        called = []
        async def downstream(scope, receive, send):
            called.append(scope)
        boundary = Boundary(downstream, port=settings.listen_port, admission=admission)
        headers = [(b"host", f.url.removeprefix("http://").encode()), (b"origin", f.url.encode()),
                   (b"x-qorqut-actor", str(f.owner.actor_id).encode())]
        scope = {"type": "websocket", "path": "/unknown", "headers": headers, "query_string": b""}
        for extra in ([(b"host", headers[0][1])], [(b"origin", f.url.encode())], [(b"x-qorqut-actor", headers[2][1])]):
            sent = []
            async def record(message):
                sent.append(message)
            await boundary({**scope, "headers": headers + extra}, None, record)
            self.assertEqual(sent[0]["type"], "websocket.close")
        self.assertEqual(called, [])
        local = SimpleNamespace(state=SimpleNamespace(local_owner=f.owner.actor_id))
        for identity, actor in (([], f.owner.actor_id),
                ([(b"x-qorqut-actor", str(f.agent.actor_id).encode())], f.agent.actor_id)):
            await boundary({**scope, "app": local, "headers": headers[:2] + identity + [
                (b"authorization", b"Bearer obsolete"), (b"cookie", b"qorqut_session=obsolete")]}, None, record)
            self.assertEqual(called[-1]["qorqut.invocation"].actor_id, actor)
        web = create_app(settings)
        async with web.router.lifespan_context(web):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url=f.url) as client:
                deadline = time.monotonic() + 5
                while (await client.get("/health/ready")).status_code != 200:
                    self.assertLess(time.monotonic(), deadline)
                    await asyncio.sleep(0.02)
                service = web.state.application.terminals
                with patch.object(service, "observe", side_effect=RuntimeError("PRIVATE FAILURE")):
                    while service.ready:
                        self.assertLess(time.monotonic(), deadline)
                        await asyncio.sleep(0.02)
                    self.assertEqual((await client.get("/health/ready")).status_code, 503)
                self.assertEqual((await client.get("/health/live")).status_code, 200)
