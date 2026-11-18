"""Read-only, fail-closed host terminal writer inventories for full recovery.

These controllers do not stop, signal, or reconfigure tmux or any host
process.  They only yield a quiescence proof after observing the registered
legacy terminal socket is empty.  The
host-terminal boundary is cooperative: shells outside the fixed Qorqut tmux
server require an operator-drained or offline recovery path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from time import monotonic

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import QuiescenceProof


_BROKER_SOCKET = "qorqut"
_NO_SERVER = re.compile(r"no server running on /[^\r\n]+\n?\Z", re.IGNORECASE)
_NO_SOCKET = re.compile(r"error connecting to /[^\r\n]+ \(No such file or directory\)\n?\Z", re.IGNORECASE)
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


class HostTerminalWriters:
    """Prove only an empty, fixed Qorqut tmux server; never signal its sessions."""

    def __init__(self, executable: str | Path, *, socket: str = _BROKER_SOCKET, command_runner=_command,
                 _test_socket: bool = False):
        if socket != _BROKER_SOCKET and not _test_socket:
            raise ValueError("Host terminal inventory requires the fixed Qorqut tmux socket")
        self.executable = str(executable)
        self.socket, self.command_runner = socket, command_runner
        self.sockets = (socket,) if _test_socket else (_BROKER_SOCKET, "bazarlyk")

    @classmethod
    def isolated_for_test(cls, executable: str | Path, socket: str):
        """Construct an explicitly isolated socket fixture; never use in composition."""
        return cls(executable, socket=socket, _test_socket=True)

    def _sessions(self, *, timeout: int, socket: str | None = None) -> tuple[tuple[str, str], ...]:
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Host writer timeout must be bounded")
        # Do not use dashboard pty_broker.tmux: its config helper can write a
        # config file.  This client call is read-only and does not start a server.
        result = self.command_runner((self.executable, "-L", socket or self.socket, "-f", "/dev/null", "list-sessions",
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

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        if any(self._sessions(timeout=timeout, socket=socket) for socket in self.sockets):
            raise DomainError("recovery_not_quiescent", "Live host terminal sessions require explicit drain")
        return QuiescenceProof("registered_host_terminals", "frozen")

    def thaw(self) -> None:
        return None


class ManagedHostWriters:
    """The managed leg of the host inventory: a recovery or backup window must not run under a live managed agent host, whose
    ``claude`` keeps writing the agent's folder and transcript.

    Fail closed and read-only: a host state folder whose host lock is held by anything (a verified host, one still starting, or
    a process that cannot be proved to be ours) refuses the window with ``recovery_not_quiescent``. Nothing is signalled, closed or
    killed; the owner ends the sessions (Close in Chat) and tries again. ``terminals`` is the terminal inventory this wraps,
    asked first; ``root`` is ``<runtime>/managed-agents`` (or a callable returning it)."""

    def __init__(self, terminals, root, inspect=None):
        self.terminals, self._root, self._inspect = terminals, root, inspect

    def running(self) -> list[str]:
        """The agents whose host lock is held (empty when no managed agent was ever switched on)."""
        root = Path(self._root() if callable(self._root) else self._root)
        if not root.is_dir():
            return []
        inspect = self._inspect
        if inspect is None:
            from qorqut.managed_runtime.launcher import inspect_host as inspect
        busy = []
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                held = inspect(directory).status != "absent"
            except Exception as exc:  # noqa: BLE001 - what cannot be inspected is not proved free
                raise DomainError("recovery_not_quiescent", "Managed session inventory is unavailable", retryable=True) from exc
            if held:
                busy.append(directory.name)
        return busy

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        proof = self.terminals.inventory_and_freeze(timeout=timeout)
        if self.running():
            raise DomainError("recovery_not_quiescent", "Live managed agent sessions require explicit close", retryable=True)
        return proof

    def thaw(self) -> None:
        self.terminals.thaw()


class LegacyHostWriters:
    """Compose the terminal inventory into RecoveryQuiescer's host port."""

    def __init__(self, terminals: HostTerminalWriters):
        self.terminals = terminals

    def inventory_and_freeze(self, *, timeout: int) -> QuiescenceProof:
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Legacy host writer timeout must be bounded")
        deadline = monotonic() + timeout
        self.terminals.inventory_and_freeze(timeout=_remaining(deadline))
        return QuiescenceProof("legacy_host_writers", "frozen")

    def thaw(self) -> None:
        self.terminals.thaw()
