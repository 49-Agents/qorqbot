"""Whether this computer can run agents in terminals (the legacy Terminals mode): is there a tmux that runs?

Qorqbot does not ship tmux. Terminals mode is offered only where the owner's computer already has it, found on the service's
own ``PATH`` (the same lookup the terminal broker does when it is loaded). ``availability()`` never raises and is cached
briefly, so Settings can ask on every read without starting a process each time.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

TMUX_MISSING = "tmux_missing"
TMUX_BROKEN = "tmux_broken"
CACHE_SECONDS = 30.0
VERSION_TIMEOUT = 5

REASONS = {
    TMUX_MISSING: "Terminals need tmux, which is not installed on this computer.",
    TMUX_BROKEN: "The tmux on this computer does not start, so Terminals cannot be used.",
}

_lock = threading.Lock()
_cached: tuple[float, dict] | None = None


def find_tmux():
    """The tmux program on the service's ``PATH``, or ``None``."""
    return shutil.which("tmux")


def _probe(which=find_tmux, run=subprocess.run) -> dict:
    path = which()
    if not path:
        return {"available": False, "reason": TMUX_MISSING, "detail": REASONS[TMUX_MISSING], "path": None, "version": None}
    try:
        # ``tmux -V`` prints the version and talks to no server.
        done = run([path, "-V"], capture_output=True, text=True, timeout=VERSION_TIMEOUT, check=False)
        text = (done.stdout or "").strip()
        ok = done.returncode == 0 and bool(text)
    except (OSError, subprocess.SubprocessError):
        text, ok = "", False
    if not ok:
        return {"available": False, "reason": TMUX_BROKEN, "detail": REASONS[TMUX_BROKEN], "path": path, "version": None}
    found = re.search(r"[0-9][0-9A-Za-z.\-]*", text)
    return {"available": True, "reason": None, "detail": None, "path": path, "version": found.group(0) if found else text}


def availability(*, refresh=False, which=find_tmux, run=subprocess.run, clock=time.monotonic) -> dict:
    """``{"available", "reason", "detail", "path", "version"}``. ``reason`` is ``None``, ``tmux_missing`` or ``tmux_broken``;
    ``detail`` is the sentence the owner reads."""
    global _cached
    now = clock()
    with _lock:
        if not refresh and _cached is not None and now - _cached[0] < CACHE_SECONDS:
            return dict(_cached[1])
    result = _probe(which, run)
    with _lock:
        _cached = (now, result)
    return dict(result)


def available() -> bool:
    return availability()["available"]
