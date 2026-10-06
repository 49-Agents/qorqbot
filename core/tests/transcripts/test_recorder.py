import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unittest

from qorqut_core.transcripts.recorder import Recorder
from qorqut_core.transcripts.spool import CaptureBlocked, CaptureSpool
from support import binding, child_env, cli_args, new_id, raw, registered


class NestedTerminal:
    """Real outer PTY -> recorder -> real child controlling PTY."""

    def __init__(self, path, program, *args):
        self.path = path
        self.run = binding()
        self.source_id, self.epoch = new_id(), new_id()
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.attributes = termios.tcgetattr(self.slave)
        self.size(24, 80)
        self.process = subprocess.Popen(
            cli_args(path, self.run, self.source_id, self.epoch, [sys.executable, "-c", program, *args]),
            stdin=self.slave, stdout=self.slave, stderr=self.slave,
            start_new_session=True, close_fds=True, env=child_env(),
        )
        os.set_blocking(self.master, False)
        self.output = bytearray()

    def size(self, rows, columns):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))

    def read_until(self, marker=None, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker is not None and marker in self.output:
                return bytes(self.output)
            readable, _, _ = select.select([self.master], [], [], 0.02)
            if readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b""
                if data:
                    self.output.extend(data)
                    continue
            if self.process.poll() is not None:
                if marker is not None and marker not in self.output:
                    raise AssertionError(f"child exited before marker; output={self.output!r}")
                return bytes(self.output)
        raise AssertionError(f"nested PTY did not finish; {len(self.output)} output bytes")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        os.close(self.master)
        os.close(self.slave)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "spool.sqlite3"

    def terminal(self, code, *args):
        terminal = NestedTerminal(self.path, code, *args)
        self.addCleanup(terminal.close)
        return terminal

    def assert_captured(self, terminal, expected, exit_code):
        self.assertEqual(terminal.process.wait(timeout=5), exit_code)
        self.assertEqual(bytes(terminal.output), expected)
        self.assertEqual(raw(self.path, terminal.source_id, terminal.epoch), expected)
        with CaptureSpool(self.path) as spool:
            state = spool.state(terminal.source_id, terminal.epoch)
            self.assertEqual(state["local_captured_offset"], len(expected))
            self.assertEqual(state["host_archived_offset"], 0)
            self.assertEqual(state["finalized_offset"], len(expected))
            self.assertEqual(state["drain_complete"], 1)
            self.assertEqual(state["health"], "healthy" if exit_code == 0 else "incomplete")
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.attributes)

    def test_real_nested_pty_exact_unicode_binary_giant_and_partial_output(self):
        program = r'''
import os, tty
tty.setraw(0)
def emit(data):
    while data:
        data = data[os.write(1, data):]
emit("Unicode: 🧭 á 中文\n".encode())
emit(bytes(range(256)))
emit(b'{"tool":"' + b"x" * (9 * 1024 * 1024) + b'"}\n')
emit(b'{"partial":"\xf0\x9f')
os._exit(0)
'''
        terminal = self.terminal(program)
        terminal.read_until(timeout=30)
        expected = ("Unicode: 🧭 á 中文\n".encode() + bytes(range(256))
                    + b'{"tool":"' + b"x" * (9 * 1024 * 1024) + b'"}\n'
                    + b'{"partial":"\xf0\x9f')
        self.assert_captured(terminal, expected, 0)

    def test_input_unicode_bracketed_paste_and_literal_argv_remain_terminal_only(self):
        data = "\x1b[200~multiline\n🧭 and 'quotes' $HOME `cmd`\x1b[201~".encode()
        literal = "literal $(touch forbidden) `echo no`; > file"
        program = r'''
import os, sys, tty
tty.setraw(0)
os.write(1, b"READY\n")
data = b""
while len(data) < int(sys.argv[1]):
    data += os.read(0, int(sys.argv[1]) - len(data))
os.write(1, b"RECEIVED:" + data + b"\nARGV:" + sys.argv[2].encode())
'''
        terminal = self.terminal(program, str(len(data)), literal)
        terminal.read_until(b"READY\n")
        os.write(terminal.master, data)
        terminal.read_until()
        self.assert_captured(terminal, b"READY\nRECEIVED:" + data + b"\nARGV:" + literal.encode(), 0)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT source_type, native_session_id FROM capture_sources").fetchall(),
                             [("terminal", None)])

    def test_manual_input_is_not_captured_when_child_does_not_echo(self):
        terminal = self.terminal(r'''
import os, tty
tty.setraw(0)
os.write(1, b"READY")
os.read(0, 32)
os.write(1, b"DONE")
''')
        terminal.read_until(b"READY")
        os.write(terminal.master, b"private manual input")
        terminal.read_until()
        self.assert_captured(terminal, b"READYDONE", 0)

    def test_resize_foreground_signals_and_controlling_terminal(self):
        program = r'''
import fcntl, os, signal, struct, termios, tty
tty.setraw(0)
def size(*_):
    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
    os.write(1, f"SIZE:{rows},{cols}\n".encode())
def interrupt(*_): os.write(1, b"INT\n")
def terminate(*_):
    os.write(1, b"TERM\n")
    raise SystemExit(23)
signal.signal(signal.SIGWINCH, size)
signal.signal(signal.SIGINT, interrupt)
signal.signal(signal.SIGTERM, terminate)
assert os.tcgetpgrp(0) == os.getpgrp()
assert os.getsid(0) == os.getpid()
size()
while True: signal.pause()
'''
        terminal = self.terminal(program)
        terminal.read_until(b"SIZE:24,80\n")
        terminal.size(41, 132)
        terminal.process.send_signal(signal.SIGWINCH)
        terminal.read_until(b"SIZE:41,132\n")
        terminal.process.send_signal(signal.SIGINT)
        terminal.read_until(b"INT\n")
        terminal.process.send_signal(signal.SIGTERM)
        terminal.read_until()
        self.assert_captured(terminal, b"SIZE:24,80\nSIZE:41,132\nINT\nTERM\n", 23)

    def test_child_signal_exit_drains_buffered_tail_and_preserves_signal_status(self):
        terminal = self.terminal(r'''
import os, signal, tty
tty.setraw(0)
data = b"tail-" * 50000 + b"partial"
while data: data = data[os.write(1, data):]
os.kill(os.getpid(), signal.SIGKILL)
''')
        terminal.read_until()
        self.assert_captured(terminal, b"tail-" * 50000 + b"partial", 128 + signal.SIGKILL)
        with CaptureSpool(self.path) as spool:
            self.assertEqual(spool.state(terminal.source_id, terminal.epoch)["child_exit_code"], -signal.SIGKILL)

    def test_ordering_seam_uses_independent_committed_reads_and_handles_partial_writes(self):
        spool, source, epoch, run = registered(self.path, kind="terminal")
        self.addCleanup(spool.close)
        recorder = Recorder(spool, source.source_id, epoch, run.run_id)
        input_fd = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, input_fd)
        with tempfile.TemporaryFile() as output:
            emitted = bytearray()
            def display(fd, data):
                # A separate connection cannot see an uncommitted chunk/cursor.
                with sqlite3.connect(self.path) as observer:
                    captured = observer.execute("SELECT local_captured_offset FROM capture_epochs").fetchone()[0]
                    durable = b"".join(row[0] for row in observer.execute(
                        "SELECT payload FROM capture_chunks ORDER BY offset"))
                self.assertGreaterEqual(captured, len(emitted) + len(data))
                self.assertEqual(durable[len(emitted):len(emitted) + len(data)], data)
                count = os.write(fd, data[:17])
                emitted.extend(data[:count])
                return count
            code = "import os,tty; tty.setraw(0); os.write(1, b'exact-\\xf0\\x9f\\xa7\\xad' * 1000)"
            self.assertFalse(recorder.admission_ready)
            self.assertEqual(recorder.run([sys.executable, "-c", code], input_fd=input_fd,
                                          output_fd=output.fileno(), display_write=display), 0)
            self.assertFalse(recorder.admission_ready)
            self.assertEqual(emitted, b"exact-\xf0\x9f\xa7\xad" * 1000)
            with self.assertRaises(CaptureBlocked):
                recorder.run([sys.executable, "-c", "raise RuntimeError('must not launch')"],
                             input_fd=input_fd, output_fd=output.fileno())

    def test_capture_failure_blocks_forwarding_and_new_launch(self):
        spool, source, epoch, run = registered(self.path, kind="terminal")
        self.addCleanup(spool.close)
        recorder = Recorder(spool, source.source_id, epoch, run.run_id)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        tty.setraw(slave)
        seen = bytearray()
        with tempfile.TemporaryFile() as output:
            def display(fd, data):
                seen.extend(data)
                # After committed READY, force the next chunk/cursor transaction
                # to roll back. The input here precedes failure; readiness is
                # checked after failure, not rejection of unattempted input.
                with sqlite3.connect(self.path) as connection:
                    connection.execute("""CREATE TRIGGER fail_append BEFORE INSERT ON capture_chunks
                        BEGIN SELECT RAISE(ABORT, 'injected full disk'); END""")
                os.write(master, b"go")
                return os.write(fd, data)
            program = r'''
import os, tty
tty.setraw(0)
os.write(1, b"READY")
os.read(0, 2)
os.write(1, b"UNCOMMITTED")
os.read(0, 16)
os.write(1, b"ADMITTED")
'''
            with self.assertRaises(CaptureBlocked):
                recorder.run([sys.executable, "-c", program], input_fd=slave,
                             output_fd=output.fileno(), display_write=display)
        self.assertEqual(seen, b"READY")
        self.assertEqual(raw(self.path, source.source_id, epoch), b"READY")
        self.assertFalse(recorder.admission_ready)
        self.assertEqual(spool.state(source.source_id, epoch)["health"], "blocked")
        self.assertEqual(spool.state(source.source_id, epoch)["drain_complete"], 0)
        with self.assertRaises(CaptureBlocked):
            spool.claim_recording(source.source_id, epoch, run.run_id)

    def test_killed_recorder_retains_committed_bytes_and_restart_refuses_relaunch(self):
        terminal = self.terminal(r'''
import os, signal, tty
tty.setraw(0)
os.write(1, b"DURABLE")
signal.pause()
''')
        terminal.read_until(b"DURABLE")
        terminal.process.kill()
        terminal.process.wait(timeout=3)
        self.assertEqual(raw(self.path, terminal.source_id, terminal.epoch), b"DURABLE")
        marker = Path(self.directory.name) / "must-not-exist"
        process = subprocess.run(
            cli_args(self.path, terminal.run, terminal.source_id, terminal.epoch,
                     [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)]),
            env=child_env(), capture_output=True, timeout=5,
        )
        self.assertEqual(process.returncode, 74, process.stderr)
        self.assertFalse(marker.exists())
        with CaptureSpool(self.path) as spool:
            state = spool.state(terminal.source_id, terminal.epoch)
            self.assertIsNone(state["recorder_finished_at"])
            self.assertEqual(state["drain_complete"], 0)
            self.assertFalse(Recorder(spool, terminal.source_id, terminal.epoch, terminal.run.run_id).admission_ready)

    def failure_case(self, case):
        fixture = Path(__file__).with_name("pty_failures.py")
        directory = Path(self.directory.name) / case
        directory.mkdir()
        process = subprocess.run(
            [sys.executable, str(fixture), str(directory), case],
            env=child_env(), capture_output=True, timeout=20,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["returned"], result)
        self.assertFalse(result["admission_ready"], result)
        self.assertTrue(result["direct_exited"], result)
        self.assertTrue(result["initially_canonical"], result)
        for restored in ("terminal_restored", "mask_restored", "handlers_restored",
                         "output_flags_restored", "inner_descriptors_closed"):
            self.assertTrue(result[restored], result)
        return result

    def test_failure_escalates_direct_child_with_a_different_foreground_group(self):
        for case in ("foreground", "foreground_leader_exits"):
            with self.subTest(case=case):
                result = self.failure_case(case)
                self.assertEqual(result["exception"], "OSError", result)
                self.assertTrue(result["original_group"], result)
                self.assertTrue(result["member_exited"], result)
                self.assertTrue(result["direct_reaped"], result)
                self.assertEqual(result["captured"], "READY", result)
                self.assertEqual(result["health"], "blocked", result)

    def test_failure_terminates_original_group_after_its_direct_leader_exits(self):
        result = self.failure_case("samegroup")
        self.assertEqual(result["exception"], "CaptureBlocked", result)
        self.assertTrue(result["original_group"], result)
        self.assertTrue(result["member_exited"], result)
        self.assertTrue(result["direct_reaped"], result)
        self.assertEqual(result["captured"], "READY", result)
        self.assertEqual(result["health"], "blocked", result)

    def test_failure_terminates_owned_session_after_leader_exited_before_cleanup(self):
        for case in ("forwarded_term", "foreground_exit", "background_exit"):
            with self.subTest(case=case):
                result = self.failure_case(case)
                self.assertTrue(result["leader_exited_before_failure"], result)
                self.assertTrue(result["member_live_at_failure"], result)
                self.assertTrue(result["original_group"], result)
                self.assertTrue(result["member_same_session"], result)
                self.assertTrue(result["member_separate_group"], result)
                self.assertEqual(result["member_was_foreground"], case != "background_exit", result)
                if case == "forwarded_term":
                    self.assertTrue(result["term_forwarded_before_failure"], result)
                self.assertEqual(result["exception"], "OSError", result)
                self.assertEqual(result["last_error"], "OSError", result)
                self.assertTrue(result["member_exited"], result)
                self.assertTrue(result["direct_reaped"], result)
                self.assertEqual(result["captured"], "READY", result)
                self.assertEqual(result["health"], "blocked", result)
                self.assertLess(result["failure_seconds"], 4, result)

    def test_term_before_login_survives_handler_reset_and_prevents_exec(self):
        result = self.failure_case("startup")
        self.assertEqual(result["exit"], -signal.SIGTERM, result)
        self.assertTrue(result["direct_reaped"], result)
        self.assertEqual(result["captured"], "", result)
        self.assertEqual(result["health"], "incomplete", result)

    def test_unconfirmed_cleanup_is_bounded_preserves_failure_and_restores_parent(self):
        result = self.failure_case("unconfirmed")
        self.assertEqual(result["exception"], "CleanupUnconfirmed", result)
        self.assertEqual(result["cause"], "OSError", result)
        self.assertIn("cleanup", result["last_error"], result)
        self.assertEqual(result["captured"], "READY", result)
        self.assertEqual(result["health"], "blocked", result)


if __name__ == "__main__":
    unittest.main()
