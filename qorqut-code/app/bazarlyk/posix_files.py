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
    if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError(errno.ESTALE, "Database descriptor path identifies a different file")
    return f"file:{path}?mode=ro&immutable=1"


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
