"""Explicit foreground desktop lifetime, independent of the MCP process."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from typing import Literal
from uuid import UUID

from pydantic import Field

from .config import ServiceConfig, load_config
from .contracts import BrowserError, ControlStatus, FrozenModel, Identity
from .state import (OWNER_FILE, Store, check_paths, checked_path, exclusive_file,
                    fsync_directory, mount_inventory, read_private)
from .supervisor import (DesktopBinaries, ProcessFingerprint, ReadyState, bind_child,
                         check_ready, child_environment, close_chrome, desktop_argv, fingerprint,
                         owns_listener, present, stop_helper, stop_owned, verify_process, verify_ready)
from .state import replace_file

START_TIMEOUT = 30
STOP_TIMEOUT = 20
Role = Literal["display", "window_manager", "chrome"]


class DesktopState(FrozenModel):
    identity: Identity
    resource_hash: str
    supervisor: ProcessFingerprint
    phase: Literal["starting", "running", "stopping", "stopped", "failed"]
    processes: dict[Role, ProcessFingerprint] = Field(default_factory=dict)
    error: str | None = None


def paths(config):
    root = config.database.parent
    result = {name: root / filename for name, filename in (
        ("lock", "desktop.lock"), ("state", "desktop.json"), ("xauthority", "desktop.xauthority"),
        ("helper", "helper-process.json"))}
    if config.state_file in result.values():
        raise BrowserError("invalid_argument", "Readiness path collides with desktop state")
    return result


def initialize(config):
    """Explicitly create only configured owned directories; never adopt data."""
    paths(config)
    if config.uid != os.getuid():
        raise BrowserError("not_ready", "Configured uid must be the current user")
    mounts = mount_inventory()
    directories = sorted(config.owned_directories, key=lambda p: len(p.parts))
    for path in directories:
        if path.resolve() != path:
            raise BrowserError("not_ready", "Managed path has a symlink")
        mount = max((m for m in mounts if path.is_relative_to(m.path)), key=lambda m: len(m.path.parts))
        if mount not in config.mounts:
            raise BrowserError("not_ready", "Configured mount is missing or replaced")
        if path.exists():
            checked_path(path, config.uid, directory=True)
            marker = path / OWNER_FILE
            if marker.exists():
                if read_private(marker, config.uid, 512).decode() != str(config.binding.identity.agent_id):
                    raise BrowserError("target_changed", "Directory belongs to another agent")
            elif any(path.iterdir()):
                raise BrowserError("not_ready", "Initialization will not adopt a nonempty unclaimed directory")
        elif path.parent not in directories:
            checked_path(path.parent, config.uid, directory=True)
    for path in directories:
        path.mkdir(mode=0o700, exist_ok=True)
    Store(config).initialize()
    lock = paths(config)["lock"]
    try:
        exclusive_file(lock, b"")
    except FileExistsError:
        checked_path(lock, config.uid)


@contextmanager
def desktop_lock(config):
    check_paths(config)
    path = paths(config)["lock"]
    checked_path(path, config.uid)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserError("not_ready", "A desktop supervisor already owns this runtime") from None
        actual, expected = os.fstat(fd), path.stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise BrowserError("not_ready", "Desktop lock changed")
        yield
    finally:
        os.close(fd)


def read_state(config):
    path = paths(config)["state"]
    if not os.path.lexists(path):
        return None
    saved = DesktopState.model_validate_json(read_private(path, config.uid))
    if saved.resource_hash != config.resource_hash or saved.identity.agent_id != config.binding.identity.agent_id:
        raise BrowserError("target_changed", "Desktop state has another resource binding")
    return saved


def status(config):
    check_paths(config)
    saved = read_state(config)
    if saved is None:
        return {"phase": "initialized", "ready": False, "control": Store(config).status().model_dump(mode="json")}
    alive = present(saved.supervisor)
    processes = {role: present(process) for role, process in saved.processes.items()}
    ready = False
    if alive and saved.phase == "running" and all(processes.values()):
        check_ready(config)
        ready = True
    return {**saved.model_dump(mode="json"), "supervisor_alive": alive,
            "processes_alive": processes, "ready": ready,
            "control": Store(config).status().model_dump(mode="json")}


def _xauthority(config, path):
    cookie = os.urandom(16)
    def field(value):
        return struct.pack("!H", len(value)) + value
    # Normal local MIT-MAGIC-COOKIE-1 X authentication; unrelated to VNC login.
    content = struct.pack("!H", 256) + b"".join(field(value) for value in (
        socket.gethostname().encode(), str(config.display).encode(), b"MIT-MAGIC-COOKIE-1", cookie))
    replace_file(path, content, config.uid)
    return cookie


def _display_ready(config, child, cookie):
    if not owns_listener(child.fingerprint, config.vnc_port):
        return False
    path = Path(f"/tmp/.X11-unix/X{config.display}")
    if not stat.S_ISSOCK(path.stat().st_mode):
        return False
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(1)
        connection.connect(str(path))
        name = b"MIT-MAGIC-COOKIE-1"
        connection.sendall(struct.pack("<BBHHHHH", ord("l"), 0, 11, 0, len(name), len(cookie), 0)
                           + name + b"\0" * (-len(name) % 4) + cookie)
        return connection.recv(8)[:1] == b"\1"


class Desktop:
    def __init__(self, service: ServiceConfig):
        if service.desktop is None:
            raise BrowserError("not_ready", "Explicit desktop binaries are required")
        self.config, self.settings = service.browser, service.desktop
        self.children = {}
        self.state = None
        self.stopping = False
        self.started = False
        self.close_requested = False

    def save(self, **updates):
        self.state = self.state.model_copy(update={**updates,
            "processes": {role: child.fingerprint for role, child in self.children.items()}})
        replace_file(paths(self.config)["state"], self.state.model_dump_json().encode(), self.config.uid)

    def withdraw(self):
        if os.path.lexists(self.config.state_file):
            checked_path(self.config.state_file, self.config.uid)
            self.config.state_file.unlink()
            fsync_directory(self.config.state_file.parent)
        store = Store(self.config)
        with store.transaction() as db:
            current = Store.status_in(db)
            Store.set_status(db, ControlStatus(binding=current.binding,
                mode="human" if current.mode == "human" else "reconciling", ready=False),
                evidence="Desktop lifetime changed; explicit control reconciliation required")

    def launch(self, role, argv, environment):
        path = self.config.database.parent / f"desktop-{self.state.identity.browser_instance}-{role}.log"
        exclusive_file(path, b"")
        with path.open("ab") as log:
            process = subprocess.Popen(argv, env=environment, cwd=self.config.home,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, umask=0o077)
        try:
            self.children[role] = bind_child(process, self.config, role)
        except (OSError, BrowserError):
            # An immediate exec/exit failure has no usable live fingerprint.
            # Reap only this child; never discover or signal another process.
            process.wait(timeout=1)
            raise BrowserError("not_ready", f"{role} exited before ownership could be bound") from None
        self.save()
        return self.children[role]

    def wait_ready(self, predicate):
        deadline = time.monotonic() + START_TIMEOUT
        while not self.stopping:
            try:
                if predicate():
                    return
            except (OSError, BrowserError, ValueError):
                pass
            for child in self.children.values():
                # Keep exited leaders unreaped for fingerprint-bound group cleanup.
                event = os.waitid(os.P_PID, child.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if event is not None:
                    raise BrowserError("not_ready", f"{child.role} exited during startup")
            if time.monotonic() >= deadline:
                raise BrowserError("not_ready", "Desktop readiness timed out; inspect owned startup logs")
            time.sleep(.05)
        raise BrowserError("not_ready", "Desktop startup was stopped")

    def start_children(self):
        config = self.config
        authority = paths(config)["xauthority"]
        cookie = _xauthority(config, authority)
        binaries = DesktopBinaries(self.settings.chrome, self.settings.xvnc, self.settings.window_manager)
        argv = desktop_argv(config, binaries, xauthority=authority,
                            width=self.settings.width, height=self.settings.height)
        environment = child_environment(config, xauthority=authority)
        display = self.launch("display", argv["display"], environment)
        self.wait_ready(lambda: _display_ready(config, display, cookie))
        self.launch("window_manager", argv["window_manager"], environment)
        self.launch("chrome", argv["chrome"], environment)

        def ready():
            # A configured launcher may exec Chrome. Bind its final owned child
            # identity before publishing any readiness, retaining its Popen.
            chrome = bind_child(self.children["chrome"].process, config, "chrome")
            self.children["chrome"] = chrome
            candidate = ReadyState(identity=config.binding.identity, resource_hash=config.resource_hash,
                                   chrome=chrome.fingerprint, display=display.fingerprint)
            verify_ready(config, candidate)
            verify_process(self.children["window_manager"].fingerprint)
            return candidate

        self.wait_ready(ready)
        candidate = ready()
        replace_file(config.state_file, candidate.model_dump_json().encode(), config.uid)
        self.save(phase="running")

    def shutdown(self):
        self.withdraw()
        self.save(phase="stopping")
        deadline = time.monotonic() + STOP_TIMEOUT
        def remaining():
            return max(.001, deadline - time.monotonic())
        with Store(self.config).lock(remaining()):
            if "chrome" in self.children:
                if self.started:
                    request = not self.close_requested
                    self.close_requested = True
                    close_chrome(self.children["chrome"], self.config, timeout=remaining(), request=request)
                else:
                    # Failed startup cleans only its own children and makes no
                    # clean-profile claim. It never removes surviving locks.
                    stop_owned(self.children["chrome"], self.config, timeout=remaining())
                del self.children["chrome"]
                self.save()
            stop_helper(self.config, timeout=remaining())
            for role in ("window_manager", "display"):
                if role in self.children:
                    stop_owned(self.children[role], self.config, timeout=remaining())
                    del self.children[role]
                    self.save()
            # No lock cleanup: a Chrome lock left after exit requires inspection.
        self.save(phase="stopped")

    def run(self):
        config = self.config
        with desktop_lock(config):
            with Store(config).connection():
                pass
            if config.binding.identity.boot_id != UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
                raise BrowserError("target_changed", "Configured boot identity does not match this host")
            previous = read_state(config)
            if previous is None and os.path.lexists(config.state_file):
                raise BrowserError("not_ready", "Unowned readiness record requires explicit inspection")
            if previous is not None:
                if previous.identity.browser_instance == config.binding.identity.browser_instance:
                    raise BrowserError("target_changed", "A new Chrome lifetime requires a fresh browser_instance")
                if present(previous.supervisor) or any(present(p) for p in previous.processes.values()):
                    raise BrowserError("not_ready", "Previous desktop processes remain; no replacement launched")
            self.state = DesktopState(identity=config.binding.identity, resource_hash=config.resource_hash,
                                      supervisor=fingerprint(os.getpid()), phase="starting")
            self.save()  # durably consume this start identity before launching children
            original = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            for sig in original:
                signal.signal(sig, lambda *_: setattr(self, "stopping", True))
            error = None
            try:
                self.withdraw()
                self.start_children()
                self.started = True
                while not self.stopping:
                    for child in self.children.values():
                        verify_process(child.fingerprint)
                    time.sleep(.1)
            except Exception as failure:
                error = getattr(failure, "code", "not_ready")
                print(json.dumps({"error": error, "message": str(failure)}), file=sys.stderr, flush=True)
            try:
                while True:
                    self.stopping = False
                    try:
                        self.shutdown()
                        if error is not None:
                            self.save(phase="failed", error=error)
                        return 1 if error else 0
                    except Exception as failure:
                        error = getattr(failure, "code", "not_ready")
                        self.save(phase="failed", error=error)
                        print(json.dumps({"error": error, "message": str(failure)}), file=sys.stderr, flush=True)
                        # Retain child handles and the lock if shutdown cannot be
                        # confirmed. An explicit stop can retry; never SIGKILL.
                        while not self.stopping:
                            time.sleep(.1)
            finally:
                for sig, handler in original.items():
                    signal.signal(sig, handler)


def stop(config):
    check_paths(config)
    saved = read_state(config)
    if saved is None:
        return status(config)
    if saved.identity != config.binding.identity:
        raise BrowserError("target_changed", "Stop requires the owning desktop identity")
    if saved.phase in {"stopped", "failed"} and not saved.processes and not present(saved.supervisor):
        return status(config)
    fd = os.pidfd_open(saved.supervisor.pid)
    try:
        verify_process(saved.supervisor)
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 3 * STOP_TIMEOUT + 5
    while time.monotonic() < deadline:
        current = read_state(config)
        if current.supervisor != saved.supervisor:
            raise BrowserError("target_changed", "Supervisor changed while waiting for stop")
        if current.phase in {"stopped", "failed"} and not current.processes:
            return status(config)
        if not present(saved.supervisor):
            raise BrowserError("not_ready", "Supervisor exited without confirming owned shutdown")
        time.sleep(.05)
    raise BrowserError("not_ready", "Shutdown is unconfirmed; owned logs and desktop state retained")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Owned foreground Chrome/TigerVNC desktop")
    parser.add_argument("operation", choices=("init", "start", "status", "stop"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        service = load_config(args.config)
        if args.operation == "start":
            return Desktop(service).run()
        if args.operation == "init":
            initialize(service.browser)
        result = stop(service.browser) if args.operation == "stop" else status(service.browser)
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({"error": getattr(error, "code", "not_ready"), "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
