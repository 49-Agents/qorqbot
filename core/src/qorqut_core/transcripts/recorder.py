"""Linux nested-PTY recorder; commit each output block before display.

The runner supplies an explicit spool and identities, followed by child argv
after ``--``. No shell is inserted. Failure cleanup covers its direct child and
verified owned PTY session; producer cgroups, runner-service survival and automatic
recovery are not provided here. A reserved/used terminal epoch cannot launch twice.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import resource
import select
import signal
import sqlite3
import struct
import sys
import termios
import time
import tty
from typing import Callable, Sequence

from ..runner.models import ProcessFingerprint, RunnerError
from ..runner.process import fingerprint, verify_process
from . import probe
from .spool import CHUNK_BYTES, CaptureBlocked, CaptureError, CaptureSpool, RunBinding, Source, _failure_category


_FORWARDED_SIGNALS = (
    signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT,
    signal.SIGUSR1, signal.SIGUSR2, signal.SIGTSTP, signal.SIGCONT,
)

# Linux asm-generic/ioctls.h; Python's termios omits this request on some builds.
_TIOCGSID = 0x5429


class CleanupUnconfirmed(CaptureBlocked):
    """Owned-process cleanup could not be confirmed within its finite boundary."""


@dataclass(frozen=True)
class _Process:
    pid: int
    start: int
    sid: int
    pgid: int
    state: bytes

    @property
    def live(self) -> bool:
        return self.state not in {b"Z", b"X", b"x"}


def _process_stat(pid: int) -> _Process | None:
    """Only identity/state fields; no command, environment or native-store reads."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            record = stream.read(4096)
    except (FileNotFoundError, ProcessLookupError):
        return None
    try:
        identity, fields = record.rsplit(b") ", 1)
        if int(identity.split(b" (", 1)[0]) != pid or not record.endswith(b"\n"):
            raise ValueError("inconsistent process identity")
        values = fields.split()
        return _Process(pid, int(values[19]), int(values[3]), int(values[2]), values[0])
    except (ValueError, IndexError) as exc:
        raise CleanupUnconfirmed("process identity/state observation is unavailable") from exc


class Recorder:
    def __init__(self, spool: CaptureSpool, source_id: str, source_epoch: str, run_id: str, *,
                 probe_address: str | None = None):
        self.spool = spool
        self.source_id = source_id
        self.source_epoch = source_epoch
        self.run_id = run_id
        self.probe_address = probe_address
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

    def _answer_probe(self, listener, child: ProcessFingerprint | None, *,
                      child_status: int | None, output_open: bool) -> None:
        received = probe._receive_request(listener)
        if received is None:
            return
        request, peer = received
        binding = self.spool.recording_binding(self.source_id, self.source_epoch, self.run_id)
        if (request.binding != binding or request.source_id != self.source_id
                or request.source_epoch != self.source_epoch):
            return
        recorder = fingerprint(os.getpid())
        if str(recorder.boot_id) != binding.boot_id:
            return
        active = self.admission_ready and child_status is None and output_open
        if child is not None:
            try:
                verify_process(child)
            except RunnerError:
                child = None
        active = active and child is not None and self.admission_ready
        observation = probe.ProbeObservation(
            version=1, nonce=request.nonce, binding=binding,
            source_id=self.source_id, source_epoch=self.source_epoch,
            recorder=recorder, child=child, terminal_active=active,
        )
        probe._send_observation(listener, observation, peer)

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
    def _observe_child(child_pid: int) -> int | None:
        """Observe native exit without releasing the PID/session ownership anchor."""
        observed = os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if observed is None:
            return None
        if observed.si_code == os.CLD_EXITED:
            return observed.si_status
        return -observed.si_status

    @staticmethod
    def _foreground_group(master_fd: int, child_pid: int) -> int | None:
        """Discover foreground ownership only through our PTY's actual session."""
        try:
            sid = struct.unpack("i", fcntl.ioctl(master_fd, _TIOCGSID, b"\0" * 4))[0]
            if sid != child_pid:
                return None
            group = os.tcgetpgrp(master_fd)
            return group if group > 0 else None
        except OSError as exc:
            if exc.errno not in (errno.ENOTTY, errno.EIO):
                raise
            return None  # No child session yet, or the PTY has hung up.

    @staticmethod
    def _forward_signal(master_fd: int, child_pid: int, signum: int, child_alive: bool) -> None:
        foreground = Recorder._foreground_group(master_fd, child_pid)
        groups = {foreground} if foreground is not None else set()
        if signum in (signal.SIGTERM, signal.SIGHUP) and os.getsid(child_pid) == child_pid:
            groups.add(child_pid)
        delivered = False
        direct_delivered = False
        for group in groups:
            try:
                os.killpg(group, signum)
                delivered = True
                direct_delivered |= group == child_pid
            except ProcessLookupError:
                pass
        if child_alive and (not delivered or (
            signum in (signal.SIGTERM, signal.SIGHUP) and not direct_delivered
        )):
            # The child's PTY session/group may not exist yet. Its unreaped PID
            # is still ours; signal it directly, never its inherited parent PGID.
            os.kill(child_pid, signum)

    @staticmethod
    def _owned_members(child_pid: int, child_start: int, deadline: float) -> list[_Process]:
        """Fresh membership in the session anchored by the unreaped child.

        The exact child's SID proves ownership even after exit and PTY hangup.
        Background groups in that SID are included; detached sessions are not.
        Unreadable potentially relevant stat data is an unknown observation,
        not evidence of quiescence.
        """
        observer = _process_stat(os.getpid())
        if observer is None or observer.sid != os.getsid(0) or observer.pgid != os.getpgrp():
            raise CleanupUnconfirmed("procfs and observer namespace identities disagree")
        direct = _process_stat(child_pid)
        if direct is None or direct.start != child_start:
            raise CleanupUnconfirmed("unreaped child identity is unavailable")
        members = [direct] if direct.live else []
        if direct.sid != child_pid:
            return members  # Pre-login: only the exact child, never its parent's session.
        with os.scandir("/proc") as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise CleanupUnconfirmed("owned-session observation deadline expired")
                if not entry.name.isdecimal() or int(entry.name) == child_pid:
                    continue
                observed = _process_stat(int(entry.name))
                if observed is not None and observed.sid == child_pid and observed.live:
                    members.append(observed)
        return members

    @staticmethod
    def _signal_member(member: _Process, child_pid: int, signum: int) -> bool:
        """Recheck PID/start/session after pidfd_open, then signal that identity."""
        try:
            descriptor = os.pidfd_open(member.pid)
        except ProcessLookupError:
            return False
        try:
            fresh = _process_stat(member.pid)
            if fresh is None or fresh.start != member.start or not fresh.live:
                return False
            if fresh.pid != child_pid and fresh.sid != child_pid:
                return False
            try:
                signal.pidfd_send_signal(descriptor, signum)
                return True
            except ProcessLookupError:
                return False  # The next observation reconciles disappearance.
        finally:
            os.close(descriptor)

    @staticmethod
    def _cleanup_child(child_pid: int, child_start: int) -> None:
        """Bounded TERM/CONT, KILL and confirmation; only the caller reaps the leader.

        Signal the exact child and positively identified members of its verified
        PTY session through pidfds. The unreaped leader anchors that session even
        when it exits before cleanup. No inherited or detached session is owned.
        """
        last_error = None
        for signum in (signal.SIGTERM, signal.SIGKILL):
            deadline = time.monotonic() + 1.0
            signalled = set()
            while time.monotonic() < deadline:
                try:
                    status = Recorder._observe_child(child_pid)
                    members = Recorder._owned_members(child_pid, child_start, deadline)
                    if status is not None and not members:
                        return
                    for member in members:
                        if (member.pid, member.start) not in signalled:
                            if Recorder._signal_member(member, child_pid, signum):
                                if signum == signal.SIGTERM:
                                    Recorder._signal_member(member, child_pid, signal.SIGCONT)
                                signalled.add((member.pid, member.start))
                    last_error = None
                except ChildProcessError as exc:
                    raise CleanupUnconfirmed("direct child ownership was lost during cleanup") from exc
                except (OSError, CleanupUnconfirmed) as exc:
                    last_error = exc
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        raise CleanupUnconfirmed("owned process cleanup could not be confirmed before deadline") from last_error

    @staticmethod
    def _reap_child(child_pid: int) -> None:
        """Final direct-child boundary, never a wait on arbitrary descendants."""
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if Recorder._observe_child(child_pid) is not None:
                pid, _ = os.waitpid(child_pid, os.WNOHANG)
                if pid == child_pid:
                    return
            time.sleep(0.005)
        raise CleanupUnconfirmed("direct child exit/reaping could not be confirmed")

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
        master_fd = slave_fd = -1
        child_pid = None
        child_start = None
        child_status = None
        failure = None
        saved_termios = None
        saved_output_flags = None
        saved_signal_mask = None
        handlers = {}
        listener = None
        probe_open = False
        probe_child = None
        probe_cleanup_errors = []
        claimed = False
        try:
            if self.probe_address is not None:
                listener = probe._open_listener(self.probe_address)
                probe_open = True
            self.spool.claim_recording(self.source_id, self.source_epoch, self.run_id)
            claimed = True
            master_fd, slave_fd = os.openpty()
            if os.isatty(input_fd):
                saved_termios = termios.tcgetattr(input_fd)
                termios.tcsetattr(slave_fd, termios.TCSANOW, saved_termios)
            self._set_size(input_fd, master_fd)
            saved_signal_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {*_FORWARDED_SIGNALS, signal.SIGWINCH},
            )
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
                    for signum in handlers:
                        signal.signal(signum, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
                    os.login_tty(slave_fd)  # setsid, controlling tty, stdin/stdout/stderr
                    os.closerange(3, fd_limit)
                    # A TERM sent to the exact child before login_tty stays
                    # pending through handler reset and is delivered before exec.
                    signal.pthread_sigmask(signal.SIG_SETMASK, saved_signal_mask)
                    os.execvp(argv[0], list(argv))
                except BaseException:
                    os.write(2, b"recorder: child exec failed\n")
                    os._exit(127)
            signal.pthread_sigmask(signal.SIG_SETMASK, saved_signal_mask)
            observed_child = _process_stat(child_pid)
            if observed_child is None:
                raise CleanupUnconfirmed("new child identity is unavailable")
            child_start = observed_child.start
            if listener is not None:
                try:
                    observed = fingerprint(child_pid)
                    if observed.start_ticks == child_start:
                        probe_child = observed
                except RunnerError:
                    pass  # Failure to observe our direct child cannot imply readiness.
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
                    child_status = self._observe_child(child_pid)
                    if child_status is not None:
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
                if probe_open and listener.fileno() < 0:
                    probe_open = False
                if probe_open:
                    reads.append(listener.fileno())
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
                if probe_open and listener.fileno() in ready_read:
                    try:
                        self._answer_probe(listener, probe_child, child_status=child_status,
                                           output_open=output_open)
                    except (OSError, ValueError, CaptureError, sqlite3.Error, RunnerError):
                        # Optional observation must not abandon committed/queued
                        # output or kill the producer. Disable only this endpoint.
                        probe_open = False
                        try:
                            listener.close()
                        except OSError as exc:
                            probe_cleanup_errors.append(exc)
            exit_code = child_status
            self.spool.finish_recording(self.source_id, self.source_epoch, exit_code)
            return exit_code
        except BaseException as exc:
            failure = exc
            self._active = False
            if claimed:
                try:
                    self.spool.block(self.source_id, self.source_epoch, _failure_category(exc))
                except BaseException:
                    exc.add_note("Blocked health could not be persisted; admission remains closed.")
            raise
        finally:
            self._active = False
            cleanup_error = None
            restoration_errors = probe_cleanup_errors
            try:
                if child_pid is not None and child_pid > 0:
                    try:
                        if failure is not None:
                            self._cleanup_child(child_pid, child_start)
                    except BaseException as exc:
                        cleanup_error = exc
                    finally:
                        # Session inspection/signalling has ended. Reap an observed
                        # direct exit even when session confirmation was unsuccessful.
                        try:
                            self._reap_child(child_pid)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc
            finally:
                # Every restoration is attempted, even if cleanup or an earlier
                # restoration failed. Restore the caller's mask on all paths.
                restorations = [(os.close, (fd,)) for fd in (master_fd, slave_fd) if fd >= 0]
                if listener is not None:
                    restorations.append((listener.close, ()))
                if saved_output_flags is not None:
                    restorations.append((fcntl.fcntl, (output_fd, fcntl.F_SETFL, saved_output_flags)))
                if saved_termios is not None:
                    restorations.append((termios.tcsetattr, (input_fd, termios.TCSANOW, saved_termios)))
                restorations.extend((signal.signal, (number, handler)) for number, handler in handlers.items())
                if saved_signal_mask is not None:
                    restorations.append((signal.pthread_sigmask, (signal.SIG_SETMASK, saved_signal_mask)))
                for restore, arguments in restorations:
                    try:
                        restore(*arguments)
                    except BaseException as exc:
                        restoration_errors.append(exc)
            if cleanup_error is not None or restoration_errors:
                cause = cleanup_error or restoration_errors[0]
                reason = f"{_failure_category(failure) if failure else 'recorder'}: cleanup/restoration unconfirmed"
                try:
                    self.spool.block(self.source_id, self.source_epoch, reason)
                except BaseException:
                    pass  # Process-local admission was already closed.
                error = CleanupUnconfirmed(reason)
                error.add_note(f"Cleanup/restoration cause: {type(cause).__name__}: {cause}")
                raise error from (failure or cause)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable local nested-PTY byte recorder")
    parser.add_argument("--spool", required=True, type=Path)
    for name in ("installation-id", "agent-id", "recovery-epoch", "boot-id",
                 "run-id", "source-id", "source-epoch"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--environment-generation", required=True, type=int)
    parser.add_argument("--run-generation", required=True, type=int)
    parser.add_argument("--busy-timeout-ms", type=int, default=2000)
    parser.add_argument("--probe-address")
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
            status = Recorder(spool, args.source_id, args.source_epoch, args.run_id,
                              probe_address=args.probe_address).run(args.child[1:])
            return status if status >= 0 else 128 - status
    except (CaptureError, OSError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(f"recorder: capture unavailable ({_failure_category(exc)})\n")
        return 74


if __name__ == "__main__":
    raise SystemExit(main())
