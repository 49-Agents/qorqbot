"""Local protocol and real nested-PTY observations; no native/provider fixtures."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import errno
import json
import os
from pathlib import Path
import select
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.runner.models import RunnerError
from qorqut_core.runner.process import fingerprint, verify_process
from qorqut_core.transcripts import probe
from qorqut_core.transcripts.recorder import Recorder
from qorqut_core.transcripts.spool import BindingConflict, CaptureSpool
from support import binding, child_env, cli_args, new_id, raw, registered
from test_recorder import NestedTerminal


def live_binding():
    return replace(binding(), boot_id=str(fingerprint(os.getpid()).boot_id))


@contextmanager
def return_socket():
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.bind("\0qorqut-test-" + uuid4().hex)
        client.settimeout(0.05)
        yield client


_CHILD = r'''
import json, os, pathlib, sys, tty
import qorqut_core
from qorqut_core.runner.process import fingerprint
tty.setraw(0)
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "fingerprint": fingerprint(os.getpid()).model_dump(mode="json"),
    "package": qorqut_core.__file__,
}))
os.write(1, b"READY")
while True:
    value = os.read(0, 1)
    if value == b"E":
        os.write(1, "exact:\x00🧭\n".encode())
    elif value == b"Q":
        os.write(1, b"DONE")
        break
    else:
        os.write(1, b"UNEXPECTED_INPUT:" + value)
'''


class LiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "spool.sqlite3"
        self.address = "test-" + uuid4().hex

    def terminal(self, *, patch_code=None, run=None):
        # Reuse the accepted nested-PTY fixture and its exact loaded import root.
        def arguments(*args, **kwargs):
            command = cli_args(*args, **kwargs)
            if patch_code:
                return [sys.executable, "-c", patch_code, *command[3:]]
            return command
        with patch("test_recorder.cli_args", arguments):
            terminal = NestedTerminal(self.path, _CHILD, str(self.root / "child.json"),
                                      run=run or live_binding(), probe_address=self.address)
        self.addCleanup(terminal.close)
        terminal.read_until(b"READY")
        self.expected = fingerprint(terminal.process.pid)
        self.recorder_fd = os.pidfd_open(terminal.process.pid)
        self.addCleanup(os.close, self.recorder_fd)
        verify_process(self.expected)
        # Fixture-written child identity and import origin, never PID/name search.
        report = json.loads((self.root / "child.json").read_text())
        import qorqut_core
        self.assertEqual(Path(report["package"]).resolve(), Path(qorqut_core.__file__).resolve())
        self.child = probe.ProcessFingerprint.model_validate(report["fingerprint"])
        child_fd = os.pidfd_open(self.child.pid)
        verify_process(self.child)
        self.addCleanup(os.close, child_fd)
        def clean_child():
            if not select.select([child_fd], [], [], 0)[0]:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            self.assertTrue(select.select([child_fd], [], [], 3)[0], "owned child cleanup unconfirmed")
        self.addCleanup(clean_child)
        return terminal

    def query(self, terminal, **kwargs):
        return probe.probe_recorder(self.address, terminal.run, terminal.source_id,
                                    terminal.epoch, self.expected, **kwargs)

    def snapshot(self):
        with sqlite3.connect(self.path) as connection:
            return tuple(connection.iterdump())

    def finish(self, terminal, expected=b"READYDONE"):
        os.write(terminal.master, b"Q")
        terminal.read_until()
        self.assertEqual(terminal.process.wait(timeout=3), 0)
        self.assertEqual(bytes(terminal.output), expected)
        self.assertEqual(raw(self.path, terminal.source_id, terminal.epoch), expected)
        self.assertEqual(termios.tcgetattr(terminal.slave), terminal.attributes)
        # Closing releases the abstract name without unlinking any filesystem path.
        with probe._open_listener(self.address):
            pass

    def test_idle_bytes_stop_resume_and_historical_healthy_exit(self):
        terminal = self.terminal()
        before = self.snapshot()
        first = self.query(terminal)
        self.assertTrue(first.verified)
        self.assertTrue(first.terminal_active)
        self.assertEqual(first.observation.recorder, self.expected)
        self.assertEqual(first.observation.child, self.child)
        self.assertEqual(first.observation.binding, terminal.run)
        self.assertEqual(self.snapshot(), before)
        signal.pidfd_send_signal(self.recorder_fd, signal.SIGSTOP)
        try:
            # A non-reaping wait observes the exact direct recorder's stopped state.
            stopped = os.waitid(os.P_PID, terminal.process.pid, os.WSTOPPED | os.WNOWAIT)
            self.assertEqual(stopped.si_code, os.CLD_STOPPED)
            started = time.monotonic()
            result = self.query(terminal, timeout_ms=80)
            elapsed = time.monotonic() - started
            self.assertEqual(result.reason, "unavailable")
            self.assertFalse(result.verified)
            self.assertFalse(result.terminal_active)
            self.assertGreaterEqual(elapsed, 0.07)
            self.assertLess(elapsed, 0.8)
            self.assertEqual(self.snapshot(), before)
        finally:
            signal.pidfd_send_signal(self.recorder_fd, signal.SIGCONT)
        resumed = self.query(terminal)
        self.assertTrue(resumed.terminal_active)
        self.assertNotEqual(first.observation.nonce, resumed.observation.nonce)
        self.assertEqual(self.snapshot(), before)
        os.write(terminal.master, b"E")
        emitted = "exact:\x00🧭\n".encode()
        terminal.read_until(emitted)
        after_bytes = self.snapshot()
        self.assertTrue(self.query(terminal).terminal_active)
        self.assertEqual(self.snapshot(), after_bytes)
        self.finish(terminal, b"READY" + emitted + b"DONE")
        with CaptureSpool(self.path) as spool:
            state = spool.state(terminal.source_id, terminal.epoch)
            self.assertEqual(state["health"], "healthy")
            self.assertEqual(state["host_archived_offset"], 0)
            self.assertEqual(state["local_captured_offset"], len(b"READY" + emitted + b"DONE"))
        self.assertFalse(self.query(terminal).terminal_active)

    def test_malformed_requests_and_vanished_clients_do_not_touch_capture(self):
        terminal = self.terminal()
        request = probe.ProbeRequest(version=1, nonce=uuid4(), binding=terminal.run,
                                     source_id=terminal.source_id, source_epoch=terminal.epoch)
        baseline = json.loads(request.model_dump_json())
        malformed = [b"not-json", b"x" * (probe.MAX_DATAGRAM_BYTES + 1)]
        for path, value in [
            (("version",), 2), (("version",), True), (("extra",), "do-not-echo"),
            (("nonce",), "wrong"), (("source_id",), new_id()), (("source_epoch",), new_id()),
            *((('binding', key), new_id() if isinstance(value, str) else value + 1)
              for key, value in asdict(terminal.run).items()),
            (("binding", "extra"), "forbidden"),
        ]:
            data = json.loads(json.dumps(baseline))
            parent = data if len(path) == 1 else data[path[0]]
            parent[path[-1]] = value
            malformed.append(json.dumps(data).encode())
        before = self.snapshot()
        with return_socket() as client:
            for data in malformed:
                client.sendto(data, probe._address(self.address))
                with self.assertRaises(TimeoutError):
                    client.recv(8192)
            # Ancillary truncation is rejected, including any SCM_RIGHTS.
            with open(os.devnull, "rb") as descriptor:
                client.sendmsg([request.model_dump_json().encode()],
                               [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", descriptor.fileno()))],
                               0, probe._address(self.address))
            with self.assertRaises(TimeoutError):
                client.recv(8192)
        with return_socket() as vanished:
            vanished.sendto(request.model_dump_json().encode(), probe._address(self.address))
        # A bound client that never reads fills its queue. Replies must remain
        # nonblocking while the recorder keeps servicing capture and queries.
        with return_socket() as slow:
            slow.settimeout(1)
            for _ in range(24):
                slow.sendto(request.model_dump_json().encode(), probe._address(self.address))
            self.assertTrue(self.query(terminal).terminal_active)
        self.assertTrue(self.query(terminal).terminal_active)
        self.assertEqual(self.snapshot(), before)
        self.finish(terminal)

    def test_observer_receive_send_and_closed_endpoint_leave_capture_running(self):
        for seam in ("_receive_request", "_send_observation", "closed"):
            with self.subTest(seam=seam), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.path = Path(directory) / "spool.sqlite3"
                self.address = uuid4().hex
                name = "_send_observation" if seam == "closed" else seam
                effect = ("result = original(*args)\n    args[0].close()\n    return result"
                          if seam == "closed" else
                          'if count == 2:\n        raise OSError(errno.EBADF, "fixture observer failure")\n    return original(*args)')
                code = f'''
import errno
from qorqut_core.transcripts import probe
from qorqut_core.transcripts.recorder import main
original = probe.{name}
count = 0
def fail(*args):
    global count
    count += 1
    {effect}
probe.{name} = fail
raise SystemExit(main())
'''
                terminal = self.terminal(patch_code=code)
                self.assertTrue(self.query(terminal).terminal_active)
                self.assertFalse(self.query(terminal, timeout_ms=40).verified)
                self.assertFalse(self.query(terminal, timeout_ms=40).verified)
                os.write(terminal.master, b"E")
                emitted = "exact:\x00🧭\n".encode()
                terminal.read_until(emitted)
                self.finish(terminal, b"READY" + emitted + b"DONE")
                with CaptureSpool(self.path) as spool:
                    self.assertEqual(spool.state(terminal.source_id, terminal.epoch)["health"], "healthy")

    def test_local_latch_child_loss_exit_eof_and_final_readiness_are_inactive(self):
        cases = {
            "local_latch": 'self.spool.last_error = "fixture unpersisted failure"',
            "inactive": 'self._active = False',
            "child_changed": 'child = child.model_copy(update={"start_ticks": child.start_ticks + 1})',
            "exit_observed": 'kwargs["child_status"] = 0',
            "eof_observed": 'kwargs["output_open"] = False',
            "final_readiness": '''
    real_verify = recorder_module.verify_process
    def lose_ready(expected):
        real_verify(expected)
        self.spool.last_error = "fixture failure during child observation"
    recorder_module.verify_process = lose_ready
''',
        }
        for case, effect in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=self.root) as directory:
                self.path = Path(directory) / "spool.sqlite3"
                self.address = uuid4().hex
                code = f'''
from qorqut_core.transcripts import recorder as recorder_module
original = recorder_module.Recorder._answer_probe
def inactive(self, listener, child, **kwargs):
    {effect}
    return original(self, listener, child, **kwargs)
recorder_module.Recorder._answer_probe = inactive
raise SystemExit(recorder_module.main())
'''
                terminal = self.terminal(patch_code=code)
                before = self.snapshot()
                result = self.query(terminal)
                self.assertTrue(result.verified, result)
                self.assertFalse(result.terminal_active)
                self.assertEqual(result.reason, "inactive")
                self.assertEqual(result.observation.child is None, case == "child_changed")
                self.assertEqual(self.snapshot(), before)
                with CaptureSpool(self.path) as spool:
                    self.assertEqual(spool.state(terminal.source_id, terminal.epoch)["health"], "healthy")
                signal.pidfd_send_signal(self.recorder_fd, signal.SIGTERM)
                terminal.read_until()
                self.assertEqual(terminal.process.wait(timeout=3), 128 + signal.SIGTERM)
                self.assertEqual(raw(self.path, terminal.source_id, terminal.epoch), b"READY")

    def test_wrong_stored_boot_cannot_answer_with_invented_coordinates(self):
        terminal = self.terminal(run=binding())
        with return_socket() as client:
            request = probe.ProbeRequest(version=1, nonce=uuid4(), binding=terminal.run,
                                         source_id=terminal.source_id, source_epoch=terminal.epoch)
            client.sendto(request.model_dump_json().encode(), probe._address(self.address))
            with self.assertRaises(TimeoutError):
                client.recv(8192)
        self.finish(terminal)


class ProbeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.expected = fingerprint(os.getpid())
        self.run = live_binding()
        self.source, self.epoch = new_id(), new_id()
        self.address = uuid4().hex

    def query(self, **kwargs):
        return probe.probe_recorder(self.address, self.run, self.source, self.epoch, self.expected, **kwargs)

    def exchange(self, mutate=lambda data: data, *, client_transform=None, repeat=1, send_rights=False):
        errors, counts, names = [], [], []
        with probe._open_listener(self.address) as listener:
            listener.settimeout(2)
            def respond():
                try:
                    request, peer = listener.recvfrom(8192)
                    body = json.loads(request)
                    body.update(recorder=self.expected.model_dump(mode="json"),
                                child=self.expected.model_dump(mode="json"), terminal_active=True)
                    body = mutate(body)
                    data = body if isinstance(body, bytes) else json.dumps(body).encode()
                    for _ in range(repeat):
                        try:
                            if send_rights:
                                with open(os.devnull, "rb") as descriptor:
                                    listener.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                              struct.pack("i", descriptor.fileno()))], 0, peer)
                            else:
                                listener.sendto(data, peer)
                        except ConnectionRefusedError:
                            break
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=respond)
            thread.start()
            real_socket = socket.socket
            class Client:
                def __init__(self, *args):
                    self.inner = real_socket(*args)
                def __getattr__(self, name):
                    return getattr(self.inner, name)
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    names.append(self.inner.getsockname())
                    self.inner.close()
                def recvmsg(self, *args):
                    counts.append(1)
                    packet = self.inner.recvmsg(*args)
                    return client_transform(packet) if client_transform else packet
            try:
                with patch.object(probe.socket, "socket", Client):
                    result = self.query(timeout_ms=40)
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive(), "fixture responder did not stop")
            self.assertEqual(errors, [])
        for name in names:
            with real_socket(socket.AF_UNIX, socket.SOCK_DGRAM) as released:
                released.bind(name)
        return result, len(counts)

    def test_strict_wire_mismatches_never_verify(self):
        edits = [
            (("nonce",), new_id()), (("version",), 2), (("version",), True),
            (("version",), 1.0), (("version",), "1"), (("terminal_active",), 1),
            (("terminal_active",), "true"), (("source_id",), new_id()), (("source_epoch",), new_id()),
            (("extra",), "do-not-echo"), (("binding", "extra"), 1), (("recorder", "extra"), 1),
            (("child",), None),
            *((('binding', key), new_id() if isinstance(value, str) else value + 1)
              for key, value in asdict(self.run).items()),
            *((('recorder', key), new_id() if key == 'boot_id' else value + 1)
              for key, value in self.expected.model_dump(mode="json").items()),
            (("recorder", "uid"), True), (("recorder", "start_ticks"), 2**63),
        ]
        for path, value in edits:
            with self.subTest(path=path, value=value):
                def mutate(body):
                    parent = body if len(path) == 1 else body[path[0]]
                    parent[path[-1]] = value
                    return body
                result, _ = self.exchange(mutate)
                self.assertFalse(result.verified)
                self.assertFalse(result.terminal_active)
                self.assertIsNone(result.observation)
                self.assertEqual(result.reason, "invalid_response")
        for data in (b"garbage", b"{" * 8192, b"x" * 8193):
            with self.subTest(data_length=len(data)):
                result, _ = self.exchange(lambda _: data)
                self.assertEqual(result.reason, "invalid_response")

    def test_credentials_and_truncation_are_required_and_replies_are_bounded(self):
        # Linux ucred has a signed pid_t but unsigned uid_t/gid_t. This is a
        # synthetic ancillary boundary check, not a change to OS credentials.
        high_uid = self.expected.model_copy(update={"uid": 2**31 + 7})
        self.assertTrue(probe._sender_matches(
            [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
              struct.pack("iII", high_uid.pid, high_uid.uid, 2**31 + 9))], 0, high_uid,
        ))
        for kind in ("missing", "pid", "uid", "duplicate", "short", "ancillary_truncation", "truncation"):
            with self.subTest(kind=kind):
                def mutate(packet):
                    data, ancillary, flags, peer = packet
                    if kind == "missing": ancillary = []
                    elif kind in {"pid", "uid"}:
                        credentials = list(struct.unpack("3i", ancillary[0][2]))
                        credentials[0 if kind == "pid" else 1] += 1
                        ancillary = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, struct.pack("3i", *credentials))]
                    elif kind == "duplicate": ancillary *= 2
                    elif kind == "short": ancillary = [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, b"x")]
                    elif kind == "ancillary_truncation": flags |= socket.MSG_CTRUNC
                    else: flags |= socket.MSG_TRUNC
                    return data, ancillary, flags, peer
                result, _ = self.exchange(client_transform=mutate)
                self.assertEqual(result.reason, "invalid_response")
        result, count = self.exchange(lambda body: {**body, "nonce": new_id()}, repeat=12)
        self.assertEqual(result.reason, "invalid_response")
        self.assertEqual(count, 8)
        before = set(os.listdir("/proc/self/fd"))
        result, _ = self.exchange(send_rights=True)
        self.assertEqual(result.reason, "invalid_response")
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_authentic_inactive_and_post_reply_process_change(self):
        result, _ = self.exchange(lambda body: {**body, "terminal_active": False, "child": None})
        self.assertTrue(result.verified)
        self.assertFalse(result.terminal_active)
        self.assertEqual(result.reason, "inactive")
        with patch.object(probe, "verify_process", side_effect=[None, RunnerError("target_changed", "fixture change")]) as verify:
            result, _ = self.exchange()
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result.reason, "target_changed")
        self.assertFalse(result.terminal_active)

    def test_different_live_process_cannot_impersonate_expected_recorder(self):
        with probe._open_listener(self.address) as listener:
            code = r'''
import json, os, socket, sys
from qorqut_core.runner.process import fingerprint
s = socket.socket(fileno=int(sys.argv[1]))
request, peer = s.recvfrom(8192)
data = json.loads(request)
claimed = json.loads(sys.argv[2])
data.update(recorder=claimed, child=fingerprint(os.getpid()).model_dump(mode="json"), terminal_active=True)
s.sendto(json.dumps(data).encode(), peer)
sys.stdin.buffer.read(1)
'''
            listener.setblocking(True)
            process = subprocess.Popen([sys.executable, "-c", code, str(listener.fileno()),
                                        self.expected.model_dump_json()], pass_fds=(listener.fileno(),),
                                       env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                result = self.query(timeout_ms=500)
                self.assertIsNone(process.poll(), "responder must still be live during verification")
                self.assertEqual(result.reason, "invalid_response")
                self.assertFalse(result.terminal_active)
            finally:
                stdout, stderr = process.communicate(b"Q", timeout=3)
            self.assertEqual(process.returncode, 0, (stdout, stderr))

    def test_arguments_and_stale_fingerprints_fail_before_socket_io(self):
        base = dict(address=self.address, expected_binding=self.run, source_id=self.source,
                    source_epoch=self.epoch, expected_recorder=self.expected)
        for key, values in {
            "address": ("", "a" * 81, "has space", "trailing\n", "🧭", b"bytes", None),
            "source_id": ("bad", uuid4()), "source_epoch": ("bad",),
            "timeout_ms": (True, 0, 1001, 1.0, "200"),
            "expected_binding": (asdict(self.run),), "expected_recorder": (self.expected.model_dump(),),
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), patch.object(probe.socket, "socket") as create:
                    with self.assertRaises((ValueError, TypeError)):
                        probe.probe_recorder(**{**base, key: value})
                    create.assert_not_called()
        for field, value in self.expected.model_dump().items():
            changed = uuid4() if field == "boot_id" else value + 1
            with self.subTest(fingerprint_field=field), patch.object(probe.socket, "socket") as create:
                result = probe.probe_recorder(**{**base, "expected_recorder": self.expected.model_copy(update={field: changed})})
                self.assertEqual(result.reason, "target_changed")
                create.assert_not_called()
        with patch.object(probe, "_supported", return_value=False):
            self.assertEqual(self.query().reason, "unsupported")
        self.assertEqual(self.query().reason, "unavailable")

    def test_occupied_endpoint_and_startup_failure_preserve_cleanup_and_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            spool, source, epoch, run = registered(Path(directory) / "capture.db", kind="terminal", run=self.run)
            with spool, probe._open_listener(self.address) as occupied:
                recorder = Recorder(spool, source.source_id, epoch, run.run_id, probe_address=self.address)
                original = spool.state(source.source_id, epoch)
                with patch("os.fork") as fork:
                    with self.assertRaises(OSError) as failure:
                        recorder.run([sys.executable, "-c", "raise SystemExit(0)"])
                    self.assertEqual(failure.exception.errno, errno.EADDRINUSE)
                    fork.assert_not_called()
                self.assertEqual(spool.state(source.source_id, epoch), original)
                self.assertGreaterEqual(occupied.fileno(), 0)
            with CaptureSpool(Path(directory) / "capture.db") as spool:
                recorder = Recorder(spool, source.source_id, epoch, run.run_id, probe_address=self.address)
                with patch("os.openpty", side_effect=OSError(errno.EMFILE, "fixture startup failure")):
                    with self.assertRaises(OSError):
                        recorder.run([sys.executable, "-c", "raise SystemExit(0)"])
                self.assertFalse(recorder.admission_ready)
                with probe._open_listener(self.address):
                    pass
                before = tuple(spool._connection.iterdump())
                self.assertEqual(spool.recording_binding(source.source_id, epoch, run.run_id), run)
                for source_id, source_epoch, run_id in ((new_id(), epoch, run.run_id),
                                                      (source.source_id, new_id(), run.run_id),
                                                      (source.source_id, epoch, new_id())):
                    with self.assertRaises(BindingConflict):
                        spool.recording_binding(source_id, source_epoch, run_id)
                self.assertEqual(tuple(spool._connection.iterdump()), before)


if __name__ == "__main__":
    unittest.main()
