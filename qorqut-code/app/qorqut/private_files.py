"""Owner-private files: 0600 regular files in 0700 directories owned by this process user.

Symlinks, non-regular files and group/world-accessible secrets are rejected. Nothing here logs or returns a path
or a value to an agent; callers keep the bytes in memory only for as long as they need them.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat

from qorqut_operations.contracts import DomainError

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


def _refuse(message):
    return DomainError("invalid_configuration", message)


def require_private_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private directories must be absolute paths")
    if create and not os.path.lexists(path):
        path.mkdir(mode=DIRECTORY_MODE, parents=True)
        os.chmod(path, DIRECTORY_MODE)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _refuse("A private directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _refuse("A private directory must be a real directory, not a symlink")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise _refuse("A private directory must be owned by the service user with mode 0700")
    return path


def read_private_file(path, *, max_bytes=65536, directory_check=True):
    """Read a private regular file without following a final symlink; the parent must be private too."""
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private files must be absolute paths")
    if directory_check:
        require_private_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _refuse("A private file cannot be opened (missing or a symlink)") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse("A private file must be a regular file")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _refuse("A private file must be owned by the service user with mode 0600")
        if info.st_size > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        return data
    finally:
        os.close(fd)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_file(directory, name, data):
    """Create a new 0600 file exclusively, fsync it and its directory. Existing files are never replaced."""
    directory = require_private_directory(directory)
    if os.sep in name or name in {"", ".", ".."}:
        raise _refuse("Invalid private file name")
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(directory / name)
        except OSError:
            pass
        raise
    os.close(fd)
    fsync_directory(directory)
    return directory / name
