"""Descriptor-relative directory access that never follows a symlink and never resolves the path.

The donor opened ``os.path.realpath(root)``, which silently accepted a runtime root that had been replaced by a link.
Here every component of an absolute path is opened without following a link, relative to the previous folder, so a
replaced component is refused. This is path validation, not a guard against a same-user filesystem race.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from qorqut_system import private_files


def open_directory(path: Path) -> int:
    """A descriptor for an absolute directory, with every component opened without following links."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise ValueError("an absolute path without traversal is required")
    descriptor = private_files.open_folder(path.anchor).descriptor
    try:
        for part in path.parts[1:]:
            child = private_files.open_child_folder(descriptor, part).descriptor
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def names_directory(descriptor: int, path: Path) -> bool:
    """Whether ``path`` still names exactly the directory ``descriptor`` holds (not replaced, not a link)."""
    try:
        now = private_files.stat_folder(path)
    except OSError:
        return False
    held = private_files.stat_folder(descriptor)
    return stat.S_ISDIR(now.st_mode) and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)
