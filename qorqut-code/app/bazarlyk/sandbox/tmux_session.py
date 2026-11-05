"""Create one native tmux session from a trusted JSON stdin request.

This module is executed inside an agent container.  It deliberately has no
network or Qorqut imports: the caller supplies only a validated shell, working
directory, dimensions, and session environment over stdin.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys


_SESSION = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_LOCK = "/tmp/qorqut-tmux-session.lock"
_MAX_INPUT = 256 * 1024
_TMUX = "/usr/bin/tmux"


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run((_TMUX, *args), capture_output=True, text=True,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("tmux could not be invoked") from None


def _validate(request: object) -> dict:
    if not isinstance(request, dict) or set(request) != {"session", "shell", "cwd", "env", "cols", "rows"}:
        raise ValueError("invalid session request")
    session, shell, cwd, env, cols, rows = (request[key] for key in ("session", "shell", "cwd", "env", "cols", "rows"))
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise ValueError("invalid session")
    if (not isinstance(shell, list) or not shell
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in shell)):
        raise ValueError("invalid shell")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd:
        raise ValueError("invalid cwd")
    if (not isinstance(env, dict) or any(not isinstance(key, str) or not key or "\0" in key
                                         or not isinstance(value, str) or "\0" in value
                                         for key, value in env.items())):
        raise ValueError("invalid environment")
    if (type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500
            or not 5 <= rows <= 250):
        raise ValueError("invalid dimensions")
    return {"session": session, "shell": shell, "cwd": cwd, "env": env, "cols": cols, "rows": rows}


def ensure(request: dict) -> int:
    session = request["session"]
    # One lock covers all sessions for this container's tmux user.  The second
    # has-session check makes concurrent requests idempotent and avoids ever
    # replacing the environment of an existing session.
    Path(_LOCK).touch(mode=0o600, exist_ok=True)
    with open(_LOCK, "r+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                existing = _run("has-session", "-t", "=" + session)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            if existing.returncode == 0:
                return 0
            if existing.returncode != 1:
                raise ValueError("tmux session check failed")

            env_args = []
            for key, value in request["env"].items():
                env_args.extend(("-e", key + "=" + value))
            try:
                created = _run("new-session", "-d", "-s", session, "-c", request["cwd"],
                               "-x", str(request["cols"]), "-y", str(request["rows"]),
                               *env_args, "--", *request["shell"])
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            if created.returncode:
                # A request can race with a creator outside this helper.  Do
                # not treat any other tmux failure as a successful ensure.
                try:
                    raced = _run("has-session", "-t", "=" + session)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from None
                if raced.returncode == 0:
                    return 0
                raise ValueError("tmux session creation failed")
            try:
                configured = _run("set-option", "-w", "-t", "=" + session + ":", "window-size", "manual")
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            if configured.returncode:
                raise ValueError("tmux session sizing failed")
            return 0
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            return _fail("session request is too large")
        request = _validate(json.loads(raw.decode("utf-8")))
        return ensure(request)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
