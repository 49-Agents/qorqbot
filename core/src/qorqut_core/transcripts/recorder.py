"""Linux nested-PTY recorder; commit each output block before display.

The runner supplies an explicit spool and identities, followed by child argv
after ``--``. No shell is inserted. This owns only its direct PTY process group;
producer cgroups, runner-service survival and automatic recovery are not provided
here. A reserved/used terminal epoch cannot launch twice after an uncertain exit.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
from pathlib import Path
import resource
import select
import signal
import sqlite3
import sys
import termios
import time
import tty
from typing import Callable, Sequence

from .spool import CHUNK_BYTES, CaptureBlocked, CaptureError, CaptureSpool, RunBinding, Source


_FORWARDED_SIGNALS = (
    signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
    signal.SIGUSR1, signal.SIGUSR2, signal.SIGTSTP, signal.SIGCONT,
)


class Recorder:
    def __init__(self, spool: CaptureSpool, source_id: str, source_epoch: str, run_id: str):
        self.spool = spool
        self.source_id = source_id
        self.source_epoch = source_epoch
        self.run_id = run_id
        self._active = False
        self._signals: set[int] = set()
        self._resize = False

    @property
    def admission_ready(self) -> bool:
        """Process-local readiness. A historical SQLite health row is insufficient."""
        if not self._active:
            return False
        try:
            self.spool.require_healthy(self.source_id, self.source_epoch)
        except (CaptureError, OSError, sqlite3.Error):
            return False
        return True

    def _signal(self, signum, _frame):
        if signum == signal.SIGWINCH:
            self._resize = True
        else:
            self._signals.add(signum)

    @staticmethod
    def _set_size(parent_fd: int, master_fd: int) -> None:
        if os.isatty(parent_fd):
            size = fcntl.ioctl(parent_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)

    @staticmethod
    def _forward_signal(master_fd: int, child_pid: int, signum: int, child_alive: bool) -> None:
        groups = set()
        try:
            group = os.tcgetpgrp(master_fd)
            if group > 0:
                groups.add(group)
        except OSError:
            pass
        if child_alive and (not groups or signum in (signal.SIGTERM, signal.SIGHUP)):
            groups.add(child_pid)
        for group in groups:
            try:
                os.killpg(group, signum)
            except ProcessLookupError:
                pass

    @staticmethod
    def _cleanup_child(child_pid: int, master_fd: int, status: int | None) -> None:
        """Bounded cleanup of the process/PTY we created, never a shared server."""
        if status is not None:
            return
        Recorder._forward_signal(master_fd, child_pid, signal.SIGTERM, True)
        # A stopped child must continue to handle TERM.
        Recorder._forward_signal(master_fd, child_pid, signal.SIGCONT, True)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            pid, _ = os.waitpid(child_pid, os.WNOHANG)
            if pid:
                return
            time.sleep(0.01)
        Recorder._forward_signal(master_fd, child_pid, signal.SIGKILL, True)
        os.waitpid(child_pid, 0)

    def run(self, argv: Sequence[str], *, input_fd: int = 0, output_fd: int = 1,
            display_write: Callable[[int, bytes], int] = os.write) -> int:
        """Return native exit status (negative for a signal), after PTY EOF drain.

        display_write is a narrow ordering-test seam; it is invoked only for
        committed bytes. It has os.write's partial-write/nonblocking semantics.
        Input is relayed unchanged and never inserted into a semantic source.
        """
        if isinstance(argv, (str, bytes)) or not argv or any(
            not isinstance(arg, str) or "\x00" in arg for arg in argv
        ) or not argv[0]:
            raise ValueError("child argv must be a nonempty sequence of strings")
        self.spool.claim_recording(self.source_id, self.source_epoch, self.run_id)
        master_fd = slave_fd = -1
        child_pid = None
        child_status = None
        saved_termios = None
        saved_output_flags = None
        handlers = {}
        try:
            master_fd, slave_fd = os.openpty()
            if os.isatty(input_fd):
                saved_termios = termios.tcgetattr(input_fd)
                termios.tcsetattr(slave_fd, termios.TCSANOW, saved_termios)
            self._set_size(input_fd, master_fd)
            for signum in (*_FORWARDED_SIGNALS, signal.SIGWINCH):
                handlers[signum] = signal.signal(signum, self._signal)
            # Determine the close range before fork; child uses only stdlib OS calls.
            fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            if fd_limit == resource.RLIM_INFINITY:
                fd_limit = os.sysconf("SC_OPEN_MAX")
            child_pid = os.fork()
            if child_pid == 0:
                try:
                    os.close(master_fd)
                    os.login_tty(slave_fd)  # setsid, controlling tty, stdin/stdout/stderr
                    for signum in handlers:
                        signal.signal(signum, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                    os.closerange(3, fd_limit)
                    os.execvp(argv[0], list(argv))
                except BaseException:
                    os.write(2, b"recorder: child exec failed\n")
                    os._exit(127)
            os.close(slave_fd)
            slave_fd = -1
            os.set_blocking(master_fd, False)
            if saved_termios is not None:
                tty.setraw(input_fd, termios.TCSANOW)
            saved_output_flags = fcntl.fcntl(output_fd, fcntl.F_GETFL)
            fcntl.fcntl(output_fd, fcntl.F_SETFL, saved_output_flags | os.O_NONBLOCK)
            self.spool.activate_recording(self.source_id, self.source_epoch)
            self._active = True
            offset = 0
            to_display = b""
            to_child = b""
            input_open = True
            output_open = True
            while output_open or to_display or child_status is None:
                if self._resize:
                    self._resize = False
                    self._set_size(input_fd, master_fd)
                signals, self._signals = self._signals, set()
                for signum in signals:
                    self._forward_signal(master_fd, child_pid, signum, child_status is None)
                if child_status is None:
                    pid, status = os.waitpid(child_pid, os.WNOHANG)
                    if pid:
                        child_status = status
                        self._active = False
                # A dead child does not imply an empty PTY. Drain to EOF/EIO.
                reads = []
                writes = []
                if output_open and not to_display:
                    reads.append(master_fd)
                if input_open and output_open and child_status is None and not to_child:
                    reads.append(input_fd)
                if to_display:
                    writes.append(output_fd)
                if to_child and output_open and child_status is None:
                    writes.append(master_fd)
                ready_read, ready_write, _ = select.select(reads, writes, [], 0.05)
                # Handle output/capture before admitting simultaneous input.
                if master_fd in ready_read:
                    try:
                        data = os.read(master_fd, CHUNK_BYTES)
                    except BlockingIOError:
                        data = None
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        data = b""  # Linux PTY slave closed, including buffered tail drained
                    if data:
                        offset = self.spool.append(self.source_id, self.source_epoch, offset, data)
                        to_display = data
                    elif data == b"":
                        output_open = False
                        to_child = b""
                        self._active = False
                if output_fd in ready_write and to_display:
                    try:
                        count = display_write(output_fd, to_display)
                        if not 0 < count <= len(to_display):
                            raise OSError("display did not accept bytes")
                        to_display = to_display[count:]
                    except BlockingIOError:
                        pass
                if input_fd in ready_read and self._active:
                    self.spool.require_healthy(self.source_id, self.source_epoch)
                    try:
                        to_child = os.read(input_fd, 65536)
                    except BlockingIOError:
                        to_child = b""
                    else:
                        if not to_child:
                            input_open = False
                if master_fd in ready_write and to_child and self._active:
                    self.spool.require_healthy(self.source_id, self.source_epoch)
                    try:
                        count = os.write(master_fd, to_child)
                        to_child = to_child[count:]
                    except BlockingIOError:
                        pass
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        to_child = b""
            exit_code = os.waitstatus_to_exitcode(child_status)
            self.spool.finish_recording(self.source_id, self.source_epoch, exit_code)
            return exit_code
        except BaseException as exc:
            self._active = False
            self.spool.block(self.source_id, self.source_epoch, type(exc).__name__)
            raise
        finally:
            self._active = False
            try:
                if child_pid is not None and child_pid > 0:
                    self._cleanup_child(child_pid, master_fd, child_status)
            finally:
                for descriptor in (master_fd, slave_fd):
                    if descriptor >= 0:
                        os.close(descriptor)
                try:
                    if saved_output_flags is not None:
                        fcntl.fcntl(output_fd, fcntl.F_SETFL, saved_output_flags)
                    if saved_termios is not None:
                        termios.tcsetattr(input_fd, termios.TCSANOW, saved_termios)
                finally:
                    for signum, handler in handlers.items():
                        signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable local nested-PTY byte recorder")
    parser.add_argument("--spool", required=True, type=Path)
    for name in ("installation-id", "agent-id", "recovery-epoch", "boot-id",
                 "run-id", "source-id", "source-epoch"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--environment-generation", required=True, type=int)
    parser.add_argument("--run-generation", required=True, type=int)
    parser.add_argument("--busy-timeout-ms", type=int, default=2000)
    parser.add_argument("child", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if len(args.child) < 2 or args.child[0] != "--":
        parser.error("child argv is required after --")
    try:
        binding = RunBinding(
            args.installation_id, args.agent_id, args.recovery_epoch,
            args.environment_generation, args.boot_id, args.run_id, args.run_generation,
        )
        with CaptureSpool(args.spool, busy_timeout_ms=args.busy_timeout_ms) as spool:
            spool.register_run(binding)
            spool.register_source(
                Source(args.source_id, args.installation_id, args.agent_id, "terminal"),
                args.source_epoch, args.run_id,
                capability_evidence="recorder-owned PTY output; no native semantic capture",
            )
            status = Recorder(spool, args.source_id, args.source_epoch, args.run_id).run(args.child[1:])
            return status if status >= 0 else 128 - status
    except (CaptureError, OSError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(f"recorder: capture unavailable ({type(exc).__name__})\n")
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
