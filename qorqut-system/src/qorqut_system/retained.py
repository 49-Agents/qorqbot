"""Retained file descriptors: address an already-open file where the system allows it.

Linux uses ``/proc/self/fd``, macOS uses ``/dev/fd``. The address is compared with the open descriptor before it is
returned, so a descriptor that was closed and reused, or a path that names another file, is refused.
"""
from __future__ import annotations

import errno
import os
import stat
import sys

from .errors import UnsupportedSystem


def _file_identity(result: os.stat_result) -> tuple:
    if sys.platform == "darwin":
        # Darwin's fdesc /dev/fd/N duplicates descriptor N, but recent macOS reports
        # a different st_dev through it than fstat() does for the same vnode. Use
        # inode, type, size and both timestamps instead of the device number.
        return (result.st_ino, stat.S_IFMT(result.st_mode), result.st_size,
                result.st_mtime_ns, result.st_birthtime)
    return (result.st_dev, result.st_ino)


def immutable_database_uri(descriptor: int) -> str:
    """Address an already-open regular file for SQLite's sidecar-free reader."""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(errno.EINVAL, "Immutable database descriptor is not a regular file")
    if sys.platform == "darwin":
        path = f"/dev/fd/{descriptor}"
    elif sys.platform.startswith("linux"):
        path = f"/proc/self/fd/{descriptor}"
    else:
        raise OSError(errno.ENOTSUP, "This host lacks a qualified retained-file descriptor path")
    named = os.stat(path)
    if _file_identity(named) != _file_identity(opened):
        raise OSError(errno.ESTALE, "Database descriptor path identifies a different file")
    return f"file:{path}?mode=ro&immutable=1"


def require_descriptor_socket_paths() -> None:
    """Darwin /dev/fd does not support safe descriptor-relative child traversal."""
    if not sys.platform.startswith("linux"):
        raise UnsupportedSystem("Encrypted recovery administration is not yet supported on this operating system")