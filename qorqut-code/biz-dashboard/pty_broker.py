"""Terminal mirroring for biz-dashboard.

Two layers. The agent's shell lives in a **tmux session** on a dedicated tmux server (socket
`-L bazarlyk`), one session per agent, so it outlives the dashboard: a restart kills only the
mirror, never the agent. The dashboard mirrors it through a **PTY** whose child is
`tmux attach-session`; output is captured by a reader thread into a scrollback ring buffer and
streamed to any number of viewers (SSE), input is written back to the PTY master. When `tmux`
is not installed the PTY child is the shell itself (the old behaviour, no persistence).

The owner can attach to the same session from any shell: `tmux -L bazarlyk attach -t NAME`.

Linux only (stdlib `pty`); the target host is Linux. No third-party Python dependencies.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading

RING_CAP = 256 * 1024  # bytes of scrollback kept for late joiners

# Claude Code marks its own subprocesses so a nested `claude` knows it is a child session (no
# transcript, parent's messaging socket, parent's effort). When the dashboard itself was started
# from inside a Claude Code session those markers sit in os.environ and every agent PTY would
# inherit them: the agent's `claude` then runs as a child of the owner's session and saves no
# transcript. Terminals are root sessions; strip the markers. CLAUDE_CONFIG_DIR and ANTHROPIC_*
# are legitimate user config and stay.
_STRIP_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT"}
_STRIP_PREFIX = "CLAUDE_CODE_"


def clean_env(base: dict | None = None) -> dict:
    """`base` (default os.environ) without the Claude Code child-session markers."""
    src = os.environ if base is None else base
    return {k: v for k, v in src.items() if k not in _STRIP_EXACT and not k.startswith(_STRIP_PREFIX)}


class Terminal:
    """One PTY-backed process, its scrollback, and mirror/input plumbing."""

    def __init__(self, key: str, cmd: list[str], cwd: str, env: dict | None = None,
                 cols: int = 120, rows: int = 32) -> None:
        self.key = key
        self.cmd = cmd
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self._io_lock = threading.Lock()
        self._stopping = threading.Event()

        self.master_fd, slave_fd = pty.openpty()
        self._set_winsize(rows, cols)

        full_env = {**clean_env(), **(env or {}), "TERM": "xterm-256color"}
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
        with self._io_lock:
            if self._stopping.is_set() or self.master_fd < 0:
                return
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
        while not self._stopping.is_set():
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if self._stopping.is_set():
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
        # Only the reader closes the descriptor, after its last possible read.
        # Closing it in stop() lets select/read consume a newly reused file fd.
        self._stopping.set()
        with self._io_lock:
            descriptor, self.master_fd = self.master_fd, -1
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        self.exit_code = self.proc.returncode
        self.done = True

    # ---- input ----
    def write(self, data: bytes) -> None:
        with self._io_lock:
            if self._stopping.is_set() or self.master_fd < 0:
                return
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
        self._stopping.set()
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=3)


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


# ------------------------------------------------------------------ tmux sessions
#
# The tmux server is started (by the first `new-session`) under the dashboard's cleaned
# environment only. A business's `.env` reaches its session through `new-session -e K=V` (session
# scope): the server's global environment never holds a business secret, so nothing leaks between
# businesses. `-e` puts the values in the argv of `new-session -d`, a process that exits within
# milliseconds; the long-lived `attach-session` client carries no secrets. An already-running
# session keeps the env it was created with: a changed `.env` needs `kill-session` + a fresh open.
#
# Server options come from a config file passed with `-f` (read when the server starts): no status
# bar and no prefix key (the terminal is transparent, Ctrl-B reaches the shell), tmux-256color
# inside, deep history, mouse on so the wheel scrolls tmux history in the browser and over ssh. Window size is `manual` per window (set right after creation) so the
# browser is the authority (resize_session); a global `window-size manual` crashes tmux 3.4 when a
# second detached session is created, so it is never set globally.
#
# Targets: `=NAME` is an exact session match; window commands take `=NAME:`.

TMUX = shutil.which("tmux")
TMUX_SOCK = "bazarlyk"
TMUX_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")
TMUX_CONF = """\
set -g status off
set -g prefix None
set -g history-limit 50000
set -sg escape-time 0
set -g default-terminal tmux-256color
set -g mouse on
"""
_NAME_BAD = re.compile(r"[^A-Za-z0-9-]")


def tmux_available() -> bool:
    return TMUX is not None


def session_name(slug: str, agent: str) -> str:
    """`<slug>__<agent>`. tmux forbids `.` and `:` in names and gives `@ % $` meaning in targets, so
    the slug is reduced to letters, digits and dashes; it then cannot contain `__`, which makes the
    split unambiguous (an agent name may contain underscores)."""
    return f"{_NAME_BAD.sub('-', slug)}__{agent}"


def session_prefix(slug: str) -> str:
    return f"{_NAME_BAD.sub('-', slug)}__"


def _conf_path() -> str:
    d = TMUX_CONFIG_DIR
    p = os.path.join(d, f"tmux-{TMUX_SOCK}.conf")
    try:
        if os.path.islink(d):
            return "/dev/null"
        os.makedirs(d, mode=0o700, exist_ok=True)
        existing = None
        try:
            with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as fh:
                existing = fh.read()
        except FileNotFoundError:
            pass
        if existing != TMUX_CONF:
            with os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w", encoding="utf-8") as fh:
                fh.write(TMUX_CONF)
    except OSError:
        pass
    return p


def tmux(*args: str, env: dict | None = None) -> tuple[int, str]:
    """Run one tmux command against the bazarlyk server. (rc, combined output)."""
    if TMUX is None:
        return 127, "tmux not installed"
    try:
        r = subprocess.run([TMUX, "-L", TMUX_SOCK, "-f", _conf_path(), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=20,
                           env=clean_env() if env is None else env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def session_alive(name: str) -> bool:
    return tmux("has-session", "-t", f"={name}")[0] == 0


def ensure_session(name: str, cmd: list[str], cwd: str, env: dict | None = None,
                   cols: int = 120, rows: int = 32) -> bool:
    """Create the session running `cmd` in `cwd` unless it exists. `env` is session-scoped.
    Returns True when it was created. Two concurrent creators: the loser's `new-session` fails
    with "duplicate session", which is the same outcome, so it is not an error."""
    if session_alive(name):
        return False
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", str(cols), "-y", str(rows)]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    rc, out = tmux(*args, "--", *cmd)
    if rc and "duplicate session" not in out:
        raise RuntimeError(f"tmux new-session failed: {out.strip()}")
    if rc == 0:
        tmux("set-option", "-w", "-t", f"={name}:", "window-size", "manual")
    return rc == 0


def attach_cmd(name: str) -> list[str]:
    return [TMUX, "-L", TMUX_SOCK, "-f", _conf_path(), "attach-session", "-t", f"={name}"]


def resize_session(name: str, rows: int, cols: int) -> None:
    tmux("resize-window", "-t", f"={name}:", "-x", str(cols), "-y", str(rows))


def kill_session(name: str) -> bool:
    """True when a session was there to kill."""
    return tmux("kill-session", "-t", f"={name}")[0] == 0


def list_sessions(prefix: str = "") -> list[str]:
    rc, out = tmux("list-sessions", "-F", "#{session_name}")
    if rc:
        return []
    return [n for n in out.split() if n.startswith(prefix)]
