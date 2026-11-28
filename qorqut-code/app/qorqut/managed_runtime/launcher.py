"""Start a managed agent host detached, and find and verify one that is already running.

``prepare_launch`` writes the private ``launch.json`` (0600) the host reads and unlinks at once; the agent's resolved environment
and the control token travel only in that file, never in argv. A launcher then starts
``python -B -P -m qorqut.managed_runtime.host --state <dir>`` (working directory ``/``: neither the state directory, which the agent's
own process could write to, nor the agent folder is ever on ``sys.path``; ``-P`` where the interpreter has it, Python 3.11+) with a
small allow-listed environment of its own. ``-I`` is not used because it would ignore the ``PYTHONPATH`` this host needs to find
the application's code. Under systemd only variables that cannot carry credentials are passed as ``--setenv=`` (visible in
``ps``); the others (proxy URLs may embed a user and password) go to the host as ``host_env`` in ``launch.json``:

* ``DetachedLauncher``: a detached child in a session of its own, for folder installs and macOS (the
  ``browser/native_launcher.py`` pattern).
* ``UserUnitLauncher``: a transient user service (``qorqut-managed-<installation hash>-<agent>.service``) for Linux
  service installs, because a session of the child's own does not survive a service restart (the service's cgroup is
  killed). It follows ``agent_browsers.UserUnitLauncher``: fixed argv, derived unit name, paths taken through
  ``outside_private_mounts``.
  A service that sees its instance through a private bind mount is the exception (``default_launcher``): its host starts
  detached, inside the service's mount namespace.

``default_launcher`` chooses between them by the rule ``AgentBrowserManager`` uses. After a service restart
``inspect_host``/``running_hosts`` find hosts again by their lock, process identity, command line, socket peer and ``hello``;
anything that does not check out is "absent" (only when its lock is free) or "unverified" (lock held, not provably ours), and
nothing is ever killed by name or port. The macOS paths here have not been run on a Mac.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from qorqut_system import places as _places
from qorqut_system import platform as _platform
from qorqut_system import private_files as _private_files
from qorqut_system import processes as _processes
from qorqut_system.errors import UnsupportedSystem

from . import state as layout
from .client import HostClient, HostUnavailable

UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,150}\.service")
UNIT_RUNNING = {"active", "activating", "reloading"}
START_TIMEOUT = 45.0

# What the host process itself inherits. Nothing else from the service's environment: the agent's own environment (with its
# secrets) reaches only the claude process, through launch.json.
ENVIRONMENT_ALLOWLIST = (
    "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "SHELL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")


# Variables whose values cannot carry a credential: the only ones that may appear in a command line (``--setenv``).
ARGV_SAFE_ENVIRONMENT = frozenset({
    "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "SHELL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE", "QORQUT_FOLDER_ROOT", "QORQUT_NATIVE_ROOT", "LD_LIBRARY_PATH", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR"})
NEUTRAL_DIRECTORY = "/"


def split_environment(environment: dict):
    """``(argv_safe, private)``: the variables that may be passed in a command line, and those that must not be."""
    safe = {key: value for key, value in environment.items() if key in ARGV_SAFE_ENVIRONMENT}
    return safe, {key: value for key, value in environment.items() if key not in ARGV_SAFE_ENVIRONMENT}


class LaunchError(RuntimeError):
    """The host could not be started or verified. ``reason`` is a short code (``host_unverified``, ``invalid_launch``,
    ``driver_unavailable``, ``host_did_not_start``, ``not_ready``, ``systemd_unavailable`` ...); ``detail`` is bounded."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason + (": " + detail if detail else ""))
        self.reason, self.detail = reason, detail


# -- launch.json -----------------------------------------------------------------------------------------------------------

def new_token() -> str:
    return secrets.token_urlsafe(32)


def prepare_launch(state_dir, spec: dict) -> dict:
    """Validate ``spec`` and write ``launch.json`` (0600) into ``state_dir``. Returns the stored record, which contains the
    generated ``token`` and ``instance``; the caller must not log it.

    ``spec`` keys: ``agent_id`` (equals the directory name), ``folder`` (the prepared agent workspace, absolute),
    ``executable`` (the claude binary), ``args`` (list of str, no driver-owned flags), ``env`` (name -> value, ``None`` removes),
    ``resume_session_id`` or ``None``, ``send_role`` (``True`` sends ``/role`` on a fresh session; a string sends that text),
    ``idle_seconds`` (default 900; 0 disables), ``driver`` (``module:Class``; default the Claude driver), and the optional
    ``client`` (``claude`` or ``codex``: what the host's role command and permission answers key on), ``permission_mode`` (``bypass``
    or ``deny``: how the host answers the client's permission requests itself; anything else is ``deny``), ``model`` and ``effort``
    (the owner's launch options, for a driver that takes them other than as arguments), and the optional
    ``socket_path``, ``close_seconds``, ``ready_seconds`` (how long the Claude process must have stayed up before the host is
    ready, default 0.4; 0 waits for the driver's announcement), ``event_limit``, ``token`` and ``instance``. The launcher may add
    ``host_env`` (the host process's credential-bearing environment, see ``UserUnitLauncher``)."""
    state_dir = Path(state_dir)
    try:
        agent_id = spec["agent_id"]
        if str(uuid.UUID(agent_id)) != agent_id or agent_id != state_dir.name:
            raise ValueError("agent_id")
        if not (isinstance(spec["folder"], str) and os.path.isabs(spec["folder"]) and os.path.isdir(spec["folder"])):
            raise ValueError("folder")
        if not (isinstance(spec["executable"], str) and spec["executable"]):
            raise ValueError("executable")
        args, env = list(spec.get("args") or []), dict(spec.get("env") or {})
        if not all(isinstance(item, str) for item in args):
            raise ValueError("args")
        if not all(isinstance(key, str) and key and (value is None or isinstance(value, str)) for key, value in env.items()):
            raise ValueError("env")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise LaunchError("invalid_launch", "bad launch field: %s" % (error.args[0] if error.args else "spec")) from None
    record = {key: spec[key] for key in ("socket_path", "close_seconds", "ready_seconds", "event_limit", "driver", "client",
                                                   "permission_mode", "model", "effort") if spec.get(key) is not None}
    record.update(schema_version=layout.SCHEMA_VERSION, agent_id=agent_id, folder=spec["folder"], executable=spec["executable"],
                  args=args, env=env, resume_session_id=spec.get("resume_session_id"), send_role=spec.get("send_role", False),
                  idle_seconds=spec.get("idle_seconds", 900), token=spec.get("token") or new_token(),
                  instance=spec.get("instance") or str(uuid.uuid4()))
    layout.write_json_private(state_dir / layout.LAUNCH_FILE, record)
    return record


def host_environment(extra: dict | None = None) -> dict:
    """The environment of the host process: the allow-list above, the installation's paths (``native_paths``) and a
    ``PYTHONPATH`` that makes the host run this service's code (relative entries made absolute)."""
    from qorqut import native_paths
    env = {key: os.environ[key] for key in ENVIRONMENT_ALLOWLIST if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env.setdefault("LANG", "C.UTF-8")
    env = native_paths.environment(env)
    import qorqut
    entries = [str(Path(qorqut.__file__).resolve().parent.parent)]
    entries += [os.path.abspath(item) for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entries))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


def host_argv(state_dir, python=None) -> list:
    flags = ["-B"]
    if python in (None, sys.executable) and sys.version_info >= (3, 11):
        flags.append("-P")          # do not put the working directory (or the script's) on sys.path
    return [python or sys.executable, *flags, "-m", layout.HOST_MODULE, "--state", str(state_dir)]


# -- launchers ---------------------------------------------------------------------------------------------------------------

class DetachedLauncher:
    """Folder installs and macOS: a detached child in its own session, output to ``host.log``."""

    def __init__(self, extra_environment: dict | None = None):
        self.extra_environment = dict(extra_environment or {})
        self.exit_codes: dict = {}

    def active(self, state_dir) -> bool:
        return layout.lock_held(Path(state_dir) / layout.LOCK_FILE)

    def start(self, state_dir) -> bool:
        """Start a host for ``state_dir`` (whose ``launch.json`` is written). ``False`` when one already holds the lock."""
        state_dir = Path(state_dir)
        if self.active(state_dir):
            return False
        with _private_files.open_private_file(state_dir, layout.LOG_FILE, append=True) as log:
            process = _processes.start_in_background(host_argv(state_dir), env=host_environment(self.extra_environment),
                                                     cwd=NEUTRAL_DIRECTORY, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                     close_fds=True)
        # The service lives on, so the child must be reaped when it exits; after a service restart init does it.
        threading.Thread(target=self._reap, args=(process, str(state_dir)), name="managed-host-reaper", daemon=True).start()
        return True

    def _reap(self, process, key):
        self.exit_codes[key] = process.wait()

    def stop(self, state_dir):
        """Nothing to do: the host is closed through its control socket, never by signal from here."""


def unit_name(state_dir) -> str:
    """The transient user service of one agent's host on this installation: derived, never supplied by a caller."""
    state_dir = Path(state_dir)
    return "qorqut-managed-%s-%s.service" % (hashlib.sha256(str(state_dir.parent).encode()).hexdigest()[:12], state_dir.name)


def _outside_private_mounts(path):
    """The same path by one the user manager's namespace also has; today's resolution, now in the layer."""
    return _places.outside_private_mounts(path)


class UserUnitLauncher:
    """Linux service installs: the host runs as one transient user service, outside the application's cgroup.

    A unit that is already running is reused, never duplicated. The application user needs a running user manager (lingering).
    Not run against a real service install here: tests inject ``runner``."""

    def __init__(self, runner=subprocess.run, sleep=time.sleep, outside=_outside_private_mounts, extra_environment=None):
        self.runner, self.sleep, self.outside = runner, sleep, outside
        self.extra_environment = dict(extra_environment or {})

    @staticmethod
    def _require_service_manager():
        """Refuse where no session host runs under the user's service manager; today's own message for that."""
        if _platform.family() != "linux":
            raise LaunchError("systemd_unavailable", "systemd services are supported only on Linux")

    def state(self, unit) -> str:
        if not UNIT.fullmatch(unit):
            raise LaunchError("invalid_launch", "the host unit name is invalid")
        self._require_service_manager()
        try:
            return _places.session_host_state(unit, runner=self.runner)
        except UnsupportedSystem:
            raise LaunchError("systemd_unavailable", "the application user's systemd manager is not reachable (enable lingering "
                              "for that user and check its user manager)") from None
        except (OSError, subprocess.SubprocessError) as error:
            raise LaunchError("systemd_unavailable", _places.SYSTEMCTL_NAME + " could not be run: "
                              + type(error).__name__) from error

    def active(self, state_dir) -> bool:
        return self.state(unit_name(state_dir)) in UNIT_RUNNING

    def start(self, state_dir) -> bool:
        """``False`` when that exact unit was already running (nothing new is launched)."""
        unit = unit_name(state_dir)
        for _ in range(50):                      # a unit that is still stopping must finish before it can start again
            current = self.state(unit)
            if current != "deactivating":
                break
            self.sleep(0.2)
        if current in UNIT_RUNNING:
            return False
        directory = Path(self.outside(state_dir))
        argv_safe, private = split_environment(host_environment(self.extra_environment))
        if private:
            _add_host_environment(Path(state_dir), private)
        python = str(self.outside(sys.executable))
        flags = ["-B", "-P"] if sys.version_info >= (3, 11) else ["-B"]
        self._require_service_manager()
        try:
            done = _places.run_session_host(unit=unit, description="Qorqut managed agent host",
                                            working_directory=NEUTRAL_DIRECTORY, stop_timeout=40,
                                            command=[python, *flags, "-m", layout.HOST_MODULE, "--state", str(directory)],
                                            log_directory=directory, log_name=layout.LOG_FILE, environment=argv_safe,
                                            runner=self.runner)
        except (OSError, subprocess.SubprocessError) as error:
            raise LaunchError("systemd_unavailable", _places.SYSTEMCTL_NAME + " could not be run: "
                              + type(error).__name__) from error
        if done.returncode != 0:
            if self.state(unit) in UNIT_RUNNING:  # raced with another start of the same unit
                return False
            first = (done.stderr.strip().splitlines() or ["no detail"])[0]
            raise LaunchError("systemd_unavailable", _places.SYSTEMD_RUN_NAME + " could not start the host: " + first[:200])
        return True

    def stop(self, state_dir):
        """Stop exactly this host's unit; only after a verified host ignored its own close request."""
        self._require_service_manager()
        try:
            _places.stop_session_host(unit_name(state_dir), runner=self.runner)
        except (OSError, subprocess.SubprocessError) as error:
            raise LaunchError("systemd_unavailable", _places.SYSTEMCTL_NAME + " could not be run: "
                              + type(error).__name__) from error


def _add_host_environment(state_dir: Path, private: dict) -> None:
    """Put the environment that must not be in argv into the (0600) launch file the host reads and unlinks at once."""
    path = state_dir / layout.LAUNCH_FILE
    record = layout.read_json(path, layout.LAUNCH_LIMIT)
    if record is None:
        raise LaunchError("invalid_launch", "launch.json is missing or not private")
    record["host_env"] = dict(private)
    layout.write_json_private(path, record)


def behind_private_mount(path) -> bool:
    """Whether this service sees ``path`` only through a bind mount of its own (a unit with ``BindPaths=``)."""
    try:
        return Path(_outside_private_mounts(path)) != Path(path)
    except Exception:  # noqa: BLE001 - not knowing is "no": the ordinary rule applies
        return False


def default_launcher(extra_environment=None, state_dir=None):
    """The rule ``AgentBrowserManager`` uses: a detached process for folder and native installs and for macOS, a transient user
    service for a Linux service install.

    One exception: a service that sees its instance through a private bind mount starts the host detached, inside its own mount
    namespace. A user service runs outside that namespace, where the agent's folder, its prepared settings and hooks, and the
    folder name its conversations are filed under either do not exist or are a different directory; only the state directory
    could be translated. Such a host lives as long as the unit lets a child live: across a restart with ``KillMode=process``
    (as agent terminals did), otherwise it ends with the service and the session opens again at the next input."""
    from qorqut import native_paths
    if native_paths.root() is not None or native_paths.folder_root() is not None or _platform.family() == "mac":
        return DetachedLauncher(extra_environment)
    if state_dir is not None and behind_private_mount(state_dir):
        return DetachedLauncher(extra_environment)
    return UserUnitLauncher(extra_environment=extra_environment)


# -- finding and verifying ---------------------------------------------------------------------------------------------------

@dataclass
class VerifiedHost:
    """A host that passed every check. ``hello`` is the identity it reported; ``client`` talks to exactly this instance."""
    state_dir: Path
    agent_id: str
    instance: str
    host_pid: int
    host_identity: str
    claude_pid: int | None
    claude_identity: str | None
    session_id: str | None
    transcript_path: str | None
    state: str
    hello: dict = field(repr=False, default_factory=dict)
    agent_client: str | None = None   # ``claude`` or ``codex`` (``client()`` is the connection)

    @property
    def owner(self) -> str:
        return layout.host_owner(self.host_pid, self.host_identity)

    def client(self, timeout: float = 5.0) -> HostClient:
        return HostClient(self.state_dir, timeout=timeout, expect_instance=self.instance)


@dataclass
class Inspection:
    """``status``: ``running`` (``host`` set), ``starting`` (lock held, the host has not finished publishing), ``unverified``
    (lock held, but not provably this module for this directory: leave it alone), or ``absent`` (lock free)."""
    status: str
    reason: str = ""
    host: VerifiedHost | None = None


NEGATIVE_CACHE_SECONDS = 4.0
_negative: dict = {}
_negative_lock = threading.Lock()


def _negative_get(key, clock):
    with _negative_lock:
        found = _negative.get(key)
        if found is not None and clock() - found[0] < NEGATIVE_CACHE_SECONDS:
            return found[1]
        _negative.pop(key, None)
    return None


def _negative_put(key, reason, clock):
    with _negative_lock:
        if len(_negative) > 256:
            _negative.clear()
        _negative[key] = (clock(), reason)


def inspect_host(state_dir, *, timeout: float = 3.0, clock=time.monotonic) -> Inspection:
    """The four checks of a running host (a host that holds the lock but does not answer ``hello`` is remembered as such for
    ``NEGATIVE_CACHE_SECONDS``, so repeated calls do not each wait ``timeout``): (1) ``host.lock`` is held; (2) ``host.json``'s pid has the recorded identity and a
    ``qorqut.managed_runtime.host --state <dir>`` command line; (3) the socket's peer pid is that pid; (4) ``hello`` with the
    token returns the same agent and instance and a Claude process that descends from the host."""
    state_dir = Path(state_dir)
    if not layout.lock_held(state_dir / layout.LOCK_FILE):
        return Inspection("absent", "lock_free")
    record = layout.read_host(state_dir)
    if record is None or record["state"] == "starting" and not (state_dir / layout.TOKEN_FILE).exists():
        return Inspection("starting", "no_record")
    pid, identity = record.get("host_pid"), record.get("host_identity")
    if not isinstance(pid, int) or not identity or not layout.process_alive(pid, identity):
        return Inspection("unverified", "host_identity")
    if not layout.host_command_matches(pid, state_dir):
        return Inspection("unverified", "host_command")
    if record["state"] == "starting":
        return Inspection("starting", "starting")
    # A host that holds the lock but does not answer costs ``timeout`` seconds per call: remember that for a few seconds, keyed by
    # exactly this host (pid, identity, instance), so repeated inspections (a page poll, ``running_hosts``) return at once.
    key = (str(state_dir), pid, identity, record.get("instance"))
    cached = _negative_get(key, clock)
    if cached is not None:
        return Inspection("unverified", cached)
    try:
        hello = HostClient(state_dir, timeout=timeout, expect_instance=record.get("instance")).hello()
    except HostUnavailable as error:
        if error.reason == "timeout":
            _negative_put(key, "hello_" + error.reason, clock)
        return Inspection("unverified", "hello_" + error.reason)
    if (hello.get("status") != "ok" or hello.get("agent_id") != state_dir.name or hello.get("agent_id") != record.get("agent_id")
            or hello.get("instance") != record.get("instance") or hello.get("host_pid") != pid
            or hello.get("host_identity") != identity):
        return Inspection("unverified", "hello_mismatch")
    claude_pid, claude_identity = hello.get("claude_pid"), hello.get("claude_identity")
    if claude_pid is not None:
        if not layout.descends_from(claude_pid, pid):
            return Inspection("unverified", "claude_not_descendant")
        if claude_identity is not None and not layout.process_alive(claude_pid, claude_identity):
            return Inspection("unverified", "claude_identity")
    elif hello.get("state") == "ready":
        return Inspection("unverified", "claude_missing")
    return Inspection("running", "", VerifiedHost(
        state_dir=state_dir, agent_id=hello["agent_id"], instance=hello["instance"], host_pid=pid, host_identity=identity,
        claude_pid=claude_pid, claude_identity=claude_identity, session_id=hello.get("session_id"),
        transcript_path=hello.get("transcript_path"), state=hello.get("state") or record["state"], hello=hello,
        agent_client=hello.get("client") if isinstance(hello.get("client"), str) else None))


def verify_host(state_dir, **options) -> VerifiedHost | None:
    inspection = inspect_host(state_dir, **options)
    return inspection.host if inspection.status == "running" else None


def running_hosts(root, *, clean: bool = False) -> dict:
    """``{agent_id: VerifiedHost}`` for every verified host under ``root`` (``<runtime>/managed-agents``). With ``clean`` a
    directory whose lock is free has its stale socket, token and launch file removed. Hosts that are starting or unverified are
    neither returned nor touched."""
    found = {}
    root = Path(root)
    if not root.is_dir():
        return found
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        inspection = inspect_host(directory)
        if inspection.status == "running":
            found[directory.name] = inspection.host
        elif inspection.status == "absent" and clean:
            layout.clean_stale(directory)
    return found


def wait_until_gone(state_dir, timeout: float = 30.0, sleep=time.sleep) -> bool:
    """The host's lock is free (it exited)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not layout.lock_held(Path(state_dir) / layout.LOCK_FILE):
            return True
        sleep(0.05)
    return not layout.lock_held(Path(state_dir) / layout.LOCK_FILE)


def start_host(state_dir, spec: dict, *, launcher=None, timeout: float = START_TIMEOUT, sleep=time.sleep) -> VerifiedHost:
    """Open a session: return the verified host already running for this agent, or launch one and wait until it is ``ready``.

    The caller holds the agent's lifecycle lock. Raises ``LaunchError``. A host that is still starting when ``timeout`` ends is
    left alone (nothing is killed); repeating the call waits for the same start. ``launch.json`` never outlives a failure."""
    try:
        state_dir = layout.private_directory(state_dir)
    except (OSError, ValueError):
        raise LaunchError("invalid_launch", "the state directory is not private to this user") from None
    launcher = launcher or default_launcher(state_dir=state_dir)
    deadline = time.monotonic() + timeout
    inspection = inspect_host(state_dir)
    if inspection.status == "running":
        return inspection.host
    if inspection.status == "unverified":
        raise LaunchError("host_unverified", inspection.reason)
    instance = None
    if inspection.status == "absent":
        layout.clean_stale(state_dir)
        try:
            (state_dir / layout.HOST_FILE).unlink()   # a finished host's record is not this start's
        except FileNotFoundError:
            pass
        record = prepare_launch(state_dir, spec)
        instance = record["instance"]
        try:
            launcher.start(state_dir)
        except Exception:
            _discard_launch(state_dir)
            raise
    started = time.monotonic()
    try:
        while True:
            current = layout.read_host(state_dir)
            if current is not None and (instance is None or current.get("instance") in (instance, None)):
                if current["state"] == "ready":
                    verified = inspect_host(state_dir)
                    if verified.status == "running":
                        return verified.host
                elif current["state"] in ("failed", "closed"):
                    exit_record = current.get("exit") or {}
                    raise LaunchError(str(exit_record.get("reason") or "host_failed"), str(exit_record.get("detail") or ""))
            if (not layout.lock_held(state_dir / layout.LOCK_FILE) and time.monotonic() - started > 2.0
                    and (current is None or current["state"] in layout.LIVE_STATES)):
                raise LaunchError("host_did_not_start", "the host exited without reporting")
            if time.monotonic() >= deadline:
                raise LaunchError("not_ready", "the session did not become ready in time")
            sleep(0.05)
    finally:
        if instance is not None:
            _discard_launch(state_dir)


def _discard_launch(state_dir):
    try:
        (Path(state_dir) / layout.LAUNCH_FILE).unlink()
    except FileNotFoundError:
        pass
