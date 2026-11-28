"""Lifecycle of the one browser the agents use: status, ensure, repair, restart and stop.

Every agent uses the installation's one **Shared browser**: a managed browser that belongs to no agent (its state directory is
named ``SHARED_ID``) and that all agents use at once, each in its own tabs, so they share its sign-ins. Ordubashy (or the
owner) administers it through ``agent_browser_manage``, which takes only an action: there is no other browser to name.
Nothing here takes a path, executable, endpoint, port, flag, service name or command from the caller; the server derives
everything. Lifecycle state is file-backed under ``<runtime>/agent-browsers/``: private ownership metadata, the Chrome
profile, the supervisor's readiness record and bounded action receipts. The database is only read (authorization and the
list of agents); no host effect runs inside a database transaction.

An agent is connected by the ``shared.json`` record in its own state directory and a target file in its workspace that
names the Shared browser; ``ensure``, ``repair`` and ``restart`` write both for every permanent agent. A small supervisor
(``agent_supervisor``) owns the one headless Chrome with its profile and a loopback CDP port reserved here. Folder
installations and macOS use a detached supervisor; existing Linux service installations use a deterministic transient
*user* service (``systemd-run --user``).
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage import safe_io
from qorqut.bootstrap import browser
from qorqut.bootstrap.client import path_has_symlink, write_json_private
from qorqut.composition.locations import agent_workspace_of
from .agent_supervisor import port_free
from .control import owns_listener, process_identity

ACTIONS = ("status", "ensure", "repair", "restart", "stop")
# The Shared browser's state directory, service unit and locks are named by this fixed ID in place of an agent's. It is a
# version 5 UUID, which no agent has (agents have random version 4 IDs).
SHARED_ID = "da69c2a4-cd9c-5aa7-9a94-176338b13618"
MEMBERSHIP_FILE = "shared.json"
PORTS = range(20400, 21400)          # reserved for managed browsers
CHROME = "/usr/bin/google-chrome-stable"
SYSTEMCTL, SUDO, SYSTEMD_RUN = "/usr/bin/systemctl", "/usr/bin/sudo", "/usr/bin/systemd-run"
UNIT_RUNNING = {"active", "activating", "reloading"}
DEPLOYMENT_DIRECTORIES = (Path("/etc/qorqut-browser-desktops"),)
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,150}\.service")
KEY = re.compile(r"[\x21-\x7e]{1,200}")
META_LIMIT, RECEIPT_LIMIT = 16384, 32
STOP_REQUEST_FILE = "stop-requested.json"
DIAGNOSTIC = 300


def state_root(paths):
    return paths.runtime / "agent-browsers"


def agent_state(paths, agent_id):
    return state_root(paths) / agent_id


def shared_workspace(paths):
    """Where the Shared browser keeps its own target file. It is no agent's workspace."""
    return agent_state(paths, SHARED_ID) / "workspace"


def membership_damaged(paths, agent_id):
    """The agent's record is there but is not exactly what ``ensure`` writes. (A symbolic link is not this: it is
    refused by ``uses_shared``, and no action may write through it.)"""
    record = agent_state(paths, agent_id) / MEMBERSHIP_FILE
    return (not path_has_symlink(record) and record.exists()
            and _read_json(record) != {"schema_version": 1, "agent_id": agent_id, "browser": "shared"})


def uses_shared(paths, agent_id):
    """Whether the agent is connected to the Shared browser. A record that is there but is not exactly what ``ensure`` wrote
    is an error, never read as "not connected": a damaged file must not send the agent's commands anywhere else."""
    record = agent_state(paths, agent_id) / MEMBERSHIP_FILE
    if path_has_symlink(record):
        raise DomainError("browser_metadata_invalid", "The agent's browser record crosses a symbolic link; nothing was changed")
    if not record.exists():
        return False
    if membership_damaged(paths, agent_id):
        raise DomainError("browser_metadata_invalid", "The agent's saved browser record is invalid; nothing was changed. "
                          "Ordubashy replaces it with agent_browser_manage (action repair).")
    return True


def shared_target(paths):
    """The target file content of an agent on the Shared browser, or ``None`` while the Shared browser has no valid ownership
    record."""
    directory = agent_state(paths, SHARED_ID)
    meta = _read_json(directory / "meta.json")
    if meta is None or not meta_valid(meta, SHARED_ID, directory) or meta["mode"] != "managed":
        return None
    return {**meta["target"]["content"], "shared": True}


def _private_directory(path):
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "Browser state paths cannot follow symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise DomainError("invalid_configuration", "Browser state directory is not private to the application user")
    return path


def _lock_file(paths, agent_id, kind):
    return state_root(paths) / "locks" / f"{agent_id}.{kind}"


def _open_lock(path):
    return os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)


@contextmanager
def _nonblocking(path, *, exclusive=True, busy):
    """A try-lock on a private lock file; ``busy`` is the DomainError raised when someone else holds it."""
    descriptor = _open_lock(path)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise busy from None
        yield
    finally:
        os.close(descriptor)


def lifecycle_lock(paths, agent_id):
    """The exclusive, nonblocking lifecycle lock of one browser: one management action at a time."""
    locks = _private_directory(_private_directory(state_root(paths)) / "locks")
    return _nonblocking(locks / f"{agent_id}.lifecycle", busy=DomainError(
        "browser_management_busy", "Another browser management action is in progress", retryable=True))


@contextmanager
def allocation_lock(paths, sleep=time.sleep, wait=5.0):
    """The global lock under which a port is chosen *and* its ownership is written, so no two agents can pick the same free
    port. Acquired without blocking, retried for a bounded time; never held while a browser starts."""
    locks = _private_directory(_private_directory(state_root(paths)) / "locks")
    deadline = time.monotonic() + wait
    while True:
        descriptor = _open_lock(locks / "allocation.lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            os.close(descriptor)
            if time.monotonic() >= deadline:
                raise DomainError("browser_management_busy", "Another browser port allocation is in progress",
                                  retryable=True) from None
            sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)


def _lock_free(path):
    """Whether nobody holds ``path`` (a missing file is free); it never creates the file."""
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)


@contextmanager
def command_use(paths, agent_id):
    """Held (shared) across one browser command against a registered agent browser; a lifecycle mutation that would disturb
    the browser holds it exclusively, so it is refused while a command runs and commands wait out nothing: they are refused
    while a restart or stop is in progress. An unregistered target has no state directory and is untouched."""
    directory = agent_state(paths, agent_id)
    if not directory.is_dir():
        yield
        return
    shared = uses_shared(paths, agent_id)
    owner = SHARED_ID if shared else agent_id
    locks = _private_directory(state_root(paths) / "locks")
    with _nonblocking(locks / f"{owner}.use", exclusive=False, busy=DomainError(
            "browser_maintenance", "The browser is being restarted or stopped; retry shortly", retryable=True)):
        if shared:
            _require_shared_browser(paths)
        else:
            _require_owned_browser(directory, agent_id)
        yield


def _require_shared_browser(paths):
    """With the Shared browser's use lock held: it must be its own supervised Chrome. There is no other browser to fall back
    to, so anything else is an error."""
    directory = agent_state(paths, SHARED_ID)
    meta = _read_json(directory / "meta.json")
    if (meta is None or not meta_valid(meta, SHARED_ID, directory) or meta["mode"] != "managed"
            or supervisor_state(directory, meta)["state"] != "ready"):
        raise DomainError("browser_not_ready", "The Shared browser is not running as its supervised process (it stopped, or "
                          "another process holds its port). Ordubashy repairs it with agent_browser_manage (action repair).")


def _require_owned_browser(directory, agent_id):
    """With the use lock held, for an agent the next ``ensure`` has yet to connect to the Shared browser: a Chrome this
    manager started for it earlier must still be its own supervised Chrome, never whatever happens to answer on its stored
    port. Anything else is left to the caller."""
    meta = _read_json(directory / "meta.json")
    if meta is None or meta.get("mode") != "managed" or not meta_valid(meta, agent_id, directory):
        return
    if supervisor_state(directory, meta)["state"] != "ready":
        raise DomainError("browser_not_ready", "Your browser is not running as its supervised process (it stopped, or "
                          "another process holds its port). Tell Ordubashy; he can repair it with agent_browser_manage.")


def _read_json(path, limit=META_LIMIT):
    try:
        value = json.loads(safe_io.read_bytes(path, limit=limit, private=True))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _alive(pid, identity):
    """The exact process (boot identity + start time) is still running and not a zombie."""
    try:
        if process_identity(pid) != identity:
            return False
        if sys.platform == 'darwin':
            from qorqut.process_info import is_zombie
            return not is_zombie(pid) and process_identity(pid) == identity
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError, TypeError):
        return False


def cdp_healthy(endpoint, timeout=3):
    connection = None
    try:
        target = urlsplit(endpoint)
        if (target.scheme != 'http' or target.hostname not in {'127.0.0.1', '::1', 'localhost'}
                or target.username or target.password or target.path not in {'', '/'}
                or target.query or target.fragment or not target.port):
            return False
        # CDP is a local HTTP probe. Constructing urllib's default opener also
        # built a new TLS context and loaded the CA store on every status poll.
        # This connection uses no proxy, redirects or TLS setup.
        connection = HTTPConnection(target.hostname, target.port, timeout=timeout)
        connection.request('GET', '/json/version')
        with connection.getresponse() as response:
            if response.status != 200:
                return False
            body = response.read(65537)
            return len(body) <= 65536 and bool(json.loads(body).get('webSocketDebuggerUrl'))
    except (OSError, ValueError, AttributeError, HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def _content(endpoint, runtime_dir):
    return {"schema_version": 1, "cdp_endpoint": endpoint, "runtime_dir": runtime_dir}


def _port(endpoint):
    return int(endpoint.rsplit(":", 1)[1])


def _bound(text):
    return text if len(text) <= DIAGNOSTIC else text[:DIAGNOSTIC - 1] + "…"


def unit_name(root, agent_id):
    """The transient user service of one agent's supervisor on this installation: derived, never supplied by a caller."""
    return f"qorqut-agent-browser-{hashlib.sha256(str(root).encode()).hexdigest()[:12]}-{agent_id}.service"


def meta_valid(meta, agent_id, directory):
    try:
        target = meta["target"]
        content = target["content"]
        managed = meta["managed"]
        return (meta["schema_version"] == 1 and meta["agent_id"] == agent_id and meta["mode"] == "managed"
                and set(content) == {"schema_version", "cdp_endpoint", "runtime_dir"} and isinstance(target["workspace"], str)
                and isinstance(managed["port"], int) and 1024 <= managed["port"] <= 65535
                and managed["profile"] == str(directory / "profile") and os.path.isabs(managed["chrome"])
                and managed["unit"] == unit_name(directory.parent, agent_id) and bool(UNIT.fullmatch(managed["unit"]))
                and content["cdp_endpoint"] == f"http://127.0.0.1:{managed['port']}")
    except (KeyError, TypeError):
        return False


def supervisor_state(directory, meta):
    """``{state, pid, chrome_pid, detail}``; ``ready`` only when every recorded identity and the listener still match."""
    held = not _lock_free(directory / "supervisor.lock")
    ready = _read_json(directory / "ready.json")
    base = {"state": "stopped", "held": held, "detail": None}
    if ready is None or ready.get("agent_id") != meta["agent_id"]:
        return {**base, "state": "unverified" if held else "stopped"}
    if not held:
        return {**base, "detail": ready.get("detail")}
    port = meta["managed"]["port"]
    try:
        verified = (_alive(ready["supervisor_pid"], ready["supervisor_identity"])
                    and ready.get("port") == port)
        if ready.get("state") == "ready" and verified and _alive(ready["chrome_pid"], ready["chrome_identity"]) \
                and owns_listener(ready["chrome_pid"], port, "127.0.0.1"):
            return {**base, "state": "ready", "pid": ready["supervisor_pid"], "identity": ready["supervisor_identity"],
                    "chrome_pid": ready["chrome_pid"], "chrome_identity": ready["chrome_identity"]}
        if verified and ready.get("state") == "starting":
            return {**base, "state": "starting", "pid": ready["supervisor_pid"], "identity": ready["supervisor_identity"]}
    except (KeyError, TypeError, OSError, ValueError):
        pass
    return {**base, "state": "unverified"}


class SystemctlAdapter:
    """The only way the supervisor's user service reaches systemd: fixed argv, a validated unit name, no shell."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    @staticmethod
    def _environment(scope):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        if scope == "user":
            runtime = f"/run/user/{os.getuid()}"
            env["XDG_RUNTIME_DIR"] = runtime
            if Path(runtime, "bus").exists():
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
        return env

    def _run(self, scope, argv, timeout):
        if sys.platform != 'linux':
            raise DomainError("browser_service_unavailable", "Systemd browser services are supported only on Linux; "
                              "this platform uses a detached supervisor")
        try:
            done = self.runner(argv, env=self._environment(scope), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DomainError("browser_service_unavailable", "systemctl could not be run: " + type(exc).__name__) from None
        return done


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


class UserUnitLauncher:
    """Runs a managed browser's supervisor as one transient user service, outside the application's own cgroup.

    ``systemd-run --user --unit=<derived name>`` with a fixed argv and environment; the unit name is derived from this
    installation and the agent, never supplied. A unit that is already running is reused, never duplicated. An unreachable user
    manager is reported precisely (the application user needs a running user manager, i.e. lingering).

    The user manager does not share the application service's mount namespace, so every path handed to it is first taken
    through ``outside_private_mounts``. The supervisor is also given the path its ownership record names when that differs."""

    def __init__(self, runner=subprocess.run, sleep=time.sleep):
        self.adapter, self.sleep = SystemctlAdapter(runner), sleep

    def state(self, unit):
        if not UNIT.fullmatch(unit):
            raise DomainError("browser_binding_invalid", "The supervisor service identity is invalid")
        done = self.adapter._run("user", [SYSTEMCTL, "--user", "show", unit, "--property=LoadState,ActiveState"], 10)
        if done.returncode != 0:
            raise DomainError("browser_service_unavailable", "The application user's systemd manager is not reachable, so "
                              "the browser supervisor cannot be started outside the application (enable lingering for "
                              "that user and check its user manager)")
        values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        return values.get("ActiveState", "inactive") if values.get("LoadState") == "loaded" else "inactive"

    def active(self, unit):
        return self.state(unit) in UNIT_RUNNING

    def start(self, unit, directory):
        """Start the supervisor unit; ``False`` when that exact unit was already running (nothing new is launched)."""
        for _ in range(50):                      # a unit that is still stopping must finish before it can be started again
            state = self.state(unit)
            if state != "deactivating":
                break
            self.sleep(0.2)
        if state in UNIT_RUNNING:
            return False
        import qorqut
        from . import idle_tabs
        state = outside_private_mounts(directory)
        package = outside_private_mounts(Path(qorqut.__file__).resolve().parent.parent)
        # The last entry: where this application's browser commands record that a tab is in use (idle_tabs).
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(state), "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": os.pathsep.join(filter(None, [str(package), os.environ.get("PYTHONPATH", "")])),
                       idle_tabs.SESSIONS: os.pathsep.join(str(outside_private_mounts(place))
                                                           for place in idle_tabs.session_directories())}
        argv = [SYSTEMD_RUN, "--user", f"--unit={unit}", "--collect", "--quiet",
                "--description=Qorqut agent browser supervisor", f"--working-directory={state}",
                "--property=TimeoutStopSec=30", *(f"--setenv={key}={value}" for key, value in environment.items()),
                "--", str(outside_private_mounts(sys.executable)), "-m", "qorqut.browser.agent_supervisor", "--state", str(state),
                *(["--recorded-state", str(directory)] if state != Path(directory) else [])]
        done = self.adapter._run("user", argv, 30)
        if done.returncode != 0:
            if self.state(unit) in UNIT_RUNNING:        # raced with another start of the same unit
                return False
            raise DomainError("browser_service_unavailable", _bound("systemd-run could not start the browser supervisor: "
                                                                   + (done.stderr.strip().splitlines() or ["no detail"])[0]))
        return True

    def stop(self, unit):
        """Stop exactly this supervisor unit (used only after a verified supervisor ignored its own stop request)."""
        if not UNIT.fullmatch(unit):
            raise DomainError("browser_binding_invalid", "The supervisor service identity is invalid")
        self.adapter._run("user", [SYSTEMCTL, "--user", "stop", unit], 60)


class AgentBrowserManager:
    def __init__(self, runtime, *, launcher=None, probe=None, chrome=CHROME, ports=PORTS, runtime_directory=None,
                 deployments=DEPLOYMENT_DIRECTORIES, start_timeout=45, stop_timeout=20, health_timeout=60, sleep=time.sleep):
        self.runtime = runtime
        self.probe = probe or cdp_healthy
        from qorqut import native_paths
        native, folder = native_paths.root(), native_paths.folder_root()
        if native is not None or folder is not None or sys.platform == 'darwin':
            if chrome == CHROME:
                chrome = (native_paths.executable("chrome") if native is not None else
                          native_paths.installed_chrome() or
                          (native_paths.MAC_CHROME if sys.platform == 'darwin' else CHROME))
            if deployments == DEPLOYMENT_DIRECTORIES:
                deployments = (runtime.paths.instance_root / "config/browser-desktops",)
            if launcher is None:
                from .native_launcher import NativeLauncher
                launcher = NativeLauncher(state_root(runtime.paths))
        self.chrome, self.ports, self.deployments = chrome, ports, deployments
        self.runtime_directory = runtime_directory or (lambda: runtime.browser_access.runtime_directory())
        self.start_timeout, self.stop_timeout, self.health_timeout, self.sleep = start_timeout, stop_timeout, health_timeout, sleep
        self.launcher = launcher or UserUnitLauncher(sleep=sleep)

    @property
    def paths(self):
        return self.runtime.paths

    # -- entry -------------------------------------------------------------------------------------------------------------

    def manage(self, principal_id, arguments, idempotency_key=None, *, owner_control=False):
        """One management action on the Shared browser on behalf of the authenticated ``principal_id``."""
        action = arguments.get("action")
        if action not in ACTIONS:
            raise DomainError("validation_error", "Unsupported browser management action")
        if action != "status" and not (isinstance(idempotency_key, str) and KEY.fullmatch(idempotency_key)):
            raise DomainError("validation_error", "Mutating browser management needs a bounded idempotency_key")
        admission = self.runtime.application.writer_admission
        with admission.writer() if admission is not None and action != "status" else nullcontext():
            self.runtime.require_authority()
            agents = self._authorize(principal_id, owner_control=owner_control)
            if action == "status":
                return self._result("status", self._snapshot(), agents, changed=False)
            with lifecycle_lock(self.paths, SHARED_ID):
                # Who may act, and which agents there are, is decided again with the lock held, immediately before any effect.
                agents = self._authorize(principal_id, owner_control=owner_control)
                return self._mutate(action, agents, idempotency_key)

    def _authorize(self, principal_id, *, owner_control=False):
        """Only the installation owner (``owner_control``) or the configured Ordubashy may manage the browser. Returns the
        permanent agents that use it (Ordubashy and the active global workers), lead agent first."""
        from qorqut.agents.global_scope import active_global
        from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE
        from qorqut.management.identity import configured_ordubashy, is_active_installation_owner
        from qorqut.browser.operations import AGENT_BROWSER_MANAGE
        application = self.runtime.application
        with self.runtime.database.read() as uow:
            context = application.policy.context_for(principal_id, project_id=None, review_project_id=None, uow=uow)
            if owner_control:
                if not is_active_installation_owner(uow, context.principal_id, context.principal_kind):
                    raise DomainError("forbidden", "Only the installation owner can set up the agent browser")
            else:
                application.authorize(context, self.runtime.registry.get(AGENT_BROWSER_MANAGE), [], uow, namespace="global")
                if context.principal_kind != "agent" or context.principal_id != configured_ordubashy(uow):
                    raise DomainError("forbidden", "Only the installation owner or Ordubashy can manage the browser")
            rows = uow.connection.execute(
                "SELECT a.agent_id,p.display_name FROM agent_profiles a "
                "JOIN principals p ON p.id=a.agent_id "
                "WHERE a.status='active' AND p.active=1 ORDER BY a.agent_id").fetchall()
            agents = []
            for row in rows:
                data = active_global(uow.connection, row["agent_id"])
                if data and data["role"]["id"] in {ORDUBASHY_ROLE, GLOBAL_WORKER_ROLE}:
                    agents.append(SimpleNamespace(
                        agent_id=row["agent_id"], name=row["display_name"], lead=data["role"]["id"] == ORDUBASHY_ROLE,
                        workspace=agent_workspace_of(self.paths, row["agent_id"])))
        return sorted(agents, key=lambda agent: (not agent.lead, agent.name))

    # -- inspection --------------------------------------------------------------------------------------------------------

    def _snapshot(self, *, mutating=False):
        """What is on disk and running for the Shared browser. Its target file lives in its own folder, no agent's workspace."""
        s = SimpleNamespace(directory=agent_state(self.paths, SHARED_ID), workspace=shared_workspace(self.paths), target=None,
                            target_state="absent", target_error=None, meta=None, meta_error=None,
                            running=False, healthy=False, foreign=False, busy=None, conflict=None)
        if path_has_symlink(s.workspace / ".qorqut/browser.json"):
            s.target_state, s.target_error = "invalid", "The browser target path crosses a symbolic link."
        else:
            try:
                s.target = _content(*browser.read_target(s.workspace))
                s.target_state = "valid"
            except browser.BrowserNotConfigured:
                pass
            except browser.BrowserError as exc:
                s.target_state, s.target_error = "invalid", _bound(str(exc))
        if path_has_symlink(s.directory):
            s.meta_error = "The browser state path crosses a symbolic link."
        elif s.directory.is_dir():
            s.meta = _read_json(s.directory / "meta.json")
            unreserved = s.meta is None and not (s.directory / "meta.json").exists() and not (s.directory / "ready.json").exists()
            if not unreserved and (s.meta is None or not self._meta_valid(s.meta, SHARED_ID, s.directory)):
                s.meta, s.meta_error = None, "The saved browser ownership record is missing or invalid."
        if s.meta is not None:
            if s.target_state == "valid" and s.target != s.meta["target"]["content"]:
                s.conflict = "The browser target file differs from the saved ownership record."
            s.running = self._supervisor(s.directory, s.meta)["state"] == "ready"
        endpoint = s.meta["target"]["content"]["cdp_endpoint"] if s.meta is not None else None
        answered = bool(endpoint) and self.probe(endpoint)
        # The browser is healthy only when it is the exact supervised Chrome that owns its port; any other listener there
        # (a spoofed or replacement process) answers CDP but is neither adopted nor reported healthy.
        s.healthy = answered and s.running
        s.foreign = answered and not s.running
        if not mutating and s.directory.is_dir() and not s.meta_error:
            # A running action holds these locks itself, so only a read-only status reports them as someone else's.
            use, life = _lock_file(self.paths, SHARED_ID, "use"), _lock_file(self.paths, SHARED_ID, "lifecycle")
            s.busy = ("a browser command is running" if not _lock_free(use) else
                      "a management action is in progress" if not _lock_free(life) else None)
        return s

    @staticmethod
    def _state(s, capable=True):
        if s.conflict:
            return "conflict"
        if s.busy:
            return "busy"
        if s.target_state == "invalid" or s.meta_error:
            return "invalid"
        if s.healthy and s.target_state == "valid":
            return "healthy"
        if s.target_state == "valid" and s.meta is not None:
            return "stopped"
        if s.meta is not None:
            return "target_missing"
        return "missing" if capable else "unsupported"

    def _new_chrome(self):
        """The Chrome a browser created now runs on: the installation's explicit choice (``managed_chrome.external_choice``,
        then ``self.chrome``), else the installed Google Chrome, else None: no browser is created and the owner is asked to
        install Google Chrome. Chromium and Qorqbot's own build are never chosen for a new browser. A browser that exists
        keeps the Chrome in its ownership record."""
        from . import managed_chrome
        return self.chrome if managed_chrome.external_choice() else managed_chrome.google_chrome()

    def _capable(self):
        chrome = self._new_chrome()
        if chrome is None or not (Path(chrome).is_file() and os.access(chrome, os.X_OK)):
            return False
        try:
            browser.runtime_executable(self.runtime_directory())
        except browser.BrowserError:
            return False
        return True

    def _advice(self, s, state):
        if state == "healthy":
            return "Ready."
        if state == "missing":
            return "The browser is not set up. Use action=ensure to create it."
        if state == "unsupported":
            from . import managed_chrome
            if self._new_chrome() is None:
                return managed_chrome.GOOGLE_CHROME_NEEDED_FOR_AGENT
            return "This host cannot create the browser yet (Chrome or the pinned browser runtime is missing)."
        if state == "target_missing":
            return "The saved target file is missing. Use action=repair to restore it and start the browser."
        if state == "stopped":
            if s.foreign:
                return ("Another process answers on the browser's reserved port; it is not the owned browser and was not "
                        "adopted or stopped. repair starts the owned browser only if the port is free.")
            return "The browser is not responding. Use action=repair (or ensure) to start it."
        if state == "busy":
            return "Retry shortly: " + s.busy + "."
        return _bound((s.conflict or s.target_error or s.meta_error or "Not usable.") + " Use action=repair to set it right; "
                      "the profile and its sign-ins are kept.")

    def _connection(self, agent):
        """``(connected, code, problem)`` for one agent. It is connected only when its record and its target file both name the
        Shared browser as it is recorded now. ``problem`` says why ``ensure`` cannot connect it, when it cannot."""
        if path_has_symlink(agent.workspace) or not agent.workspace.is_dir():
            return False, "browser_workspace_unavailable", "This agent's folder isn't ready yet. Send the agent a message in Chat once; it is connected then."
        record, configured = agent_state(self.paths, agent.agent_id) / MEMBERSHIP_FILE, agent.workspace / ".qorqut"
        if (path_has_symlink(record) or path_has_symlink(configured / "browser.json")
                or (configured.exists() and not configured.is_dir())):
            return False, "browser_configuration_invalid", ("This agent's browser files cross a symbolic link or are not "
                                                            "regular files, so nothing is written through them.")
        if not record.exists() or membership_damaged(self.paths, agent.agent_id):
            return False, None, None
        expected = shared_target(self.paths)
        try:
            endpoint, runtime_dir = browser.read_target(agent.workspace)
        except browser.BrowserError:
            return False, None, None
        return (expected is not None and browser.target_shared(agent.workspace)
                and _content(endpoint, runtime_dir) == _content(expected["cdp_endpoint"], expected["runtime_dir"])), None, None

    def _result(self, action, s, agents, *, changed, verified=None, note=None, replayed=False, problems=None):
        state = self._state(s, self._capable() if s.target_state == "absent" and s.meta is None else True)
        report = []
        for agent in agents:
            connected, _, problem = self._connection(agent)
            problem = None if connected else (problems or {}).get(agent.agent_id) or problem
            report.append({"agent_id": agent.agent_id, "name": agent.name, "connected": connected,
                           **({"problem": _bound(problem)} if problem else {})})
        waiting = [item["name"] for item in report if not item["connected"] and "problem" not in item]
        if state == "healthy" and waiting:
            state = "target_missing"
            note = note or "Not connected to the browser yet: " + ", ".join(waiting) + ". Use action=ensure to connect them."
        result = {"action": action, "changed": changed, "state": state, "browser": "shared", "profile_preserved": True,
                  "diagnostic": _bound(note or self._advice(s, state)), "agents": report}
        if verified is not None:
            result["verified"] = {"cdp_health": verified}
        if s.meta_error:
            result["metadata_error"] = s.meta_error
        if replayed:
            result["replayed"] = True
        if (s.directory / STOP_REQUEST_FILE).exists():
            result["stop_requested"] = True
        return result

    # -- ownership metadata ------------------------------------------------------------------------------------------------

    _meta_valid = staticmethod(meta_valid)

    def _write_meta(self, directory, meta):
        write_json_private(directory / "meta.json", meta)

    # -- supervisor --------------------------------------------------------------------------------------------------------

    _supervisor = staticmethod(supervisor_state)

    def _settled(self, directory, meta):
        """The supervisor state, allowing a just-spawned supervisor a moment to publish its first record."""
        for _ in range(10):
            state = self._supervisor(directory, meta)
            if state["state"] != "unverified":
                break
            self.sleep(0.2)
        return state

    def _start_managed(self, ctx, s):
        meta, directory = s.meta, s.directory
        state = self._settled(directory, meta)
        if state["state"] == "ready":
            return False
        if state["state"] == "unverified":
            raise DomainError("browser_state_unverified", "A process holds this browser's lock but its identity cannot be "
                              "verified; nothing was started or stopped")
        chrome = meta["managed"]["chrome"]
        if not (Path(chrome).is_file() and os.access(chrome, os.X_OK)):
            from . import managed_chrome
            if managed_chrome.is_own(chrome, self.paths.runtime):
                raise DomainError("browser_capability_missing", "This browser's Chrome is missing at " + chrome
                                  + ". Qorqbot's own Chrome is downloaded again by itself")
            # The browser is not moved to another Chrome: its profile may have been opened by a newer one (see
            # ``managed_chrome.preferred``), so only the same Chrome installed again brings it back.
            raise DomainError("browser_chrome_missing", "This browser's Chrome is missing at " + chrome
                              + ". It has to be installed there again; the browser keeps its profile and sign-ins meanwhile")
        unit = meta["managed"]["unit"]
        if state["state"] == "starting" or self.launcher.active(unit):
            ctx.effect = True          # an earlier start is still in flight: wait for it, never launch a second browser
        else:
            if not port_free(meta["managed"]["port"]):
                raise DomainError("browser_port_conflict", "The reserved CDP port is occupied by another process; it was not "
                                  "adopted or stopped and no other port was assigned")
            ctx.effect = True
            (directory / "ready.json").unlink(missing_ok=True)      # a failure recorded by an earlier supervisor is not this start's
            self.launcher.start(unit, directory)
        self._await_ready(directory, meta, unit)
        return True

    def _await_ready(self, directory, meta, unit):
        deadline = time.monotonic() + self.start_timeout
        next_check = time.monotonic() + 1
        while time.monotonic() < deadline:
            if self._supervisor(directory, meta)["state"] == "ready":
                return
            ready = _read_json(directory / "ready.json") or {}
            if ready.get("state") == "failed":
                raise DomainError("browser_start_failed", _bound("The browser did not start: " + str(ready.get("detail"))))
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + 1
                if not self.launcher.active(unit) and self._supervisor(directory, meta)["state"] != "ready":
                    raise DomainError("browser_start_failed", "The browser supervisor service exited before the browser was "
                                      "ready; nothing was adopted")
            self.sleep(0.2)
        raise DomainError("browser_not_ready", "The browser did not become ready in time; repeating ensure waits for the same "
                          "start and never launches a second browser", retryable=True)

    def _stop_managed(self, ctx, s):
        meta, directory = s.meta, s.directory
        state = self._settled(directory, meta)
        if state["state"] == "stopped":
            return False
        if state["state"] != "ready" and state["state"] != "starting":
            raise DomainError("browser_state_unverified", "A process holds this browser's lock but its identity cannot be "
                              "verified; nothing was stopped")
        ctx.effect = True
        pid, identity = state["pid"], state["identity"]
        if _alive(pid, identity):
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_timeout
        while time.monotonic() < deadline:
            if _lock_free(directory / "supervisor.lock") and not _alive(pid, identity):
                return True
            self.sleep(0.2)
        # Escalate only against the two processes whose exact identity the supervisor recorded.
        if _alive(pid, identity):
            os.kill(pid, signal.SIGKILL)
        if state.get("chrome_pid") and _alive(state["chrome_pid"], state["chrome_identity"]):
            os.killpg(state["chrome_pid"], signal.SIGKILL)
        try:
            self.launcher.stop(meta["managed"]["unit"])        # clears only this exact supervisor service
        except DomainError:
            pass
        self.sleep(0.5)
        if not _lock_free(directory / "supervisor.lock"):
            raise DomainError("browser_stop_timeout", "The owned browser did not stop in time")
        return True

    # -- mutations ---------------------------------------------------------------------------------------------------------

    def _mutate(self, action, agents, key):
        _private_directory(_private_directory(agent_state(self.paths, SHARED_ID)) / "workspace")
        ledger = _Receipts(agent_state(self.paths, SHARED_ID))
        entry = ledger.find(key)
        if entry is not None:
            if entry["action"] != action:
                raise DomainError("idempotency_conflict", "This idempotency_key was already used for a different action")
            if entry["state"] == "completed":
                return {**entry["result"], "replayed": True}
            if entry["state"] in {"started", "uncertain"} and action != "ensure":
                ledger.put(key, action, "uncertain")
                return self._result(action, self._snapshot(mutating=True), agents, changed=False, replayed=True,
                                    note="An earlier attempt with this key did not report completion, so it was not repeated. "
                                         "The current state is shown; inspect it and use a new idempotency_key to act again.") | {
                    "outcome": "uncertain"}
        ctx = SimpleNamespace(effect=False)
        ledger.put(key, action, "started")
        try:
            changed = self._apply(ctx, action, self._snapshot(mutating=True))
            after, problems = self._verified(changed, verify=action != "stop"), {}
            if action != "stop":
                # Every permanent agent is connected in the same action. One agent's failure leaves the others connected and
                # is reported under that agent.
                for agent in agents:
                    try:
                        changed = self._connect(ctx, agent) or changed
                    except DomainError as exc:
                        problems[agent.agent_id] = str(exc)
            result = self._result(action, after, agents, changed=changed, verified=after.healthy if action != "stop" else None,
                                  problems=problems)
        except Exception:
            ledger.put(key, action, "uncertain" if ctx.effect else "failed")
            raise
        ledger.put(key, action, "completed", result)
        return result

    def _apply(self, ctx, action, s):
        """Run one action on the Shared browser itself. True when anything changed."""
        if action == "repair":
            s = self._mend(ctx, s)
        if s.meta_error:
            raise DomainError("browser_metadata_invalid", s.meta_error + " Nothing was changed. Use action=repair: it sets the "
                              "browser up again and keeps its profile and sign-ins.")
        if s.conflict:
            raise DomainError("browser_target_conflict", s.conflict + " Nothing was overwritten. Use action=repair to write it again.")
        if s.target_state == "invalid":
            raise DomainError("browser_configuration_invalid", (s.target_error or "Invalid browser target.") +
                              " The file was not overwritten. Use action=repair to write it again.")
        if s.meta is None:
            if action in {"restart", "stop"}:
                raise DomainError("browser_not_configured", "There is no browser to " + action + ". Use action=ensure.")
            return self._create(ctx, s)
        changed = False
        if action in {"restart", "stop"}:
            with self._exclusive_use():
                changed = self._stop_managed(ctx, s)
                if action == "restart":
                    changed = self._start_managed(ctx, s) or changed
                    if s.target_state == "absent":
                        changed = self._publish_target(s.meta) or changed
                self._record_stop_request(s, action == "stop")
            return changed
        # ensure / repair: converge to a running, published browser; repair also restarts one that does not respond.
        # The restart's stop, start and target restore all happen while no browser command can begin or be running.
        restart = action == "repair" and s.running and not s.healthy
        with self._exclusive_use() if restart else nullcontext():
            if restart:
                self._stop_managed(ctx, s)
                changed = True
            changed = self._start_managed(ctx, s) or changed
            if s.target_state == "absent":
                changed = self._publish_target(s.meta) or changed
            self._record_stop_request(s, False)
        return changed

    def _mend(self, ctx, s):
        """``repair`` sets right the Shared browser's own two files, which nothing but this manager writes: a target file that
        is unreadable or differs from the ownership record is written again from that record, and an unreadable ownership
        record is dropped so the browser is reserved again. The profile, and so every sign-in, is kept either way. Nothing
        is written through a symbolic link, and a record is never dropped while a process still holds the browser's lock."""
        target_file = s.workspace / ".qorqut/browser.json"
        if s.meta_error and not path_has_symlink(s.directory):
            if not _lock_free(s.directory / "supervisor.lock"):
                raise DomainError("browser_state_unverified", "A browser process is running but its ownership record cannot be "
                                  "read, so it was not stopped or replaced. It ends when this computer restarts; repair then.")
            ctx.effect = True
            for stale in (s.directory / "meta.json", s.directory / "ready.json", target_file):
                if not path_has_symlink(stale):
                    stale.unlink(missing_ok=True)
        elif s.meta is not None and (s.conflict or s.target_state == "invalid") and not path_has_symlink(target_file):
            ctx.effect = True
            target_file.unlink(missing_ok=True)
        else:
            return s
        return self._snapshot(mutating=True)

    @staticmethod
    def _record_stop_request(s, requested):
        """Remember a deliberate stop across restarts, so automatic setup does not undo it. Any explicit start clears it."""
        marker = s.directory / STOP_REQUEST_FILE
        if requested:
            write_json_private(marker, {"at": utc_now()})
        else:
            marker.unlink(missing_ok=True)

    @contextmanager
    def _exclusive_use(self, owner=SHARED_ID):
        """A disruptive change is refused while any browser command holds the browser's use lock."""
        with _nonblocking(_lock_file(self.paths, owner, "use"), busy=DomainError(
                "browser_in_use", "A browser command is running in the browser; retry when it finishes", retryable=True)):
            yield

    def _create(self, ctx, s):
        try:
            runtime_dir = self.runtime_directory()
            browser.runtime_executable(runtime_dir)
        except browser.BrowserError as exc:
            raise DomainError("browser_capability_missing", _bound("The browser command is unavailable: " + str(exc))) from None
        chrome = self._new_chrome()
        if chrome is None:
            from . import managed_chrome
            raise DomainError("google_chrome_needed", managed_chrome.GOOGLE_CHROME_NEEDED_FOR_AGENT)
        if not (Path(chrome).is_absolute() and Path(chrome).is_file() and os.access(chrome, os.X_OK)):
            raise DomainError("browser_capability_missing", "The Chrome this installation is set to use is missing at " + chrome)
        meta = self._reserve(s, runtime_dir, chrome)     # ownership is on disk before anything launches
        ctx.effect = True
        self._start_managed(ctx, self._snapshot(mutating=True))
        self._publish_target(meta)
        return True

    def _reserve(self, s, runtime_dir, chrome):
        """Choose the browser's port and write its durable ownership as one step under the global allocation lock. The lock
        is released before Chrome starts. An interrupted reservation (a directory without ownership, or ownership without a
        running browser) is picked up again by the next ensure."""
        with allocation_lock(self.paths, self.sleep):
            port = self._choose()
            directory = s.directory
            content = _content(f"http://127.0.0.1:{port}", runtime_dir)
            meta = {"schema_version": 1, "agent_id": SHARED_ID, "mode": "managed", "created_at": utc_now(),
                    "target": {"workspace": str(s.workspace), "content": content,
                               "sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()},
                    "managed": {"port": port, "profile": str(directory / "profile"), "chrome": chrome,
                                "unit": unit_name(directory.parent, SHARED_ID)}}
            _private_directory(directory)
            _private_directory(directory / "profile")
            self._write_meta(directory, meta)
        return meta

    def _publish_target(self, meta):
        """Write the Shared browser's own target file from its ownership record. The folder is this manager's alone."""
        configured = shared_workspace(self.paths) / ".qorqut"
        if path_has_symlink(configured / "browser.json") or (configured.exists() and not configured.is_dir()):
            raise DomainError("browser_configuration_invalid", "The browser's own target path is unsafe; nothing was written")
        write_json_private(configured / "browser.json", meta["target"]["content"])
        return True

    def _verified(self, changed, *, verify):
        """The snapshot after an action. With ``verify`` the browser must pass the ownership and CDP health check, waited
        for when something was just started; otherwise the action fails and nothing is reported ready."""
        deadline = time.monotonic() + (self.health_timeout if changed else 0)
        while True:
            after = self._snapshot(mutating=True)
            if not verify or (after.healthy and after.target_state == "valid") or time.monotonic() >= deadline:
                break
            self.sleep(0.5)
        if verify and not (after.healthy and after.target_state == "valid"):
            raise DomainError("browser_not_ready", "The browser did not pass the ownership and CDP health check; it is not "
                              "reported ready. Use action=repair, or inspect the state with action=status.", retryable=True,
                              details={"state": self._state(after)})
        return after

    # -- connecting the agents ---------------------------------------------------------------------------------------------

    def _connect(self, ctx, agent):
        """Connect one agent to the Shared browser: write its record and point its target file at the Shared browser as it
        is recorded now, replacing whatever target the workspace had. A Chrome this manager started earlier for that agent
        alone is stopped first; its profile stays on disk. True when anything was written."""
        connected, code, problem = self._connection(agent)
        if connected:
            return False
        if problem:
            raise DomainError(code, problem)
        expected = shared_target(self.paths)
        if expected is None:
            raise DomainError("browser_not_ready", "The Shared browser has no ownership record; nothing was written")
        directory = agent_state(self.paths, agent.agent_id)
        own = _read_json(directory / "meta.json") if directory.is_dir() and not path_has_symlink(directory) else None
        if own is not None and meta_valid(own, agent.agent_id, directory):
            with self._exclusive_use(agent.agent_id):
                self._stop_managed(ctx, SimpleNamespace(meta=own, directory=directory))
        ctx.effect = True
        _private_directory(directory)
        write_json_private(directory / MEMBERSHIP_FILE, {"schema_version": 1, "agent_id": agent.agent_id, "browser": "shared"})
        write_json_private(agent.workspace / ".qorqut/browser.json", expected)
        return True

    # -- port allocation ---------------------------------------------------------------------------------------------------

    def _choose(self):
        used = set()
        for meta_file in state_root(self.paths).glob("*/meta.json"):
            meta = _read_json(meta_file)
            try:
                used.add(_port(meta["target"]["content"]["cdp_endpoint"]))
                if meta["agent_id"] == SHARED_ID and meta["mode"] == "managed":
                    return meta["managed"]["port"]        # a retry keeps its own reservation
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        with self.runtime.database.read() as uow:
            profiles = uow.connection.execute(
                "SELECT agent_id FROM agent_profiles").fetchall()
        for row in profiles:
            try:
                used.add(_port(browser.read_target(agent_workspace_of(self.paths, row["agent_id"]))[0]))
            except (browser.BrowserError, ValueError, OSError, IndexError):
                continue
        for directory in self.deployments:
            try:
                files = sorted(directory.glob("*.json"))
            except OSError:
                continue
            for file in files[:64]:
                try:
                    used |= _cdp_ports(json.loads(file.read_bytes()[:65536]))
                except (OSError, ValueError):
                    continue
        for port in self.ports:
            if port not in used and port_free(port):
                return port
        raise DomainError("browser_capability_missing", "No free port remains in the reserved agent browser range")


def _cdp_ports(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "cdp_port" and type(item) is int:
                found.add(item)
            else:
                found |= _cdp_ports(item)
    elif isinstance(value, list):
        for item in value:
            found |= _cdp_ports(item)
    return found


class _Receipts:
    """Bounded action receipts, written only while the browser's lifecycle lock is held."""

    def __init__(self, directory):
        self.path = directory / "receipts.json"

    def _entries(self):
        data = _read_json(self.path, 65536) if self.path.parent.is_dir() else None
        entries = data.get("entries") if data else None
        return entries if isinstance(entries, list) else []

    def find(self, key):
        return next((entry for entry in self._entries() if entry.get("key") == key), None)

    def put(self, key, action, state, result=None):
        if not self.path.parent.is_dir():
            return
        entries = [entry for entry in self._entries() if entry.get("key") != key]
        entries.append({"key": key, "action": action, "state": state, "at": utc_now(), **({"result": result} if result else {})})
        write_json_private(self.path, {"entries": entries[-RECEIPT_LIMIT:]})
