"""Read-only, fail-closed legacy host-writer inventories for full recovery.

These controllers do not stop, signal, or reconfigure tmux, systemd, Chrome, or
any host process.  They only yield a quiescence proof after observing the
registered legacy terminal socket and browser-service state are empty.  The
host-terminal boundary is cooperative: shells outside the fixed Bazarlyk tmux
server require an operator-drained or offline recovery path.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from time import monotonic
from uuid import UUID

from bazarlyk.browser.config import browser_state_directory
from bazarlyk.browser.control import process_identity
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from qorqut_operations.storage.quiescence import QuiescenceProof


_BROKER_SOCKET = "bazarlyk"
_NO_SERVER = re.compile(r"no server running on /[^\r\n]+\n?\Z", re.IGNORECASE)
_NO_SOCKET = re.compile(r"error connecting to /[^\r\n]+ \(No such file or directory\)\n?\Z", re.IGNORECASE)
_METADATA_LIMIT = 16_384
_SESSION_ID = re.compile(r"\$[0-9]+\Z")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


def _remaining(deadline: float) -> int:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("legacy writer inventory deadline elapsed")
    return max(1, min(20, int(remaining)))


def _command(arguments: tuple[str, ...], *, timeout: int) -> CommandResult:
    try:
        completed = subprocess.run(arguments, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError("recovery_not_quiescent", "Legacy writer inventory is unavailable", retryable=True) from exc
    return CommandResult(completed.returncode, (completed.stdout or "") + (completed.stderr or ""))


def _canonical_business_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise DomainError("recovery_not_quiescent", "Registered browser inventory contains an invalid business identity") from exc
    return value


class HostTerminalWriters:
    """Prove only an empty, fixed Bazarlyk tmux server; never signal its sessions."""

    def __init__(self, executable: str | Path, *, socket: str = _BROKER_SOCKET, command_runner=_command,
                 _test_socket: bool = False):
        if socket != _BROKER_SOCKET and not _test_socket:
            raise ValueError("Host terminal inventory requires the fixed Bazarlyk tmux socket")
        self.executable = str(executable)
        self.socket, self.command_runner = socket, command_runner

    @classmethod
    def isolated_for_test(cls, executable: str | Path, socket: str):
        """Construct an explicitly isolated socket fixture; never use in composition."""
        return cls(executable, socket=socket, _test_socket=True)

    def _sessions(self, *, timeout: int) -> tuple[tuple[str, str], ...]:
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Host writer timeout must be bounded")
        # Do not use dashboard pty_broker.tmux: its config helper can write a
        # config file.  This client call is read-only and does not start a server.
        result = self.command_runner((self.executable, "-L", self.socket, "-f", "/dev/null", "list-sessions",
                                      "-F", "#{session_id}\t#{session_name}"), timeout=timeout)
        if not isinstance(result, CommandResult):
            raise DomainError("recovery_not_quiescent", "Terminal socket inventory is invalid")
        if result.returncode == 0:
            sessions = []
            for line in result.output.splitlines():
                try:
                    session_id, name = line.split("\t")
                except ValueError:
                    raise DomainError("recovery_not_quiescent", "Terminal socket inventory is malformed") from None
                if not _SESSION_ID.fullmatch(session_id) or not name or "\x00" in name:
                    raise DomainError("recovery_not_quiescent", "Terminal socket inventory is malformed")
                sessions.append((session_id, name))
            return tuple(sessions)
        if result.returncode == 1 and (_NO_SERVER.fullmatch(result.output) or _NO_SOCKET.fullmatch(result.output)):
            return ()
        raise DomainError("recovery_not_quiescent", "Terminal socket inventory is unavailable", retryable=True)

    def business_inactive(self, session_prefix: str, *, timeout: int) -> None:
        """Refuse only a selected business's native broker sessions.

        The caller receives ``session_prefix`` from the trusted terminal broker,
        rather than deriving a guess from a display name.  We still reject a
        malformed complete inventory, because an unknown row can hide the
        selected session and invalidate this migration proof.
        """
        if (not isinstance(session_prefix, str) or not session_prefix or len(session_prefix) > 256
                or any(ord(character) < 33 or ord(character) > 126 for character in session_prefix)):
            raise DomainError("recovery_not_quiescent", "Terminal session prefix is invalid")
        if any(name.startswith(session_prefix) for _identity, name in self._sessions(timeout=timeout)):
            raise DomainError("recovery_not_quiescent", "Selected business has live host terminal sessions")

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        if self._sessions(timeout=timeout):
            raise DomainError("recovery_not_quiescent", "Live host terminal sessions require explicit drain")
        return QuiescenceProof("registered_host_terminals", "frozen")

    def thaw(self) -> None:
        return None


class LegacyBrowserWriters:
    """Inspect exact legacy browser units and managed service state without stopping either.

    ``owns_sandbox_lock`` is supplied only by the current sandbox browser owner.
    It may exempt a held shared ``service.lock`` after proving it owns the exact
    lock/generation/profile; a sandbox intent or row alone never does so.
    """

    def __init__(self, database, paths, *, systemctl: str | Path = "/usr/bin/systemctl", command_runner=_command,
                 identity_reader=process_identity, owns_sandbox_lock=lambda _business_id: False):
        if not callable(owns_sandbox_lock):
            raise TypeError("Sandbox browser lock verifier must be callable")
        self.database, self.paths = database, paths
        self.systemctl, self.command_runner = str(systemctl), command_runner
        self.identity_reader, self.owns_sandbox_lock = identity_reader, owns_sandbox_lock

    def _business_ids(self) -> tuple[str, ...]:
        with self.database.read() as uow:
            rows = uow.connection.execute("SELECT id FROM businesses ORDER BY id").fetchall()
        return tuple(_canonical_business_id(row[0]) for row in rows)

    @staticmethod
    def _service_name(business_id: str) -> str:
        return "qorqut-business-browser@" + business_id + ".service"

    def _unit_inactive(self, business_id: str, *, deadline: float) -> None:
        unit = self._service_name(business_id)
        result = self.command_runner((self.systemctl, "show", "--property=LoadState", "--property=ActiveState",
                                      "--property=MainPID", "--property=ControlPID", "--property=ControlGroup",
                                      "--property=TasksCurrent", unit), timeout=_remaining(deadline))
        if not isinstance(result, CommandResult):
            raise DomainError("recovery_not_quiescent", "Legacy browser service inventory is invalid")
        # --value ordering is not a systemd contract.  Bind every property by
        # name and require zero task count as the cgroup-empty proof.
        properties = {}
        if result.returncode == 0:
            try:
                for line in result.output.splitlines():
                    name, value = line.split("=", 1)
                    if name in properties:
                        raise ValueError
                    properties[name] = value
            except ValueError:
                raise DomainError("recovery_not_quiescent", "Legacy browser service inventory is invalid") from None
            expected = {"LoadState", "ActiveState", "MainPID", "ControlPID", "ControlGroup", "TasksCurrent"}
            inactive = (set(properties) == expected and properties["LoadState"] in {"loaded", "not-found"}
                        and properties["ActiveState"] == "inactive" and properties["MainPID"] == "0"
                        and properties["ControlPID"] == "0")
            # Native observed systemd fixture: a loaded but never-started
            # templated unit reports no ControlGroup and TasksCurrent=[not set].
            # This exact conjunction has neither a unit leader/control process
            # nor an allocated cgroup to contain surviving descendants.  Any
            # cgroup with unknown/nonzero task accounting remains ambiguous.
            empty_cgroup = properties.get("ControlGroup") == "" and properties.get("TasksCurrent") == "[not set]"
            drained_cgroup = bool(properties.get("ControlGroup")) and properties.get("TasksCurrent") == "0"
            if inactive and (empty_cgroup or drained_cgroup):
                return
        # systemctl can use a nonzero result for an absent templated instance.
        missing = "Unit " + unit + " could not be found."
        if result.returncode and result.output.strip() == missing:
            return
        raise DomainError("recovery_not_quiescent", "A legacy browser service is active or its state is uncertain")

    @staticmethod
    def _read_ready(path: Path) -> dict:
        try:
            data = safe_io.read_bytes(path, limit=_METADATA_LIMIT)
        except FileNotFoundError:
            return {}
        except (DomainError, OSError) as exc:
            raise DomainError("recovery_not_quiescent", "Legacy browser state cannot be read") from exc
        try:
            value = json.loads(data.decode("utf-8"))
            keys = {"manager_pid", "manager_identity", "chrome_pid", "chrome_identity", "relay_pid", "relay_identity"}
            if not isinstance(value, dict) or not keys <= set(value):
                raise ValueError
            for name in ("manager", "chrome", "relay"):
                if type(value[name + "_pid"]) is not int or value[name + "_pid"] < 1:
                    raise ValueError
                if not isinstance(value[name + "_identity"], str) or not value[name + "_identity"]:
                    raise ValueError
            return value
        except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            raise DomainError("recovery_not_quiescent", "Legacy browser state is invalid") from exc

    @staticmethod
    def _lock_held(path: Path) -> bool:
        try:
            with safe_io.parent(path) as (parent, name):
                try:
                    descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                         dir_fd=parent)
                except FileNotFoundError:
                    return False
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise DomainError("recovery_not_quiescent", "Legacy browser lock is not an ordinary file")
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return True
                    return False
                finally:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
        except FileNotFoundError:
            return False
        except (DomainError, OSError) as exc:
            if isinstance(exc, DomainError) and exc.code == "recovery_not_quiescent":
                raise
            raise DomainError("recovery_not_quiescent", "Legacy browser lock cannot be inspected") from exc

    def _managed_state_inactive(self, business_id: str) -> None:
        directory = browser_state_directory(self.paths, business_id)
        held = self._lock_held(directory / "service.lock")
        if held:
            try:
                owned = self.owns_sandbox_lock(business_id)
            except Exception as exc:
                raise DomainError("recovery_not_quiescent", "Sandbox browser lock ownership is uncertain") from exc
            if owned is not True:
                raise DomainError("recovery_not_quiescent", "A browser writer lock remains held")
        ready = self._read_ready(directory / "ready.json")
        for name in ("manager", "chrome", "relay"):
            if not ready:
                break
            try:
                observed = self.identity_reader(ready[name + "_pid"])
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError) as exc:
                raise DomainError("recovery_not_quiescent", "Legacy browser process identity is uncertain") from exc
            if observed == ready[name + "_identity"]:
                raise DomainError("recovery_not_quiescent", "A managed legacy browser process remains active")

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Legacy browser timeout must be bounded")
        deadline = monotonic() + timeout
        for business_id in self._business_ids():
            self._unit_inactive(business_id, deadline=deadline)
            self._managed_state_inactive(business_id)
        return QuiescenceProof("registered_legacy_browsers", "frozen")

    def business_inactive(self, business_id: str, *, timeout: int) -> None:
        """Prove one selected business has no legacy browser writer."""
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Legacy browser timeout must be bounded")
        business_id = _canonical_business_id(business_id)
        deadline = monotonic() + timeout
        self._unit_inactive(business_id, deadline=deadline)
        self._managed_state_inactive(business_id)

    def thaw(self) -> None:
        return None


class LegacyHostWriters:
    """Compose the terminal and browser inventories into RecoveryQuiescer's host port."""

    def __init__(self, terminals: HostTerminalWriters, browsers: LegacyBrowserWriters):
        self.terminals, self.browsers = terminals, browsers

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Legacy host writer timeout must be bounded")
        deadline = monotonic() + timeout
        self.terminals.inventory_and_freeze(timeout=_remaining(deadline))
        self.browsers.inventory_and_freeze(timeout=_remaining(deadline))
        return QuiescenceProof("legacy_host_writers", "frozen")

    def adoption_inactive(self, business: dict, _paths, *, timeout: int, session_prefix) -> None:
        """Read-only, exact-business host writer proof used before adoption."""
        if not isinstance(business, dict):
            raise DomainError("recovery_not_quiescent", "Registered business identity is invalid")
        business_id = _canonical_business_id(business.get("id"))
        self.terminals.business_inactive(session_prefix, timeout=timeout)
        self.browsers.business_inactive(business_id, timeout=timeout)

    def thaw(self) -> None:
        self.browsers.thaw()
        self.terminals.thaw()
