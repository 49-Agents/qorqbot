"""Linux desktop process ownership and fixed launch/shutdown helpers.

P09 mechanisms adapted from Qorqut core e71fd1e527368bfd41b9815126f4bf33452aeebb:
tools/browser/desktop_session.py and bazarlyk/browser/{control,isolated/session}.py.
See docs/evidence/browser-r1.md for provenance and unproved integration limits.
"""

from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .contracts import BrowserConfig, BrowserError, FrozenModel, Identity, digest
from .state import check_paths, checked_path, fsync_directory, read_private, replace_file


class ProcessFingerprint(FrozenModel):
    pid: int = Field(gt=1)
    boot_id: UUID
    start_ticks: int = Field(ge=0)
    uid: int = Field(ge=0)
    executable_device: int
    executable_inode: int


def _fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def fingerprint(pid: int) -> ProcessFingerprint:
    """A PID alone is never process ownership."""
    fields = _fields(pid)
    if fields[0] in {"Z", "X"}:
        raise BrowserError("not_ready", "Owned process has exited")
    executable = Path(f"/proc/{pid}/exe").stat()
    value = ProcessFingerprint(pid=pid, boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                               start_ticks=int(fields[19]), uid=Path(f"/proc/{pid}").stat().st_uid,
                               executable_device=executable.st_dev, executable_inode=executable.st_ino)
    if _fields(pid)[19] != fields[19]:
        raise BrowserError("not_ready", "Process changed during observation")
    return value


def verify_process(expected: ProcessFingerprint):
    try:
        if fingerprint(expected.pid) != expected:
            raise ValueError("fingerprint mismatch")
    except (OSError, ValueError, IndexError) as error:
        raise BrowserError("not_ready", "Owned process is unavailable or replaced") from error


def present(process: ProcessFingerprint) -> bool:
    """An absent process is different from an unverified live PID."""
    if process.boot_id != UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
        return False
    try:
        fields = _fields(process.pid)
    except FileNotFoundError:
        return False
    if fields[0] in {"Z", "X"} and int(fields[19]) == process.start_ticks:
        return False
    verify_process(process)
    return True


def helper_session(config: BrowserConfig) -> str:
    return "qorqut-" + digest({"identity": config.binding.identity.model_dump(mode="json"),
                              "resources": config.resource_hash})[:24]


def helper_socket(config: BrowserConfig) -> Path:
    return config.database.parent / "helper-tmp/agent-browser" / (helper_session(config) + ".sock")


class HelperState(FrozenModel):
    identity: Identity
    resource_hash: str
    process: ProcessFingerprint


def _helper_association(config, process):
    verify_process(process)
    if process.uid != config.uid or process.boot_id != config.binding.identity.boot_id:
        raise BrowserError("target_changed", "Helper belongs to another user or boot")
    path = helper_socket(config)
    if int(read_private(path.with_suffix(".pid"), config.uid, 32)) != process.pid:
        raise BrowserError("not_ready", "Helper session PID changed")
    sockets = set()
    for fd in Path(f"/proc/{process.pid}/fd").iterdir():
        try:
            sockets.add(os.readlink(fd))
        except FileNotFoundError:
            continue
    for line in Path(f"/proc/{process.pid}/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) == 8 and fields[7] == str(path) and f"socket:[{fields[6]}]" in sockets:
            verify_process(process)
            return
    raise BrowserError("not_ready", "Helper does not own its configured session socket")


def record_helper(config: BrowserConfig, executable: Path):
    """Capture only this fixed session's actual native daemon, when present."""
    path = helper_socket(config)
    if not os.path.lexists(path.with_suffix(".pid")) and not os.path.lexists(path):
        return  # the injected owning-test helper has no persistent daemon
    process = fingerprint(int(read_private(path.with_suffix(".pid"), config.uid, 32)))
    actual = executable.stat()
    if (process.executable_device, process.executable_inode) != (actual.st_dev, actual.st_ino):
        raise BrowserError("not_ready", "Session process is not the configured native helper")
    _helper_association(config, process)
    saved = HelperState(identity=config.binding.identity, resource_hash=config.resource_hash, process=process)
    replace_file(config.database.parent / "helper-process.json", saved.model_dump_json().encode(), config.uid)


def stop_helper(config: BrowserConfig, *, timeout=20):
    """Caller holds the command lock with admission closed; no forced unlock."""
    path = config.database.parent / "helper-process.json"
    if not os.path.lexists(path):
        socket_path = helper_socket(config)
        if os.path.lexists(socket_path) or os.path.lexists(socket_path.with_suffix(".pid")):
            raise BrowserError("not_ready", "Helper state is unknown; desktop retained")
        return
    saved = HelperState.model_validate_json(read_private(path, config.uid))
    if saved.identity != config.binding.identity or saved.resource_hash != config.resource_hash:
        raise BrowserError("target_changed", "Helper has another browser/resource binding")
    if present(saved.process):
        _helper_association(config, saved.process)
        fd = os.pidfd_open(saved.process.pid)
        try:
            verify_process(saved.process)
            signal.pidfd_send_signal(fd, signal.SIGTERM)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(int(timeout * 1000)):
                raise BrowserError("not_ready", "Owned helper has not exited; desktop retained")
        finally:
            os.close(fd)
    path.unlink()
    fsync_directory(path.parent)


def owns_listener(expected: ProcessFingerprint, port: int) -> bool:
    verify_process(expected)
    sockets = set()
    for fd in Path(f"/proc/{expected.pid}/fd").iterdir():
        try:
            sockets.add(os.readlink(fd))
        except FileNotFoundError:
            continue
    # This increment deliberately accepts only IPv4 loopback CDP/VNC bindings.
    for line in Path(f"/proc/{expected.pid}/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        address, number = fields[1].split(":")
        if (address == "0100007F" and int(number, 16) == port and fields[3] == "0A"
                and f"socket:[{fields[9]}]" in sockets):
            verify_process(expected)
            return True
    return False


class ReadyState(FrozenModel):
    identity: Identity
    resource_hash: str
    chrome: ProcessFingerprint
    display: ProcessFingerprint


def browser_websocket(config: BrowserConfig, chrome: ProcessFingerprint, *, timeout=2) -> str:
    if not owns_listener(chrome, config.cdp_port):
        raise BrowserError("not_ready", "Owned Chrome does not own its CDP listener")
    connection = http.client.HTTPConnection(config.cdp_host, config.cdp_port, timeout=timeout)
    try:
        connection.request("GET", "/json/version")
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200 or len(body) > 65536:
            raise ValueError("invalid CDP identity response")
        endpoint = json.loads(body)["webSocketDebuggerUrl"]
        url = urlsplit(endpoint)
        if (url.scheme != "ws" or url.hostname != config.cdp_host or url.port != config.cdp_port
                or url.username is not None or url.password is not None
                or url.query or url.fragment or not url.path.startswith("/devtools/browser/")):
            raise ValueError("CDP selected another endpoint")
        verify_process(chrome)
        return endpoint
    finally:
        connection.close()


def check_ready(config: BrowserConfig):
    check_paths(config)
    try:
        saved = ReadyState.model_validate_json(read_private(config.state_file, config.uid))
        verify_ready(config, saved)
    except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException) as error:
        raise BrowserError("not_ready", "Managed browser readiness cannot be confirmed") from error


def verify_ready(config: BrowserConfig, saved: ReadyState):
    """Validate a candidate before the supervisor publishes readiness."""
    check_paths(config)
    try:
        if saved.identity != config.binding.identity or saved.resource_hash != config.resource_hash:
            raise BrowserError("target_changed", "Readiness belongs to another browser instance")
        for process in (saved.chrome, saved.display):
            if process.boot_id != config.binding.identity.boot_id or process.uid != config.uid:
                raise BrowserError("target_changed", "Desktop process has another boot/user binding")
        if saved.chrome.pid == saved.display.pid:
            raise BrowserError("not_ready", "Chrome and display must be distinct owned processes")
        if not owns_listener(saved.chrome, config.cdp_port) or not owns_listener(saved.display, config.vnc_port):
            raise BrowserError("not_ready", "The bound processes do not own the configured listeners")
        browser_websocket(config, saved.chrome)
        verify_process(saved.chrome)
        verify_process(saved.display)
    except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException) as error:
        raise BrowserError("not_ready", "Managed browser readiness cannot be confirmed") from error


@dataclass(frozen=True)
class DesktopBinaries:
    chrome: Path
    xvnc: Path
    window_manager: Path


def child_environment(config: BrowserConfig, *, xauthority: Path | None = None) -> dict[str, str]:
    # Do not inherit helper flags, proxies, personal HOME, XDG or session buses.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(config.home),
            "DISPLAY": f":{config.display}", "XDG_CONFIG_HOME": str(config.home / ".config"),
            "XDG_CACHE_HOME": str(config.home / ".cache"), "XDG_DATA_HOME": str(config.home / ".local/share")}
    if xauthority is not None:
        checked_path(xauthority, config.uid)
        environment["XAUTHORITY"] = str(xauthority)
    return environment


def desktop_argv(config: BrowserConfig, binaries: DesktopBinaries, *, xauthority: Path,
                 width=1280, height=900) -> dict[str, tuple[str, ...]]:
    """Build fixed start argv, without launching or preparing a fresh profile."""
    check_paths(config)
    if not 320 <= width <= 8192 or not 240 <= height <= 8192:
        raise BrowserError("invalid_argument", "Unsupported display geometry")
    for executable in (binaries.chrome, binaries.xvnc, binaries.window_manager):
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise BrowserError("not_ready", "Explicit desktop executable is unavailable")
    for path in (xauthority,):
        if not any(path.is_relative_to(root) for root in (config.home, config.database.parent)):
            raise BrowserError("invalid_argument", "X authority must be within managed state")
        if checked_path(path, config.uid).st_mode & 0o077:
            raise BrowserError("not_ready", "X authority must be private")
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        if os.path.lexists(config.profile / name):
            raise BrowserError("not_ready", "Existing profile lock requires explicit inspection; never removed here")
    if (os.path.lexists(f"/tmp/.X{config.display}-lock")
            or os.path.lexists(f"/tmp/.X11-unix/X{config.display}")):
        raise BrowserError("not_ready", "Configured display is already claimed")
    for port in (config.cdp_port, config.vnc_port):
        with socket.socket() as probe:
            # A clean close can leave accepted TCP connections in TIME_WAIT.
            # Match the real listeners' reuse behavior without sharing a live port.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((config.cdp_host, port))
            except OSError as error:
                raise BrowserError("not_ready", "Configured desktop port is already claimed") from error
    return {
        "display": (str(binaries.xvnc), f":{config.display}", "-geometry", f"{width}x{height}",
                    "-depth", "24", "-rfbport", str(config.vnc_port), "-interface", "127.0.0.1",
                    "-localhost", "yes", "-SecurityTypes", "None",
                    "-AlwaysShared", "-nolisten", "tcp", "-auth", str(xauthority)),
        "window_manager": (str(binaries.window_manager),),
        "chrome": (str(binaries.chrome), f"--user-data-dir={config.profile}",
                   "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={config.cdp_port}",
                   "--no-first-run", "--no-default-browser-check", f"--window-size={width},{height}",
                   "--start-maximized", "about:blank"),
    }


@dataclass(frozen=True)
class OwnedChild:
    process: subprocess.Popen
    fingerprint: ProcessFingerprint
    identity: Identity
    resource_hash: str
    role: Literal["chrome", "window_manager", "display"]


def bind_child(process: subprocess.Popen, config: BrowserConfig, role: str) -> OwnedChild:
    """Bind an actual unreaped child launched by the caller with start_new_session.

    This does not discover/adopt an existing browser or guess ownership from argv.
    The caller is responsible for using desktop_argv and child_environment.
    """
    check_paths(config)
    if role not in {"chrome", "window_manager", "display"} or process.returncode is not None:
        raise BrowserError("not_ready", "Cannot bind a reaped or unrecognized desktop child")
    fields = _fields(process.pid)
    observed = fingerprint(process.pid)
    if (int(fields[1]) != os.getpid() or int(fields[2]) != process.pid
            or observed.uid != config.uid or observed.boot_id != config.binding.identity.boot_id):
        raise BrowserError("target_changed", "Process is not this caller's owned session leader")
    return OwnedChild(process, observed, config.binding.identity, config.resource_hash, role)


def _live_group(pgid):
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = _fields(path.name)
        except FileNotFoundError:
            continue
        if fields[0] not in {"Z", "X"} and int(fields[2]) == pgid:
            return True
    return False


def _verify_child(child: OwnedChild, config: BrowserConfig):
    if child.identity != config.binding.identity or child.resource_hash != config.resource_hash:
        raise BrowserError("target_changed", "Desktop child belongs to another browser binding")
    process, saved = child.process, child.fingerprint
    if process.returncode is not None or process.pid != saved.pid:
        raise BrowserError("not_ready", "Process ownership was lost by reaping the leader")
    try:
        # WNOWAIT leaves even an exited leader unreaped, reserving its group ID.
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        fields = _fields(process.pid)
        if (int(fields[19]) != saved.start_ticks or int(fields[2]) != process.pid
                or Path(f"/proc/{process.pid}").stat().st_uid != saved.uid
                or UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()) != saved.boot_id):
            raise ValueError("process changed")
        if fields[0] not in {"Z", "X"}:
            verify_process(saved)
    except (OSError, ValueError, IndexError) as error:
        raise BrowserError("not_ready", "Cannot prove ownership of the desktop process group") from error


def stop_owned(child: OwnedChild, config: BrowserConfig, *, timeout=20):
    """Signal-based child cleanup; normal Chrome close uses close_chrome."""
    _verify_child(child, config)
    if _live_group(child.process.pid):
        if child.role == "chrome" and _fields(child.process.pid)[0] not in {"Z", "X"}:
            # Let the browser's main process close its profile and children.
            # Signalling all renderers at once can interrupt normal shutdown.
            os.kill(child.process.pid, signal.SIGTERM)
        else:
            os.killpg(child.process.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0, timeout)
    while _live_group(child.process.pid):
        if time.monotonic() >= deadline:
            raise BrowserError("not_ready", "Desktop process group has not exited; display retained")
        time.sleep(.02)
    child.process.wait(timeout=max(.1, deadline - time.monotonic()))


def close_chrome(child: OwnedChild, config: BrowserConfig, *, timeout=20, request=True):
    """One bounded Browser.close, then actual owned exit and lock observation."""
    _verify_child(child, config)
    if child.role != "chrome":
        raise BrowserError("target_changed", "Chrome close requires the Chrome child")
    deadline = time.monotonic() + max(0, timeout)
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise BrowserError("not_ready", "Chrome shutdown deadline exceeded; display retained")
        return value
    if request and _live_group(child.process.pid):
        endpoint = browser_websocket(config, child.fingerprint, timeout=min(2, remaining()))
        # A preconnected fixed-target socket also makes websockets reject HTTP
        # redirects. No proxy inheritance, reconnection or generic CDP dispatch.
        with socket.create_connection((config.cdp_host, config.cdp_port), timeout=min(3, remaining())) as tcp:
            with connect(endpoint, sock=tcp, proxy=None, legacy=True, compression=None,
                         open_timeout=min(3, remaining()), close_timeout=min(1, remaining()),
                         ping_interval=None, max_size=65536, max_queue=4) as websocket:
                _verify_child(child, config)
                if not owns_listener(child.fingerprint, config.cdp_port):
                    raise BrowserError("not_ready", "Chrome listener changed before close")
                websocket.socket.settimeout(remaining())
                try:
                    websocket.send('{"id":1,"method":"Browser.close"}')
                    reply = json.loads(websocket.recv(timeout=min(3, remaining())))
                    if reply.get("id") != 1 or "error" in reply:
                        raise BrowserError("not_ready", "Chrome did not confirm its close request")
                except ConnectionClosed:
                    pass  # only process/lock observation below establishes exit
    while _live_group(child.process.pid):
        _verify_child(child, config)
        remaining()
        time.sleep(.02)
    _verify_child(child, config)
    if any(os.path.lexists(config.profile / name) for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")):
        raise BrowserError("not_ready", "Chrome exited with profile locks retained; clean shutdown unconfirmed")
    child.process.wait(timeout=max(.1, remaining()))


def ordered_shutdown(config: BrowserConfig, *, chrome: OwnedChild, window_manager: OwnedChild,
                     display: OwnedChild, timeout=20):
    """Verified Chrome/group exit precedes display teardown. No forced-flush claim.

    An unknown/reaped/stuck Chrome stops this sequence with the display retained.
    Detached descendants require the later runtime-unit/cgroup integration.
    """
    children = (chrome, window_manager, display)
    if tuple(child.role for child in children) != ("chrome", "window_manager", "display"):
        raise BrowserError("target_changed", "Wrong desktop shutdown role bindings")
    if len({child.process.pid for child in children}) != 3:
        raise BrowserError("target_changed", "Desktop roles must have distinct process bindings")
    for child in children:
        _verify_child(child, config)
    for child in children:
        stop_owned(child, config, timeout=timeout)
