"""Platform-specific descriptor paths; never replace retained descriptors with authored paths."""
from __future__ import annotations

import errno
import os
import stat
import sys

from qorqut_operations.contracts import DomainError


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


def _file_identity(result: os.stat_result) -> tuple:
    if sys.platform == "darwin":
        # Darwin's fdesc /dev/fd/N duplicates descriptor N, but recent macOS reports
        # a different st_dev through it than fstat() does for the same vnode. Use
        # inode, type, size and both timestamps instead of the device number.
        return (result.st_ino, stat.S_IFMT(result.st_mode), result.st_size,
                result.st_mtime_ns, result.st_birthtime)
    return (result.st_dev, result.st_ino)


def require_anonymous_publication() -> None:
    """Advanced managed upgrades retain their Linux-only, unnamed-file contract."""
    if not sys.platform.startswith("linux") or not hasattr(os, "O_TMPFILE"):
        raise DomainError("unsupported_platform",
                          "Offline managed instruction/layout upgrades are not yet supported on this operating system")


def require_descriptor_socket_paths() -> None:
    """Darwin /dev/fd does not support safe descriptor-relative child traversal."""
    if not sys.platform.startswith("linux"):
        raise DomainError("unsupported_platform",
                          "Encrypted recovery administration is not yet supported on this operating system")
