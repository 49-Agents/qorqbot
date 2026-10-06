"""Owned, pipe-gated PTY failure fixtures for test_recorder (no provider calls).

The outer fixture process is a subreaper solely for its synthetic children. It
holds their pidfds before releasing any failure/startup gate, so even a broken
recorder cannot leak the fixture's producer processes or hang the test suite.
"""

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import sys
import termios
import time


_FOREGROUND_CASES = {
    "foreground", "foreground_leader_exits", "unconfirmed", "forwarded_term", "foreground_exit",
}
_EARLY_EXIT_CASES = {"forwarded_term", "foreground_exit", "background_exit"}


def _send(path, message):
    fd = os.open(path, os.O_WRONLY)
    try:
        data = (json.dumps(message) + "\n").encode()
        assert len(data) < 4096
        assert os.write(fd, data) == len(data)
    finally:
        os.close(fd)


def _child(root, case):
    import tty
    tty.setraw(0)
    if case == "startup":
        os.write(1, b"EXECUTED_AFTER_TERM")
        return
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    if case in {"foreground", "unconfirmed"}:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    read_fd, write_fd = os.pipe()
    member = os.fork()
    if member == 0:
        os.close(read_fd)
        if case in _FOREGROUND_CASES or case == "background_exit":
            os.setpgid(0, 0)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(write_fd, b"configured")
        os.close(write_fd)
        while True:
            signal.pause()
    os.close(write_fd)
    assert os.read(read_fd, 16) == b"configured"
    os.close(read_fd)
    if case in _FOREGROUND_CASES:
        os.tcsetpgrp(0, member)
    _send(root / "events", {"event": "children", "direct": os.getpid(), "member": member,
                            "pgid": os.getpgrp(), "sid": os.getsid(0),
                            "member_pgid": os.getpgid(member), "member_sid": os.getsid(member),
                            "foreground": os.tcgetpgrp(0)})
    # The supervisor acquires both pidfds before letting READY reach Recorder.
    gate = os.open(root / "child_gate", os.O_RDONLY)
    assert os.read(gate, 1) == b"G"
    os.close(gate)
    os.write(1, b"READY")
    if case == "samegroup":
        assert os.read(0, 2) == b"go"
        os.write(1, b"UNCOMMITTED")
    while True:
        signal.pause()


def _worker(root, case, gate):
    from qorqut_core.transcripts.recorder import Recorder
    from support import raw, registered

    spool, source, epoch, run = registered(root / "spool.sqlite3", kind="terminal")
    recorder = Recorder(spool, source.source_id, epoch, run.run_id)
    master, slave = os.openpty()
    before = termios.tcgetattr(slave)  # Deliberately canonical/echoing, not raw.
    inner_fds = {}
    real_openpty = os.openpty
    def tracked_openpty():
        pair = real_openpty()
        for fd in pair:
            info = os.fstat(fd)
            inner_fds[fd] = (info.st_dev, info.st_ino, info.st_rdev)
        return pair
    os.openpty = tracked_openpty
    before_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    expected_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    watched_signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGWINCH)
    handlers = {number: signal.getsignal(number) for number in watched_signals}
    if case == "startup":
        real_login = os.login_tty
        def login(fd):
            _send(root / "events", {"event": "pre_login", "direct": os.getpid()})
            assert os.read(gate, 1) == b"G"
            real_login(fd)
        os.login_tty = login
        real_activate = spool.activate_recording
        def activate(*args):
            real_activate(*args)
            _send(root / "events", {"event": "active"})
        spool.activate_recording = activate
    if case in {"startup", "forwarded_term"}:
        real_forward = Recorder._forward_signal
        def forward(*args):
            result = real_forward(*args)
            if args[2] == signal.SIGTERM:
                _send(root / "events", {"event": "forwarded"})
                if case == "forwarded_term":
                    # The supervisor observes the real leader exit via its
                    # already acquired pidfd; nobody reaps it before cleanup.
                    assert os.read(gate, 1) == b"X"
            return result
        Recorder._forward_signal = staticmethod(forward)
    if case == "forwarded_term":
        real_append = spool.append
        def append(*args):
            result = real_append(*args)
            _send(root / "events", {"event": "capture_boundary"})
            assert os.read(gate, 1) == b"C"
            return result
        spool.append = append
    if case == "unconfirmed":
        # Make the exit observer unavailable; never fake a successful kill/wait.
        # Real TERM/KILL still run. Final cleanup must report uncertainty, restore
        # parent state, and leave actual child reaping to this fixture supervisor.
        Recorder._observe_child = staticmethod(lambda _pid: None)
    seen = bytearray()
    def display(fd, data):
        seen.extend(data)
        if case != "startup" and b"READY" in seen:
            _send(root / "events", {"event": "display_boundary"})
            assert os.read(gate, 1) == b"F"
            if case in _FOREGROUND_CASES or case == "background_exit":
                raise OSError("fixture display failure")
            with sqlite3.connect(root / "spool.sqlite3") as connection:
                connection.execute("""CREATE TRIGGER fail_append BEFORE INSERT ON capture_chunks
                    BEGIN SELECT RAISE(ABORT, 'fixture capture failure'); END""")
            os.write(master, b"go")
        return os.write(fd, data)
    report = {}
    try:
        with (root / "display").open("wb") as output:
            flags = fcntl.fcntl(output.fileno(), fcntl.F_GETFL)
            try:
                report["exit"] = recorder.run(
                    [sys.executable, __file__, "--child", str(root), case],
                    input_fd=slave, output_fd=output.fileno(), display_write=display,
                )
            except BaseException as exc:
                report["exception"] = type(exc).__name__
                report["cause"] = type(exc.__cause__).__name__ if exc.__cause__ else None
            report["output_flags_restored"] = fcntl.fcntl(output.fileno(), fcntl.F_GETFL) == flags
            report["inner_descriptors_closed"] = True
            for fd, identity in inner_fds.items():
                try:
                    info = os.fstat(fd)
                except OSError as exc:
                    if exc.errno != errno.EBADF:
                        raise
                else:
                    # The closed slave FD number can legitimately be reused by
                    # a SQLite connection in the failure injector.
                    if (info.st_dev, info.st_ino, info.st_rdev) == identity:
                        report["inner_descriptors_closed"] = False
        report.update(
            captured=raw(root / "spool.sqlite3", source.source_id, epoch).decode(),
            health=spool.state(source.source_id, epoch)["health"],
            last_error=spool.state(source.source_id, epoch)["last_error"],
            admission_ready=recorder.admission_ready,
            initially_canonical=bool(before[3] & termios.ICANON),
            terminal_restored=termios.tcgetattr(slave) == before,
            mask_restored=signal.pthread_sigmask(signal.SIG_BLOCK, set()) == expected_mask,
            handlers_restored=all(signal.getsignal(number) == handler for number, handler in handlers.items()),
        )
        _send(root / "events", {"event": "returned", **report})
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before_mask)
        spool.close()
        os.close(master)
        os.close(slave)


def exercise(root, case):
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    os.mkfifo(root / "events", 0o600)
    os.mkfifo(root / "child_gate", 0o600)
    events = os.open(root / "events", os.O_RDWR | os.O_NONBLOCK)
    child_gate = os.open(root / "child_gate", os.O_RDWR | os.O_NONBLOCK)
    gate_read, gate_write = os.pipe()
    handles = {}
    worker = os.fork()
    if worker == 0:
        try:
            os.close(gate_write)
            _worker(root, case, gate_read)
            os._exit(0)
        except BaseException as exc:
            _send(root / "events", {"event": "worker_error", "error": repr(exc)})
            os._exit(1)
    os.close(gate_read)
    handles["worker"] = (worker, os.pidfd_open(worker))
    buffer = b""
    observed = set()
    report = {"case": case, "returned": False}
    failure_started = None
    deadline = time.monotonic() + 8
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([events], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            buffer += os.read(events, 65536)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                event = message.pop("event")
                observed.add(event)
                if event in {"pre_login", "children"}:
                    for name in ("direct", "member"):
                        if name in message:
                            handles[name] = (message[name], os.pidfd_open(message[name]))
                    if event == "children":
                        report["original_group"] = message["pgid"] == message["direct"] == message["sid"]
                        report["member_same_session"] = message["member_sid"] == message["sid"]
                        report["member_separate_group"] = message["member_pgid"] == message["member"] != message["direct"]
                        report["member_was_foreground"] = message["foreground"] == message["member_pgid"]
                        os.write(child_gate, b"G")
                if case == "startup" and {"pre_login", "active"} <= observed and "term_sent" not in observed:
                    signal.pidfd_send_signal(handles["worker"][1], signal.SIGTERM)
                    observed.add("term_sent")
                if event == "forwarded":
                    if case == "forwarded_term":
                        assert select.select([handles["direct"][1]], [], [], 3)[0]
                        report["term_forwarded_before_failure"] = True
                        os.write(gate_write, b"X")
                    else:
                        os.write(gate_write, b"G")
                if event == "capture_boundary":
                    signal.pidfd_send_signal(handles["worker"][1], signal.SIGTERM)
                    os.write(gate_write, b"C")
                if event == "display_boundary":
                    if case in _EARLY_EXIT_CASES:
                        if case != "forwarded_term":
                            signal.pidfd_send_signal(handles["direct"][1], signal.SIGKILL)
                        assert select.select([handles["direct"][1]], [], [], 3)[0]
                        assert not select.select([handles["member"][1]], [], [], 0)[0]
                        report["leader_exited_before_failure"] = True
                        report["member_live_at_failure"] = True
                    failure_started = time.monotonic()
                    os.write(gate_write, b"F")
                if event in {"returned", "worker_error"}:
                    report.update(message, returned=event == "returned")
                    if failure_started is not None:
                        report["failure_seconds"] = time.monotonic() - failure_started
                    for name, (_, fd) in handles.items():
                        if name != "worker":
                            report[name + "_exited"] = bool(select.select([fd], [], [], 0)[0])
                    try:
                        signal.pidfd_send_signal(handles["direct"][1], 0)
                    except ProcessLookupError:
                        report["direct_reaped"] = True
                    else:
                        report["direct_reaped"] = False
                    return report
        return report
    finally:
        # Only positively owned pidfds are signalled, including against pre-fix
        # code that returns with a live member or blocks forever in waitpid.
        for _, fd in handles.values():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid, fd in handles.values():
            if not select.select([fd], [], [], 3)[0]:
                raise AssertionError("fixture process did not exit after owned pidfd cleanup")
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            os.close(fd)
        for fd in (events, child_gate, gate_write):
            os.close(fd)


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        _child(Path(sys.argv[2]), sys.argv[3])
    else:
        print(json.dumps(exercise(Path(sys.argv[1]), sys.argv[2]), sort_keys=True))
