"""Trusted host connector for a browser's private loopback CDP socket.

Chrome remains bound to ``127.0.0.1`` inside its browser container.  A fixed
privileged helper enters the exact Docker network namespace and passes the
connected socket back through a private Unix rendezvous socket.  No browser
URL, host, command, Docker option, or socket is supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import array
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time

from qorqut_operations.contracts import DomainError


_ID = re.compile(r"[a-f0-9]{64}\Z")
_NETNS = re.compile(r"/(?:var/)?run/docker/netns/[a-f0-9]{1,128}\Z")
_INSTANCE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HELPER = r'''
import array, base64, json, socket, sys
import re

value = json.load(sys.stdin)
control = value.get("control")
nonce = value.get("nonce")
port = value.get("port")
if (not isinstance(control, str) or not re.fullmatch(r"/tmp/qorqut-cdp-[A-Za-z0-9_.-]+/rendezvous\.sock", control)
        or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce)
        or not isinstance(port, int) or not 1024 <= port <= 65535):
    raise SystemExit(2)
try:
    token = base64.b16decode(nonce.encode("ascii"), casefold=True)
    rendezvous = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    rendezvous.settimeout(10)
    rendezvous.connect(control)
    target = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        descriptor = array.array("i", [target.fileno()])
        rendezvous.sendmsg([token], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptor)])
    finally:
        target.close()
finally:
    try:
        rendezvous.close()
    except Exception:
        pass
'''


@dataclass(frozen=True)
class BrowserBinding:
    """Current DB/runtime browser identity and its registered CDP port."""

    business_id: str
    browser_id: str
    generation: int
    instance_id: str
    cdp_port: int

    def __post_init__(self):
        from bazarlyk.sandbox.config import business_identity
        business_identity(self.business_id)
        if not _ID.fullmatch(self.browser_id):
            raise DomainError("invalid_configuration", "Browser container identity is invalid")
        if type(self.generation) is not int or self.generation < 1:
            raise DomainError("invalid_configuration", "Browser generation is invalid")
        if not isinstance(self.instance_id, str) or not _INSTANCE.fullmatch(self.instance_id):
            raise DomainError("invalid_configuration", "Browser instance identity is invalid")
        if type(self.cdp_port) is not int or not 1024 <= self.cdp_port <= 65535:
            raise DomainError("invalid_configuration", "Browser CDP port is invalid")


class BrowserSocketConnector:
    """Connect to one verified browser CDP endpoint without exposing a port."""

    def __init__(self, docker, ownership, *, current_binding, privilege_command=("sudo", "-n"),
                 launcher=None, helper_uid=0, timeout=10):
        if not callable(current_binding):
            raise TypeError("Browser connector requires a current binding reader")
        if not privilege_command or any(not isinstance(value, str) or not value or "\0" in value for value in privilege_command):
            raise TypeError("Browser connector privilege command is invalid")
        if launcher is not None and not callable(launcher):
            raise TypeError("Browser connector launcher is invalid")
        if type(helper_uid) is not int or helper_uid < 0:
            raise ValueError("Browser connector helper UID is invalid")
        if not 0 < timeout <= 60:
            raise ValueError("Browser connector timeout is invalid")
        self.docker, self.ownership, self.current_binding = docker, ownership, current_binding
        self.privilege_command, self.launcher, self.helper_uid, self.timeout = tuple(privilege_command), launcher, helper_uid, timeout

    def _observed(self, binding):
        current = self.current_binding()
        if not isinstance(current, BrowserBinding) or current != binding:
            raise DomainError("environment_conflict", "Browser CDP binding is stale")
        if binding.business_id != self.ownership.business_id:
            raise DomainError("environment_conflict", "Browser CDP binding belongs to another project")
        observed = self.docker.owned("container", binding.browser_id, self.ownership)
        if observed is None or observed.get("Id") != binding.browser_id:
            raise DomainError("environment_conflict", "Browser container identity is stale")
        labels = observed.get("Config", {}).get("Labels", {})
        if (not observed.get("State", {}).get("Running") or labels.get("qorqut.role") != "browser"
                or labels.get("qorqut.generation") != str(binding.generation)):
            raise DomainError("environment_conflict", "Browser container is not the current running generation")
        namespace = observed.get("NetworkSettings", {}).get("SandboxKey")
        if not isinstance(namespace, str) or not _NETNS.fullmatch(namespace):
            raise DomainError("environment_conflict", "Browser network namespace is not a Docker namespace")
        return namespace

    def _launch(self, namespace, control, port, nonce):
        command = (*self.privilege_command, "nsenter", "--net=" + namespace, "--",
                   "/usr/bin/python3", "-I", "-c", _HELPER)
        if self.launcher is not None:
            process = self.launcher(namespace, control, port, nonce)
        else:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, close_fds=True, text=True)
        if process is None or process.stdin is None:
            raise DomainError("environment_unavailable", "Browser CDP helper could not start", retryable=True)
        try:
            process.stdin.write(json.dumps({"control": str(control), "port": port,
                                            "nonce": nonce.hex()}, separators=(",", ":")) + "\n")
            process.stdin.close()
        except (OSError, ValueError):
            self._stop(process)
            raise DomainError("environment_unavailable", "Browser CDP helper could not start", retryable=True) from None
        return process

    @staticmethod
    def _stop(process):
        try:
            if process.poll() is None:
                process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        except (OSError, subprocess.SubprocessError):
            pass

    def connect(self, binding: BrowserBinding):
        """Return a nonblocking connected socket after two exact-state checks."""
        namespace = self._observed(binding)
        directory = Path(tempfile.mkdtemp(prefix="qorqut-cdp-", dir="/tmp"))
        os.chmod(directory, 0o700)
        control = directory / "rendezvous.sock"
        rendezvous = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        process = None
        received = None
        success = False
        try:
            rendezvous.bind(str(control))
            os.chmod(control, 0o600)
            rendezvous.listen(1)
            rendezvous.settimeout(self.timeout)
            nonce = secrets.token_bytes(32)
            process = self._launch(namespace, control, binding.cdp_port, nonce)
            deadline = time.monotonic() + self.timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout
            rendezvous.settimeout(remaining)
            peer, _ = rendezvous.accept()
            with peer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout
                peer.settimeout(remaining)
                if hasattr(socket, "SO_PEERCRED"):
                    raw = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, uid, _ = struct.unpack("3i", raw)
                    # sudo may be the peer-visible process while it forks
                    # nsenter/python.  The private 0700 rendezvous, random
                    # nonce, and trusted root UID form the identity boundary;
                    # requiring Popen.pid would reject the production helper.
                    if uid != self.helper_uid:
                        raise DomainError("environment_conflict", "Browser CDP helper identity is invalid")
                recv_flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
                data, ancillary, flags, _ = peer.recvmsg(
                    128, socket.CMSG_SPACE(array.array("i").itemsize), recv_flags)
                fds = []
                ancillary_valid = True
                for level, kind, payload in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        values = array.array("i")
                        if len(payload) % values.itemsize:
                            ancillary_valid = False
                        values.frombytes(payload[:len(payload) - (len(payload) % values.itemsize)])
                        fds.extend(values)
                    else:
                        ancillary_valid = False
                truncation = getattr(socket, "MSG_CTRUNC", 0) | getattr(socket, "MSG_TRUNC", 0)
                if (flags & truncation) or not ancillary_valid or data != nonce or len(fds) != 1:
                    for fd in fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    raise DomainError("environment_conflict", "Browser CDP rendezvous returned an invalid socket")
                try:
                    os.set_inheritable(fds[0], False)
                except OSError:
                    os.close(fds[0])
                    raise DomainError("environment_conflict", "Browser CDP socket could not be secured") from None
                try:
                    received = socket.socket(fileno=fds[0])
                except (OSError, ValueError):
                    os.close(fds[0])
                    raise DomainError("environment_conflict", "Browser CDP helper returned no socket") from None
                received.setblocking(False)
            # A replacement can race the helper's connect; reject the FD if
            # the committed browser identity changed while it was in flight.
            if self._observed(binding) != namespace:
                raise DomainError("environment_conflict", "Browser network namespace changed during connect")
            success = True
            return received
        except socket.timeout:
            raise DomainError("environment_unavailable", "Browser CDP connection timed out", retryable=True) from None
        except (OSError, ValueError):
            raise DomainError("environment_unavailable", "Browser CDP connection failed", retryable=True) from None
        finally:
            if received is not None and not success:
                received.close()
            if process is not None:
                self._stop(process)
            try:
                rendezvous.close()
            except OSError:
                pass
            shutil.rmtree(directory, ignore_errors=True)
