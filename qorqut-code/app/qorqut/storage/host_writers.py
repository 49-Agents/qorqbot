"""Read-only, fail-closed host writer inventory for full recovery.

A recovery or backup window must not run under a live managed agent host, whose headless client keeps writing the agent's folder
and transcript. This controller never closes, signals or kills one: it only yields a quiescence proof when no host lock is held.
"""

from __future__ import annotations

from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import QuiescenceProof


class ManagedHostWriters:
    """Fail closed and read-only: a host state folder whose host lock is held by anything (a verified host, one still starting, or
    a process that cannot be proved to be ours) refuses the window with ``recovery_not_quiescent``. Nothing is signalled, closed or
    killed; the owner ends the sessions (Close in Chat) and tries again. ``root`` is ``<runtime>/managed-agents`` (or a callable
    returning it)."""

    def __init__(self, root, inspect=None):
        self._root, self._inspect = root, inspect

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
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("Host writer timeout must be bounded")
        if self.running():
            raise DomainError("recovery_not_quiescent", "Live managed agent sessions require explicit close", retryable=True)
        return QuiescenceProof("registered_managed_hosts", "frozen")

    def thaw(self) -> None:
        return None
