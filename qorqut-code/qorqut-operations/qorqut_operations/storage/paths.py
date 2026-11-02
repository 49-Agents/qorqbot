"""Registered-path resolution under one trusted root."""

from __future__ import annotations

from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.files import RegisteredFileTarget


def registered_path(root, value):
    root = Path(root).resolve(strict=True)
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise DomainError("invalid_configuration", "Registered data path is outside the operations root") from exc
    return RegisteredFileTarget(root, path.as_posix()).resolve()
