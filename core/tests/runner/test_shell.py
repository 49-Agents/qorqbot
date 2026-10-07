"""Owning boundary checks with disposable real tmux, Bash, PTYs and recording."""

import ctypes
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from qorqut_core.runner.models import RunnerError
from qorqut_core.runner.process import fingerprint, verify_process
from qorqut_core.runner.shell import ShellManager, ShellSpec, ShellView
from qorqut_core.runner.shell_models import ShellDimensions
from qorqut_core.runner.storage import input_lock
from qorqut_core.transcripts.spool import CaptureSpool


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("owned shell fixture did not reach expected state")


class ShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Adopt only descendants of this test process so tmux's daemonized
        # fixture processes can be reaped here, without depending on host PID 1.
        cls.libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if cls.libc.prctl(37, ctypes.byref(previous), 0, 0, 0):
            raise OSError(ctypes.get_errno(), "get child subreaper")
        cls.previous_subreaper = previous.value
        if cls.libc.prctl(36, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "set child subreaper")

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.previous_subreaper, 0, 0, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qsh-")
        self.root = Path(self.temp.name)
        # Exercise argv handling for Unicode, whitespace, tmux formats, shell
        # substitution and the tmux argv parser's trailing-semicolon case.
        self.home = self.root / "HOME ü $(touch SHOULD_NOT_EXIST);"
        self.workspace = self.root / "work #{pid} ' ü;"
        self.home.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        self.args = (self.root / "spool.sqlite3", self.root / "runtime", self.root / "s",
                     Path(sys.executable), Path("/usr/bin/tmux"), Path("/usr/bin/bash"))
        self.spec = ShellSpec(installation_id=uuid4(), agent_id=uuid4(), recovery_epoch=uuid4(),
                              environment_generation=1, home=self.home, workspace=self.workspace,
                              path="/usr/bin:/bin:/unused ü $(touch PATH_NOT_EXECUTED);")
        self.manager = ShellManager(*self.args)
        self.attachments = []
        self.views = []
        self.children = []

    def tearDown(self):
        for attachment in self.attachments:
            attachment.close()
        for view in self.views:
            try:
                self.manager.shutdown(view.terminal_id)
            except RunnerError:
                # Only positively recorded fixture processes are eligible for
                # fallback cleanup after an intentionally damaged binding.
                for expected in (view.shell, view.recorder, view.tmux.server if view.tmux else None):
                    if expected is not None:
                        self.signal_owned(expected, signal.SIGKILL, missing_ok=True)
        for expected in self.children:
            self.signal_owned(expected, signal.SIGKILL, missing_ok=True)
        # Every remaining child here is a descendant of this disposable test
        # process (including tmux's short-lived double-fork intermediates).
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                time.sleep(0.02)
        else:
            for view in self.views:
                for expected in (view.shell, view.recorder, view.tmux.server if view.tmux else None):
                    if expected is not None:
                        self.signal_owned(expected, signal.SIGKILL, missing_ok=True)
            # Failing tests still clean only their positively recorded processes.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    if not os.waitpid(-1, os.WNOHANG)[0]:
                        time.sleep(0.02)
                except ChildProcessError:
                    break
            self.temp.cleanup()
            self.fail("owned fixture children were not reaped")
        self.temp.cleanup()

    @staticmethod
    def signal_owned(expected, sig, missing_ok=False):
        try:
            fd = os.pidfd_open(expected.pid)
            try:
                verify_process(expected)
                signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        except (ProcessLookupError, RunnerError):
            if not missing_ok:
                raise

    def start(self):
        view = self.manager.ensure(self.spec)
        self.views.append(view)
        self.assertEqual(view.state, "running", view.reason)
        return view

    def attach(self, view, viewer=None, *, read_only=True):
        result = self.manager.attach(view.terminal_id, viewer or uuid4(), read_only=read_only)
        self.attachments.append(result)
        return result

    def captured(self, view):
        with CaptureSpool(self.args[0]) as spool:
            data, offset = b"", 0
            while True:
                page = spool.pending(str(view.source_id), str(view.source_epoch), after=offset)
                if not page.chunks:
                    return data
                data += b"".join(chunk.payload for chunk in page.chunks)
                offset = page.next_offset

    def test_arguments_and_initialization_are_passive_and_bounded(self):
        with patch("subprocess.Popen", side_effect=AssertionError("constructor launched a process")):
            ShellManager(*self.args).close()
        for change in ({"provider": "codex"}, {"environment_generation": True},
                       {"path": "x\0y"}, {"home": self.workspace}, {"workspace": Path("relative")}):
            with self.assertRaises((ValidationError, ValueError)):
                ShellSpec.model_validate({**self.spec.model_dump(), **change})
        with self.assertRaises(ValueError):
            ShellManager(*(*self.args[:2], self.root / ("s" * 110), *self.args[3:]))
        with self.assertRaises(ValueError):
            ShellManager(*self.args[:-1], "/usr/bin/true")
        with sqlite3.connect(self.args[0]) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM runner_shell_terminals").fetchone()[0], 0)
        self.assertFalse(self.args[2].exists())
        self.assertEqual(list(self.home.iterdir()), [])
        for columns, rows in ((True, 24), (80, False), (80.0, 24), ("80", 24),
                              (19, 24), (501, 24), (80, 4), (80, 251)):
            with self.assertRaises(ValidationError):
                ShellDimensions(columns=columns, rows=rows)
        dimensions = ShellDimensions(columns=80, rows=24)
        with self.assertRaises(ValidationError):
            dimensions.columns = 100

    def test_verified_geometry_and_local_mirror_size_are_independent_of_input(self):
        view = self.start()
        self.assertEqual(view.dimensions, ShellDimensions(columns=80, rows=24))
        writer = self.attach(view, read_only=False)
        control = self.manager.take_control(view.terminal_id, writer.viewer_id, 0)
        self.assertEqual(writer.resize(132, 40, epoch=control.epoch, sequence=1).outcome, "submitted")
        wide = self.manager.inspect(view.terminal_id)
        self.assertEqual(wide.dimensions, ShellDimensions(columns=132, rows=40))
        self.assertEqual(termios.tcgetwinsize(writer._fd), (40, 132))
        writer.write(b"printf '\\033[2J\\033[1;100H%s\\033[35;1H%s\\033[2;1H' RIGHT_TAIL_217 BOTTOM_ROW_217\r",
                     epoch=control.epoch, sequence=2)
        wait_for(lambda: b"RIGHT_TAIL_217\x1b[35;1HBOTTOM_ROW_217" in self.captured(view))
        screen = self.manager._command("capture-pane", "-p", "-t", view.tmux.pane_id).splitlines()
        self.assertEqual(screen[0][99:], b"RIGHT_TAIL_217")
        self.assertEqual(screen[34], b"BOTTOM_ROW_217")
        observer = self.attach(wide)
        self.assertEqual(observer.dimensions, wide.dimensions)
        self.assertEqual(termios.tcgetwinsize(observer._fd), (40, 132))
        def client_size(attachment):
            lines = self.manager._command("list-clients", "-F",
                "#{client_pid}|#{client_width}|#{client_height}|#{client_flags}").decode().splitlines()
            return next(line.split("|")[1:] for line in lines if line.startswith(f"{attachment._process.pid}|"))
        wait_for(lambda: client_size(observer)[:2] == ["132", "40"])
        self.assertTrue({"read-only", "ignore-size"}.issubset(client_size(observer)[2].split(",")))
        display = bytearray()
        def visible():
            display.extend(observer.read())
            return b"RIGHT_TAIL_217" in display and b"BOTTOM_ROW_217" in display
        wait_for(visible)
        baseline, held = self.captured(view), self.manager.control(view.terminal_id)
        # This display-only path must finish while a different thread owns the
        # actual runner flock. No tmux/process query or PTY input is permitted.
        with ThreadPoolExecutor(max_workers=1) as pool, input_lock(self.args[0], view.terminal_id):
            with patch.object(self.manager, "_command", side_effect=AssertionError("Display resize queried tmux")), \
                 patch("qorqut_core.runner.shell.verify_process", side_effect=AssertionError("Display resize probed processes")), \
                 patch("os.write", side_effect=AssertionError("Display resize wrote PTY input")), \
                 patch.object(observer._process, "send_signal", wraps=observer._process.send_signal) as notified, \
                 patch("termios.tcsetwinsize", wraps=termios.tcsetwinsize) as sized:
                pool.submit(observer.set_display_size, 90, 28).result(timeout=1)
                pool.submit(observer.set_display_size, 90, 28).result(timeout=1)
                notified.assert_called_once_with(signal.SIGWINCH)
                sized.assert_called_once_with(observer._fd, (28, 90))
        wait_for(lambda: client_size(observer)[:2] == ["90", "28"])
        self.assertEqual(termios.tcgetwinsize(observer._fd), (28, 90))
        self.assertEqual(self.manager.inspect(view.terminal_id).dimensions, wide.dimensions)
        self.assertEqual(self.manager.control(view.terminal_id), held)
        self.assertEqual(self.captured(view), baseline)
        for columns, rows in ((True, 24), (80, "24"), (19, 24), (501, 24), (80, 4), (80, 251)):
            with self.assertRaises(RunnerError):
                observer.set_display_size(columns, rows)
        self.assertEqual(observer.dimensions, ShellDimensions(columns=90, rows=28))
        # Both immutable observations and attached geometry must fail closed on
        # malformed/out-of-bound tmux output, including the final target check.
        original = self.manager._command
        for geometry in (b"bad|40", b"19|40", b"501|40", b"132|4", b"132|251"):
            calls = 0
            def malformed(*args, **kwargs):
                nonlocal calls
                result = original(*args, **kwargs)
                if args[0] == "list-panes":
                    calls += 1
                    if calls == 2:
                        return result.rsplit(b"|", 2)[0] + b"|" + geometry + b"\n"
                return result
            with patch.object(self.manager, "_command", side_effect=malformed):
                unknown = self.manager.inspect(view.terminal_id)
            self.assertEqual(unknown.state, "unknown")
            self.assertIsNone(unknown.dimensions)
        observer.close()
        with self.assertRaises(RunnerError):
            observer.set_display_size(90, 28)

    def test_ensure_reopen_capture_and_attachment_flow(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic-not-for-child",
                                    "OPENAI_API_KEY": "synthetic-not-for-child",
                                    "BASH_ENV": "/never/source/this", "CODEX_HOME": "/not/the/managed/home"}):
            view = self.start()
        again = self.manager.ensure(self.spec)
        self.assertEqual((again.terminal_id, again.tmux, again.shell), (view.terminal_id, view.tmux, view.shell))
        self.assertEqual(again.control.mode, "unclaimed")
        self.assertTrue(view.capture.verified and view.capture.terminal_active)
        self.assertNotEqual(view.shell, view.recorder)
        self.assertEqual(Path(f"/proc/{view.shell.pid}/cwd").resolve(), self.workspace)
        environment = dict(item.split(b"=", 1) for item in Path(f"/proc/{view.shell.pid}/environ").read_bytes().split(b"\0") if item)
        for key in (b"ANTHROPIC_API_KEY", b"OPENAI_API_KEY", b"BASH_ENV"):
            self.assertNotIn(key, environment)
        self.assertEqual(environment[b"HOME"], os.fsencode(self.home))
        self.assertEqual(environment[b"PATH"], self.spec.path.encode())
        self.assertEqual(environment[b"CODEX_HOME"], os.fsencode(self.home / ".codex"))
        self.assertEqual(environment[b"TERM"], b"xterm-256color")
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertFalse((self.workspace / "CLAUDE.md").exists())
        self.assertFalse((self.home / ".codex/config.toml").exists())
        self.assertFalse((self.home / ".claude/settings.json").exists())
        self.assertEqual(Path(f"/proc/{view.recorder.pid}/task/{view.recorder.pid}/children").read_text().split(),
                         [str(view.shell.pid)])
        self.assertEqual(Path(f"/proc/{view.shell.pid}/task/{view.shell.pid}/children").read_text().split(), [])
        writer = self.attach(view, read_only=False)
        control = self.manager.take_control(view.terminal_id, writer.viewer_id, 0)
        result = writer.write("printf '%s\\n' 'literal ü ☃ $(not executed)'\r".encode(), epoch=control.epoch, sequence=1)
        self.assertEqual(result.outcome, "submitted")
        wait_for(lambda: b"literal \xc3\xbc \xe2\x98\x83 $(not executed)\r\n" in self.captured(view))
        # A real viewer-process exit leaves both recording and Bash alive.
        writer.close()
        self.manager.close()
        self.manager = ShellManager(*self.args)
        reopened = self.manager.ensure(self.spec)
        self.assertEqual((reopened.tmux, reopened.shell), (view.tmux, view.shell))
        self.assertEqual(reopened.control.viewer_id, writer.viewer_id)
        observer = self.attach(view)
        display = bytearray()
        def displayed():
            display.extend(observer.read())
            return b"literal" in display and "☃".encode() in display
        wait_for(displayed)
        with sqlite3.connect(self.args[0]) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runner_shell_terminals").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT run_id,run_generation FROM capture_runs").fetchall(), [(str(view.terminal_id), 1)])
            self.assertEqual(db.execute("SELECT source_type,native_session_id FROM capture_sources").fetchall(), [("terminal", None)])

    def manager_process(self, *, crash=False):
        program = """
import json, os, sys
from qorqut_core.runner.shell import ShellManager, ShellSpec
request = json.load(sys.stdin)
manager = ShellManager(*request['args'])
if request['crash']:
    launch = manager._launch
    def interrupted(row):
        launch(row)
        os._exit(71)
    manager._launch = interrupted
print(manager.ensure(ShellSpec.model_validate(request['spec'])).model_dump_json(), flush=True)
"""
        return subprocess.run([sys.executable, "-I", "-c", program],
                              input=json.dumps({"args": [str(arg) for arg in self.args], "crash": crash,
                                                "spec": self.spec.model_dump(mode="json")}),
                              text=True, capture_output=True, timeout=15,
                              env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"})

    def test_concurrent_managers_exit_and_reconnect_while_shell_child_survives(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.manager_process(), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        first, second = [ShellView.model_validate_json(result.stdout) for result in results]
        self.views.append(first)
        self.assertEqual((first.terminal_id, first.shell, first.tmux), (second.terminal_id, second.shell, second.tmux))
        self.assertEqual(first.state, "running")
        writer = self.attach(first, read_only=False)
        control = self.manager.take_control(first.terminal_id, writer.viewer_id, 0)
        result = writer.write(b"sleep 30 & echo $! > child.pid; (sleep 0.3; printf 'WHILE_AWAY\\n') &\r",
                              epoch=control.epoch, sequence=1)
        self.assertEqual(result.outcome, "submitted")
        wait_for(lambda: (self.workspace / "child.pid").exists())
        child = fingerprint(int((self.workspace / "child.pid").read_text()))
        self.children.append(child)
        writer.close()
        self.manager.close()
        self.manager = ShellManager(*self.args)
        recovered = self.manager.ensure(self.spec)
        self.assertEqual((recovered.shell, recovered.tmux), (first.shell, first.tmux))
        verify_process(child)
        self.assertEqual(recovered.control.viewer_id, writer.viewer_id)
        wait_for(lambda: b"WHILE_AWAY\r\n" in self.captured(first))
        self.attach(first).close()
        verify_process(child)
        verify_process(first.shell)
        self.signal_owned(child, signal.SIGTERM)
        with self.assertRaises(RunnerError):
            self.manager.ensure(self.spec.model_copy(update={"path": "/bin"}))
        with self.assertRaises(RunnerError):
            self.manager.ensure(self.spec.model_copy(update={"agent_id": uuid4()}))

    def test_observers_control_takeover_stale_frames_resize_and_exit(self):
        view = self.start()
        wait_for(lambda: b"bash-" in self.captured(view))
        baseline = self.captured(view)
        observer1, observer2 = self.attach(view), self.attach(view)
        writer1, writer2 = self.attach(view, read_only=False), self.attach(view, read_only=False)
        time.sleep(0.1)
        self.assertEqual(self.captured(view), baseline, "attach initialization reached the shell")
        self.assertFalse(os.get_blocking(writer1._fd))
        for observer in (observer1, observer2):
            with self.assertRaises(RunnerError):
                observer.write(b"bad\r", epoch=0, sequence=1)
            with self.assertRaises(RunnerError):
                observer.resize(100, 30, epoch=0, sequence=1)
            with self.assertRaises(RunnerError):
                observer.read(65537)
        first = self.manager.take_control(view.terminal_id, writer1.viewer_id, 0)
        # Ctrl-U is an explicit physical-owner control, not sanitized text or
        # bracketed paste. Leave the replacement command unsubmitted for takeover.
        writer1.write(b"discard this\x15printf preserved > preserved", epoch=first.epoch, sequence=1)
        delayed = threading.Event()
        def stale_frame():
            self.assertTrue(delayed.wait(3))
            with self.assertRaises(RunnerError):
                writer1.write(b"\r", epoch=first.epoch, sequence=2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(stale_frame)
            second = self.manager.take_control(view.terminal_id, writer2.viewer_id, first.epoch)
            delayed.set()
            waiting.result(timeout=5)
        with self.assertRaises(RunnerError):
            self.manager.release_control(view.terminal_id, writer1.viewer_id, first.epoch)
        self.assertFalse((self.workspace / "preserved").exists())  # No implicit Enter or clearing typed text.
        writer2.write(b"\r", epoch=second.epoch, sequence=1)
        wait_for(lambda: (self.workspace / "preserved").exists())
        self.assertEqual((self.workspace / "preserved").read_text(), "preserved")
        self.assertEqual(writer2.resize(113, 31, epoch=second.epoch, sequence=2).outcome, "submitted")
        writer2.write(b"stty size > dimensions\r", epoch=second.epoch, sequence=3)
        wait_for(lambda: (self.workspace / "dimensions").exists() and (self.workspace / "dimensions").read_text() == "31 113\n")
        for payload in (b"", b"x" * 65537, "text"):
            with self.assertRaises(RunnerError):
                writer2.write(payload, epoch=second.epoch, sequence=4)
        with self.assertRaises(RunnerError):
            writer2.resize(True, 30, epoch=second.epoch, sequence=4)
        released = self.manager.release_control(view.terminal_id, writer2.viewer_id, second.epoch)
        self.assertEqual(released.mode, "unclaimed")
        self.assertIsNone(released.viewer_id)
        with self.assertRaises(RunnerError):
            writer2.write(b"bad\r", epoch=second.epoch, sequence=4)
        final = self.manager.take_control(view.terminal_id, writer2.viewer_id, released.epoch)
        writer2.write(b"exit\r", epoch=final.epoch, sequence=1)
        wait_for(lambda: self.manager.inspect(view.terminal_id).state == "stopped")
        with patch.object(self.manager, "_launch", side_effect=AssertionError("stopped shell restarted")):
            stopped = self.manager.ensure(self.spec)
        self.assertEqual(stopped.state, "stopped")
        self.assertFalse(stopped.capture.terminal_active)
        with self.assertRaises(RunnerError):
            self.attach(stopped)

    def test_ambiguous_launch_recovers_same_attempt_from_live_markers(self):
        crashed = self.manager_process(crash=True)
        self.assertEqual(crashed.returncode, 71, crashed.stderr)
        with sqlite3.connect(self.args[0]) as db:
            row = db.execute("SELECT terminal_id,tmux_json,shell_json FROM runner_shell_terminals").fetchone()
            self.assertEqual(row[1:], (None, None))
        with patch.object(self.manager, "_launch", side_effect=AssertionError("attempt replayed")):
            view = wait_for(lambda: (v if (v := self.manager.ensure(self.spec)).state == "running" else None))
        self.views.append(view)
        self.assertEqual(str(view.terminal_id), row[0])
        marker = self.manager._row(view.terminal_id)["launch_marker"]
        self.manager._command("set-environment", "-g", "QORQUT_SHELL_LAUNCH", "different")
        try:
            self.assertEqual(self.manager.ensure(self.spec).state, "unknown")
            with self.assertRaises(RunnerError):
                self.attach(view)
            self.assertEqual(self.manager.shutdown(view.terminal_id).state, "unknown")
            verify_process(view.shell)  # Ambiguous shutdown did not kill/adopt.
        finally:
            self.manager._command("set-environment", "-g", "QORQUT_SHELL_LAUNCH", marker)
        self.assertEqual(self.manager.ensure(self.spec).shell, view.shell)

    def test_failed_launch_is_held_and_foreign_socket_is_never_used(self):
        with socket.socket(socket.AF_UNIX) as foreign:
            foreign.bind(str(self.args[2]))
            with self.assertRaises(RunnerError):
                self.manager.ensure(self.spec)
            self.assertTrue(self.args[2].is_socket())
        self.args[2].unlink()  # This test itself bound that exact socket.
        with patch.object(self.manager, "_launch", side_effect=OSError("injected prelaunch failure")):
            unknown = self.manager.ensure(self.spec)
        self.views.append(unknown)
        self.assertEqual(unknown.state, "unknown")
        with patch.object(self.manager, "_launch", side_effect=AssertionError("uncertain slot replayed")):
            again = ShellManager(*self.args).ensure(self.spec)
        self.assertEqual((again.state, again.terminal_id), ("unknown", unknown.terminal_id))
        self.assertFalse(self.args[2].exists())
        # Different DBs cannot claim the same runtime/socket, either.
        other = ShellManager(self.root / "other.sqlite3", *self.args[1:])
        with self.assertRaises(RunnerError):
            other.ensure(self.spec.model_copy(update={"agent_id": uuid4()}))

    def test_input_uncertainty_consumes_sequence_without_replay_and_recorder_failure(self):
        view = self.start()
        writer = self.attach(view, read_only=False)
        control = self.manager.take_control(view.terminal_id, writer.viewer_id, 0)
        effect = self.manager._effect
        def lost_reply(target, argv, **kwargs):
            effect(target, argv, **kwargs)
            raise RunnerError("unavailable", "injected reply loss after input")
        with patch.object(self.manager, "_effect", side_effect=lost_reply):
            receipt = writer.write(b"printf x >> once\r", epoch=control.epoch, sequence=1)
        self.assertEqual(receipt.outcome, "uncertain")
        wait_for(lambda: (self.workspace / "once").exists())
        writer.close()
        self.assertEqual(self.manager.control(view.terminal_id).last_sequence, 1)
        replacement = self.attach(view, writer.viewer_id, read_only=False)
        with self.assertRaises(RunnerError):
            replacement.write(b"printf x >> once\r", epoch=control.epoch, sequence=1)
        self.assertEqual((self.workspace / "once").read_bytes(), b"x")
        # tmux resumes SIGSTOP'd pane leaders. Instead block this recorder's
        # next append with a short, positively owned SQLite writer hold.
        replacement.write(b"(sleep 0.2; printf CAPTURE_WAIT) &\r", epoch=control.epoch, sequence=2)
        with sqlite3.connect(self.args[0]) as held:
            held.execute("BEGIN IMMEDIATE")
            time.sleep(0.3)
            unknown = self.manager.ensure(self.spec)
            self.assertEqual(unknown.state, "unknown")
            self.assertEqual(unknown.saved_capture.health, "healthy")
            self.assertFalse(unknown.capture.verified)
            with self.assertRaises(RunnerError):
                replacement.write(b"printf bad\r", epoch=control.epoch, sequence=3)
            self.assertEqual(self.manager.control(view.terminal_id).last_sequence, 2)
        self.assertEqual(self.manager.ensure(self.spec).state, "running")
        self.signal_owned(view.recorder, signal.SIGKILL)
        wait_for(lambda: self.manager.inspect(view.terminal_id).state != "running")
        with patch.object(self.manager, "_launch", side_effect=AssertionError("recorder failure restarted shell")):
            failed = self.manager.ensure(self.spec)
        self.assertIn(failed.state, ("unknown", "stopped"))
        self.assertFalse(failed.capture.terminal_active)
