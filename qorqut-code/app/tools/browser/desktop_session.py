#!/usr/bin/python3
"""One dedicated TigerVNC display running one assigned, maximized Chrome.

Operator-installed session lifecycle only: it is not an installer, a deployment
switch or a live migration tool. Each validated JSON config names exactly one
display, VNC listener, Xauthority, VNC password file, Chrome profile and loopback
CDP port. The helper never reads commands from the owner UI or agent payloads,
never adds proxy settings, never deletes a profile lock and keeps Chrome's normal
sandbox. Profile isolation here is not an operating-system security boundary.

Commands:
  check CONFIG [CONFIG ...]     validate configs and refuse collisions between them
  argv CONFIG                   print the exact planned child commands (no secrets)
  run CONFIG [--sibling CONFIG] start, supervise and cleanly stop one session
"""

import argparse
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

SCHEMA_VERSION = 1
EX_OK, EX_CHILD_FAILED, EX_TEMPFAIL, EX_CONFIG = 0, 1, 75, 78
NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
WINDOW_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# Chrome flags accepted verbatim; everything else (proxies, --no-sandbox, other
# user-data or debugging addresses, extensions to load) is refused.
PLAIN_FLAGS = frozenset({"--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
                         "--disable-session-crashed-bubble", "--disable-extensions", "--disable-gpu"})
VALUE_FLAGS = {"--password-store": lambda value: value in {"basic", "gnome-libsecret"},
               "--class": lambda value: bool(WINDOW_CLASS.fullmatch(value))}
ENVIRONMENT_KEYS = frozenset({"HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"})
PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy", "socks_proxy"})
# "none": Chrome uses its basic password store and no keyring is started.
# "gnome-keyring-credential": the unit's systemd credential `keyring-password`
# unlocks a private gnome-keyring on this session's own D-Bus.
# "existing-unit": a managed:false reservation whose own unit keeps its keyring
# handling (Ordubashy's unlock helper); this helper never runs it.
KEYRING_MODES = frozenset({"none", "gnome-keyring-credential", "existing-unit"})
KEYRING_CREDENTIAL = "keyring-password"
ACTIVATION_VARIABLES = ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")
CGNAT = ipaddress.ip_network("100.64.0.0/10")


class ConfigError(Exception):
    pass


class Busy(Exception):
    pass


@dataclass(frozen=True)
class Binaries:
    """Fixed system or packaged executables. Session configs cannot select commands."""
    xvnc: str = "/usr/bin/Xtigervnc"
    xauth: str = "/usr/bin/xauth"
    xdpyinfo: str = "/usr/bin/xdpyinfo"
    xset: str = "/usr/bin/xset"
    window_manager: str = "/usr/bin/openbox"
    chrome: str = "/usr/bin/google-chrome-stable"
    dbus_daemon: str = "/usr/bin/dbus-daemon"
    dbus_update: str = "/usr/bin/dbus-update-activation-environment"
    keyring_daemon: str = "/usr/bin/gnome-keyring-daemon"
    native_root: str | None = None


def installed_binaries(environment=None):
    environment = os.environ if environment is None else environment
    value = environment.get("QORQUT_NATIVE_ROOT")
    if not value:
        return Binaries()
    # Xorg quotes its -xkbdir in a shell command to its compiler. Spaces work,
    # but shell expansions and PATH separators must never enter that command.
    if any(character in value for character in ('"', "\\", "$", "`", ":", "\n", "\r")):
        raise ConfigError("the packaged desktop path contains unsupported shell characters")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise ConfigError("QORQUT_NATIVE_ROOT must name the installed native runtime")
    names = {"xvnc": "Xtigervnc", "xauth": "xauth", "xdpyinfo": "xdpyinfo", "xset": "xset",
             "window_manager": "openbox", "chrome": "chrome", "dbus_daemon": "dbus-daemon",
             "dbus_update": "dbus-update-activation-environment", "keyring_daemon": "gnome-keyring-daemon"}
    paths = {key: str(root / "bin" / name) for key, name in names.items()}
    if any(not Path(path).is_file() or not os.access(path, os.X_OK) for path in paths.values()):
        raise ConfigError("the packaged desktop tools are incomplete")
    return Binaries(**paths, native_root=str(root))


@dataclass(frozen=True)
class Session:
    name: str
    managed: bool
    display: int
    width: int
    height: int
    vnc_interface: str
    vnc_port: int
    vnc_password_file: str
    xauthority: str
    environment: dict
    keyring: str
    user_data_dir: str
    cdp_port: int
    flags: tuple
    start_url: str
    display_timeout: float = 15.0
    chrome_stop_timeout: float = 20.0

    @property
    def display_name(self):
        return f":{self.display}"


def _fields(value, where, required, optional=()):
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    missing, unknown = set(required) - set(value), set(value) - set(required) - set(optional)
    if missing or unknown:
        raise ConfigError(f"{where} has missing {sorted(missing)} or unsupported {sorted(unknown)} fields")
    return value


def _integer(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{where} must be an integer from {low} to {high}")
    return value


def _absolute(value, where):
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or len(value) > 1024 or "/../" in value + "/":
        raise ConfigError(f"{where} must be an absolute path")
    return os.path.normpath(value)


def _interface(value):
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        raise ConfigError("vnc.interface must be one specific IP address") from None
    if address.is_unspecified or address.is_multicast or not (
            address.is_loopback or address.is_private or (address.version == 4 and address in CGNAT)):
        raise ConfigError("vnc.interface must be a loopback or private address reachable from guacd")
    return str(address)


def _start_url(value):
    if value == "about:blank":
        return value
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ConfigError("chrome.start_url must be about:blank or an http(s) URL")
    url = urlsplit(value)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        raise ConfigError("chrome.start_url must be about:blank or an http(s) URL without credentials")
    return value


def _flags(values):
    if not isinstance(values, list) or len(values) > 32:
        raise ConfigError("chrome.flags must be a short list")
    seen = set()
    for flag in values:
        if not isinstance(flag, str):
            raise ConfigError("chrome.flags entries must be text")
        key, _, argument = flag.partition("=")
        if key in seen:
            raise ConfigError(f"chrome flag {key} is repeated")
        seen.add(key)
        if flag in PLAIN_FLAGS:
            continue
        if key in VALUE_FLAGS and argument and VALUE_FLAGS[key](argument):
            continue
        raise ConfigError(f"chrome flag {key} is not an allowed session-preserving flag")
    return tuple(values)


def parse(value):
    """Validate one complete version-1 session config."""
    value = _fields(value, "config", {"schema_version", "name", "managed", "display", "geometry", "vnc", "xauthority",
                                      "environment", "keyring", "chrome"}, {"timeouts"})
    if value["schema_version"] != SCHEMA_VERSION:
        raise ConfigError("schema_version must be 1")
    if not isinstance(value["name"], str) or not NAME.fullmatch(value["name"]):
        raise ConfigError("name must be a short lowercase identifier")
    if type(value["managed"]) is not bool:
        raise ConfigError("managed must be true or false")
    geometry = _fields(value["geometry"], "geometry", {"width", "height"})
    vnc = _fields(value["vnc"], "vnc", {"interface", "port", "password_file"})
    chrome = _fields(value["chrome"], "chrome", {"user_data_dir", "cdp_port", "flags", "start_url"})
    environment = value["environment"]
    if not isinstance(environment, dict) or set(environment) - ENVIRONMENT_KEYS:
        raise ConfigError("environment may only set " + ", ".join(sorted(ENVIRONMENT_KEYS)))
    environment = {key: _absolute(path, "environment." + key) for key, path in environment.items()}
    if value["keyring"] not in KEYRING_MODES:
        raise ConfigError("keyring must be none, gnome-keyring-credential or existing-unit")
    if (value["keyring"] == "existing-unit") != (value["managed"] is False):
        raise ConfigError("keyring existing-unit is required for, and only valid on, managed:false reservations")
    flags = _flags(chrome["flags"])
    store = next((flag.partition("=")[2] for flag in flags if flag.startswith("--password-store=")), None)
    if value["keyring"] == "none" and store != "basic":
        raise ConfigError("keyring none requires the existing --password-store=basic flag")
    if value["keyring"] == "gnome-keyring-credential":
        if store == "basic":
            raise ConfigError("gnome-keyring-credential must not switch Chrome to the basic password store")
        if "XDG_RUNTIME_DIR" not in environment:
            raise ConfigError("gnome-keyring-credential requires the session's private XDG_RUNTIME_DIR")
    timeouts = _fields(value.get("timeouts", {}), "timeouts", (), {"display", "chrome_stop"})
    session = Session(
        name=value["name"], managed=value["managed"],
        display=_integer(value["display"], "display", 1, 999),
        width=_integer(geometry["width"], "geometry.width", 320, 8192),
        height=_integer(geometry["height"], "geometry.height", 240, 8192),
        vnc_interface=_interface(vnc["interface"]), vnc_port=_integer(vnc["port"], "vnc.port", 1024, 65535),
        vnc_password_file=_absolute(vnc["password_file"], "vnc.password_file"),
        xauthority=_absolute(value["xauthority"], "xauthority"),
        environment=environment, keyring=value["keyring"],
        user_data_dir=_absolute(chrome["user_data_dir"], "chrome.user_data_dir"),
        cdp_port=_integer(chrome["cdp_port"], "chrome.cdp_port", 1024, 65535),
        flags=flags, start_url=_start_url(chrome["start_url"]),
        display_timeout=_integer(timeouts.get("display", 15), "timeouts.display", 1, 120),
        chrome_stop_timeout=_integer(timeouts.get("chrome_stop", 20), "timeouts.chrome_stop", 1, 120))
    if session.vnc_port == session.cdp_port:
        raise ConfigError("vnc.port and chrome.cdp_port must differ")
    return session


def load(path):
    """Read an operator-owned config: ordinary file, not a link, not writable by others."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ConfigError(f"{path}: cannot open config ({error.strerror})") from None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in {0, os.getuid()} or info.st_size > 65536:
            raise ConfigError(f"{path}: config must be an ordinary operator-owned file, not group/world writable")
        try:
            value = json.loads(source.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ConfigError(f"{path}: config is not JSON") from None
    try:
        return parse(value)
    except ConfigError as error:
        raise ConfigError(f"{path}: {error}") from None


def collisions(sessions):
    """Refuse duplicate names, displays, listeners, CDP ports, profiles and credentials."""
    problems, owners = [], {}
    for index, session in enumerate(sessions):
        # VNC and CDP ports share one namespace so no listener can be ambiguous.
        claims = [("name", session.name), ("display", session.display), ("port", session.vnc_port),
                  ("port", session.cdp_port), ("profile", os.path.realpath(session.user_data_dir)),
                  ("Xauthority", os.path.realpath(session.xauthority)),
                  ("VNC password file", os.path.realpath(session.vnc_password_file))]
        for kind, value in claims:
            other = owners.setdefault((kind, value), index)
            if other != index:
                problems.append(f"{kind} {value} is used by both {sessions[other].name} and {session.name}")
    return sorted(set(problems))


def plan(session, binaries=None):
    """The exact child argv, in start order. Contains no secret values."""
    binaries = binaries or installed_binaries()
    local = ipaddress.ip_address(session.vnc_interface).is_loopback
    xvnc = [binaries.xvnc, session.display_name, "-geometry", f"{session.width}x{session.height}", "-depth", "24",
            "-rfbport", str(session.vnc_port), "-interface", session.vnc_interface, "-localhost", "yes" if local else "no",
            "-SecurityTypes", "VncAuth", "-PasswordFile", session.vnc_password_file,
            "-AlwaysShared", "-nolisten", "tcp", "-auth", session.xauthority]
    chrome = [binaries.chrome, f"--user-data-dir={session.user_data_dir}",
              "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={session.cdp_port}",
              *session.flags, f"--window-size={session.width},{session.height}", "--start-maximized", session.start_url]
    window_manager = [binaries.window_manager]
    if binaries.native_root:
        native = Path(binaries.native_root)
        xvnc += ["-xkbdir", str(native / "share/X11/xkb"), "-fp", str(native / "share/fonts/X11/misc")]
        window_manager += ["--config-file", str(native / "share/openbox/rc.xml")]
    return {"xvnc": xvnc, "window_manager": window_manager, "chrome": chrome}


def child_environment(session, base=None):
    environment = dict(os.environ if base is None else base)
    environment.update(session.environment)
    environment.update(DISPLAY=session.display_name, XAUTHORITY=session.xauthority)
    return environment


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _listener_free(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError:
            return False
    return True


def preflight(session, environment=None, x_directory="/tmp"):
    """Refuse to start over another live display, listener, profile user or proxy."""
    environment = os.environ if environment is None else environment
    proxies = sorted(key for key in environment if key.lower() in PROXY_VARIABLES)
    if proxies:
        raise ConfigError("refusing to start with proxy environment variables: " + ", ".join(proxies))
    if not session.managed:
        raise ConfigError(f"{session.name} is a reservation for an existing unit, not a managed session")
    lock = Path(x_directory) / f".X{session.display}-lock"
    try:
        pid = int(lock.read_text().strip())
        if pid > 0 and _alive(pid):
            raise Busy(f"display {session.display_name} is already running (pid {pid})")
    except (FileNotFoundError, ValueError):
        pass
    if not _listener_free(session.vnc_interface, session.vnc_port):
        raise Busy(f"VNC listener {session.vnc_interface}:{session.vnc_port} is in use")
    if not _listener_free("127.0.0.1", session.cdp_port):
        raise Busy(f"CDP port 127.0.0.1:{session.cdp_port} is in use")
    for path, kind in ((session.vnc_password_file, stat.S_ISREG), (session.user_data_dir, stat.S_ISDIR)):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise ConfigError(f"{path} does not exist; profiles and VNC passwords are never created here") from None
        if not kind(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigError(f"{path} must be an existing, non-link path owned by this user")
        if kind is stat.S_ISREG and info.st_mode & 0o077:
            raise ConfigError(f"{path} must have mode 0600")
    try:
        # Chrome records its live owner as host-pid. Never remove this lock.
        host, _, owner = os.readlink(Path(session.user_data_dir) / "SingletonLock").rpartition("-")
        if host == socket.gethostname() and owner.isdigit() and _alive(int(owner)):
            raise Busy(f"profile {session.user_data_dir} is already open by pid {owner}")
    except (FileNotFoundError, OSError):
        pass


def log(session, message):
    print(f"desktop-session[{session.name}]: {message}", file=sys.stderr, flush=True)


def _group_of(pid):
    """(state, pgrp) from /proc, or None when the process is gone."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        rest = text[text.rindex(")") + 2:].split()
        return rest[0], int(rest[2])
    except (OSError, ValueError, IndexError):
        return None


def leader_exited(process):
    """True once the child leader has exited; peeks without reaping so its pid (and
    therefore its process group id) cannot be reused until cleanup has finished."""
    if process.returncode is not None:
        return True
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def group_members(pgid):
    """Live (non-zombie) processes of a group whose leader we still hold unreaped."""
    members = []
    for entry in Path("/proc").iterdir():
        fields = _group_of(entry.name) if entry.name.isdigit() else None
        if fields and fields[0] not in "ZX" and fields[1] == pgid:
            members.append(int(entry.name))
    return members


class OwnershipLost(RuntimeError):
    """A child leader was reaped elsewhere, so its group id can no longer be trusted."""


class Supervisor:
    def __init__(self, session, binaries=None, environment=None):
        binaries = binaries or installed_binaries(environment)
        self.session, self.binaries = session, binaries
        self.environment = child_environment(session, environment)
        if binaries.native_root:
            native = Path(binaries.native_root)
            installation = native.parent.parent.resolve()
            home = Path(session.user_data_dir).parent
            socket_home = installation / ".run/d" / hashlib.sha256(session.name.encode()).hexdigest()[:12]
            defaults = {"HOME": home, "XDG_CONFIG_HOME": home / "config", "XDG_CACHE_HOME": home / "cache",
                        "XDG_DATA_HOME": home / "data", "XDG_RUNTIME_DIR": socket_home}
            for key, value in defaults.items():
                directory = Path(session.environment.get(key, value))
                if not directory.resolve().is_relative_to(installation):
                    raise ConfigError("packaged desktop state must remain inside its installation folder")
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.environment[key] = str(directory)
            for path in (session.user_data_dir, session.xauthority, session.vnc_password_file):
                if not Path(path).resolve().is_relative_to(installation):
                    raise ConfigError("packaged desktop state must remain inside its installation folder")
            temporary = installation / ".run/c"
            temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.environment.update(LD_LIBRARY_PATH=str(native / "lib"), TMPDIR=str(temporary),
                                    FONTCONFIG_FILE=str(native / "share/fonts/fonts.conf"),
                                    XDG_DATA_DIRS=str(native / "share"), XKB_CONFIG_ROOT=str(native / "share/X11/xkb"),
                                    PATH=str(native / "bin") + ":/usr/bin:/bin")
        self.children, self.stopping, self.window_manager_restarts = {}, False, 0
        self.cleaned = set()

    def _spawn(self, role, argv, **options):
        # Each child leads its own process group so shutdown reaches its helpers
        # without ever signalling another session's processes.
        options.setdefault("stdin", subprocess.DEVNULL)
        if self.binaries.native_root:
            options.setdefault("cwd", self.environment["XDG_RUNTIME_DIR"])
        process = subprocess.Popen(argv, env=self.environment, start_new_session=True, **options)
        self.children[role] = process
        self.cleaned.discard(role)
        return process

    def _xauthority(self):
        path = Path(self.session.xauthority)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        # The cookie is written through stdin, never argv.
        command = f"add {self.session.display_name} . {secrets.token_hex(16)}\n"
        subprocess.run([self.binaries.xauth, "-q", "-f", str(path), "source", "-"], input=command.encode(),
                       env=self.environment, check=True, stdout=subprocess.DEVNULL, timeout=15)

    def _wait_for_display(self):
        deadline = time.monotonic() + self.session.display_timeout
        while time.monotonic() < deadline:
            if leader_exited(self.children["xvnc"]):
                raise RuntimeError(f"Xtigervnc exited with status {self.children['xvnc'].returncode} before the display was ready")
            if subprocess.run([self.binaries.xdpyinfo], env=self.environment, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10).returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError(f"display {self.session.display_name} was not ready within {self.session.display_timeout}s")

    def _keyring(self):
        if self.session.keyring != "gnome-keyring-credential":
            return
        credentials = self.environment.get("CREDENTIALS_DIRECTORY")
        if not credentials:
            raise ConfigError("the systemd keyring-password credential is required")
        subprocess.run([self.binaries.dbus_update, *ACTIVATION_VARIABLES], env=self.environment, check=True, timeout=30)
        control = Path(self.environment["XDG_RUNTIME_DIR"]) / "keyring"
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(Path(credentials) / KEYRING_CREDENTIAL, "rb") as password:
            if self.binaries.native_root:
                self._spawn("keyring", [self.binaries.keyring_daemon, "--foreground", "--components=secrets",
                                        f"--control-directory={control}", "--unlock"],
                            stdin=password, stdout=subprocess.DEVNULL)
                return
            subprocess.run([self.binaries.keyring_daemon, "--daemonize", "--components=secrets",
                            f"--control-directory={control}", "--unlock"], stdin=password, stdout=subprocess.DEVNULL,
                           env=self.environment, check=True, timeout=30)

    def _session_bus(self):
        # Equivalent of the previous dbus-run-session wrapper, but supervised here
        # so this helper stays the unit's main process and can stop Chrome first.
        config = ["--session"]
        if self.binaries.native_root:
            from xml.sax.saxutils import escape
            directory = Path(self.environment["XDG_RUNTIME_DIR"])
            address = str(directory / "bus")
            if len(os.fsencode(address)) > 100:
                raise ConfigError("desktop runtime path is too long for a Unix socket; use a shorter installation folder")
            path = directory / "dbus-session.conf"
            # No system service activation, /etc includes or machine-specific configuration.
            content = ('<busconfig><type>session</type><listen>unix:path=' + escape(address) + '</listen>'
                       '<auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/>'
                       '<allow receive_sender="*"/><allow own="*"/></policy></busconfig>')
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
            path.chmod(0o600)
            config = ["--config-file=" + str(path)]
        bus = self._spawn("dbus", [self.binaries.dbus_daemon, *config, "--nofork", "--nopidfile", "--print-address=1"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        ready, _, _ = select.select([bus.stdout], [], [], 15)
        address = bus.stdout.readline().decode(errors="replace").strip() if ready else ""
        if not address:
            raise RuntimeError("the private session bus did not start")
        self.environment["DBUS_SESSION_BUS_ADDRESS"] = address

    def start(self):
        commands = plan(self.session, self.binaries)
        self._session_bus()
        self._xauthority()
        self._spawn("xvnc", commands["xvnc"])
        self._wait_for_display()
        for argument in (["s", "off"], ["-dpms"]):
            subprocess.run([self.binaries.xset, *argument], env=self.environment, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        self._spawn("window_manager", commands["window_manager"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._keyring()
        self._spawn("chrome", commands["chrome"])
        log(self.session, f"started display {self.session.display_name} with Chrome pid {self.children['chrome'].pid}")

    def _stop_child(self, role, timeout):
        """Stop a child's whole process group, even when its leader already exited.

        The leader stays unreaped until its group is clean, so the group id cannot
        belong to anyone else meanwhile. A leader reaped by someone else means the
        ownership is lost: nothing is signalled from stale numeric IDs."""
        process = self.children.get(role)
        if process is None or role in self.cleaned:
            return
        self.cleaned.add(role)
        if process.returncode is not None:
            raise OwnershipLost(f"{role} (pid {process.pid}) was reaped before its group was cleaned; "
                                "not signalling by stale ID, inspect and clean its processes manually")
        pgid = process.pid
        alive = lambda: not leader_exited(process) or bool(group_members(pgid))
        for number, limit in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5)):
            if not alive():
                break
            if number == signal.SIGKILL:
                log(self.session, f"{role} did not stop within {timeout}s; killing its process group")
            try:
                os.killpg(pgid, number)  # a zombie leader still reserves the group id
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + limit
            while alive() and time.monotonic() < deadline:
                time.sleep(0.05)
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            log(self.session, f"{role} could not be reaped after SIGKILL; leaving it for the service manager")
        if group_members(pgid):
            log(self.session, f"{role} left processes in its group after SIGKILL")

    def stop(self):
        # Chrome first so it flushes its profile while the display still exists.
        lost = []
        for role, timeout in (("chrome", self.session.chrome_stop_timeout), ("window_manager", 5),
                              ("xvnc", 10), ("keyring", 5), ("dbus", 5)):
            try:
                self._stop_child(role, timeout)
            except OwnershipLost as error:
                log(self.session, str(error))
                lost.append(str(error))
        if lost:
            raise OwnershipLost("; ".join(lost))

    def supervise(self, poll=0.25):
        """Return EX_OK after a requested stop or EX_CHILD_FAILED after a child exits."""
        while not self.stopping:
            for role in ("dbus", "xvnc", "chrome", *(('keyring',) if "keyring" in self.children else ())):
                if leader_exited(self.children[role]):
                    log(self.session, f"{role} exited with status {self.children[role].returncode}; stopping this session")
                    self.stop()
                    return EX_CHILD_FAILED
            manager = self.children["window_manager"]
            if leader_exited(manager):
                if self.window_manager_restarts >= 3:
                    log(self.session, "window manager keeps exiting; stopping this session")
                    self.stop()
                    return EX_CHILD_FAILED
                self.window_manager_restarts += 1
                self._stop_child("window_manager", 5)
                self._spawn("window_manager", plan(self.session, self.binaries)["window_manager"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(poll)
        self.stop()
        return EX_OK

    def request_stop(self, *_):
        self.stopping = True


def run(session, siblings=(), binaries=None, environment=None, x_directory="/tmp"):
    problems = collisions([session, *siblings])
    if problems:
        raise ConfigError("; ".join(problems))
    preflight(session, environment, x_directory)
    supervisor = Supervisor(session, binaries, environment)
    previous = {number: signal.signal(number, supervisor.request_stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        try:
            supervisor.start()
        except BaseException:
            supervisor.stop()
            raise
        return supervisor.supervise()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("configs", nargs="+")
    show = commands.add_parser("argv")
    show.add_argument("config")
    start = commands.add_parser("run")
    start.add_argument("config")
    start.add_argument("--sibling", action="append", default=[])
    options = parser.parse_args(argv)
    try:
        if options.command == "check":
            sessions = [load(path) for path in options.configs]
            problems = collisions(sessions)
            if problems:
                raise ConfigError("; ".join(problems))
            print(json.dumps([{"name": s.name, "managed": s.managed, "display": s.display_name,
                               "vnc": f"{s.vnc_interface}:{s.vnc_port}", "cdp": f"127.0.0.1:{s.cdp_port}",
                               "profile": s.user_data_dir, "keyring": s.keyring} for s in sessions], indent=2))
            return EX_OK
        if options.command == "argv":
            print(json.dumps(plan(load(options.config)), indent=2))
            return EX_OK
        return run(load(options.config), [load(path) for path in options.sibling])
    except ConfigError as error:
        print(f"desktop-session: configuration refused: {error}", file=sys.stderr)
        return EX_CONFIG
    except Busy as error:
        print(f"desktop-session: not started: {error}", file=sys.stderr)
        return EX_TEMPFAIL
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"desktop-session: failed: {error}", file=sys.stderr)
        return EX_CHILD_FAILED


if __name__ == "__main__":
    sys.exit(main())
