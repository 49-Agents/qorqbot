"""Privileged creation of one brand-new immutable engine release directory.

Only used when a local-integration engine plan freezes
``deployment_recipe.sudo_artifact_placement``. Exactly three direct-argv commands
run through the lock-guarded local executor with non-interactive sudo: creating
the exact new release directory, copying the already verified staged artifact
into it and restoring that new directory's own mode. Nothing existing is
changed, deleted or retried.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any

from . import v12
from .errors import ReleaseError

RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,95}")
SUDO = ["sudo", "-n"]
MKDIR = "/usr/bin/mkdir"
COPY = "/usr/bin/cp"
CHMOD = "/usr/bin/chmod"
ROOT_MODE = "0755"
ROOT_UID = 0


def _normalized(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    # Original-installation paths and symlinks anywhere in the ancestry are refused;
    # unlike source inputs, a system release root has no workspace containment.
    path = v12.checked_path(value)
    if str(path) != value:
        raise ReleaseError(f"{field} must be a normalized absolute path")
    return path


def _lstat(path: Path, what: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise ReleaseError(f"{what} cannot be inspected: {path}") from exc


def _verify_release_root(release_root: Path) -> None:
    metadata = _lstat(release_root, "release root")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseError(f"release root must be an existing normal directory: {release_root}")
    if metadata.st_uid != ROOT_UID or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ReleaseError(f"release root must be root-owned and not group/world writable: {release_root}")


def _verify_artifact_entries(artifact: Path) -> None:
    """Only regular files and directories without setuid/setgid may be copied with privilege."""

    pending = [artifact]
    while pending:
        current = pending.pop()
        mode = _lstat(current, "staged artifact entry").st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ReleaseError(f"privileged placement refuses a non-regular staged entry (link, device, FIFO or socket): {current}")
        if mode & (stat.S_ISUID | stat.S_ISGID):
            raise ReleaseError(f"privileged placement refuses a setuid/setgid staged entry: {current}")
        if stat.S_ISDIR(mode):
            try:
                names = sorted(os.listdir(current))
            except OSError as exc:
                raise ReleaseError(f"staged artifact directory cannot be read: {current}") from exc
            pending.extend(current / name for name in names)


def commands(artifact: Path, release: Path) -> tuple[list[str], list[str], list[str]]:
    """The create, copy and root-mode argv lists; no shell, no recursion into existing paths."""

    # No -p: every missing parent or existing destination is an error.
    create = [*SUDO, MKDIR, f"--mode={ROOT_MODE}", "--", str(release)]
    copy = [*SUDO, COPY, "--recursive", "--no-dereference", "--preserve=mode,timestamps",
            "--no-preserve=ownership", "--", f"{artifact}/.", f"{release}/"]
    # cp --preserve=mode applies the private staging root's mode to the new root; restore
    # exactly that one non-recursive mode. Child modes stay as copied and digest-verified.
    restore = [*SUDO, CHMOD, ROOT_MODE, "--", str(release)]
    return create, copy, restore


def place(executor: Any, artifact: Path, release_root: Any, release_id: Any) -> Path:
    """Create ``release_root/release_id`` and copy ``artifact`` into it; return the release path.

    The caller has already verified the artifact against its stage evidence and
    the local source, and verifies the copy against that evidence afterwards.
    """

    if getattr(executor, "host", None):
        raise ReleaseError("privileged artifact placement is only supported for the local executor")
    if not isinstance(release_id, str) or not RELEASE_ID.fullmatch(release_id):
        raise ReleaseError("privileged artifact placement needs a validated release id")
    artifact = _normalized(str(artifact), "staged artifact")
    if not stat.S_ISDIR(_lstat(artifact, "staged artifact").st_mode):
        raise ReleaseError(f"staged artifact must be a normal directory: {artifact}")
    root = _normalized(release_root, "release_root")
    _verify_release_root(root)
    release = root / release_id
    if executor.entry_exists(release):
        raise ReleaseError(f"release already exists (including a dangling link); it is never overwritten: {release}")
    _verify_artifact_entries(artifact)
    create, copy, restore = commands(artifact, release)
    try:
        executor.run(create)
    except ReleaseError as exc:
        raise ReleaseError(f"privileged creation of {release} failed; no copy was attempted and nothing was removed") from exc
    for argv, what in ((copy, "copy into"), (restore, "mode restore of")):
        try:
            executor.run(argv)
        except ReleaseError as exc:
            raise ReleaseError(f"privileged {what} {release} failed or was interrupted; the new directory "
                               "was not removed or retried, reconcile the release before any retry") from exc
    return release
