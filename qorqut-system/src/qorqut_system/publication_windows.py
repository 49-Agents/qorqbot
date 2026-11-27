"""Exclusive publication of a fully written file on Windows: not available in this first version.

A Windows version needs a different shape: Windows has no folder handle that is passed from function to function, and
the package update runs through the app rather than through a folder handle. The operations are present so that a
caller fails with a clear message instead of a missing import.
"""

from __future__ import annotations

from .errors import UnsupportedSystem

RENAME_EXCL = 0x00000004
_MESSAGE = "Offline managed instruction/layout upgrades are not yet supported on this operating system"


class Pending:
    """A written-but-unpublished file; the first Windows version cannot publish one."""

    def __init__(self, fd, staging_fd=None, staged=None):
        self.fd, self.staging_fd, self.staged, self.published = fd, staging_fd, staged, False


def publication_supported() -> bool:
    return False


def require_anonymous_publication() -> None:
    raise UnsupportedSystem(_MESSAGE)


def prepare_publication(staging) -> None:
    raise UnsupportedSystem(_MESSAGE)


def require_same_device(*directories) -> None:
    require_anonymous_publication()


def anonymous_file(dir_fd: int, *, read: bool = False) -> Pending:
    require_anonymous_publication()


def publish(pending: Pending, dir_fd: int, name: str) -> None:
    require_anonymous_publication()


def discard(pending: Pending) -> None:
    import os
    os.close(pending.fd)