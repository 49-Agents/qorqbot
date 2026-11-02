"""Manager-only lifecycle of the agents' own browsers: status, ensure, repair, restart and stop.

Ordubashy (or the owner) administers an active permanent worker's configured browser through ``agent_browser_manage``. Nothing
here takes a path, executable, endpoint, port, flag, service name or command from the caller; the agent UUID selects the
workspace and the server derives everything else. Lifecycle state is file-backed under ``<runtime>/agent-browsers/<agent>/``:
private ownership metadata, the Chrome profile, the supervisor's readiness record and bounded action receipts. The database is
only read (authorization); no host effect runs inside a database transaction.

Two ownership modes exist and nothing else is ever touched:

* ``managed``: a browser this module created. A small supervisor (``agent_supervisor``) owns one headless Chrome with a profile
  that belongs to the agent and a loopback CDP port reserved here. The supervisor runs as a deterministic transient *user*
  service (``systemd-run --user``), so it lives outside the application service and survives its restarts.
* ``legacy``: a pre-existing browser an operator explicitly bound (``agent_binding``) to one exact systemd unit. A
  ``.bazarlyk/browser.json`` alone never proves ownership of anything.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
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
from urllib.request import ProxyHandler, build_opener

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.storage import safe_io
from bazarlyk.bootstrap import browser
from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from bazarlyk.composition.locations import agent_workspace_of
from .agent_supervisor import port_free
from .control import owns_listener, process_identity

ACTIONS = ("status", "ensure", "repair", "restart", "stop")
PORTS = range(20400, 21400)          # reserved for agents' own managed browsers
CHROME = "/usr/bin/google-chrome-stable"
SYSTEMCTL, SUDO, SYSTEMD_RUN = "/usr/bin/systemctl", "/usr/bin/sudo", "/usr/bin/systemd-run"
UNIT_RUNNING = {"active", "activating", "reloading"}
DEPLOYMENT_DIRECTORIES = (Path("/etc/qorqut-browser-desktops"),)
UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,150}\.service")
KEY = re.compile(r"[\x21-\x7e]{1,200}")
META_LIMIT, RECEIPT_LIMIT = 16384, 32
DIAGNOSTIC = 300


def state_root(paths):
    return paths.runtime / "agent-browsers"


def agent_state(paths, agent_id):
    return state_root(paths) / agent_id


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
    """The agent's exclusive, nonblocking lifecycle lock: one management action (or one operator binding) at a time."""
    locks = _private_directory(_private_directory(state_root(paths)) / "locks")
    return _nonblocking(locks / f"{agent_id}.lifecycle", busy=DomainError(
        "browser_management_busy", "Another browser management action for this agent is in progress", retryable=True))


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
    locks = _private_directory(state_root(paths) / "locks")
    with _nonblocking(locks / f"{agent_id}.use", exclusive=False, busy=DomainError(
            "browser_maintenance", "The browser is being restarted or stopped; retry shortly", retryable=True)):
        _require_owned_browser(directory, agent_id)
        yield


def _require_owned_browser(directory, agent_id):
    """With the use lock held: a managed browser must still be its own supervised Chrome, never whatever happens to answer on
    its stored port. A browser nobody manages (no ownership record) or a bound legacy service is left to the caller."""
    meta = _read_json(directory / "meta.json")
    if meta is None or meta.get("mode") != "managed" or not meta_valid(meta, agent_id, directory):
        return
    if supervisor_state(directory, meta)["state"] != "ready":
        raise DomainError("browser_not_ready", "Your dedicated browser is not running as its supervised process (it stopped, or "
                          "another process holds its port). Tell Ordubashy; he can repair it with agent_browser_manage.")


def _read_json(path, limit=META_LIMIT):
    try:
        value = json.loads(safe_io.read_bytes(path, limit=limit, private=True))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _alive(pid, identity):
    """The exact process (boot ID + start ticks) is still running and not a zombie."""
    try:
        if process_identity(pid) != identity:
            return False
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError, TypeError):
        return False


def cdp_healthy(endpoint, timeout=3):
    try:
        with build_opener(ProxyHandler({})).open(endpoint + "/json/version", timeout=timeout) as response:
            return bool(json.load(response).get("webSocketDebuggerUrl"))
    except (OSError, ValueError, AttributeError):
        return False


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
        if (meta["schema_version"] != 1 or meta["agent_id"] != agent_id or meta["mode"] not in {"managed", "legacy"}
                or set(content) != {"schema_version", "cdp_endpoint", "runtime_dir"} or not isinstance(target["workspace"], str)):
            return False
        if meta["mode"] == "managed":
            managed = meta["managed"]
            return (isinstance(managed["port"], int) and 1024 <= managed["port"] <= 65535
                    and managed["profile"] == str(directory / "profile") and os.path.isabs(managed["chrome"])
                    and managed["unit"] == unit_name(directory.parent, agent_id) and bool(UNIT.fullmatch(managed["unit"]))
                    and content["cdp_endpoint"] == f"http://127.0.0.1:{managed['port']}")
        legacy = meta["legacy"]
        return (legacy["scope"] in {"user", "system"} and bool(UNIT.fullmatch(legacy["unit"]))
                and isinstance(legacy["unit_fingerprint"], str) and bool(legacy["unit_fingerprint"])
                and legacy["endpoint"] == content["cdp_endpoint"] and os.path.isabs(legacy["profile"]))
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
    """The only way a legacy binding reaches systemd: fixed argv, a validated unit name, no shell.

    User units use ``systemctl --user``; system units use the installation's noninteractive ``sudo -n systemctl`` for
    the start, restart and stop verbs only (read-only ``show`` needs no privilege)."""

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
        try:
            done = self.runner(argv, env=self._environment(scope), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DomainError("browser_service_unavailable", "systemctl could not be run: " + type(exc).__name__) from None
        return done

    def inspect(self, scope, unit):
        """``{loaded, active, fingerprint, evidence}``: the unit's static definition, never volatile runtime fields."""
        if scope not in {"user", "system"} or not UNIT.fullmatch(unit):
            raise DomainError("browser_binding_invalid", "The bound service identity is invalid")
        argv = [SYSTEMCTL] + (["--user"] if scope == "user" else []) + [
            "show", unit, "--property=Id,LoadState,ActiveState,FragmentPath,DropInPaths,ExecStart"]
        done = self._run(scope, argv, 10)
        if done.returncode != 0:
            raise DomainError("browser_service_unavailable", "systemd did not describe the bound service")
        values = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
        loaded = values.get("LoadState") == "loaded" and values.get("Id") == unit
        evidence, parts = [], {"id": values.get("Id")}
        paths = [values.get("FragmentPath", "")] + [item for item in values.get("DropInPaths", "").split() if item]
        for argv_text in re.findall(r"argv\[\]=([^;]*);", values.get("ExecStart", "")):
            paths += [token for token in argv_text.split() if token.startswith("/") and token.endswith(".json")]
        for path in paths[:12]:
            try:
                data = Path(path).read_bytes()[:65536]
            except OSError:
                if path == values.get("FragmentPath"):
                    raise DomainError("browser_binding_unverifiable", "The bound service definition is not readable") from None
                continue
            evidence.append(data.decode("utf-8", "replace"))
            parts[path] = hashlib.sha256(data).hexdigest()
        fingerprint = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        return {"loaded": loaded, "active": values.get("ActiveState") in {"active", "activating", "reloading"},
                "fingerprint": fingerprint, "evidence": "\n".join(evidence)}

    def control(self, scope, verb, unit):
        if verb not in {"start", "restart", "stop"} or scope not in {"user", "system"} or not UNIT.fullmatch(unit):
            raise DomainError("browser_binding_invalid", "The bound service identity is invalid")
        argv = ([SYSTEMCTL, "--user"] if scope == "user" else [SUDO, "-n", SYSTEMCTL]) + [verb, unit]
        done = self._run(scope, argv, 90)
        if done.returncode != 0:
            privilege = scope == "system" and ("password" in done.stderr or "sudo" in done.stderr)
            raise DomainError("browser_privilege_unavailable" if privilege else "browser_service_failed",
                              _bound(("The installation grants no noninteractive privilege to " if privilege else
                                      "systemctl refused to ") + f"{verb} the bound service."))


class UserUnitLauncher:
    """Runs a managed browser's supervisor as one transient user service, outside the application's own cgroup.

    ``systemd-run --user --unit=<derived name>`` with a fixed argv and environment; the unit name is derived from this
    installation and the agent, never supplied. A unit that is already running is reused, never duplicated. An unreachable user
    manager is reported precisely (the application user needs a running user manager, i.e. lingering)."""

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
        import bazarlyk
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(directory), "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(bazarlyk.__file__).resolve().parent.parent),
                                                                   os.environ.get("PYTHONPATH", "")]))}
        argv = [SYSTEMD_RUN, "--user", f"--unit={unit}", "--collect", "--quiet",
                "--description=Qorqut agent browser supervisor", f"--working-directory={directory}",
                "--property=TimeoutStopSec=30", *(f"--setenv={key}={value}" for key, value in environment.items()),
                "--", sys.executable, "-m", "bazarlyk.browser.agent_supervisor", "--state", str(directory)]
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
    def __init__(self, runtime, *, systemctl=None, launcher=None, probe=None, chrome=CHROME, ports=PORTS, runtime_directory=None,
                 deployments=DEPLOYMENT_DIRECTORIES, start_timeout=45, stop_timeout=20, health_timeout=60, sleep=time.sleep):
        self.runtime = runtime
        self.systemctl = systemctl or SystemctlAdapter()
        self.probe = probe or cdp_healthy
        from bazarlyk import native_paths
        native = native_paths.root()
        if native is not None:
            if chrome == CHROME:
                chrome = native_paths.executable("chrome")
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

    def manage(self, principal_id, arguments, idempotency_key=None):
        """One management action for ``arguments['agent_id']`` on behalf of the authenticated agent ``principal_id``."""
        action = arguments.get("action")
        try:
            agent_id = str(uuid.UUID(str(arguments.get("agent_id"))))
        except ValueError:
            raise DomainError("validation_error", "agent_id must be a UUID") from None
        if action not in ACTIONS:
            raise DomainError("validation_error", "Unsupported browser management action")
        if action != "status" and not (isinstance(idempotency_key, str) and KEY.fullmatch(idempotency_key)):
            raise DomainError("validation_error", "Mutating browser management needs a bounded idempotency_key")
        admission = self.runtime.application.writer_admission
        with admission.writer() if admission is not None and action != "status" else nullcontext():
            self.runtime.require_authority()
            workspace = self._authorize(principal_id, agent_id)
            if action == "status":
                return self._status(agent_id, workspace)
            with lifecycle_lock(self.paths, agent_id):
                # Eligibility is decided again with the lock held, immediately before any effect.
                workspace = self._authorize(principal_id, agent_id)
                return self._mutate(action, agent_id, workspace, idempotency_key)

    def _authorize(self, principal_id, agent_id):
        from bazarlyk.agents.worker_access import require_worker_manager
        from bazarlyk.browser.operations import AGENT_BROWSER_MANAGE
        application = self.runtime.application
        with self.runtime.database.read() as uow:
            context = application.policy.context_for(principal_id, business_id=None, review_business_id=None, uow=uow)
            application.authorize(context, self.runtime.registry.get(AGENT_BROWSER_MANAGE), [], uow, namespace="global")
            require_worker_manager(context, uow, agent_id)
            profile = uow.connection.execute(
                "SELECT p.home_business_id,b.slug FROM agent_profiles p LEFT JOIN businesses b ON b.id=p.home_business_id "
                "WHERE p.agent_id=?", (agent_id,)).fetchone()
        if profile is None:
            raise DomainError("not_found", "No such agent")
        return agent_workspace_of(self.paths, agent_id, profile["home_business_id"], profile["slug"])

    # -- inspection --------------------------------------------------------------------------------------------------------

    def _snapshot(self, agent_id, workspace, *, mutating=False):
        s = SimpleNamespace(agent_id=agent_id, workspace=workspace, directory=agent_state(self.paths, agent_id), target=None,
                            target_state="absent", target_error=None, meta=None, meta_error=None, ownership="none",
                            running=False, unit=None, healthy=False, foreign=False, busy=None, conflict=None)
        target_file = workspace / ".bazarlyk/browser.json"
        if path_has_symlink(target_file):
            s.target_state, s.target_error = "invalid", "The browser target path crosses a symbolic link."
        else:
            try:
                s.target = _content(*browser.read_target(workspace))
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
            if not unreserved and (s.meta is None or not self._meta_valid(s.meta, agent_id, s.directory)):
                s.meta, s.meta_error = None, "The saved browser ownership record is missing or invalid."
        if s.meta is not None:
            s.ownership = "managed" if s.meta["mode"] == "managed" else "legacy-bound"
            if s.meta["target"]["workspace"] != str(workspace):
                s.conflict = "The agent's workspace no longer matches its saved browser ownership."
            elif s.target_state == "valid" and s.target != s.meta["target"]["content"]:
                s.conflict = "The browser target file differs from the saved ownership record; it was left unchanged."
            if s.meta["mode"] == "managed":
                s.running = self._supervisor(s.directory, s.meta)["state"] == "ready"
            else:
                try:
                    unit = self.systemctl.inspect(s.meta["legacy"]["scope"], s.meta["legacy"]["unit"])
                    s.unit = unit
                    s.running = unit["active"]
                    if not unit["loaded"] or unit["fingerprint"] != s.meta["legacy"]["unit_fingerprint"]:
                        s.conflict = s.conflict or "The bound service definition changed since it was registered."
                except DomainError as exc:
                    s.unit = {"error": exc.code}
        elif s.target_state == "valid":
            s.ownership = "external"
        endpoint = s.target["cdp_endpoint"] if s.target_state == "valid" else (
            s.meta["target"]["content"]["cdp_endpoint"] if s.meta is not None and s.target_state == "absent" else None)
        answered = bool(endpoint) and self.probe(endpoint)
        # A managed browser is healthy only when it is the exact supervised Chrome that owns its port; any other listener there
        # (a spoofed or replacement process) answers CDP but is neither adopted nor reported healthy.
        s.healthy = answered and (s.running if s.ownership == "managed" else True)
        s.foreign = answered and s.ownership == "managed" and not s.running
        if not mutating and s.directory.is_dir() and not s.meta_error:
            # A running action holds these locks itself, so only a read-only status reports them as someone else's.
            use, life = _lock_file(self.paths, agent_id, "use"), _lock_file(self.paths, agent_id, "lifecycle")
            s.busy = ("a browser command is running" if not _lock_free(use) else
                      "a management action is in progress" if not _lock_free(life) else None)
        return s

    @staticmethod
    def _state(s, capable=True):
        if s.conflict:
            return "conflict"
        if s.busy:
            return "busy"
        if s.target_state == "invalid":
            return "invalid"
        if s.healthy and s.target_state == "valid":
            return "healthy"
        if s.target_state == "valid":
            return "stopped"
        if s.meta is not None:
            return "target_missing"
        return "missing" if capable else "unsupported"

    def _capable(self):
        if not (Path(self.chrome).is_file() and os.access(self.chrome, os.X_OK)):
            return False
        try:
            browser.runtime_executable(self.runtime_directory())
        except browser.BrowserError:
            return False
        return True

    def _advice(self, s, state):
        if state == "healthy":
            return "Ready." if s.ownership != "external" else "Healthy; managed outside this host (no service binding)."
        if state == "missing":
            return "No browser is configured. Use action=ensure to create this agent's dedicated browser."
        if state == "unsupported":
            return "This host cannot create a managed browser (Chrome or the pinned browser runtime is missing)."
        if state == "target_missing":
            return "The saved target file is missing. Use action=repair to restore it and start the browser."
        if state == "stopped":
            if s.ownership == "external":
                return ("browser_management_unconfigured: this browser has no registered service binding, so it cannot be "
                        "started or restarted here. The operator must bind its unit (docs/BROWSER-MANAGEMENT.md).")
            if s.foreign:
                return ("Another process answers on this browser's reserved port; it is not the owned browser and was not "
                        "adopted or stopped. repair starts the owned browser only if the port is free.")
            return "The owned browser is not responding. Use action=repair (or ensure) to start it."
        if state == "busy":
            return "Retry shortly: " + s.busy + "."
        return _bound(s.conflict or s.target_error or "Not usable.")

    def _status(self, agent_id, workspace):
        s = self._snapshot(agent_id, workspace)
        state = self._state(s, self._capable() if s.target_state == "absent" and s.meta is None else True)
        return self._result("status", s, state, changed=False)

    def _result(self, action, s, state, *, changed, verified=None, note=None, replayed=False):
        result = {"agent_id": s.agent_id, "action": action, "changed": changed, "state": state, "ownership": s.ownership,
                  "profile_preserved": True, "target": {"configured": s.target_state == "valid", "managed": s.ownership in {"managed", "legacy-bound"}},
                  "diagnostic": _bound(note or self._advice(s, state))}
        if verified is not None:
            result["verified"] = {"cdp_health": verified}
        if s.meta_error:
            result["metadata_error"] = s.meta_error
        if s.unit and "error" in s.unit:
            result["service_error"] = s.unit["error"]
        if replayed:
            result["replayed"] = True
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
        if not (Path(meta["managed"]["chrome"]).is_file() and os.access(meta["managed"]["chrome"], os.X_OK)):
            raise DomainError("browser_capability_missing", "Chrome is not installed at the supported path "
                              + meta["managed"]["chrome"] + "; install the OS browser package (nothing is downloaded here)")
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

    # -- legacy binding ----------------------------------------------------------------------------------------------------

    def _legacy_verify(self, s):
        legacy = s.meta["legacy"]
        if s.unit is None or "error" in (s.unit or {}):
            raise DomainError("browser_service_unavailable", "systemd did not describe the bound service; nothing was changed")
        if not s.unit["loaded"] or s.unit["fingerprint"] != legacy["unit_fingerprint"]:
            raise DomainError("browser_binding_conflict", "The bound service no longer matches its registered definition; "
                              "nothing was changed and the binding must be re-registered by the operator")
        return legacy

    def _legacy_control(self, ctx, s, verb):
        legacy = self._legacy_verify(s)
        if verb == "start" and s.unit["active"]:
            return False
        if verb == "stop" and not s.unit["active"]:
            return False
        ctx.effect = True
        self.systemctl.control(legacy["scope"], verb, legacy["unit"])
        return True

    # -- mutations ---------------------------------------------------------------------------------------------------------

    def _mutate(self, action, agent_id, workspace, key):
        s = self._snapshot(agent_id, workspace, mutating=True)
        ledger = _Receipts(s.directory)
        entry = ledger.find(key)
        if entry is not None:
            if entry["action"] != action:
                raise DomainError("idempotency_conflict", "This idempotency_key was already used for a different action")
            if entry["state"] == "completed":
                return {**entry["result"], "replayed": True}
            if entry["state"] in {"started", "uncertain"} and action != "ensure":
                ledger.put(key, action, "uncertain")
                return self._result(action, s, self._state(s), changed=False, replayed=True,
                                    note="An earlier attempt with this key did not report completion, so it was not repeated. "
                                         "The current state is shown; inspect it and use a new idempotency_key to act again.") | {
                    "outcome": "uncertain"}
        ctx = SimpleNamespace(effect=False)
        ledger.put(key, action, "started")
        try:
            result = self._apply(ctx, action, s)
        except Exception:
            ledger.put(key, action, "uncertain" if ctx.effect else "failed")
            raise
        ledger.put(key, action, "completed", result)
        return result

    def _apply(self, ctx, action, s):
        if s.meta_error:
            raise DomainError("browser_metadata_invalid", s.meta_error + " Nothing was changed; the operator must inspect it.")
        if s.conflict:
            raise DomainError("browser_target_conflict", s.conflict + " Nothing was overwritten.")
        if s.target_state == "invalid":
            raise DomainError("browser_configuration_invalid", (s.target_error or "Invalid browser target.") +
                              " The file was not overwritten.")
        if s.meta is None:
            return self._unowned(ctx, action, s)
        mode = s.meta["mode"]
        changed = False
        if action in {"restart", "stop"}:
            with self._exclusive_use(s):
                if mode == "managed":
                    changed = self._stop_managed(ctx, s)
                    if action == "restart":
                        changed = self._start_managed(ctx, s) or changed
                elif action == "stop":
                    changed = self._legacy_control(ctx, s, "stop")
                else:
                    changed = self._restart_legacy(ctx, s)
                if action == "restart" and s.target_state == "absent":
                    changed = self._restore(s) or changed
            return self._finish(action, s, changed, verify=action == "restart")
        # ensure / repair: converge to a running, published browser; repair also restarts an owned one that does not respond.
        # The restart's stop, start and target restore all happen while no browser command can begin or be running.
        restart = action == "repair" and s.running and not s.healthy
        with self._exclusive_use(s) if restart else nullcontext():
            if mode == "managed":
                if restart:
                    self._stop_managed(ctx, s)
                    changed = True
                changed = self._start_managed(ctx, s) or changed
            elif restart:
                changed = self._restart_legacy(ctx, s)
            else:
                changed = self._legacy_control(ctx, s, "start")
            if s.target_state == "absent":
                changed = self._restore(s) or changed
        return self._finish(action, s, changed, verify=True)

    def _restart_legacy(self, ctx, s):
        legacy = self._legacy_verify(s)
        ctx.effect = True
        self.systemctl.control(legacy["scope"], "restart", legacy["unit"])
        return True

    @contextmanager
    def _exclusive_use(self, s):
        """A disruptive change is refused while any browser command holds the agent's use lock."""
        with _nonblocking(_lock_file(self.paths, s.agent_id, "use"), busy=DomainError(
                "browser_in_use", "A browser command is running against this agent's browser; retry when it finishes",
                retryable=True)):
            yield

    def _unowned(self, ctx, action, s):
        if s.target_state == "valid":
            if s.healthy and action in {"ensure", "repair"}:
                return self._result(action, s, "healthy", changed=False, verified=True)
            raise DomainError("browser_management_unconfigured", "This agent's existing browser has no registered service binding, "
                              "so it cannot be " + {"ensure": "started", "repair": "repaired", "restart": "restarted",
                                                    "stop": "stopped"}[action] + " here. This is an installation step, not a "
                              "missing permission: the operator registers it once (docs/BROWSER-MANAGEMENT.md).")
        if action in {"restart", "stop"}:
            raise DomainError("browser_not_configured", "This agent has no browser to " + action + ". Use action=ensure.")
        return self._create(ctx, action, s)

    def _create(self, ctx, action, s):
        runtime_dir = self.runtime_directory()
        try:
            browser.runtime_executable(runtime_dir)
        except browser.BrowserError as exc:
            raise DomainError("browser_capability_missing", _bound("The pinned browser runtime is unavailable: " + str(exc))) from None
        if not (Path(self.chrome).is_file() and os.access(self.chrome, os.X_OK)):
            raise DomainError("browser_capability_missing", "Chrome is not installed at the supported path " + self.chrome +
                              "; install the OS browser package (nothing is downloaded here)")
        workspace = s.workspace
        if path_has_symlink(workspace) or not workspace.is_dir():
            raise DomainError("browser_workspace_unavailable", "The agent's workspace is not prepared; start the agent once first")
        meta = self._reserve(s, runtime_dir)             # ownership is on disk before anything launches
        ctx.effect = True
        fresh = self._snapshot(s.agent_id, workspace, mutating=True)
        self._start_managed(ctx, fresh)
        self._publish_target(fresh, meta["target"]["content"])
        return self._finish(action, s, True, verify=True)

    def _reserve(self, s, runtime_dir):
        """Choose the agent's port and write its durable ownership as one step under the global allocation lock, so two
        agents can never pick the same free port. The lock is released before Chrome starts. An interrupted reservation
        (a directory without ownership, or ownership without a running browser) is picked up again by the next ensure."""
        with allocation_lock(self.paths, self.sleep):
            port = self._choose(s.agent_id)
            directory = s.directory
            content = _content(f"http://127.0.0.1:{port}", runtime_dir)
            meta = {"schema_version": 1, "agent_id": s.agent_id, "mode": "managed", "created_at": utc_now(),
                    "target": {"workspace": str(s.workspace), "content": content,
                               "sha256": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()},
                    "managed": {"port": port, "profile": str(directory / "profile"), "chrome": self.chrome,
                                "unit": unit_name(directory.parent, s.agent_id)}}
            _private_directory(directory)
            _private_directory(directory / "profile")
            self._write_meta(directory, meta)
        return meta

    def _publish_target(self, s, content):
        configured = s.workspace / ".bazarlyk"
        if path_has_symlink(configured) or (configured.exists() and not configured.is_dir()):
            raise DomainError("browser_configuration_invalid", "The workspace's .bazarlyk path is unsafe; nothing was written")
        if (s.workspace / ".bazarlyk/browser.json").exists():
            raise DomainError("browser_target_conflict", "A browser target appeared meanwhile; it was left unchanged")
        write_json_private(s.workspace / ".bazarlyk/browser.json", content)

    def _restore(self, s):
        """Rewrite a missing target file from the saved ownership record; an existing file is never replaced."""
        self._publish_target(s, s.meta["target"]["content"])
        return True

    def _finish(self, action, before, changed, *, verify):
        deadline = time.monotonic() + (self.health_timeout if changed else 0)
        while True:
            after = self._snapshot(before.agent_id, before.workspace, mutating=True)
            if not (verify and after.target_state == "valid") or after.healthy or time.monotonic() >= deadline:
                break
            self.sleep(0.5)
        if verify and after.target_state == "valid" and not after.healthy:
            raise DomainError("browser_not_ready", "The browser did not pass the ownership and CDP health check; it is not "
                              "reported ready. Use action=repair, or inspect the state with action=status.", retryable=True,
                              details={"state": self._state(after)})
        return self._result(action, after, self._state(after), changed=changed,
                            verified=after.healthy if verify else None)

    # -- port allocation ---------------------------------------------------------------------------------------------------

    def _choose(self, agent_id):
        used = set()
        for meta_file in state_root(self.paths).glob("*/meta.json"):
            meta = _read_json(meta_file)
            try:
                used.add(_port(meta["target"]["content"]["cdp_endpoint"]))
                if meta["agent_id"] == agent_id and meta["mode"] == "managed":
                    return meta["managed"]["port"]        # a retry keeps its own reservation
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        with self.runtime.database.read() as uow:
            profiles = uow.connection.execute(
                "SELECT p.agent_id,p.home_business_id,b.slug FROM agent_profiles p "
                "LEFT JOIN businesses b ON b.id=p.home_business_id").fetchall()
        for row in profiles:
            try:
                used.add(_port(browser.read_target(agent_workspace_of(self.paths, row["agent_id"], row["home_business_id"],
                                                                      row["slug"]))[0]))
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
    """Bounded per-agent action receipts, written only while the agent's lifecycle lock is held."""

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
