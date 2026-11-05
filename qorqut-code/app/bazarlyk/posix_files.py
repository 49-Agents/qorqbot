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


# ---- exclusive publication of a fully written file -------------------------------------------------------------------
# Managed instruction and layout updates publish each new file only once it is completely written and synced, and only
# if its name is still absent; an interrupted publication must never leave a partial file at the destination.
#
# Linux: the file is written unnamed (O_TMPFILE) in the destination directory and linked to its name (linkat through
# procfs, which fails if the name exists). Nothing named exists until publication.
#
# macOS has no O_TMPFILE. The file is written in a private staging directory on the same file system and published
# with renameatx_np(RENAME_EXCL), which is atomic, keeps the inode (recorded identities stay valid) and fails if the name
# exists. An interruption can leave a partial file only in the staging directory, which nothing reads; the destination
# never sees one. Leftovers there are inert and are removed by discard() or a later prepare_publication().

RENAME_EXCL = 0x00000004
_staging: dict = {}


def _darwin() -> bool:
    return sys.platform == "darwin"


def publication_supported() -> bool:
    return (sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE")) or _darwin()


def require_anonymous_publication() -> None:
    """Managed updates need exclusive, all-or-nothing publication (Linux and macOS; Windows runs Qorqbot in WSL2)."""
    if not publication_supported():
        raise DomainError("unsupported_platform",
                          "Offline managed instruction/layout upgrades are not yet supported on this operating system")


def prepare_publication(staging) -> None:
    """Set up macOS publication for this process with a private staging directory (a no-op on Linux).

    The directory is created 0700 if needed, must be owned by this user and not be a symlink, and is opened once. Only
    files this module named (``.publish-*``) are removed from it; a caller holds the update's exclusive lock."""
    if not _darwin():
        return
    from pathlib import Path
    staging = Path(staging)
    staging.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise DomainError("unsafe_path", "The publication staging directory must be private to this user")
        for name in os.listdir(fd):
            if name.startswith(".publish-"):
                os.unlink(name, dir_fd=fd)
    except BaseException:
        os.close(fd)
        raise
    previous = _staging.pop("fd", None)
    if previous is not None:
        os.close(previous)
    _staging["fd"] = fd


def _staging_fd() -> int:
    staging = _staging.get("fd")
    if staging is None:
        raise DomainError("unsupported_platform", "Publication staging was not prepared for this update")
    return staging


def _cross_device(directory=None) -> OSError:
    where = f" ({directory})" if directory is not None else ""
    return OSError(errno.EXDEV, "The publication staging directory is on a different file system" + where)


def require_same_device(*directories) -> None:
    """Refuse, before any effect, a macOS publication destination on another file system than the staging directory.

    A read-only preflight for callers that publish several files in a row: each directory is opened without following
    a symlink and its device is compared with the already-validated staging descriptor, so a mismatch is found before
    the first publication rather than by the one that happens to need staging (a no-op on Linux)."""
    if not _darwin():
        return
    device = os.fstat(_staging_fd()).st_dev
    for directory in directories:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if os.fstat(fd).st_dev != device:
                raise _cross_device(directory)
        finally:
            os.close(fd)


class Pending:
    """A written-but-unpublished file: ``fd`` is open for writing; ``publish()`` gives it its name exactly once."""

    def __init__(self, fd, staging_fd=None, staged=None):
        self.fd, self.staging_fd, self.staged, self.published = fd, staging_fd, staged, False


def anonymous_file(dir_fd: int, *, read: bool = False) -> Pending:
    """A new file, mode 0600, that will be published into ``dir_fd``."""
    access = (os.O_RDWR if read else os.O_WRONLY) | os.O_CLOEXEC
    if sys.platform.startswith("linux") and hasattr(os, "O_TMPFILE"):
        return Pending(os.open(".", access | os.O_TMPFILE, 0o600, dir_fd=dir_fd))
    if not _darwin():
        require_anonymous_publication()
    staging = _staging_fd()
    if os.fstat(staging).st_dev != os.fstat(dir_fd).st_dev:
        raise _cross_device()
    import secrets
    name = ".publish-" + secrets.token_hex(16)
    fd = os.open(name, access | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging)
    return Pending(fd, staging, name)


def publish(pending: Pending, dir_fd: int, name: str) -> None:
    """Give a fully written and synced file its name in ``dir_fd``; FileExistsError if the name already exists."""
    if pending.published:
        raise OSError(errno.EINVAL, "File already published")
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if pending.staged is None:
        # Linux procfs linkat avoids requiring CAP_DAC_READ_SEARCH (AT_SYMLINK_FOLLOW).
        result = libc.linkat(-100, os.fsencode("/proc/self/fd/" + str(pending.fd)), dir_fd, os.fsencode(name), 0x400)
    else:
        result = libc.renameatx_np(pending.staging_fd, os.fsencode(pending.staged), dir_fd, os.fsencode(name), RENAME_EXCL)
    if result != 0:
        error = ctypes.get_errno()
        raise (FileExistsError if error == errno.EEXIST else OSError)(error, os.strerror(error), name)
    pending.published = True


def discard(pending: Pending) -> None:
    """Close the file; an unpublished staged file is removed."""
    try:
        if pending.staged is not None and not pending.published:
            try:
                os.unlink(pending.staged, dir_fd=pending.staging_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(pending.fd)


def require_descriptor_socket_paths() -> None:
    """Darwin /dev/fd does not support safe descriptor-relative child traversal."""
    if not sys.platform.startswith("linux"):
        raise DomainError("unsupported_platform",
                          "Encrypted recovery administration is not yet supported on this operating system")
