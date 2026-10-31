"""Terminal mirroring for biz-dashboard.

A PTY per agent, keyed by an opaque id. The PTY runs whatever command the agent's
config says (a shell, `claude`, the engine — brain-agnostic). Output is captured by a
reader thread into a scrollback ring buffer and mirrored to any number of viewers (SSE);
input is written back to the PTY master. Linux only (stdlib `pty`) — the target host is
Linux; on Windows this module raises on import, which is fine (the dashboard runs on the
host, not the command-center machine).

No third-party dependencies.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading

RING_CAP = 256 * 1024  # bytes of scrollback kept for late joiners


class Terminal:
    """One PTY-backed process, its scrollback, and mirror/input plumbing."""

    def __init__(self, key: str, cmd: list[str], cwd: str, env: dict | None = None,
                 cols: int = 120, rows: int = 32) -> None:
        self.key = key
        self.cmd = cmd
        self.cwd = cwd
        self.cols = cols
        self.rows = rows

        self.master_fd, slave_fd = pty.openpty()
        self._set_winsize(rows, cols)

        full_env = {**os.environ, **(env or {}), "TERM": "xterm-256color"}
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,  # own session + controlling tty (the slave)
            env=full_env,
            close_fds=True,
        )
        os.close(slave_fd)

        self._chunks: list[bytes] = []  # scrollback ring
        self._nbytes = 0
        self.seq = 0  # total chunks ever appended (monotonic; a mirror cursor)
        self.done = False
        self.exit_code: int | None = None
        self._lock = threading.Lock()

        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    # ---- window size ----
    def _set_winsize(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self._set_winsize(rows, cols)
        try:
            self.proc.send_signal(signal.SIGWINCH)
        except (OSError, ProcessLookupError):
            pass

    # ---- output pump ----
    def _pump(self) -> None:
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                with self._lock:
                    self._chunks.append(data)
                    self._nbytes += len(data)
                    self.seq += 1
                    while self._nbytes > RING_CAP and len(self._chunks) > 1:
                        self._nbytes -= len(self._chunks.pop(0))
            elif self.proc.poll() is not None:
                break
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self.exit_code = self.proc.returncode
        self.done = True

    # ---- input ----
    def write(self, data: bytes) -> None:
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    # ---- mirror reads ----
    def snapshot(self) -> tuple[bytes, int]:
        """Current scrollback as one blob + the seq cursor it ends at."""
        with self._lock:
            return b"".join(self._chunks), self.seq

    def tail_from(self, cursor: int) -> tuple[bytes, int]:
        """Bytes appended since `cursor` (best-effort; ring may have dropped older)."""
        with self._lock:
            first_seq = self.seq - len(self._chunks)
            start = cursor - first_seq
            if start < 0:
                start = 0
            return b"".join(self._chunks[start:]), self.seq

    # ---- lifecycle ----
    def stop(self) -> None:
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass


# ------------------------------------------------------------------ registry

_LOCK = threading.Lock()
_TERMINALS: dict[str, Terminal] = {}


def get_or_start(key: str, cmd: list[str], cwd: str, env: dict | None = None,
                 cols: int = 120, rows: int = 32) -> Terminal:
    """Return the live terminal for `key`, spawning it if absent or exited."""
    with _LOCK:
        t = _TERMINALS.get(key)
        if t is not None and not t.done:
            return t
        t = Terminal(key, cmd, cwd, env=env, cols=cols, rows=rows)
        _TERMINALS[key] = t
        return t


def get(key: str) -> Terminal | None:
    with _LOCK:
        return _TERMINALS.get(key)


def stop(key: str) -> bool:
    with _LOCK:
        t = _TERMINALS.pop(key, None)
    if t is not None:
        t.stop()
        return True
    return False


def keys() -> list[str]:
    with _LOCK:
        return list(_TERMINALS.keys())
