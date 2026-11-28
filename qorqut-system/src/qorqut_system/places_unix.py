"""Where things live on Linux and macOS, and how a session host runs under the user's service manager.

The lines are today's: the folders the managed host state, the managed launcher, the browser supervisor and the Telegram
bridge use, the socket path limits of the two systems, and the transient ``systemd-run --user`` service of a Linux
service installation. The mount table is today's read of the running service's own private bind mounts.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from .errors import UnsupportedSystem

SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"
# The same two programs by the names today's messages write them under.
SYSTEMCTL_NAME = "systemctl"
SYSTEMD_RUN_NAME = "systemd-run"
# A working directory that belongs to no service and no private mount, so a started unit cannot see this process's folder.
NEUTRAL_DIRECTORY = "/"
START_TIMEOUT = 30
# How long one query about a unit, and one request to stop it, may take.
UNIT_STATE_TIMEOUT = 10
UNIT_STOP_TIMEOUT = 60


def data_folder() -> Path:
    """The folder a per-user installation keeps its data in: ``$XDG_STATE_HOME``, or ``~/.local/state``."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def run_folder() -> Path | None:
    """The user's own short runtime folder (``/run/user/<uid>``), or ``None`` when it is missing or not this user's.

    Only a real directory of this user is returned; a caller that gets ``None`` uses the temporary folder instead."""
    runtime = Path("/run/user/%d" % os.getuid())
    try:
        if not runtime.is_symlink() and runtime.is_dir() and runtime.stat().st_uid == os.geteuid():
            return runtime
    except OSError:
        pass
    return None


def temporary_folder() -> Path:
    """The shared temporary folder this process is told to use (``TMPDIR`` when it is set)."""
    return Path(tempfile.gettempdir())


def channel_path_limit() -> int:
    """The byte limit of a local channel's path, its final NUL included: 108 on Linux and 104 on macOS."""
    return 104 if sys.platform == "darwin" else 108


def channel_path_fits(path) -> bool:
    """Whether a channel may be bound at ``path``: the address must be shorter than the limit, final NUL included."""
    return len(os.fsencode(str(path))) < channel_path_limit()


def service_manager_available() -> bool:
    """A session host can run as a transient user service: only on Linux, and only where systemd is installed."""
    return sys.platform.startswith("linux") and Path(SYSTEMD_RUN).is_file()


def require_service_manager() -> None:
    """Refuse, before any effect, a caller that needs the service manager where there is none."""
    if not service_manager_available():
        raise UnsupportedSystem("A session host needs the systemd user manager, which is available on Linux only")


def service_manager_environment() -> dict:
    """The environment a ``systemctl`` and ``systemd-run`` call runs with (today's lines in the managed launcher)."""
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    runtime = "/run/user/%d" % os.getuid()
    environment["XDG_RUNTIME_DIR"] = runtime
    if Path(runtime, "bus").exists():
        environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % runtime
    return environment


def session_host_command(*, unit, description, working_directory, stop_timeout, command, log_directory=None,
                         log_name=None, environment=()) -> list:
    """The ``systemd-run --user`` line that starts one transient user service for a session host.

    ``command`` is the program and its arguments; ``log_directory`` and ``log_name`` append the unit's output to one file
    inside it, and ``environment`` becomes ``--setenv`` entries, so every value in it is visible in a command line."""
    argv = [SYSTEMD_RUN, "--user", "--unit=" + unit, "--collect", "--quiet", "--description=" + description,
            "--working-directory=" + str(working_directory), "--property=TimeoutStopSec=%d" % int(stop_timeout)]
    if log_directory is not None:
        argv += ["--property=StandardOutput=append:%s/%s" % (log_directory, log_name),
                 "--property=StandardError=append:%s/%s" % (log_directory, log_name)]
    argv += ["--setenv=%s=%s" % item for item in dict(environment).items()]
    return argv + ["--", *[str(part) for part in command]]


def _run(argv, *, runner, timeout) -> subprocess.CompletedProcess:
    """Run one service manager call the way every call site runs it today; ``OSError`` when it cannot be run at all."""
    return runner(argv, env=service_manager_environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  timeout=timeout, check=False)


def run_session_host(*, unit, description, working_directory, stop_timeout, command, log_directory=None, log_name=None,
                     environment=(), runner=subprocess.run, timeout=START_TIMEOUT) -> subprocess.CompletedProcess:
    """Start that unit and return the finished ``systemd-run`` call; ``OSError`` when it cannot be run at all."""
    argv = session_host_command(unit=unit, description=description, working_directory=working_directory,
                                stop_timeout=stop_timeout, command=command, log_directory=log_directory,
                                log_name=log_name, environment=environment)
    return _run(argv, runner=runner, timeout=timeout)


def session_host_state(unit, *, runner=subprocess.run) -> str:
    """One transient user service's own state: its ``ActiveState``, and ``inactive`` when no unit of that name is loaded.

    ``UnsupportedSystem`` when the application user's systemd manager cannot be reached at all, which is what a user
    whose manager is not running answers; a caller translates that into its own error."""
    done = _run([SYSTEMCTL, "--user", "show", unit, "--property=LoadState,ActiveState"], runner=runner,
                timeout=UNIT_STATE_TIMEOUT)
    if done.returncode != 0:
        raise UnsupportedSystem("The application user's systemd manager is not reachable")
    values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    return values.get("ActiveState", "inactive") if values.get("LoadState") == "loaded" else "inactive"


def stop_session_host(unit, *, runner=subprocess.run) -> subprocess.CompletedProcess:
    """Stop exactly one transient user service and return the finished call, for a caller that ignores it."""
    return _run([SYSTEMCTL, "--user", "stop", unit], runner=runner, timeout=UNIT_STOP_TIMEOUT)


def service_main_pid(service) -> int:
    """The main process id of the installation's own service, as its service manager reports it.

    The service manager is the one on the search path: an installation's service is a system service, not this user's."""
    return int(subprocess.check_output([SYSTEMCTL_NAME, "show", service, "-p", "MainPID", "--value"]))


_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _mounts(table):
    """``(device, root, mount point)`` for each line of a ``mountinfo`` table, in table order."""
    mounts = []
    for line in table.splitlines():
        fields = line.split(" ")
        if len(fields) >= 5:
            root, point = (_MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value) for value in fields[3:5])
            mounts.append((fields[2], root, point))
    return mounts


def _below(path, parent):
    """``path`` relative to ``parent`` (empty for the directory itself), or ``None`` when it is not inside it."""
    if path == parent:
        return ""
    prefix = parent if parent.endswith("/") else parent + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def outside_private_mounts(path, table=None, same=os.path.samefile):
    """The same file or directory by a path that is also valid outside this process's private bind mounts.

    A service unit with ``BindPaths=`` sees its instance at a path that exists only in its own mount namespace. A transient
    user service starts in the user manager's namespace, where that path is missing or is a different directory. When ``path``
    lies under such a bind mount, this returns the path it has on the same filesystem's own mount point, confirmed to be the
    same file. Every other path, and one that cannot be resolved this way, is returned unchanged."""
    path = Path(path)
    if sys.platform != "linux":
        return path
    try:
        mounts = _mounts(Path("/proc/self/mountinfo").read_text() if table is None else table)
    except OSError:
        return path
    text, over = str(path), None
    for mount in mounts:                 # the deepest mount point holds the path; of equal ones the later mount is on top
        if _below(text, mount[2]) is not None and (over is None or len(mount[2]) >= len(over[2])):
            over = mount
    if over is None or over[1] == "/":   # not below a mount of a subdirectory
        return path
    rest = _below(text, over[2])
    inside = over[1].rstrip("/") + "/" + rest if rest else over[1]
    for device, root, point in sorted(mounts, key=lambda mount: len(mount[1])):
        rest = _below(inside, root)
        if device != over[0] or rest is None or (root, point) == over[1:]:
            continue
        candidate = Path(point) / rest if rest else Path(point)
        try:
            if str(candidate) != text and same(candidate, path):
                return candidate
        except OSError:
            continue
    return path