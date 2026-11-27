"""Platform-specific descriptor paths; never replace retained descriptors with authored paths.

The operations live in the system layer (``qorqut_system.publication`` and ``qorqut_system.retained``). This module
keeps their names for the files that import them during the move and translates the layer's error to the domain
error callers expect.
"""
from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_system import publication as _publication
from qorqut_system import retained as _retained
from qorqut_system.errors import UnsafePath, UnsupportedSystem

RENAME_EXCL = _publication.RENAME_EXCL
Pending = _publication.Pending


def immutable_database_uri(descriptor: int) -> str:
    """Address an already-open regular file for SQLite's sidecar-free reader."""
    return _retained.immutable_database_uri(descriptor)


def publication_supported() -> bool:
    return _publication.publication_supported()


def _unsupported(error):
    return DomainError("unsupported_platform", str(error))


def require_anonymous_publication() -> None:
    """Managed updates need exclusive, all-or-nothing publication (Linux and macOS; Windows runs Qorqbot in WSL2)."""
    try:
        _publication.require_anonymous_publication()
    except UnsupportedSystem as error:
        raise _unsupported(error) from error


def prepare_publication(staging) -> None:
    """Set up macOS publication for this process with a private staging directory (a no-op on Linux)."""
    try:
        _publication.prepare_publication(staging)
    except UnsafePath as error:
        raise DomainError("unsafe_path", str(error)) from error
    except UnsupportedSystem as error:
        raise _unsupported(error) from error


def require_same_device(*directories) -> None:
    """Refuse, before any effect, a macOS publication destination on another file system than the staging directory."""
    try:
        _publication.require_same_device(*directories)
    except UnsupportedSystem as error:
        raise _unsupported(error) from error


def anonymous_file(dir_fd: int, *, read: bool = False):
    """A new file, mode 0600, that will be published into ``dir_fd``."""
    try:
        return _publication.anonymous_file(dir_fd, read=read)
    except UnsupportedSystem as error:
        raise _unsupported(error) from error


def publish(pending, dir_fd: int, name: str) -> None:
    """Give a fully written and synced file its name in ``dir_fd``; FileExistsError if the name already exists."""
    try:
        _publication.publish(pending, dir_fd, name)
    except UnsupportedSystem as error:
        raise _unsupported(error) from error


def discard(pending) -> None:
    """Close the file; an unpublished staged file is removed."""
    _publication.discard(pending)


def require_descriptor_socket_paths() -> None:
    """Darwin /dev/fd does not support safe descriptor-relative child traversal."""
    try:
        _retained.require_descriptor_socket_paths()
    except UnsupportedSystem as error:
        raise _unsupported(error) from error