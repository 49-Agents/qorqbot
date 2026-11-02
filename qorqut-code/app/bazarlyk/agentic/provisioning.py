"""Explicit offline provisioning of the private Overseer and application-executor roots.

Nothing here runs on open, chat admission, a read or a scheduled job. Preparation is read-only; apply creates only the
missing directories, always 0700 under the configured instance, and refuses symlinks, foreign ownership or wider modes
instead of repairing them. The Operations ``.runtime`` parent keeps its established policy.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from qorqut_operations.contracts import DomainError

MODE = 0o700
ROOTS = (
    ("overseer", lambda paths: paths.operations / ".runtime" / "overseer"),
    ("overseer_archive", lambda paths: paths.overseer_archive),
    ("application_worktrees", lambda paths: paths.operations / ".agentic-worktrees"),
    ("application_states", lambda paths: paths.runtime / "agentic-worktrees"),
)
# Every root is created directly under one already-established parent; only the parts below that parent must be private.
BASES = {"overseer": lambda paths: paths.runtime,
         "overseer_archive": lambda paths: paths.runtime,
         "application_worktrees": lambda paths: paths.operations,
         "application_states": lambda paths: paths.runtime}


def _unsafe_parent(path: Path, base: Path):
    current = path
    while current != base:
        if current.is_symlink():
            return "unsafe_symlink"
        if current.exists():
            if not current.is_dir():
                return "unsafe_parent_not_directory"
            status = current.stat()
            if status.st_uid != os.geteuid():
                return "unsafe_parent_owner"
            if stat.S_IMODE(status.st_mode) & 0o077:
                return "unsafe_parent_permissions"
        current = current.parent
    return None


def inspect(path: Path, base: Path):
    """One bounded status: missing, ready or the exact refusal this path would cause."""
    unsafe = _unsafe_parent(path, base)
    if unsafe:
        return unsafe
    if not path.exists():
        return "missing"
    if path.is_symlink():
        return "unsafe_symlink"
    if not path.is_dir():
        return "unsafe_not_directory"
    status = path.stat()
    if status.st_uid != os.geteuid():
        return "unsafe_owner"
    if stat.S_IMODE(status.st_mode) & 0o077:
        return "unsafe_permissions"
    return "ready"


def plan(paths) -> dict:
    """Read-only: what a fresh/offline provisioning would create or refuse."""
    items = []
    for name, locate in ROOTS:
        path = locate(paths)
        items.append({"name": name, "path": str(path), "state": inspect(path, BASES[name](paths))})
    unsafe = [item for item in items if item["state"].startswith("unsafe_")]
    return {"schema_version": 1, "mode": "0700", "items": items,
            "complete": not unsafe, "conflicts": [{"path": item["path"], "reason": item["state"]} for item in unsafe]}


def _create_root(paths, name):
    """Create one explicitly named missing root, leaving journaling to the caller."""
    roots = dict(ROOTS)
    if name not in roots:
        raise DomainError("overseer_storage_unavailable", "Unknown private provisioning root")
    path = roots[name](paths)
    if inspect(path, BASES[name](paths)) != 'missing':
        raise DomainError("overseer_storage_unavailable", "The private provisioning root is no longer missing")
    parent = path.parent
    if not parent.is_absolute() or any(p.is_symlink() for p in (parent, *parent.parents)):
        raise DomainError("overseer_storage_unavailable", "Unsafe private provisioning parent")
    parentfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(path.name, MODE, dir_fd=parentfd)
        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parentfd)
        try:
            os.fchmod(child, MODE)
            os.fsync(child)
            info = os.fstat(child)
            os.fsync(parentfd)
            return {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}
        finally:
            os.close(child)
    finally:
        os.close(parentfd)


def apply(paths) -> dict:
    """Create the missing roots with mode 0700; preserve every correct existing one; refuse anything else."""
    runtime_parent = paths.operations / ".runtime"
    if runtime_parent.is_symlink() or not runtime_parent.is_dir():
        raise DomainError("overseer_storage_unavailable",
                          "The Operations .runtime parent must exist before private provisioning")
    created, preserved = [], []
    for name, locate in ROOTS:
        path = locate(paths)
        state = inspect(path, BASES[name](paths))
        if state == "ready":
            preserved.append(name)
            continue
        if state != "missing":
            raise DomainError("overseer_storage_unavailable",
                              f"Cannot provision {name}: {state}; repair the path explicitly, never silently")
        parent = path.parent
        if parent != paths.operations and parent != paths.runtime and not parent.is_dir():
            os.makedirs(parent, mode=MODE, exist_ok=False)
            os.chmod(parent, MODE)
        os.mkdir(path, MODE)
        os.chmod(path, MODE)
        created.append(name)
    return {"schema_version": 1, "mode": "0700", "created": created, "preserved": preserved,
            "items": plan(paths)["items"]}
