"""Small Linux desktop ownership helpers, not an installed desktop supervisor.

P09 mechanisms adapted from Qorqut core e71fd1e527368bfd41b9815126f4bf33452aeebb:
tools/browser/desktop_session.py and bazarlyk/browser/{control,isolated/session}.py.
See docs/evidence/browser-r1.md for provenance and unproved integration limits.
"""

from dataclasses import dataclass
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from .contracts import BrowserConfig, BrowserError, FrozenModel, Identity
from .state import check_paths, checked_path, read_private


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


def check_ready(config: BrowserConfig):
    check_paths(config)
    try:
        saved = ReadyState.model_validate_json(read_private(config.state_file, config.uid))
        if saved.identity != config.binding.identity or saved.resource_hash != config.resource_hash:
            raise BrowserError("target_changed", "Readiness belongs to another browser instance")
        for process in (saved.chrome, saved.display):
            if process.boot_id != config.binding.identity.boot_id or process.uid != config.uid:
                raise BrowserError("target_changed", "Desktop process has another boot/user binding")
        if saved.chrome.pid == saved.display.pid:
            raise BrowserError("not_ready", "Chrome and display must be distinct owned processes")
        if not owns_listener(saved.chrome, config.cdp_port) or not owns_listener(saved.display, config.vnc_port):
            raise BrowserError("not_ready", "The bound processes do not own the configured listeners")
        connection = http.client.HTTPConnection(config.cdp_host, config.cdp_port, timeout=2)
        try:
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                raise ValueError("invalid CDP identity response")
            url = urlsplit(json.loads(body)["webSocketDebuggerUrl"])
            if (url.scheme != "ws" or url.hostname != config.cdp_host or url.port != config.cdp_port
                    or url.username or url.password or not url.path.startswith("/devtools/browser/")):
                raise ValueError("CDP selected another endpoint")
        finally:
            connection.close()
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
                 vnc_password: Path, width=1280, height=900) -> dict[str, tuple[str, ...]]:
    """Build fixed start argv, without launching or preparing a fresh profile."""
    check_paths(config)
    if not 320 <= width <= 8192 or not 240 <= height <= 8192:
        raise BrowserError("invalid_argument", "Unsupported display geometry")
    for executable in (binaries.chrome, binaries.xvnc, binaries.window_manager):
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise BrowserError("not_ready", "Explicit desktop executable is unavailable")
    for path in (xauthority, vnc_password):
        if not any(path.is_relative_to(root) for root in (config.home, config.database.parent)):
            raise BrowserError("invalid_argument", "Desktop credentials must be within managed state")
        if checked_path(path, config.uid).st_mode & 0o077:
            raise BrowserError("not_ready", "Desktop credentials must be private")
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        if os.path.lexists(config.profile / name):
            raise BrowserError("not_ready", "Existing profile lock requires explicit inspection; never removed here")
    if (os.path.lexists(f"/tmp/.X{config.display}-lock")
            or os.path.lexists(f"/tmp/.X11-unix/X{config.display}")):
        raise BrowserError("not_ready", "Configured display is already claimed")
    for port in (config.cdp_port, config.vnc_port):
        with socket.socket() as probe:
            try:
                probe.bind((config.cdp_host, port))
            except OSError as error:
                raise BrowserError("not_ready", "Configured desktop port is already claimed") from error
    return {
        "display": (str(binaries.xvnc), f":{config.display}", "-geometry", f"{width}x{height}",
                    "-depth", "24", "-rfbport", str(config.vnc_port), "-interface", "127.0.0.1",
                    "-localhost", "yes", "-SecurityTypes", "VncAuth", "-PasswordFile", str(vnc_password),
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
    _verify_child(child, config)
    if _live_group(child.process.pid):
        os.killpg(child.process.pid, signal.SIGTERM)
    deadline = time.monotonic() + max(0, timeout)
    while _live_group(child.process.pid):
        if time.monotonic() >= deadline:
            raise BrowserError("not_ready", "Desktop process group has not exited; display retained")
        time.sleep(.02)
    child.process.wait(timeout=max(.1, deadline - time.monotonic()))


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
