"""Descriptor-relative directory access that never follows a symlink and never resolves the path.

The donor opened ``os.path.realpath(root)``, which silently accepted a runtime root that had been replaced by a link.
Here every component of an absolute path is opened with ``O_NOFOLLOW`` relative to the previous descriptor, so a
replaced component is refused. This is path validation, not a guard against a same-user filesystem race.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def open_directory(path: Path) -> int:
    """A descriptor for an absolute directory, with every component opened without following links."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise ValueError("an absolute path without traversal is required")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def names_directory(descriptor: int, path: Path) -> bool:
    """Whether ``path`` still names exactly the directory ``descriptor`` holds (not replaced, not a link)."""
    try:
        now = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    held = os.fstat(descriptor)
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)
