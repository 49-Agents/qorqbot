"""Explicit lock locations. Nothing here discovers an installation root: the application passes the
directory it owns (for the target layout, ``operations/.runtime/locks``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qorqut_operations.contracts import DomainError


@dataclass(frozen=True)
class LockPaths:
    """Where prepared-application claims, per-receipt file locks and the installation recovery lock live."""

    application: Path
    apply: Path
    recovery: Path

    def __post_init__(self):
        for name in ("application", "apply", "recovery"):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise DomainError("invalid_configuration", "Lock paths must be explicit absolute paths")
            object.__setattr__(self, name, value)

    @classmethod
    def under(cls, directory) -> LockPaths:
        """The conventional arrangement beneath one explicit absolute directory."""
        directory = Path(directory)
        if not directory.is_absolute():
            raise DomainError("invalid_configuration", "Lock paths must be explicit absolute paths")
        return cls(directory / "application-locks", directory / "apply-locks", directory / "recovery.lock")
